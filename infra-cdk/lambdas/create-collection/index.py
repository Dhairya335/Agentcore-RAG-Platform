"""
Phase 3.3 — Create Collection Lambda

POST /collections
Body: { "tenantId": "...", "name": "...", "description": "...", "color": "..." }

Single-table design: creates one METADATA item in the documents table.
  PK = TENANT#{tenantId}#COL#{colId}
  SK = METADATA
  entityType = COLLECTION   (used by list-collections GSI)
  visibilityMode = INTERNAL_ONLY | EXTERNAL_ALLOWED

INTERNAL role only — returns 403 for EXTERNAL users.
"""

import json
import os
import uuid
from datetime import datetime, timezone

import boto3

dynamodb   = boto3.client("dynamodb")
TABLE_NAME = os.environ["DOCS_TABLE_NAME"]
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")

_cors_list = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

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

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body", event)

    tenant_id      = (body.get("tenantId") or "").strip()
    name           = (body.get("name") or "").strip()
    description    = (body.get("description") or "").strip()
    color          = (body.get("color") or "blue").strip()
    visibility     = body.get("visibilityMode", "INTERNAL_ONLY")

    if not tenant_id or not name:
        return _error(400, "tenantId and name are required", event)

    if visibility not in ("INTERNAL_ONLY", "EXTERNAL_ALLOWED"):
        return _error(400, "visibilityMode must be INTERNAL_ONLY or EXTERNAL_ALLOWED", event)

    col_id = str(uuid.uuid4())
    now    = datetime.now(timezone.utc).isoformat()

    dynamodb.put_item(
        TableName=TABLE_NAME,
        Item={
            "PK":             {"S": f"TENANT#{tenant_id}#COL#{col_id}"},
            "SK":             {"S": "METADATA"},
            "entityType":     {"S": "COLLECTION"},
            "tenantId":       {"S": tenant_id},
            "colId":          {"S": col_id},
            "name":           {"S": name},
            "description":    {"S": description},
            "color":          {"S": color},
            "visibilityMode": {"S": visibility},
            "docCount":       {"N": "0"},
            "createdAt":      {"S": now},
            "updatedAt":      {"S": now},
        },
        ConditionExpression="attribute_not_exists(PK)",
    )

    return {
        "statusCode": 201,
        "headers": _cors_headers(event),
        "body": json.dumps({
            "colId":          col_id,
            "name":           name,
            "description":    description,
            "color":          color,
            "visibilityMode": visibility,
            "docCount":       0,
            "createdAt":      now,
        }),
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
