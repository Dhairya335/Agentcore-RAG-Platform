"""
Admin endpoint: POST /admin/orgs/{orgId}/invites

Creates a single-use, email-bound invite for an external user to join a
specific client org. Only INTERNAL users may call this endpoint.

Flow:
  1. Validate caller is INTERNAL via cognito:groups claim
  2. Validate org exists in orgs table and is ACTIVE
  3. Generate cryptographically random invite token (256-bit entropy)
  4. Store SHA-256(token) in fast_org_invites — never the raw token
  5. Return raw token ONCE (never stored, never retrievable again)
  6. Caller is responsible for sharing the invite URL with the recipient

Request body:
    { "invitedEmail": "user@acme.com" }

Path param:
    orgId — target org_id (must exist and be ACTIVE)

Response (201):
    {
      "inviteId":    "<uuid>",
      "inviteUrl":   "https://app.example.com/signup?invite=<raw_token>",
      "inviteToken": "<raw_token>",
      "expiresAt":   "2026-03-26T...",
      "orgId":       "acme-abc123",
      "invitedEmail": "user@acme.com"
    }

Security:
  - Raw token: 32 bytes from os.urandom, hex-encoded (64 chars) — 256-bit entropy
  - Stored: SHA-256(raw_token) only — token is mathematically unrecoverable from hash
  - Single-use: status=PENDING → CONSUMED by complete-registration endpoint
  - Email-bound: complete-registration enforces invitedEmail == authenticated user email
  - TTL: 7 days, enforced by DynamoDB TTL + explicit expiry check at consume time
"""

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timezone, timedelta

import boto3

dynamodb             = boto3.resource("dynamodb")
ORGS_TABLE_NAME      = os.environ["ORGS_TABLE_NAME"]
INVITES_TABLE_NAME   = os.environ["INVITES_TABLE_NAME"]
FRONTEND_URL         = os.environ["FRONTEND_URL"]          # required; no silent default
CORS_ALLOWED_ORIGINS = os.environ["CORS_ALLOWED_ORIGINS"]  # required; no silent default
cors_origins         = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

INVITE_TTL_DAYS: int = 7


def handler(event: dict, context: object) -> dict:
    """
    Handle POST /admin/orgs/{orgId}/invites.

    Validates the caller is INTERNAL, validates the target org exists and is
    ACTIVE, generates a cryptographically secure invite token, stores only the
    SHA-256 hash in DynamoDB, and returns the raw token once.

    Args:
        event:   API Gateway Lambda proxy event. Claims at
                 event['requestContext']['authorizer']['claims'].
                 Path param orgId at event['pathParameters']['orgId'].
                 JSON body with 'invitedEmail' (required).
        context: Lambda context object (unused).

    Returns:
        API Gateway response dict. 201 with invite details on success.
        400/403/404/500 on errors.
    """
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims", {})

    raw_groups = claims.get("cognito:groups", "")
    if not isinstance(raw_groups, str):
        return _error(400, f"Unexpected type for cognito:groups claim: {type(raw_groups)}", event)
    groups: list[str] = [g.strip() for g in raw_groups.split(",") if g.strip()]

    if "internal" not in groups:
        return _error(403, "Forbidden: only INTERNAL users may create invites", event)

    caller_sub: str = claims.get("sub", "")
    if not caller_sub:
        return _error(401, "Missing sub claim in token", event)

    org_id: str = ((event.get("pathParameters") or {}).get("orgId") or "").strip()
    if not org_id:
        return _error(400, "orgId path parameter is required", event)

    try:
        body: dict = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError as exc:
        return _error(400, f"Invalid JSON body: {exc}", event)

    invited_email: str = (body.get("invitedEmail") or "").lower().strip()
    if not invited_email or "@" not in invited_email:
        return _error(400, "invitedEmail is required and must be a valid email address", event)

    # ── Validate org exists and is ACTIVE    ─────
    orgs_table = dynamodb.Table(ORGS_TABLE_NAME)
    try:
        org_resp = orgs_table.get_item(
            Key={"org_id": org_id},
            ConsistentRead=True,
        )
    except Exception as exc:
        print(f"[CREATE-INVITE] DynamoDB GetItem failed for org_id={org_id}: {exc}")
        return _error(500, f"Failed to validate organisation: {exc}", event)

    org: dict | None = org_resp.get("Item")
    if not org:
        return _error(404, f"Organisation not found: {org_id}", event)
    if org.get("status") != "ACTIVE":
        return _error(400, f"Organisation '{org_id}' is not ACTIVE (status={org.get('status')})", event)

    # ── Generate invite token     
    # 32 bytes = 256-bit entropy. Only the SHA-256 hash is stored; the raw
    # token is returned once and never persisted anywhere.
    raw_token:  str = secrets.token_hex(32)
    token_hash: str = hashlib.sha256(raw_token.encode()).hexdigest()
    invite_id:  str = str(uuid.uuid4())

    now:           datetime = datetime.now(timezone.utc)
    expires_at:    datetime = now + timedelta(days=INVITE_TTL_DAYS)
    expires_epoch: int      = int(expires_at.timestamp())   # DynamoDB TTL uses Unix epoch seconds

    # ── Write invite record (hash only)    ──────
    invites_table = dynamodb.Table(INVITES_TABLE_NAME)
    try:
        invites_table.put_item(
            Item={
                "token_hash":    token_hash,
                "invite_id":     invite_id,
                "org_id":        org_id,
                "invited_email": invited_email,
                "role_class":    "EXTERNAL",
                "status":        "PENDING",
                "expires_at":    expires_epoch,             # epoch seconds for DynamoDB TTL
                "created_at":    now.isoformat(),
                "created_by":    caller_sub,
            },
            ConditionExpression="attribute_not_exists(token_hash)",  # hash collision guard
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        # SHA-256 collision: astronomically unlikely (2^-256) but must be handled loudly
        return _error(500, "Token hash collision — please retry the request", event)
    except Exception as exc:
        print(f"[CREATE-INVITE] DynamoDB PutItem failed for org_id={org_id}: {exc}")
        return _error(500, f"Failed to create invite: {exc}", event)

    invite_url: str = f"{FRONTEND_URL}/signup?invite={raw_token}"

    print(
        f"[CREATE-INVITE] invite_id={invite_id} email={invited_email} "
        f"org={org_id} expires={expires_at.isoformat()} by={caller_sub}"
    )

    return {
        "statusCode": 201,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({
            "inviteId":     invite_id,
            "inviteUrl":    invite_url,
            "inviteToken":  raw_token,      # returned ONCE — admin must copy or share
            "expiresAt":    expires_at.isoformat(),
            "orgId":        org_id,
            "invitedEmail": invited_email,
        }),
    }


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
        status_code: HTTP status code (400, 403, 404, 500, etc.).
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
