import json
import os
import uuid
import boto3
from datetime import datetime, timezone

s3              = boto3.client("s3")
dynamodb        = boto3.resource("dynamodb")   # high-level: used for table.get_item
dynamodb_client = boto3.client("dynamodb")     # low-level: used for transact_write_items

BUCKET_NAME = os.environ["DOCS_BUCKET_NAME"]
TABLE_NAME  = os.environ["DOCS_TABLE_NAME"]
REGION      = os.environ.get("AWS_REGION", "us-east-1")


def handler(event, context):
    """
    Called by API Gateway POST /documents/presign

    Does TWO things:
    1. Generates a presigned S3 PUT URL (browser uploads directly to S3)
    2. Creates TWO DynamoDB records atomically:
         VER#000001 = permanent version history entry
         LATEST     = always points to newest version

    Expected request body:
    {
      "fileName":    "policy-doc.txt",
      "contentType": "text/plain",
      "tenantId":    "user-123",
      "docId":       "doc-abc",     <- optional, omit to auto-generate
      "metadata":    {}             <- optional extra fields
    }

    Returns:
    {
      "uploadUrl": "https://s3.amazonaws.com/...",
      "docId":     "doc-abc",
      "version":   1,
      "s3Key":     "raw-docs/user-123/doc-abc/v1/policy-doc.txt"
    }
    """

    try:
        body = json.loads(event.get("body", "{}"))
    except Exception:
        return _error(400, "Invalid JSON body")

    file_name    = body.get("fileName")
    content_type = body.get("contentType", "text/plain")
    tenant_id    = body.get("tenantId")
    metadata     = body.get("metadata", {})

    if not file_name or not tenant_id:
        return _error(400, "fileName and tenantId are required")

    doc_id = body.get("docId") or str(uuid.uuid4())

    table     = dynamodb.Table(TABLE_NAME)
    latest_pk = f"TENANT#{tenant_id}#DOC#{doc_id}"
    latest_sk = "LATEST"

    version = 1
    try:
        resp = table.get_item(Key={"PK": latest_pk, "SK": latest_sk})
        if "Item" in resp:
            version = resp["Item"].get("latestVersion", 0) + 1
    except Exception:
        pass  # First upload ever — version stays 1

    s3_key = f"raw-docs/{tenant_id}/{doc_id}/v{version}/{file_name}"
    
    # Generate presigned S3 PUT URL browser uploads directly to S3 without going through Lambda — no bottleneck, no size limit. ExpiresIn=900 → URL expires in 15 minutes.
    try:
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket":      BUCKET_NAME,
                "Key":         s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=900,
        )
    except Exception as e:
        return _error(500, f"Failed to generate presigned URL: {str(e)}")

    now = datetime.now(timezone.utc).isoformat()

    try:
        dynamodb_client.transact_write_items(
            TransactItems=[

                # --- Record 1: Version history entry ---
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": {
                            "PK":          {"S": latest_pk},
                            "SK":          {"S": f"VER#{version:06d}"},
                            "tenantId":    {"S": tenant_id},
                            "docId":       {"S": doc_id},
                            "version":     {"N": str(version)},
                            "status":      {"S": "UPLOADED"},
                            # Phase 2C ingestion worker changes this to READY
                            "s3Key":       {"S": s3_key},
                            "fileName":    {"S": file_name},
                            "contentType": {"S": content_type},
                            "metadata":    {"M": {
                                k: {"S": str(v)}
                                for k, v in metadata.items()
                            }},
                            "createdAt":   {"S": now},
                            "updatedAt":   {"S": now},
                        },
                        # Prevent overwriting an existing version
                        # (safety guard — version numbers should be unique)
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },

                # --- Record 2: LATEST pointer ---
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": {
                            "PK":            {"S": latest_pk},
                            "SK":            {"S": latest_sk},
                            "tenantId":      {"S": tenant_id},
                            "docId":         {"S": doc_id},
                            "latestVersion": {"N": str(version)},
                            "fileName":      {"S": file_name},
                            "updatedAt":     {"S": now},
                        },
                        # No condition — LATEST is always overwritten
                    }
                },

            ]
        )
    except dynamodb_client.exceptions.TransactionCanceledException as e:
        return _error(409, f"Version conflict — document version already exists: {str(e)}")
    except Exception as e:
        return _error(500, f"Failed to write DynamoDB records: {str(e)}")

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps({
            "uploadUrl": upload_url,
            "docId":     doc_id,
            "version":   version,
            "s3Key":     s3_key,
        }),
    }


def _error(status_code, message):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps({"error": message}),
    }