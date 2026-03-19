"""
Admin endpoint: POST /admin/orgs

Creates a new client org record in the FAST-stack-orgs DynamoDB table.
Only INTERNAL users may call this endpoint (enforced by API Gateway Cognito
authorizer + explicit role check in handler).

Request body:
    { "orgName": "Acme Corp", "orgType": "CLIENT" }

Response (201):
    { "orgId": "acme-corp-a1b2c3", "orgName": "Acme Corp",
      "orgType": "CLIENT", "status": "ACTIVE" }

Security:
  - JWT validated by API Gateway Cognito authorizer before this Lambda runs
  - Role check: caller must be in Cognito 'internal' group
  - org_id is generated server-side (slugified orgName + 6-char uuid suffix)
  - Caller cannot choose or influence org_id
  - Conditional put prevents duplicate org_id (astronomically unlikely but handled)
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone

import boto3

dynamodb             = boto3.resource("dynamodb")
ORGS_TABLE_NAME      = os.environ["ORGS_TABLE_NAME"]
CORS_ALLOWED_ORIGINS = os.environ["CORS_ALLOWED_ORIGINS"]  # required; no silent default
cors_origins         = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

VALID_ORG_TYPES: frozenset[str] = frozenset({"CLIENT", "INTERNAL_VENDOR", "PERSONAL_FUTURE"})


def handler(event: dict, context: object) -> dict:
    """
    Handle POST /admin/orgs.

    Validates the caller is INTERNAL, parses orgName + orgType from the request
    body, generates a slug-based org_id, and writes the org record to DynamoDB.

    Args:
        event:   API Gateway Lambda proxy event. Claims at
                 event['requestContext']['authorizer']['claims'].
                 JSON body with 'orgName' (required) and 'orgType' (optional).
        context: Lambda context object (unused).

    Returns:
        API Gateway response dict. 201 with org record on success.
        400/403/409/500 on errors.
    """
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims", {})

    # cognito:groups is always a string from API GW Cognito authorizer claims
    raw_groups = claims.get("cognito:groups", "")
    if not isinstance(raw_groups, str):
        return _error(400, f"Unexpected type for cognito:groups claim: {type(raw_groups)}", event)
    groups: list[str] = [g.strip() for g in raw_groups.split(",") if g.strip()]

    if "internal" not in groups:
        return _error(403, "Forbidden: only INTERNAL users may create organisations", event)

    caller_sub: str = claims.get("sub", "")
    if not caller_sub:
        return _error(401, "Missing sub claim in token", event)

    try:
        body: dict = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError as exc:
        return _error(400, f"Invalid JSON body: {exc}", event)

    org_name: str = (body.get("orgName") or "").strip()
    if not org_name:
        return _error(400, "orgName is required and must be a non-empty string", event)

    # orgType defaults to CLIENT when omitted — this is intentional product logic, not a fallback
    org_type: str = (body.get("orgType") or "CLIENT").strip().upper()
    if org_type not in VALID_ORG_TYPES:
        return _error(400, f"orgType must be one of {sorted(VALID_ORG_TYPES)}", event)

    # Generate org_id: slug + 6-char uuid suffix for uniqueness
    slug:   str = _slugify(org_name)
    suffix: str = str(uuid.uuid4()).replace("-", "")[:6]
    org_id: str = f"{slug}-{suffix}"

    now: str = datetime.now(timezone.utc).isoformat()

    table = dynamodb.Table(ORGS_TABLE_NAME)
    try:
        table.put_item(
            Item={
                "org_id":     org_id,
                "org_name":   org_name,
                "org_type":   org_type,
                "status":     "ACTIVE",
                "created_at": now,
                "created_by": caller_sub,
                "settings":   {},
            },
            ConditionExpression="attribute_not_exists(org_id)",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return _error(409, f"org_id collision: '{org_id}' already exists — please retry", event)
    except Exception as exc:
        print(f"[CREATE-ORG] DynamoDB PutItem failed for org_id={org_id}: {exc}")
        return _error(500, f"Failed to create organisation: {exc}", event)

    print(f"[CREATE-ORG] created org_id={org_id} name={org_name!r} type={org_type} by={caller_sub}")

    return {
        "statusCode": 201,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({
            "orgId":   org_id,
            "orgName": org_name,
            "orgType": org_type,
            "status":  "ACTIVE",
        }),
    }


def _slugify(name: str) -> str:
    """
    Convert a human-readable org name to a lowercase, URL-safe slug.

    Replaces any sequence of non-alphanumeric characters with a hyphen,
    strips leading/trailing hyphens, and caps the result at 40 characters.

    Args:
        name: Raw org name string (e.g. "Acme Corp & Partners").

    Returns:
        Slug string (e.g. "acme-corp-partners"), max 40 chars.
    """
    s: str = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:40]


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
        status_code: HTTP status code (400, 403, 409, 500, etc.).
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
