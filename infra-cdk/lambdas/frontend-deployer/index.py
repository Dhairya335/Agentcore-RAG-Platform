"""
Custom Resource Lambda: Frontend Deployer

WHY THIS EXISTS:
  cdk deploy creates the Amplify app resource and staging S3 bucket — but it
  never pushes the actual built React files. This Lambda runs during every
  cdk deploy as a Custom Resource, receives the pre-built frontend zip (uploaded
  to staging S3 by CDK's BucketDeployment), and triggers an Amplify deployment
  from that zip.

  Result: cdk deploy does everything. No manual steps, no separate scripts.

FLOW:
  1. CDK BucketDeployment uploads frontend.zip to staging S3 bucket
  2. CDK CustomResource invokes this Lambda
  3. Lambda calls amplify.create_deployment() to get a presigned upload URL
  4. Lambda downloads the zip from staging S3 and uploads it to Amplify
  5. Lambda calls amplify.start_deployment() to trigger the live deployment
  6. Lambda sends SUCCESS back to CloudFormation
  7. Amplify serves the new frontend files at the Amplify URL
"""

import json
import urllib.request
import urllib.error
import boto3
import os
import tempfile

amplify_client = boto3.client("amplify")
s3_client = boto3.client("s3")


def handler(event, context):
    print(f"Event: {json.dumps(event)}")

    request_type    = event["RequestType"]
    props           = event["ResourceProperties"]
    response_url    = event["ResponseURL"]
    stack_id        = event["StackId"]
    request_id      = event["RequestId"]
    logical_id      = event["LogicalResourceId"]
    physical_id     = event.get("PhysicalResourceId", "frontend-deployer")

    # On Delete — nothing to do, Amplify app cleanup handled by CFN
    if request_type == "Delete":
        send_response(response_url, stack_id, request_id, logical_id,
                      physical_id, "SUCCESS", {})
        return

    try:
        app_id          = props["AppId"]
        branch_name     = props["BranchName"]
        staging_bucket  = props["StagingBucket"]
        zip_key         = props["ZipKey"]

        print(f"Deploying frontend: app={app_id} branch={branch_name} "
              f"bucket={staging_bucket} key={zip_key}")

        # Step 1: Download the built frontend zip from staging S3
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name

        print(f"Downloading s3://{staging_bucket}/{zip_key} ...")
        s3_client.download_file(staging_bucket, zip_key, tmp_path)
        zip_size = os.path.getsize(tmp_path)
        print(f"Downloaded {zip_size} bytes")

        # Step 2: Create an Amplify deployment — get a presigned upload URL
        print("Creating Amplify deployment...")
        deploy_response = amplify_client.create_deployment(
            appId=app_id,
            branchName=branch_name,
        )
        job_id      = deploy_response["jobId"]
        upload_url  = deploy_response["zipUploadUrl"]
        print(f"Got jobId={job_id}")

        # Step 3: Upload the zip to Amplify via the presigned URL
        print("Uploading zip to Amplify...")
        with open(tmp_path, "rb") as f:
            zip_data = f.read()

        req = urllib.request.Request(
            url=upload_url,
            data=zip_data,
            method="PUT",
            headers={"Content-Type": "application/zip"},
        )
        with urllib.request.urlopen(req) as resp:
            print(f"Upload response: {resp.status}")

        # Step 4: Start the deployment using the uploaded zip
        print(f"Starting deployment job={job_id}...")
        amplify_client.start_deployment(
            appId=app_id,
            branchName=branch_name,
            jobId=job_id,
        )
        print("Deployment started successfully.")

        # Clean up temp file
        os.unlink(tmp_path)

        send_response(response_url, stack_id, request_id, logical_id,
                      physical_id, "SUCCESS",
                      {"JobId": job_id, "AppId": app_id})

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_response(response_url, stack_id, request_id, logical_id,
                      physical_id, "FAILED", {}, reason=str(e))


def send_response(response_url, stack_id, request_id, logical_id,
                  physical_id, status, data, reason=""):
    """Send result back to CloudFormation via the ResponseURL (presigned S3 URL)."""
    body = json.dumps({
        "Status":             status,
        "Reason":             reason or "See CloudWatch logs for details",
        "PhysicalResourceId": physical_id,
        "StackId":            stack_id,
        "RequestId":          request_id,
        "LogicalResourceId":  logical_id,
        "Data":               data,
    }).encode("utf-8")

    req = urllib.request.Request(
        url=response_url,
        data=body,
        method="PUT",
        headers={
            "Content-Type": "",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req) as resp:
        print(f"CFN response sent: {resp.status}")