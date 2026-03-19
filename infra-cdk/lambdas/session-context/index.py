"""
Session bootstrap endpoint: GET /auth/session-context

Returns authoritative session state for the frontend to use as the single source
of truth for route guards and capability flags. Called once on app load after auth.

Response shape:
{
  "userId":           "sub-uuid",
  "email":            "user@example.com",
  "roleClass":        "INTERNAL" | "EXTERNAL",
  "membershipStatus": "ACTIVE" | "MISSING",
  "orgId":            "acme-abc123" | null,
  "orgName":          "Acme Corp" | null,
  "capabilities": {
    "canChat":         true,
    "canUpload":       true | false,
    "canViewDocs":     true | false,    // INTERNAL only
    "canManageOrgs":   true | false,    // INTERNAL only
  }
}

Fail-closed design:
  - Missing sub claim → 401
  - DynamoDB read error → 500 (never silently degrades to MISSING)
  - Org record not found for an ACTIVE membership → 500 (data inconsistency, not silent)
  - INTERNAL users always ACTIVE (no membership table lookup needed)
"""

import json
import os

import boto3

dynamodb               = boto3.resource("dynamodb")
MEMBERSHIPS_TABLE_NAME = os.environ["MEMBERSHIPS_TABLE_NAME"]
ORGS_TABLE_NAME        = os.environ["ORGS_TABLE_NAME"]
CORS_ALLOWED_ORIGINS   = os.environ["CORS_ALLOWED_ORIGINS"]  # must be set; no silent default
cors_origins           = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]


def handler(event: dict, context: object) -> dict:
    """
    Handle GET /auth/session-context.

    Reads the Cognito JWT claims injected by the API Gateway authorizer,
    resolves org membership for EXTERNAL users, and returns a session
    context payload that the frontend uses as its single source of truth.

    Args:
        event:   API Gateway Lambda proxy event dict. Claims are under
                 event['requestContext']['authorizer']['claims'].
        context: Lambda context object (unused).

    Returns:
        API Gateway response dict with statusCode, headers, and JSON body.
        200 on success, 401 if claims are missing, 404 if membership is
        ACTIVE but org record is missing (data inconsistency), 500 on
        unexpected DynamoDB errors.
    """
    claims   = (event.get("requestContext") or {}).get("authorizer", {}).get("claims", {})
    user_sub: str = claims.get("sub", "")
    email:    str = claims.get("email", "")

    if not user_sub:
        return _error(401, "Unauthorized: missing sub claim in token", event)

    # Cognito encodes groups as a JSON-list string in the ID token claim.
    # Example: "cognito:groups": "internal" or "cognito:groups": "internal,external"
    # We only accept a string here; any other type means a malformed token.
    raw_groups = claims.get("cognito:groups", "")
    if not isinstance(raw_groups, str):
        return _error(400, f"Unexpected type for cognito:groups claim: {type(raw_groups)}", event)
    groups: list[str] = [g.strip() for g in raw_groups.split(",") if g.strip()]

    role_class: str = "INTERNAL" if "internal" in groups else "EXTERNAL"

    # INTERNAL users are permanently ACTIVE — they belong to the vendor org.
    # No DynamoDB membership record is required for INTERNAL users.
    if role_class == "INTERNAL":
        return _ok(event, {
            "userId":           user_sub,
            "email":            email,
            "roleClass":        "INTERNAL",
            "membershipStatus": "ACTIVE",
            "orgId":            "org-internal",
            "orgName":          "Internal",
            "capabilities": {
                "canChat":       True,
                "canUpload":     True,
                "canViewDocs":   True,
                "canManageOrgs": True,
            },
        })

    # ── EXTERNAL: look up membership record ──────────────────────────────────
    memberships_table = dynamodb.Table(MEMBERSHIPS_TABLE_NAME)
    try:
        mem_resp = memberships_table.get_item(
            Key={"user_sub": user_sub},
            ConsistentRead=True,  # strongly consistent — authoritative for access control
        )
    except Exception as exc:
        # DynamoDB failure is a hard error — never degrade silently to MISSING.
        print(f"[SESSION] DynamoDB GetItem failed for sub={user_sub}: {exc}")
        return _error(500, "Failed to resolve membership — please retry", event)

    item: dict | None = mem_resp.get("Item")

    # No record or non-ACTIVE status → user has not accepted an invite yet
    if not item or item.get("membership_status") != "ACTIVE":
        return _ok(event, {
            "userId":           user_sub,
            "email":            email,
            "roleClass":        "EXTERNAL",
            "membershipStatus": "MISSING",
            "orgId":            None,
            "orgName":          None,
            "capabilities": {
                "canChat":       False,
                "canUpload":     False,
                "canViewDocs":   False,
                "canManageOrgs": False,
            },
        })

    org_id: str = item.get("org_id", "")
    if not org_id:
        # Membership record exists but org_id is empty — data integrity error, not a fallback.
        print(f"[SESSION] ACTIVE membership for sub={user_sub} has empty org_id")
        return _error(500, "Membership record is missing org_id — contact administrator", event)

    # ── Fetch org name (required — not optional) ─────────────────────────────
    orgs_table = dynamodb.Table(ORGS_TABLE_NAME)
    try:
        org_resp = orgs_table.get_item(
            Key={"org_id": org_id},
            ConsistentRead=True,
        )
    except Exception as exc:
        print(f"[SESSION] DynamoDB GetItem failed for org_id={org_id}: {exc}")
        return _error(500, "Failed to fetch organisation record — please retry", event)

    org_item: dict | None = org_resp.get("Item")
    if not org_item:
        # An ACTIVE membership points to a non-existent org — data inconsistency, hard error.
        print(f"[SESSION] Org record not found for org_id={org_id} (membership sub={user_sub})")
        return _error(
            404,
            f"Organisation '{org_id}' not found. Membership may be misconfigured — contact administrator.",
            event,
        )

    org_name: str = org_item["org_name"]  # KeyError if missing → Lambda will 500 with full traceback

    return _ok(event, {
        "userId":           user_sub,
        "email":            email,
        "roleClass":        "EXTERNAL",
        "membershipStatus": "ACTIVE",
        "orgId":            org_id,
        "orgName":          org_name,
        "capabilities": {
            "canChat":       True,
            "canUpload":     True,
            "canViewDocs":   False,
            "canManageOrgs": False,
        },
    })


def _ok(event: dict, body: dict) -> dict:
    """
    Build a successful (200) API Gateway response.

    Args:
        event: Original Lambda event (used to resolve CORS origin).
        body:  Dict to serialize as JSON response body.

    Returns:
        API Gateway response dict with statusCode=200.
    """
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps(body),
    }


def _error(status_code: int, message: str, event: dict) -> dict:
    """
    Build an error API Gateway response.

    Args:
        status_code: HTTP status code (e.g. 401, 404, 500).
        message:     Human-readable error description.
        event:       Original Lambda event (used to resolve CORS origin).

    Returns:
        API Gateway response dict with the given statusCode.
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({"error": message}),
    }


def _cors_origin(event: dict) -> str:
    """
    Resolve the CORS Access-Control-Allow-Origin header value.

    Matches the request Origin header against the allowed origins list.
    Returns the matched origin if found, or the first allowed origin as a
    strict default. Never returns '*' — the allowed list must be configured
    via CORS_ALLOWED_ORIGINS env var.

    Args:
        event: API Gateway Lambda proxy event.

    Returns:
        The allowed origin string to echo back in the response header.
    """
    request_origin: str = (event.get("headers") or {}).get("origin", "")
    if request_origin in cors_origins:
        return request_origin
    return cors_origins[0]  # IndexError if env var is empty → loud failure, not silent wildcard
