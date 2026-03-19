"""
Admin endpoint: GET /admin/orgs/{orgId}

Returns org metadata + all active members + pending/consumed invites.
INTERNAL role only — enforced via Cognito group claim check.

Response shape:
{
  "orgId":    "acme-abc123",
  "orgName":  "Acme Corp",
  "orgType":  "CLIENT",
  "status":   "ACTIVE",
  "createdAt": "...",
  "members": [
    { "userId": "sub-uuid", "email": "user@example.com",
      "membershipStatus": "ACTIVE", "joinedAt": "..." }
  ],
  "invites": [
    { "inviteId": "invite-uuid", "createdAt": "...", "expiresAt": "...",
      "consumed": false, "createdBy": "admin-sub" }
  ]
}

Errors (fail loudly, no silent fallbacks):
  - 403 if caller is not INTERNAL
  - 400 if orgId path param is missing
  - 404 if org record does not exist
  - 500 if any DynamoDB call fails — members and invites are required data,
    not optional; a partial response would be misleading to the admin
"""

import json
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

dynamodb               = boto3.resource("dynamodb")
ORGS_TABLE_NAME        = os.environ["ORGS_TABLE_NAME"]
MEMBERSHIPS_TABLE_NAME = os.environ["MEMBERSHIPS_TABLE_NAME"]
INVITES_TABLE_NAME     = os.environ["INVITES_TABLE_NAME"]
CORS_ALLOWED_ORIGINS   = os.environ["CORS_ALLOWED_ORIGINS"]  # required; no silent default
cors_origins           = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]


def handler(event: dict, context: object) -> dict:
    """
    Handle GET /admin/orgs/{orgId}.

    Fetches the org record, all membership records (via org-id GSI), and all
    invite records (via org-id GSI) for the given orgId.

    Args:
        event:   API Gateway Lambda proxy event. Claims at
                 event['requestContext']['authorizer']['claims'].
                 Path param orgId at event['pathParameters']['orgId'].
        context: Lambda context object (unused).

    Returns:
        API Gateway response dict. 200 with org detail on success.
        400/403/404/500 on errors — never partial data.
    """
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims", {})

    # Cognito groups claim must be a string — any other type means a malformed token
    raw_groups = claims.get("cognito:groups", "")
    if not isinstance(raw_groups, str):
        return _error(400, f"Unexpected type for cognito:groups claim: {type(raw_groups)}", event)
    groups: list[str] = [g.strip() for g in raw_groups.split(",") if g.strip()]

    if "internal" not in groups:
        return _error(403, "Forbidden: only INTERNAL users may view org detail", event)

    path_params = event.get("pathParameters") or {}
    org_id: str = path_params.get("orgId", "").strip()
    if not org_id:
        return _error(400, "orgId path parameter is required", event)

    orgs_table        = dynamodb.Table(ORGS_TABLE_NAME)
    memberships_table = dynamodb.Table(MEMBERSHIPS_TABLE_NAME)
    invites_table     = dynamodb.Table(INVITES_TABLE_NAME)

    # ── Fetch org record ──────────────────────────────────────────────────────
    try:
        org_resp = orgs_table.get_item(
            Key={"org_id": org_id},
            ConsistentRead=True,
        )
    except Exception as exc:
        print(f"[ORG DETAIL] DynamoDB GetItem failed for org_id={org_id}: {exc}")
        return _error(500, "Failed to fetch organisation record", event)

    org: dict | None = org_resp.get("Item")
    if not org:
        return _error(404, f"Organisation not found: {org_id}", event)

    # ── Fetch members via GSI ─────────────────────────────────────────────────
    # Failure here is a hard error — returning empty members to the admin
    # would be misleading (they might think the org has no members).
    try:
        members_raw = _query_all_pages(
            table=memberships_table,
            index_name="org-id-index",
            key_condition=Key("org_id").eq(org_id),
        )
    except Exception as exc:
        print(f"[ORG DETAIL] DynamoDB query failed for members org_id={org_id}: {exc}")
        return _error(500, "Failed to fetch organisation members", event)

    # ── Fetch invites via GSI ─────────────────────────────────────────────────
    try:
        invites_raw = _query_all_pages(
            table=invites_table,
            index_name="org-id-index",
            key_condition=Key("org_id").eq(org_id),
        )
    except Exception as exc:
        print(f"[ORG DETAIL] DynamoDB query failed for invites org_id={org_id}: {exc}")
        return _error(500, "Failed to fetch organisation invites", event)

    # Build member list — KeyError on missing required fields propagates as 500
    members: list[dict] = [
        {
            "userId":           item["user_sub"],
            "email":            item["email"],
            "membershipStatus": item["membership_status"],
            "joinedAt":         item["joined_at"],
        }
        for item in members_raw
    ]

    # Build invite list — convert epoch TTL back to ISO string for display
    invites: list[dict] = sorted(
        [
            {
                "inviteId":  item["invite_id"],
                "createdAt": item["created_at"],
                "expiresAt": _epoch_to_iso(int(item["expires_at"])),
                "consumed":  item.get("status", "PENDING") == "CONSUMED",
                "createdBy": item["created_by"],
            }
            for item in invites_raw
        ],
        key=lambda i: i["createdAt"],
        reverse=True,
    )

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({
            "orgId":    org["org_id"],
            "orgName":  org["org_name"],     # KeyError propagates as 500 — data integrity
            "orgType":  org["org_type"],
            "status":   org["status"],
            "createdAt": org["created_at"],
            "members":  members,
            "invites":  invites,
        }),
    }


def _epoch_to_iso(epoch_seconds: int) -> str:
    """
    Convert a Unix epoch timestamp (integer seconds) to an ISO 8601 UTC string.

    Args:
        epoch_seconds: Integer Unix timestamp in seconds.

    Returns:
        ISO 8601 string with UTC timezone, e.g. "2026-03-26T10:00:00+00:00".
    """
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def _query_all_pages(table, index_name: str, key_condition) -> list[dict]:
    """
    Query a DynamoDB GSI, following pagination until all items are returned.

    Args:
        table:          boto3 DynamoDB Table resource.
        index_name:     Name of the Global Secondary Index to query.
        key_condition:  boto3 Key condition expression (e.g. Key("org_id").eq(value)).

    Returns:
        Flat list of all item dicts returned by the query across all pages.

    Raises:
        Any boto3 / botocore exception from the underlying DynamoDB call.
        Callers must handle these; this function does not swallow errors.
    """
    items: list[dict] = []
    resp = table.query(
        IndexName=index_name,
        KeyConditionExpression=key_condition,
    )
    items.extend(resp.get("Items", []))

    while "LastEvaluatedKey" in resp:
        resp = table.query(
            IndexName=index_name,
            KeyConditionExpression=key_condition,
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))

    return items


def _cors_origin(event: dict) -> str:
    """
    Resolve the CORS Access-Control-Allow-Origin response header value.

    Matches the request Origin header against the cors_origins allowlist.
    Returns the matched origin, or the first allowed origin as the strict
    default. Never returns '*' — the allowlist must be non-empty.

    Args:
        event: API Gateway Lambda proxy event.

    Returns:
        Allowed origin string to echo in the response header.
        Raises IndexError if cors_origins is empty (loud failure, not silent).
    """
    request_origin: str = (event.get("headers") or {}).get("origin", "")
    if request_origin in cors_origins:
        return request_origin
    return cors_origins[0]


def _error(status_code: int, message: str, event: dict) -> dict:
    """
    Build an error API Gateway response dict.

    Args:
        status_code: HTTP status code (400, 403, 404, 500, etc.).
        message:     Human-readable error message returned in the JSON body.
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
