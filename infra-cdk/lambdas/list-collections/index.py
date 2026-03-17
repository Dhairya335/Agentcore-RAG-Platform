"""
Phase 3.3 — List Collections Lambda

GET /collections?tenantId={tenantId}

Queries GSI entityType-tenantId-index for all COLLECTION METADATA items
belonging to the tenant. Returns them sorted by name client-side (GSI sort
key is updatedAt, which is fine for the query; sorting is O(N log N) in
Lambda for personal-scale document counts).

INTERNAL role only.
"""

import json
import os
from boto3.dynamodb.conditions import Key
import boto3

dynamodb_res = boto3.resource("dynamodb")
TABLE_NAME   = os.environ["DOCS_TABLE_NAME"]
GSI_NAME     = "entityType-tenantId-index"
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")

_cors_list = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
_table     = dynamodb_res.Table(TABLE_NAME)

INTERNAL_GROUP = "internal"


def _get_groups(event: dict) -> list:
    claims = (event.get("requestContext", {})
                   .get("authorizer", {})
                   .get("claims", {}))
    raw = claims.get("cognito:groups", "")
    return [g.strip() for g in raw.split(",") if g.strip()] if raw else []


def handler(event, context):
    if INTERNAL_GROUP not in _get_groups(event):
        return _error(403, "Forbidden", event)

    query_params = event.get("queryStringParameters") or {}
    tenant_id    = (query_params.get("tenantId") or "").strip()

    if not tenant_id:
        return _error(400, "tenantId query parameter is required", event)

    resp = _table.query(
        IndexName=GSI_NAME,
        KeyConditionExpression=Key("entityType").eq("COLLECTION") & Key("tenantId").eq(tenant_id),
        FilterExpression=Key("SK").eq("METADATA"),
    )

    items = [_shape(item) for item in resp.get("Items", [])]
    items.sort(key=lambda c: c["name"].lower())

    return {
        "statusCode": 200,
        "headers": _cors_headers(event),
        "body": json.dumps({"collections": items, "count": len(items)}),
    }


def _shape(item: dict) -> dict:
    return {
        "colId":          item.get("colId", ""),
        "name":           item.get("name", ""),
        "description":    item.get("description", ""),
        "color":          item.get("color", "blue"),
        "visibilityMode": item.get("visibilityMode", "INTERNAL_ONLY"),
        "docCount":       int(item.get("docCount", 0)),
        "createdAt":      item.get("createdAt", ""),
        "updatedAt":      item.get("updatedAt", ""),
    }


def _cors_headers(event: dict) -> dict:
    return {
        "Content-Type":                "application/json",
        "Access-Control-Allow-Origin": _cors_origin(event),
    }


def _cors_origin(event: dict) -> str:
    origin = (event.get("headers") or {}).get("origin", "")
    return origin if origin in _cors_list else (_cors_list[0] if _cors_list else "*")


def _error(status_code: int, message: str, event: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": _cors_headers(event),
        "body": json.dumps({"error": message}),
    }
