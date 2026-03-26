"""
Phase 3.2 — Get Document Lambda

GET /documents/{docId}?tenantId={tenantId}

Returns full document metadata for a single document:
  - LATEST pointer record (current state, version number)
  - VER#000001 record (creation metadata, contentType, s3Key)

Both are fetched in a single BatchGetItem call (1 DynamoDB round-trip).

RBAC: INTERNAL role only. EXTERNAL users receive 403.

Response shape:
{
  "docId":         "uuid",
  "fileName":      "policy.pdf",
  "latestVersion": 1,
  "status":        "READY",
  "chunkCount":    21,
  "s3Key":         "tenant-id/v1/policy.pdf",
  "contentType":   "application/pdf",
  "createdAt":     "2026-03-17T...",
  "updatedAt":     "2026-03-17T...",
  "errorMessage":  null
}
"""

import json
import os

import boto3

dynamodb   = boto3.client("dynamodb")
TABLE_NAME = os.environ["DOCS_TABLE_NAME"]
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")

_cors_list = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]


def handler(event, context):
    # ── RBAC: internal only    
    role = _extract_role(event)
    if role != "INTERNAL":
        return _error(403, "Document detail access requires INTERNAL role", event)

    # ── Input   
    path_params  = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    doc_id    = path_params.get("docId", "").strip()
    tenant_id = query_params.get("tenantId", "").strip()

    if not doc_id:
        return _error(400, "docId path parameter is required", event)
    if not tenant_id:
        return _error(400, "tenantId query parameter is required", event)

    pk = f"TENANT#{tenant_id}#DOC#{doc_id}"

    # ── BatchGetItem: LATEST + VER#000001 in one round-trip      
    # We always read version 1 for creation metadata (createdAt, contentType, s3Key).
    # LATEST gives us current status, chunkCount, latestVersion.
    # Future: accept ?version= param to read a specific VER record.
    try:
        resp = dynamodb.batch_get_item(
            RequestItems={
                TABLE_NAME: {
                    "Keys": [
                        {"PK": {"S": pk}, "SK": {"S": "LATEST"}},
                        {"PK": {"S": pk}, "SK": {"S": "VER#000001"}},
                    ],
                    "ConsistentRead": False,
                }
            }
        )
    except Exception as e:
        print(f"[GET-DOC ERROR] BatchGetItem failed: {e}")
        return _error(500, f"Failed to get document: {str(e)}", event)

    items = resp.get("Responses", {}).get(TABLE_NAME, [])
    if not items:
        return _error(404, f"Document not found: {doc_id}", event)

    # Index by SK
    by_sk: dict[str, dict] = {}
    for item in items:
        sk = item.get("SK", {}).get("S", "")
        by_sk[sk] = item

    latest = by_sk.get("LATEST", {})
    ver1   = by_sk.get("VER#000001", {})

    # If neither record exists
    if not latest and not ver1:
        return _error(404, f"Document not found: {doc_id}", event)

    # ── Shape response    ─────
    def _s(item, key):  return item.get(key, {}).get("S")
    def _n(item, key):  return int(item.get(key, {}).get("N", 0)) if key in item else None

    body = {
        "docId":         doc_id,
        "tenantId":      tenant_id,
        "fileName":      _s(latest, "fileName") or _s(ver1, "fileName"),
        "latestVersion": _n(latest, "latestVersion") or _n(ver1, "version"),
        "status":        _s(latest, "status")   or _s(ver1, "status"),
        "chunkCount":    _n(latest, "chunkCount"),
        "s3Key":         _s(ver1, "s3Key"),
        "contentType":   _s(ver1, "contentType"),
        "createdAt":     _s(ver1, "createdAt"),
        "updatedAt":     _s(latest, "updatedAt") or _s(ver1, "updatedAt"),
        "errorMessage":  _s(latest, "errorMessage") or _s(ver1, "errorMessage"),
    }

    print(f"[GET-DOC] {pk} → status={body['status']}")

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
            "Cache-Control":               "no-store",
        },
        "body": json.dumps(body),
    }


# ── Helpers   ──

def _extract_role(event: dict) -> str:
    """Extract role from Cognito JWT claims injected by API Gateway authorizer."""
    try:
        claims = (
            event.get("requestContext", {})
                 .get("authorizer", {})
                 .get("claims", {})
        )
        groups = claims.get("cognito:groups", "")
        if isinstance(groups, str):
            group_list = [g.strip() for g in groups.split(",") if g.strip()]
        else:
            group_list = list(groups)

        return "INTERNAL" if "internal" in group_list else "EXTERNAL"
    except Exception:
        return "EXTERNAL"


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
