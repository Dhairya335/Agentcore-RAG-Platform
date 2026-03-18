"""
Phase 3.5 — Preview Chunks Lambda

GET /documents/{docId}/chunks?tenantId={tenantId}&limit={limit}&chunkIndex={chunkIndex}

Called by:
  - DocumentDetailPanel (first N chunks for detail view)
  - SourceViewerDrawer (specific chunk + neighbours for citation inspection)

Two query modes derived from query parameters:
  1. Preview mode  — chunkIndex absent → SELECT ... ORDER BY chunk_index LIMIT :limit
  2. Anchor mode   — chunkIndex present → fetch anchor + (limit//2) neighbours each side
     Uses single query with BETWEEN to get a contiguous window in one round-trip.

INTERNAL role only — enforced via Cognito JWT groups claim.

No pip deps: only boto3 (RDS Data API via HTTPS + IAM SigV4).
"""

import json
import os

import boto3

rds_data   = boto3.client("rds-data")
ssm        = boto3.client("ssm")

STACK_NAME           = os.environ["STACK_NAME"]
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
CHUNK_LIMIT_MAX      = 20
INTERNAL_GROUP       = "internal"

_cors_list: list[str] = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
_ssm_cache: dict[str, str] = {}


def _get_ssm(key: str) -> str:
    if key not in _ssm_cache:
        _ssm_cache[key] = ssm.get_parameter(
            Name=f"/{STACK_NAME}/rag/{key}"
        )["Parameter"]["Value"]
    return _ssm_cache[key]


def _groups(event: dict) -> list[str]:
    claims = (event.get("requestContext", {})
                   .get("authorizer", {})
                   .get("claims", {}))
    raw = claims.get("cognito:groups", "")
    return [g.strip() for g in raw.split(",") if g.strip()] if raw else []


def handler(event, context):
    if INTERNAL_GROUP not in _groups(event):
        return _error(403, "Forbidden", event)

    path_params  = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    doc_id    = (path_params.get("docId")    or "").strip()
    tenant_id = (query_params.get("tenantId") or "").strip()

    if not doc_id or not tenant_id:
        return _error(400, "docId and tenantId are required", event)

    raw_limit  = query_params.get("limit", "5")
    raw_anchor = query_params.get("chunkIndex")

    limit = min(int(raw_limit) if str(raw_limit).isdigit() else 5, CHUNK_LIMIT_MAX)

    db_cluster_arn = _get_ssm("aurora-cluster-arn")
    db_secret_arn  = _get_ssm("aurora-secret-arn")
    db_name        = _get_ssm("aurora-db-name")

    if raw_anchor is not None and str(raw_anchor).lstrip("-").isdigit():
        anchor = int(raw_anchor)
        half   = limit // 2
        lo     = max(0, anchor - half)
        hi     = anchor + half

        sql = """
            SELECT chunk_index, chunk_total, content, page_number, section_title, source_type
            FROM   fast_chunks
            WHERE  tenant_id   = :tenant_id
              AND  doc_id      = :doc_id
              AND  chunk_index BETWEEN :lo AND :hi
            ORDER  BY chunk_index
        """
        params = [
            {"name": "tenant_id", "value": {"stringValue": tenant_id}},
            {"name": "doc_id",    "value": {"stringValue": doc_id}},
            {"name": "lo",        "value": {"longValue":   lo}},
            {"name": "hi",        "value": {"longValue":   hi}},
        ]
    else:
        sql = """
            SELECT chunk_index, chunk_total, content, page_number, section_title, source_type
            FROM   fast_chunks
            WHERE  tenant_id = :tenant_id
              AND  doc_id    = :doc_id
            ORDER  BY chunk_index
            LIMIT  :limit
        """
        params = [
            {"name": "tenant_id", "value": {"stringValue": tenant_id}},
            {"name": "doc_id",    "value": {"stringValue": doc_id}},
            {"name": "limit",     "value": {"longValue":   limit}},
        ]

    resp = rds_data.execute_statement(
        resourceArn=db_cluster_arn,
        secretArn=db_secret_arn,
        database=db_name,
        sql=sql,
        parameters=params,
        includeResultMetadata=True,
    )

    columns = [c["name"] for c in resp.get("columnMetadata", [])]
    chunks  = [_row(columns, row) for row in resp.get("records", [])]

    return {
        "statusCode": 200,
        "headers": _cors_headers(event),
        "body": json.dumps({"chunks": chunks, "count": len(chunks)}),
    }


def _row(columns: list[str], row: list[dict]) -> dict:
    result = {}
    for col, cell in zip(columns, row):
        if cell.get("isNull"):
            result[col] = None
        elif "stringValue" in cell:
            result[col] = cell["stringValue"]
        elif "longValue" in cell:
            result[col] = cell["longValue"]
        elif "doubleValue" in cell:
            result[col] = cell["doubleValue"]
        else:
            result[col] = None
    return result


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
