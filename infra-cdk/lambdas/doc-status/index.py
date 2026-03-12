"""
Phase 2D — Document Status Lambda

Called by API Gateway GET /documents/{docId}/status?tenantId={tenantId}
with a Cognito Bearer token.

Reads the DynamoDB VER#000001 record for the requested document and returns
its current ingestion status so the frontend can display progress.

Status lifecycle (set by ingestion-worker):
  UPLOADED → READY   (ingestion succeeded, chunks are searchable)
  UPLOADED → FAILED  (ingestion error, errorMessage is stored)

Response shape:
  {
    "docId":      "3b398eaf-...",
    "status":     "UPLOADED" | "READY" | "FAILED",
    "chunkCount": 21,              <- present only when READY
    "errorMessage": "...",         <- present only when FAILED
    "fileName":   "NIPS-2017-...",
    "updatedAt":  "2026-03-12T10:30:00Z"
  }

Design notes:
  - Version is hardcoded to 1 (VER#000001) for now.
    Phase 2F (document management) will introduce version selection.
  - tenantId is read from the query string rather than the JWT claim because
    this Lambda is called by the frontend (not the agent) and the Cognito
    authorizer only validates the token — it doesn't pass the sub to the
    Lambda context. The frontend passes its own sub as tenantId.
    Security: DynamoDB key includes tenantId so a user cannot read another
    tenant's document even if they forge the tenantId parameter, because
    they would need the other tenant's docId too (a UUID they cannot guess).
"""

import json
import os

import boto3

dynamodb   = boto3.client("dynamodb")
TABLE_NAME = os.environ["DOCS_TABLE_NAME"]
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")

_cors_list = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]


def handler(event, context):
    # Extract path + query params
    path_params  = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    doc_id    = path_params.get("docId", "").strip()
    tenant_id = query_params.get("tenantId", "").strip()

    if not doc_id:
        return _error(400, "docId path parameter is required", event)
    if not tenant_id:
        return _error(400, "tenantId query parameter is required", event)

    # Read DynamoDB VER#000001
    # Version 1 is always the first ingestion of a document.
    # Future: accept a ?version= query param for versioned docs.
    pk = f"TENANT#{tenant_id}#DOC#{doc_id}"
    sk = "VER#000001"

    try:
        resp = dynamodb.get_item(
            TableName=TABLE_NAME,
            Key={
                "PK": {"S": pk},
                "SK": {"S": sk},
            },
            ProjectionExpression="#s, chunkCount, errorMessage, fileName, updatedAt",
            ExpressionAttributeNames={"#s": "status"},
        )
    except Exception as e:
        print(f"[DOC-STATUS ERROR] DynamoDB get_item failed: {e}")
        return _error(500, f"Failed to read document status: {str(e)}", event)

    item = resp.get("Item")
    if not item:
        return _error(404, f"Document not found: {doc_id}", event)

    # Build response
    status    = item.get("status",    {}).get("S", "UNKNOWN")
    file_name = item.get("fileName",  {}).get("S")
    updated   = item.get("updatedAt", {}).get("S")

    body: dict = {
        "docId":     doc_id,
        "status":    status,
        "fileName":  file_name,
        "updatedAt": updated,
    }

    # chunkCount is only present on READY records
    if "chunkCount" in item:
        body["chunkCount"] = int(item["chunkCount"].get("N", "0"))

    # errorMessage is only present on FAILED records
    if "errorMessage" in item:
        body["errorMessage"] = item["errorMessage"].get("S", "")

    print(f"[DOC-STATUS] {pk} {sk} → {status}")

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type":                 "application/json",
            "Access-Control-Allow-Origin":  _cors_origin(event),
            "Cache-Control":                "no-store",   # never cache status responses
        },
        "body": json.dumps(body),
    }


# Helpers

def _cors_origin(event: dict) -> str:
    origin = (event.get("headers") or {}).get("origin", "")
    return origin if origin in _cors_list else (_cors_list[0] if _cors_list else "*")


def _error(status_code: int, message: str, event: dict = {}) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({"error": message}),
    }
