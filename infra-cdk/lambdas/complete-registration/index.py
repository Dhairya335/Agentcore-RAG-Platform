"""
External user registration completion: POST /auth/complete-external-registration

Called by the frontend after a self-registered external user logs in for the
first time.  The user arrives with an invite token from the signup URL.

Flow:
  1. Validate JWT (API Gateway Cognito authorizer has done this — we extract claims)
  2. Parse raw invite token from request body
  3. Hash it with SHA-256 and look up fast_org_invites
  4. Validate: status=PENDING, not expired, email matches authenticated user
  5. Conditionally write fast_user_memberships (user_sub → org_id, role_class=EXTERNAL)
  6. Conditionally mark invite CONSUMED (prevents reuse)
  7. Ensure user is in Cognito 'external' group (idempotent)
  8. Return { registrationStatus, orgId, tokenRefreshRequired }

Security rules:
  - org_id comes from the invite record, never from the client
  - email comparison: normalize both sides to lowercase
  - Conditional writes prevent race conditions (duplicate subs, duplicate invite use)
  - Existing ACTIVE membership with SAME org_id → idempotent success (re-registration safe)
  - Existing ACTIVE membership with DIFFERENT org_id → reject (security event logged)
  - invite already CONSUMED → reject with clear error
"""

import hashlib
import json
import os
from datetime import datetime, timezone

import boto3

dynamodb  = boto3.resource("dynamodb")
cognito   = boto3.client("cognito-idp")

MEMBERSHIPS_TABLE_NAME = os.environ["MEMBERSHIPS_TABLE_NAME"]
INVITES_TABLE_NAME     = os.environ["INVITES_TABLE_NAME"]
ORGS_TABLE_NAME        = os.environ["ORGS_TABLE_NAME"]
USER_POOL_ID           = os.environ["USER_POOL_ID"]
# EXTERNAL_GROUP is stable Cognito group name — "external" is the only valid value
# in this deployment. Hard-coded rather than configurable to prevent misconfiguration.
EXTERNAL_GROUP         = "external"
CORS_ALLOWED_ORIGINS   = os.environ["CORS_ALLOWED_ORIGINS"]  # required; no silent default
cors_origins           = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]


def handler(event, context):
    # Extract validated JWT claims from API Gateway authorizer
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims", {})
    user_sub   = (claims.get("sub") or "").strip()
    user_email = (claims.get("email") or "").lower().strip()

    if not user_sub:
        return _error(401, "Missing sub claim in token", event)

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return _error(400, "Invalid JSON body", event)

    raw_token = (body.get("inviteToken") or "").strip()
    if not raw_token:
        return _error(400, "inviteToken is required", event)

    # Hash the token and look up the invite record
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    invites_table     = dynamodb.Table(INVITES_TABLE_NAME)
    memberships_table = dynamodb.Table(MEMBERSHIPS_TABLE_NAME)

    try:
        invite_resp = invites_table.get_item(
            Key={"token_hash": token_hash},
            ConsistentRead=True,
        )
    except Exception as e:
        return _error(500, f"Failed to read invite: {e}", event)

    invite = invite_resp.get("Item")
    if not invite:
        return _error(400, "Invite not found or already used", event)

    # Validate invite status
    if invite.get("status") != "PENDING":
        status = invite.get("status")
        print(f"[REG] Invite already {status} for user={user_sub}")
        return _error(400, f"Invite is no longer valid (status={status})", event)

    # Validate expiry (belt-and-suspenders alongside DynamoDB TTL)
    expires_at = invite.get("expires_at", 0)
    now_epoch  = int(datetime.now(timezone.utc).timestamp())
    if now_epoch > expires_at:
        return _error(400, "Invite has expired. Please request a new invite.", event)

    # Validate email match
    invited_email = (invite.get("invited_email") or "").lower().strip()
    if invited_email != user_email:
        print(
            f"[REG][SECURITY] Email mismatch: invite={invited_email} user={user_email} "
            f"sub={user_sub}"
        )
        return _error(403, "Invite was issued for a different email address", event)

    org_id     = invite["org_id"]
    role_class = invite.get("role_class", "EXTERNAL")
    now_iso    = datetime.now(timezone.utc).isoformat()

    # Check for existing membership (idempotency + conflict detection)
    try:
        existing_resp = memberships_table.get_item(
            Key={"user_sub": user_sub},
            ConsistentRead=True,
        )
    except Exception as e:
        return _error(500, f"Failed to check existing membership: {e}", event)

    existing = existing_resp.get("Item")
    if existing:
        if existing.get("membership_status") == "ACTIVE" and existing.get("org_id") == org_id:
            # Idempotent: user already registered to the same org
            print(f"[REG] Idempotent re-registration: user={user_sub} org={org_id}")
            return _success(org_id, user_sub, user_email, event)
        else:
            # Different org or non-ACTIVE status — reject with security log
            print(
                f"[REG][SECURITY] Membership conflict for user={user_sub}: "
                f"existing_org={existing.get('org_id')} invite_org={org_id} "
                f"existing_status={existing.get('membership_status')}"
            )
            return _error(
                409,
                "A membership record already exists for this account with a different configuration. "
                "Please contact your administrator.",
                event,
            )

    # Write membership record (conditional — prevent duplicate writes in race conditions)
    try:
        memberships_table.put_item(
            Item={
                "user_sub":          user_sub,
                "email":             user_email,
                "org_id":            org_id,
                "role_class":        role_class,
                "membership_status": "ACTIVE",
                "invited_by":        invite.get("created_by", ""),
                "joined_at":         now_iso,
                "created_at":        now_iso,
                "updated_at":        now_iso,
            },
            # Fail if another process created it since our read above
            ConditionExpression="attribute_not_exists(user_sub)",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        # Lost a race — read again and check if it's the same org (idempotent)
        resp2 = memberships_table.get_item(
            Key={"user_sub": user_sub},
            ConsistentRead=True,
        )
        item2 = resp2.get("Item")
        if item2 and item2.get("org_id") == org_id and item2.get("membership_status") == "ACTIVE":
            print(f"[REG] Race idempotent for user={user_sub} org={org_id}")
            return _success(org_id, user_sub, user_email, event)
        return _error(409, "Concurrent registration conflict — please retry", event)
    except Exception as e:
        return _error(500, f"Failed to write membership: {e}", event)

    # Mark invite CONSUMED — conditional to prevent the same invite being used twice
    try:
        invites_table.update_item(
            Key={"token_hash": token_hash},
            UpdateExpression="SET #s = :consumed, consumed_at = :now, consumed_by_sub = :sub",
            ConditionExpression="#s = :pending",  # only if still PENDING
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":consumed": "CONSUMED",
                ":pending":  "PENDING",
                ":now":      now_iso,
                ":sub":      user_sub,
            },
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        # Another process consumed it — membership was already written above, log only
        print(f"[REG] Invite already consumed by concurrent request for user={user_sub}")
    except Exception as e:
        # Membership written but invite not marked consumed — log for manual remediation
        print(f"[REG][WARN] Failed to mark invite consumed for user={user_sub}: {e}")

    # Ensure user is in Cognito external group (idempotent)
    try:
        cognito.admin_add_user_to_group(
            UserPoolId=USER_POOL_ID,
            Username=user_sub,
            GroupName=EXTERNAL_GROUP,
        )
    except cognito.exceptions.UserNotFoundException:
        print(f"[REG][WARN] User not found in Cognito for sub={user_sub}")
    except Exception as e:
        # Not fatal — JWT already contains groups from PostConfirmation trigger
        print(f"[REG][WARN] Failed to add user to Cognito group: {e}")

    print(
        f"[REG] Registration complete: user={user_sub} email={user_email} "
        f"org={org_id} role={role_class}"
    )

    # Fetch org_name for the response — required field, not a fallback
    org_name = _fetch_org_name(org_id=org_id)
    if org_name is None:
        # Membership was written successfully, but org record lookup failed.
        # Return 500 so the frontend knows the response is incomplete.
        print(f"[REG] Org record not found after successful membership write: org_id={org_id}")
        return _error(
            500,
            f"Registration succeeded but organisation '{org_id}' record could not be retrieved — "
            "contact administrator.",
            event,
        )

    return _success(
        org_id=org_id,
        org_name=org_name,
        user_sub=user_sub,
        user_email=user_email,
        event=event,
    )


def _fetch_org_name(org_id: str) -> str | None:
    """
    Fetch the org_name field from the orgs table for the given org_id.

    Args:
        org_id: The organisation identifier to look up.

    Returns:
        The org_name string if the record exists and contains the field,
        or None if the record is not found or the DynamoDB call fails.
        Callers must treat None as a hard error and return HTTP 500.
    """
    orgs_table = dynamodb.Table(ORGS_TABLE_NAME)
    try:
        resp = orgs_table.get_item(
            Key={"org_id": org_id},
            ConsistentRead=True,
        )
    except Exception as exc:
        print(f"[REG] DynamoDB GetItem failed for org_id={org_id}: {exc}")
        return None

    item = resp.get("Item")
    if not item:
        return None
    return item.get("org_name")  # None if field is unexpectedly absent


def _success(org_id: str, org_name: str, user_sub: str, user_email: str, event: dict) -> dict:
    """
    Build a successful (200) registration response.

    Args:
        org_id:     The organisation ID the user was registered into.
        org_name:   The human-readable organisation name.
        user_sub:   Cognito user sub (userId).
        user_email: Authenticated user's email address.
        event:      Original Lambda event (used to resolve CORS origin).

    Returns:
        API Gateway response dict with statusCode=200.
    """
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({
            "registrationStatus":   "COMPLETE",
            "orgId":                org_id,
            "orgName":              org_name,
            "userId":               user_sub,
            "email":                user_email,
            "tokenRefreshRequired": True,   # caller must refresh Cognito tokens for new group claim
        }),
    }


def _cors_origin(event: dict) -> str:
    """
    Resolve the CORS Access-Control-Allow-Origin response header value.

    Args:
        event: API Gateway Lambda proxy event.

    Returns:
        Allowed origin string. Raises IndexError if cors_origins is empty.
    """
    request_origin: str = (event.get("headers") or {}).get("origin", "")
    if request_origin in cors_origins:
        return request_origin
    return cors_origins[0]


def _error(status_code, message, event={}):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({"error": message}),
    }
