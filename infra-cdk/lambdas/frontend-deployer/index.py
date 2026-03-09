"""
Custom Resource Lambda: Frontend Deployer

WHY THIS EXISTS:
  cdk deploy creates the Amplify app resource and staging S3 bucket — but it
  never pushes the actual built React files. This Lambda runs during every
  cdk deploy as a Custom Resource, receives the pre-built frontend zip (uploaded
  to staging S3 by CDK BucketDeployment), assembles aws-exports.json from SSM,
  injects it into the zip, and triggers an Amplify deployment.

  Result: cdk deploy does everything. No manual steps, no separate scripts.

  aws-exports.json is built HERE (not in CDK) to avoid circular dependencies
  between the amplify/cognito/backend nested stacks.

FLOW:
  1. CDK BucketDeployment uploads frontend.zip to staging S3
  2. CDK CustomResource invokes this Lambda
  3. Lambda reads Cognito/API values from SSM
  4. Lambda injects aws-exports.json into the zip
  5. Lambda calls amplify.create_deployment() to get a presigned upload URL
  6. Lambda uploads the zip to Amplify via the presigned URL
  7. Lambda calls amplify.start_deployment() to trigger the live deployment
  8. Lambda sends SUCCESS back to CloudFormation
"""

import json
import urllib.request
import boto3
import os
import tempfile
import zipfile

amplify_client = boto3.client("amplify")
s3_client      = boto3.client("s3")
ssm_client     = boto3.client("ssm")


def get_ssm(name):
    return ssm_client.get_parameter(Name=name)["Parameter"]["Value"]


def get_ssm_optional(name, default=""):
    """Read an SSM parameter, returning default if it doesn't exist yet."""
    try:
        return ssm_client.get_parameter(Name=name)["Parameter"]["Value"]
    except ssm_client.exceptions.ParameterNotFound:
        print(f"WARNING: SSM parameter {name} not found, using default: '{default}'")
        return default


def build_aws_exports(stack_name, amplify_url, region):
    """Read all values from SSM and return aws-exports.json content as a string."""
    prefix = f"/{stack_name}"
    return json.dumps({
        "authority":              f"https://cognito-idp.{region}.amazonaws.com/{get_ssm(f'{prefix}/cognito-user-pool-id')}",
        "client_id":              get_ssm(f"{prefix}/cognito-user-pool-client-id"),
        "redirect_uri":           amplify_url,
        "post_logout_redirect_uri": amplify_url,
        "response_type":          "code",
        "scope":                  "email openid profile",
        "automaticSilentRenew":   True,
        "agentRuntimeArn":        get_ssm(f"{prefix}/runtime-arn"),
        "awsRegion":              region,
        "feedbackApiUrl":         get_ssm(f"{prefix}/feedback-api-url"),
        "docsApiUrl":             get_ssm_optional(f"{prefix}/rag/docs-api-url"),
        "agentPattern":           "strands-single-agent",
    }, indent=2)


def handler(event, context):
    print(f"Event: {json.dumps(event)}")

    request_type = event["RequestType"]
    props        = event["ResourceProperties"]
    response_url = event["ResponseURL"]
    stack_id     = event["StackId"]
    request_id   = event["RequestId"]
    logical_id   = event["LogicalResourceId"]
    physical_id  = event.get("PhysicalResourceId", "frontend-deployer")

    # On Delete — nothing to do, Amplify app cleanup handled by CFN
    if request_type == "Delete":
        send_response(response_url, stack_id, request_id, logical_id,
                      physical_id, "SUCCESS", {})
        return

    try:
        app_id         = props["AppId"]
        branch_name    = props["BranchName"]
        staging_bucket = props["StagingBucket"]
        zip_key        = props["ZipKey"]
        stack_name     = props["StackName"]
        amplify_url    = props["AmplifyUrl"]
        region         = os.environ.get("AWS_REGION", "us-east-1")

        print(f"Deploying frontend: app={app_id} branch={branch_name} "
              f"bucket={staging_bucket} key={zip_key}")

        # Step 1: Download the built frontend zip from staging S3
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name

        print(f"Downloading s3://{staging_bucket}/{zip_key} ...")
        s3_client.download_file(staging_bucket, zip_key, tmp_path)
        print(f"Downloaded {os.path.getsize(tmp_path)} bytes")

        # Step 2: Read SSM values and inject aws-exports.json into the zip
        print("Reading SSM parameters to build aws-exports.json ...")
        aws_exports = build_aws_exports(stack_name, amplify_url, region)
        print(f"aws-exports.json: {aws_exports}")

        print("Injecting aws-exports.json into zip ...")
        with zipfile.ZipFile(tmp_path, "a") as zf:
            zf.writestr("aws-exports.json", aws_exports)
        print("Injected successfully")

        # Step 3: Create an Amplify deployment — get a presigned upload URL
        print("Creating Amplify deployment ...")
        deploy_response = amplify_client.create_deployment(
            appId=app_id,
            branchName=branch_name,
        )
        job_id     = deploy_response["jobId"]
        upload_url = deploy_response["zipUploadUrl"]
        print(f"Got jobId={job_id}")

        # Step 4: Upload the zip to Amplify via the presigned URL
        print("Uploading zip to Amplify ...")
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

        # Step 5: Start the deployment
        print(f"Starting deployment job={job_id} ...")
        amplify_client.start_deployment(
            appId=app_id,
            branchName=branch_name,
            jobId=job_id,
        )
        print("Deployment started successfully.")

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
            "Content-Type":   "",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req) as resp:
        print(f"CFN response sent: {resp.status}")