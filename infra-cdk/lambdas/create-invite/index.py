"""
Admin endpoint: POST /admin/orgs/{orgId}/invites

Creates a single-use, email-bound invite for an external user to join a
specific client org.

Only INTERNAL users may call this endpoint.

Flow:
  1. Validate caller is INTERNAL
  2. Validate org exists and is ACTIVE
  3. Generate cryptographically random invite token
  4. Store SHA-256(token) in fast_org_invites — never the raw token
  5. Return raw token ONCE (never stored, never retrievable again)
  6. Caller is responsible for emailing the invite URL to the recipient

Request body:
    { "invitedEmail": "user@acme.com" }

Path param:
    orgId — the target org

Response:
    {
      "inviteUrl": "https://app.example.com/signup?invite=<raw_token>",
      "inviteToken": "<raw_token>",   <- for admin to copy/email
      "expiresAt": "2026-03-26T..."
    }

Security:
  - Raw token: 32 bytes from os.urandom, hex-encoded (64 chars) — 256-bit entropy
  - Stored: SHA-256(raw_token) only
  - Single-use: status=PENDING, consumed by complete-registration endpoint
  - Email-bound: v1 enforces invitedEmail == authenticated email at consume time
  - TTL: 7 days, enforced by both DynamoDB TTL and explicit status check
"""

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone, timedelta

import boto3

dynamodb = boto3.resource("dynamodb")

ORGS_TABLE_NAME       = os.environ["ORGS_TABLE_NAME"]
INVITES_TABLE_NAME    = os.environ["INVITES_TABLE_NAME"]
FRONTEND_URL          = os.environ.get("FRONTEND_URL", "https://example.com")
CORS_ALLOWED_ORIGINS  = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
cors_origins = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

INVITE_TTL_DAYS = 7


def handler(event, context):
    # Role guard
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims", {})
    groups = claims.get("cognito:groups", "") or ""
    if isinstance(groups, str):
        groups = groups.split(",")
    if "internal" not in groups:
        return _error(403, "Forbidden: only INTERNAL users may create invites", event)

    caller_sub = claims.get("sub", "system")

    # Path parameter
    org_id = ((event.get("pathParameters") or {}).get("orgId") or "").strip()
    if not org_id:
        return _error(400, "orgId path parameter is required", event)

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return _error(400, "Invalid JSON body", event)

    invited_email = (body.get("invitedEmail") or "").lower().strip()
    if not invited_email or "@" not in invited_email:
        return _error(400, "invitedEmail is required and must be a valid email", event)

    # Validate org exists and is ACTIVE
    orgs_table = dynamodb.Table(ORGS_TABLE_NAME)
    try:
        resp = orgs_table.get_item(
            Key={"org_id": org_id},
            ConsistentRead=True,
        )
    except Exception as e:
        return _error(500, f"Failed to read org: {e}", event)

    org = resp.get("Item")
    if not org:
        return _error(404, f"Org not found: {org_id}", event)
    if org.get("status") != "ACTIVE":
        return _error(400, f"Org {org_id} is not ACTIVE", event)

    # Generate invite token — 32 bytes (256-bit) raw, hex-encoded
    raw_token   = secrets.token_hex(32)
    token_hash  = hashlib.sha256(raw_token.encode()).hexdigest()

    now         = datetime.now(timezone.utc)
    expires_at  = now + timedelta(days=INVITE_TTL_DAYS)
    expires_epoch = int(expires_at.timestamp())  # DynamoDB TTL uses epoch seconds

    # Store ONLY the hash — raw token is never persisted
    invites_table = dynamodb.Table(INVITES_TABLE_NAME)
    try:
        invites_table.put_item(
            Item={
                "token_hash":    token_hash,
                "org_id":        org_id,
                "invited_email": invited_email,
                "role_class":    "EXTERNAL",
                "status":        "PENDING",
                "expires_at":    expires_epoch,
                "created_at":    now.isoformat(),
                "created_by":    caller_sub,
            },
            ConditionExpression="attribute_not_exists(token_hash)",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        # Hash collision — astronomically unlikely with 256-bit token, but handle it
        return _error(500, "Token hash collision — please retry", event)
    except Exception as e:
        return _error(500, f"Failed to create invite: {e}", event)

    invite_url = f"{FRONTEND_URL}/signup?invite={raw_token}"

    print(
        f"[INVITE] created invite for email={invited_email} org={org_id} "
        f"expires={expires_at.isoformat()} by={caller_sub}"
    )

    return {
        "statusCode": 201,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({
            "inviteUrl":   invite_url,
            "inviteToken": raw_token,   # returned ONCE — admin must copy or email this
            "expiresAt":   expires_at.isoformat(),
            "orgId":       org_id,
            "invitedEmail": invited_email,
        }),
    }


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
