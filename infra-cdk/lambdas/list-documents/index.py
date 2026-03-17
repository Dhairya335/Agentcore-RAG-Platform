"""
Phase 3.1 — List Documents Lambda

GET /documents?limit=20&nextPageToken=<base64>

Returns all documents for the authenticated tenant, newest first.
Queries the DynamoDB GSI tenantId-updatedAt-index on LATEST records only.

RBAC: INTERNAL role only. EXTERNAL users receive 403.

Why LATEST records only:
  The GSI partitions on tenantId. Both VER and LATEST records have tenantId.
  We filter SK = LATEST to avoid returning one row per version per document.
  The ingestion-worker denormalizes status + chunkCount into LATEST (Step 0),
  so this query returns a complete picture without any fan-out reads.

Why GSI + filter vs scan:
  Scan is O(table_size) and costs RCUs proportional to all items.
  GSI query is O(items_for_tenant) — scales cleanly per tenant.

Pagination:
  DynamoDB LastEvaluatedKey is base64-encoded JSON and returned as nextPageToken.
  Client passes it back as ?nextPageToken=... to get the next page.
"""

import base64
import json
import os
from datetime import datetime, timezone

import boto3

dynamodb   = boto3.client("dynamodb")
TABLE_NAME = os.environ["DOCS_TABLE_NAME"]
GSI_NAME   = "tenantId-updatedAt-index"
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")

_cors_list = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

DEFAULT_LIMIT = 20
MAX_LIMIT     = 100


def handler(event, context):
    # ── RBAC: internal only ──────────────────────────────────────────────────
    role = _extract_role(event)
    if role != "INTERNAL":
        return _error(403, "Document library access requires INTERNAL role", event)

    # ── Input ────────────────────────────────────────────────────────────────
    query_params = event.get("queryStringParameters") or {}
    tenant_id    = query_params.get("tenantId", "").strip()

    if not tenant_id:
        return _error(400, "tenantId query parameter is required", event)

    try:
        limit = min(int(query_params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
    except (ValueError, TypeError):
        limit = DEFAULT_LIMIT

    next_page_token = query_params.get("nextPageToken")

    # ── DynamoDB GSI query ───────────────────────────────────────────────────
    # Query GSI partitioned by tenantId, sorted by updatedAt DESC.
    # FilterExpression SK = LATEST ensures we only return one row per document
    # (not one row per version).
    query_kwargs = dict(
        TableName                 = TABLE_NAME,
        IndexName                 = GSI_NAME,
        KeyConditionExpression    = "tenantId = :tid",
        FilterExpression          = "SK = :latest",
        ExpressionAttributeValues = {
            ":tid":    {"S": tenant_id},
            ":latest": {"S": "LATEST"},
        },
        ScanIndexForward = False,   # newest first (descending updatedAt)
        Limit            = limit * 3,  # over-fetch to account for VER rows that get filtered
    )

    if next_page_token:
        try:
            lek = json.loads(base64.b64decode(next_page_token).decode())
            query_kwargs["ExclusiveStartKey"] = lek
        except Exception:
            return _error(400, "Invalid nextPageToken", event)

    try:
        resp = dynamodb.query(**query_kwargs)
    except Exception as e:
        print(f"[LIST-DOCS ERROR] DynamoDB query failed: {e}")
        return _error(500, f"Failed to list documents: {str(e)}", event)

    # ── Shape response ───────────────────────────────────────────────────────
    items   = resp.get("Items", [])
    docs    = [_shape_doc(item) for item in items]

    # Encode next page token if DynamoDB returned a continuation key
    new_token = None
    if "LastEvaluatedKey" in resp:
        new_token = base64.b64encode(
            json.dumps(resp["LastEvaluatedKey"]).encode()
        ).decode()

    body = {
        "documents":     docs,
        "count":         len(docs),
        "nextPageToken": new_token,
    }

    print(f"[LIST-DOCS] tenant={tenant_id} returned {len(docs)} docs")

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
            "Cache-Control":               "no-store",
        },
        "body": json.dumps(body),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _shape_doc(item: dict) -> dict:
    """
    Map a raw DynamoDB LATEST record to a clean frontend-facing document object.
    All fields are optional-safe — missing attributes return None gracefully.
    """
    def _s(key):  return item.get(key, {}).get("S")
    def _n(key):  return int(item.get(key, {}).get("N", 0)) if key in item else None

    # Extract docId from PK: "TENANT#{tenantId}#DOC#{docId}"
    pk     = _s("PK") or ""
    doc_id = pk.split("#DOC#")[-1] if "#DOC#" in pk else _s("docId")

    return {
        "docId":         doc_id,
        "fileName":      _s("fileName"),
        "latestVersion": _n("latestVersion"),
        "status":        _s("status"),
        "chunkCount":    _n("chunkCount"),
        "updatedAt":     _s("updatedAt"),
        "contentType":   _s("contentType"),
        "errorMessage":  _s("errorMessage"),
    }


def _extract_role(event: dict) -> str:
    """
    Extract user role from Cognito JWT claims passed by API Gateway.
    API Gateway Cognito authorizer injects claims into requestContext.
    """
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
