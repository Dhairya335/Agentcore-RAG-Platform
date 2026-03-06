"""
Custom Resource Lambda: Idempotent OpenSearch Serverless Collection Manager

  CDK/CloudFormation tracks resources in its STATE FILE. If manually deleted
  a resource from AWS without going through CloudFormation, the state file still
  says it exists. Next deploy → CloudFormation tries to UPDATE it → 404 → crash.

  This Custom Resource Lambda is called BY CloudFormation during every deploy.
  It checks if the collection ACTUALLY exists in AWS (not just in CFN state),
  and creates it if missing. This means:
    - First deploy     → creates collection, returns endpoint
    - Manual delete    → next deploy recreates it, returns endpoint  
    - Normal deploy    → collection exists, just returns endpoint
    - Stack delete     → Delete event fires, Lambda deletes collection cleanly

  1. CloudFormation calls this Lambda with event.RequestType = Create/Update/Delete
  2. Lambda does the work, then sends a response to event.ResponseURL (an S3 presigned URL)
  3. CloudFormation reads that response and continues (or fails) the stack operation
  4. Physical Resource ID = collection name → CloudFormation tracks this resource by name
"""

import json
import urllib.request
import boto3
import time
import os

client = boto3.client('opensearchserverless')


def handler(event, context):
    print(f"Event: {json.dumps(event)}")
    
    request_type    = event['RequestType']
    collection_name = event['ResourceProperties']['CollectionName']
    response_url    = event['ResponseURL']
    stack_id        = event['StackId']
    request_id      = event['RequestId']
    logical_id      = event['LogicalResourceId']
    physical_id     = event.get('PhysicalResourceId', collection_name)

    try:
        if request_type in ('Create', 'Update'):
            endpoint = ensure_collection_exists(collection_name)
            send_response(response_url, stack_id, request_id, logical_id,
                          physical_id, 'SUCCESS',
                          {'CollectionEndpoint': endpoint,
                           'CollectionName': collection_name})

        elif request_type == 'Delete':
            delete_collection_if_exists(collection_name)
            send_response(response_url, stack_id, request_id, logical_id,
                          physical_id, 'SUCCESS', {})

    except Exception as e:
        print(f"ERROR: {e}")
        send_response(response_url, stack_id, request_id, logical_id,
                      physical_id, 'FAILED', {}, reason=str(e))


def ensure_collection_exists(collection_name: str) -> str:
    """
    Check if collection exists in AWS (not just CFN state).
    Create it if missing. Wait until ACTIVE. Return endpoint.
    """
    # 1. Check if collection already exists
    existing = find_collection(collection_name)
    
    if existing:
        print(f"Collection '{collection_name}' already exists with status: {existing['status']}")
        
        # If it's being deleted (edge case), wait it out then recreate
        if existing['status'] == 'DELETING':
            print("Collection is being deleted, waiting...")
            wait_for_deletion(collection_name)
            existing = None
        elif existing['status'] == 'FAILED':
            print("Collection is in FAILED state, deleting and recreating...")
            client.delete_collection(id=existing['id'])
            wait_for_deletion(collection_name)
            existing = None
        else:
            # CREATING or ACTIVE — just wait for ACTIVE and return endpoint
            return wait_for_active(existing['id'], collection_name)
    
    if not existing:
        # 2. Create the collection fresh
        print(f"Collection '{collection_name}' not found — creating...")
        response = client.create_collection(
            name=collection_name,
            type='VECTORSEARCH',
            description=f'Vector store for RAG chatbot — managed by CDK custom resource',
        )
        collection_id = response['createCollectionDetail']['id']
        print(f"Creation started, collection ID: {collection_id}")
        return wait_for_active(collection_id, collection_name)


def find_collection(name: str) -> dict | None:
    """List all collections and find by name. Returns dict or None."""
    try:
        response = client.list_collections(collectionFilters={'name': name})
        summaries = response.get('collectionSummaries', [])
        if summaries:
            col = summaries[0]
            return {'id': col['id'], 'status': col['status'], 'arn': col['arn']}
        return None
    except Exception as e:
        print(f"Error listing collections: {e}")
        return None


def wait_for_active(collection_id: str, collection_name: str, 
                    timeout_seconds: int = 600) -> str:
    """
    Poll until collection status = ACTIVE.
    OSS collections take 2-5 minutes to become active.
    Lambda has 15 min timeout — plenty of time.
    Returns the collection endpoint URL.
    """
    print(f"Waiting for collection '{collection_name}' to become ACTIVE...")
    deadline = time.time() + timeout_seconds
    
    while time.time() < deadline:
        response = client.batch_get_collection(ids=[collection_id])
        collections = response.get('collectionDetails', [])
        
        if not collections:
            raise Exception(f"Collection {collection_id} disappeared during wait!")
        
        col = collections[0]
        status = col['status']
        print(f"  Status: {status}")
        
        if status == 'ACTIVE':
            endpoint = col['collectionEndpoint']
            print(f"Collection ACTIVE. Endpoint: {endpoint}")
            return endpoint
        elif status == 'FAILED':
            raise Exception(f"Collection creation FAILED: {col.get('failureCode')} - {col.get('failureMessage')}")
        
        # Still CREATING — wait 20 seconds and try again
        time.sleep(20)
    
    raise Exception(f"Timed out waiting for collection to become ACTIVE after {timeout_seconds}s")


def wait_for_deletion(collection_name: str, timeout_seconds: int = 300):
    """Wait until collection is fully gone from AWS."""
    print(f"Waiting for collection '{collection_name}' to be fully deleted...")
    deadline = time.time() + timeout_seconds
    
    while time.time() < deadline:
        existing = find_collection(collection_name)
        if not existing:
            print("Collection fully deleted.")
            return
        print(f"  Still exists with status: {existing['status']}")
        time.sleep(15)
    
    raise Exception(f"Timed out waiting for collection deletion")


def delete_collection_if_exists(collection_name: str):
    """Called on stack DELETE. Clean up the collection if it still exists."""
    existing = find_collection(collection_name)
    if not existing:
        print(f"Collection '{collection_name}' already gone — nothing to delete.")
        return
    
    print(f"Deleting collection '{collection_name}' (id: {existing['id']})...")
    client.delete_collection(id=existing['id'])
    wait_for_deletion(collection_name)
    print("Collection deleted successfully.")


def send_response(response_url: str, stack_id: str, request_id: str,
                  logical_id: str, physical_id: str, status: str,
                  data: dict, reason: str = ''):
    """
    Send result back to CloudFormation via the ResponseURL.
    This is a presigned S3 URL — we PUT JSON to it.
    CloudFormation is waiting for this call before continuing.
    """
    body = json.dumps({
        'Status':             status,
        'Reason':             reason or f'See CloudWatch logs for details',
        'PhysicalResourceId': physical_id,
        'StackId':            stack_id,
        'RequestId':          request_id,
        'LogicalResourceId':  logical_id,
        'Data':               data,
    }).encode('utf-8')

    req = urllib.request.Request(
        url=response_url,
        data=body,
        method='PUT',
        headers={'Content-Type': '', 'Content-Length': str(len(body))}
    )
    
    with urllib.request.urlopen(req) as resp:
        print(f"CFN response sent: {resp.status}")