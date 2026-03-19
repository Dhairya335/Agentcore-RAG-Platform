"""
Admin endpoint: GET /admin/orgs

Lists all orgs from the FAST-stack-orgs DynamoDB table.
INTERNAL role only — enforced via Cognito group claim check.

Response (200):
{
  "orgs": [
    { "orgId": "acme-abc123", "orgName": "Acme Corp", "orgType": "CLIENT",
      "status": "ACTIVE", "createdAt": "2026-03-19T..." }
  ],
  "count": 1
}

Errors:
  - 403 if caller is not INTERNAL
  - 500 if the DynamoDB scan fails (never returns partial/empty on error)
"""

import json
import os

import boto3

dynamodb             = boto3.resource("dynamodb")
ORGS_TABLE_NAME      = os.environ["ORGS_TABLE_NAME"]
CORS_ALLOWED_ORIGINS = os.environ["CORS_ALLOWED_ORIGINS"]  # required; no silent default
cors_origins         = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]


def handler(event: dict, context: object) -> dict:
    """
    Handle GET /admin/orgs.

    Scans the orgs table (full scan — org count is always small) and returns
    all records sorted by createdAt descending.

    Args:
        event:   API Gateway Lambda proxy event. Claims at
                 event['requestContext']['authorizer']['claims'].
        context: Lambda context object (unused).

    Returns:
        API Gateway response dict. 200 with org list on success.
        403 if caller is not INTERNAL. 500 if DynamoDB scan fails.
    """
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims", {})

    raw_groups = claims.get("cognito:groups", "")
    if not isinstance(raw_groups, str):
        return _error(400, f"Unexpected type for cognito:groups claim: {type(raw_groups)}", event)
    groups: list[str] = [g.strip() for g in raw_groups.split(",") if g.strip()]

    if "internal" not in groups:
        return _error(403, "Forbidden: only INTERNAL users may list organisations", event)

    orgs_table = dynamodb.Table(ORGS_TABLE_NAME)

    try:
        items: list[dict] = _scan_all_pages(table=orgs_table)
    except Exception as exc:
        print(f"[LIST-ORGS] DynamoDB scan failed: {exc}")
        return _error(500, "Failed to list organisations", event)

    # KeyError on missing fields propagates as 500 — no silent omission of required data
    orgs: list[dict] = sorted(
        [
            {
                "orgId":     item["org_id"],
                "orgName":   item["org_name"],
                "orgType":   item["org_type"],
                "status":    item["status"],
                "createdAt": item["created_at"],
            }
            for item in items
        ],
        key=lambda o: o["createdAt"],
        reverse=True,
    )

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({"orgs": orgs, "count": len(orgs)}),
    }


def _scan_all_pages(table) -> list[dict]:
    """
    Scan a DynamoDB table, following pagination until all items are returned.

    Args:
        table: boto3 DynamoDB Table resource object.

    Returns:
        Flat list of all item dicts returned by the scan across all pages.

    Raises:
        Any boto3 / botocore exception from the underlying DynamoDB call.
        Callers must handle these; this function does not swallow errors.
    """
    items: list[dict] = []
    resp = table.scan()
    items.extend(resp.get("Items", []))

    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    return items


def _cors_origin(event: dict) -> str:
    """
    Resolve the CORS Access-Control-Allow-Origin response header value.

    Matches the request Origin header against the cors_origins allowlist.
    Returns the matched origin, or the first allowed origin as the strict
    default. Never returns '*'.

    Args:
        event: API Gateway Lambda proxy event.

    Returns:
        Allowed origin string. Raises IndexError if cors_origins is empty.
    """
    request_origin: str = (event.get("headers") or {}).get("origin", "")
    if request_origin in cors_origins:
        return request_origin
    return cors_origins[0]


def _error(status_code: int, message: str, event: dict) -> dict:
    """
    Build an error API Gateway response dict.

    Args:
        status_code: HTTP status code (400, 403, 500, etc.).
        message:     Human-readable error message in the JSON body.
        event:       Original Lambda event (used to resolve CORS origin).

    Returns:
        API Gateway response dict with the given status code.
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({"error": message}),
    }
