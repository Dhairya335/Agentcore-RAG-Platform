"""
Phase 3.3 — Collection Membership Lambda

Handles both add and remove operations via HTTP method routing:

  POST   /collections/{colId}/documents        → add doc to collection
  DELETE /collections/{colId}/documents/{docId} → remove doc from collection

Body for POST: { "tenantId": "...", "docId": "..." }
Query for DELETE: ?tenantId={tenantId}

Membership item:
  PK = TENANT#{tenantId}#COL#{colId}
  SK = DOC#{docId}
  entityType = COLLECTION_MEMBER

On add: atomic transact_write_items creates membership + increments docCount.
On remove: transact_write_items deletes membership + decrements docCount (floor 0).

INTERNAL role only.
"""

import json
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

dynamodb   = boto3.client("dynamodb")
TABLE_NAME = os.environ["DOCS_TABLE_NAME"]
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")

_cors_list     = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
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

    method      = event.get("httpMethod", "")
    path_params = event.get("pathParameters") or {}
    col_id      = (path_params.get("colId") or "").strip()

    if method == "POST":
        return _add(event, col_id)
    if method == "DELETE":
        doc_id    = (path_params.get("docId") or "").strip()
        tenant_id = ((event.get("queryStringParameters") or {}).get("tenantId") or "").strip()
        return _remove(event, col_id, doc_id, tenant_id)

    return _error(405, "Method not allowed", event)


def _add(event: dict, col_id: str) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body", event)

    tenant_id = (body.get("tenantId") or "").strip()
    doc_id    = (body.get("docId") or "").strip()

    if not tenant_id or not col_id or not doc_id:
        return _error(400, "tenantId, colId, and docId are required", event)

    col_pk  = f"TENANT#{tenant_id}#COL#{col_id}"
    mem_sk  = f"DOC#{doc_id}"
    now     = datetime.now(timezone.utc).isoformat()

    try:
        dynamodb.transact_write_items(Items=[
            {
                "Put": {
                    "TableName":           TABLE_NAME,
                    "Item": {
                        "PK":         {"S": col_pk},
                        "SK":         {"S": mem_sk},
                        "entityType": {"S": "COLLECTION_MEMBER"},
                        "tenantId":   {"S": tenant_id},
                        "colId":      {"S": col_id},
                        "docId":      {"S": doc_id},
                        "addedAt":    {"S": now},
                    },
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": {
                        "PK": {"S": col_pk},
                        "SK": {"S": "METADATA"},
                    },
                    "UpdateExpression":          "SET docCount = docCount + :one, updatedAt = :now",
                    "ExpressionAttributeValues": {":one": {"N": "1"}, ":now": {"S": now}},
                    "ConditionExpression":       "attribute_exists(PK)",
                }
            },
        ])
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "TransactionCanceledException":
            return _error(409, "Document already in collection or collection does not exist", event)
        raise

    return {
        "statusCode": 200,
        "headers": _cors_headers(event),
        "body": json.dumps({"colId": col_id, "docId": doc_id, "addedAt": now}),
    }


def _remove(event: dict, col_id: str, doc_id: str, tenant_id: str) -> dict:
    if not tenant_id or not col_id or not doc_id:
        return _error(400, "tenantId, colId, and docId are required", event)

    col_pk = f"TENANT#{tenant_id}#COL#{col_id}"
    mem_sk = f"DOC#{doc_id}"
    now    = datetime.now(timezone.utc).isoformat()

    try:
        dynamodb.transact_write_items(Items=[
            {
                "Delete": {
                    "TableName":           TABLE_NAME,
                    "Key": {
                        "PK": {"S": col_pk},
                        "SK": {"S": mem_sk},
                    },
                    "ConditionExpression": "attribute_exists(PK)",
                }
            },
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": {
                        "PK": {"S": col_pk},
                        "SK": {"S": "METADATA"},
                    },
                    "UpdateExpression":          "SET docCount = if_not_exists(docCount, :zero) - :one, updatedAt = :now",
                    "ExpressionAttributeValues": {
                        ":one":  {"N": "1"},
                        ":zero": {"N": "0"},
                        ":now":  {"S": now},
                    },
                    "ConditionExpression": "attribute_exists(PK)",
                }
            },
        ])
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "TransactionCanceledException":
            return _error(404, "Membership or collection not found", event)
        raise

    return {
        "statusCode": 200,
        "headers": _cors_headers(event),
        "body": json.dumps({"colId": col_id, "docId": doc_id, "removed": True}),
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
