"""
Admin endpoint: POST /admin/orgs

Creates a new client org record in fast_orgs.
Only INTERNAL users may call this endpoint (enforced by API Gateway Cognito
authorizer + role check in handler).

Request body:
    { "orgName": "Acme Corp", "orgType": "CLIENT" }

Response:
    { "orgId": "client-acme-corp", "orgName": "Acme Corp", "orgType": "CLIENT" }

Security:
  - JWT validated by API Gateway Cognito authorizer before this Lambda runs
  - Role check: caller must be in Cognito 'internal' group
  - org_id is generated server-side (slugified orgName + random suffix)
  - Client cannot choose org_id
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")

ORGS_TABLE_NAME   = os.environ["ORGS_TABLE_NAME"]
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
cors_origins = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

VALID_ORG_TYPES = {"CLIENT", "INTERNAL_VENDOR", "PERSONAL_FUTURE"}


def handler(event, context):
    # Role guard: only INTERNAL group users may create orgs
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims", {})
    groups = claims.get("cognito:groups", "") or ""
    if isinstance(groups, str):
        groups = groups.split(",")
    if "internal" not in groups:
        return _error(403, "Forbidden: only INTERNAL users may create organisations", event)

    caller_sub = claims.get("sub", "system")

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return _error(400, "Invalid JSON body", event)

    org_name = (body.get("orgName") or "").strip()
    org_type  = (body.get("orgType") or "CLIENT").strip().upper()

    if not org_name:
        return _error(400, "orgName is required", event)
    if org_type not in VALID_ORG_TYPES:
        return _error(400, f"orgType must be one of {sorted(VALID_ORG_TYPES)}", event)

    # Generate a stable, URL-safe org_id: slug + 6-char UUID suffix
    slug    = _slugify(org_name)
    suffix  = str(uuid.uuid4()).replace("-", "")[:6]
    org_id  = f"{slug}-{suffix}"

    now = datetime.now(timezone.utc).isoformat()

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
            ConditionExpression="attribute_not_exists(org_id)",  # prevent accidental overwrite
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return _error(409, f"org_id collision: {org_id} already exists", event)
    except Exception as e:
        return _error(500, f"Failed to create org: {e}", event)

    print(f"[ORG] created org_id={org_id} name={org_name} type={org_type} by={caller_sub}")

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
    """Convert org name to a lowercase URL-safe slug."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:40]  # cap length


def _cors_origin(event):
    request_origin = (event.get("headers") or {}).get("origin", "")
    return request_origin if request_origin in cors_origins else (cors_origins[0] if cors_origins else "*")


def _error(status_code, message, event={}):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({"error": message}),
    }
