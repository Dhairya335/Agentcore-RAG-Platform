"""
Principal Resolution Module — FAST org-level tenancy.

This is the single authoritative module for resolving who a user is and
which organisation they belong to.  Every request-time code path that
needs org context MUST go through here — never derive org_id from the JWT
sub directly.

Architecture:
  Authentication  = Cognito (JWT)
  Org membership  = DynamoDB fast_user_memberships table (application data)

resolve_principal() returns a PrincipalContext with:
  user_id          — Cognito sub (immutable identity)
  email            — normalised lowercase email
  role_class       — INTERNAL | EXTERNAL
  org_id           — stable org identifier (e.g. "org-internal", "client-acme")
  membership_status — must be ACTIVE; fails closed otherwise

Security rules:
  - org_id is NEVER derived from JWT sub, email domain, or Cognito group name
  - Fail closed: missing or non-ACTIVE membership raises MembershipError
  - Strongly consistent DynamoDB read for membership (authorization-critical)
  - All callers treat the returned PrincipalContext as immutable
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import boto3
import jwt
from bedrock_agentcore.runtime import RequestContext

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

INTERNAL_GROUP        = "internal"
ROLE_INTERNAL         = "INTERNAL"
ROLE_EXTERNAL         = "EXTERNAL"
MEMBERSHIPS_TABLE_ENV = "MEMBERSHIPS_TABLE_NAME"

# Fixed org_id for the vendor / platform team.
# All INTERNAL users belong to this org.
ORG_INTERNAL = "org-internal"


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PrincipalContext:
    user_id:           str
    email:             str
    role_class:        str   # "INTERNAL" | "EXTERNAL"
    org_id:            str
    membership_status: str   # "ACTIVE" (only value returned on success)


class MembershipError(Exception):
    """
    Raised when a user's org membership cannot be resolved.
    Callers must treat this as a hard authorization failure (HTTP 403).
    """


# ─── DynamoDB client (module-level, reused across warm invocations) ────────────

_dynamodb = boto3.resource("dynamodb")


# ─── Public API ───────────────────────────────────────────────────────────────

def resolve_principal_from_context(context: RequestContext) -> PrincipalContext:
    """
    Resolve a PrincipalContext from an AgentCore RequestContext.

    Used by basic_agent.py (and any future agent entry points).
    The AgentCore Runtime has already validated the JWT — we decode
    without signature verification as per existing auth.py pattern.

    Raises:
        ValueError: Authorization header missing.
        MembershipError: Membership missing, non-ACTIVE, or inconsistent.
    """
    request_headers = context.request_headers
    if not request_headers:
        raise ValueError("No request headers in context")

    auth_header = request_headers.get("Authorization", "")
    if not auth_header:
        raise ValueError("No Authorization header in context")

    token = auth_header.removeprefix("Bearer ")
    claims = jwt.decode(
        jwt=token,
        options={"verify_signature": False},
        algorithms=["RS256"],
    )

    return _resolve_from_claims(claims)


def resolve_principal_from_claims(claims: dict[str, Any]) -> PrincipalContext:
    """
    Resolve a PrincipalContext from a decoded JWT claims dict.

    Used by Lambda handlers that already have decoded claims (e.g. presign-upload,
    admin endpoints) where the JWT was validated upstream by API Gateway.

    Raises:
        MembershipError: Membership missing, non-ACTIVE, or inconsistent.
    """
    return _resolve_from_claims(claims)


# ─── Internal resolution logic ─────────────────────────────────────────────────

def _resolve_from_claims(claims: dict[str, Any]) -> PrincipalContext:
    user_id = claims.get("sub", "").strip()
    if not user_id:
        raise MembershipError("JWT has no sub claim")

    email = (claims.get("email") or "").lower().strip()

    groups: list[str] = claims.get("cognito:groups", []) or []
    role_class = ROLE_INTERNAL if INTERNAL_GROUP in groups else ROLE_EXTERNAL

    # Look up org membership from DynamoDB (strongly consistent read)
    membership = _get_membership(user_id)

    if not membership:
        raise MembershipError(
            f"[AUTH] No membership record found for user={user_id} role={role_class}. "
            "User must complete registration before accessing the system."
        )

    status = membership.get("membership_status", "")
    if status != "ACTIVE":
        raise MembershipError(
            f"[AUTH] Membership not ACTIVE for user={user_id} status={status}. "
            "Access denied."
        )

    org_id = membership.get("org_id", "").strip()
    if not org_id:
        raise MembershipError(
            f"[AUTH] Membership record for user={user_id} has empty org_id. "
            "Data integrity error."
        )

    # Consistency check: role in membership must match role from JWT group
    stored_role = membership.get("role_class", "")
    if stored_role and stored_role != role_class:
        logger.warning(
            "[AUTH] Role mismatch: JWT groups say %s but membership table says %s "
            "for user=%s. Using JWT groups (Cognito is authoritative for role_class).",
            role_class, stored_role, user_id,
        )

    principal = PrincipalContext(
        user_id=user_id,
        email=email,
        role_class=role_class,
        org_id=org_id,
        membership_status=status,
    )
    logger.info(
        "[AUTH] resolved principal user=%s org=%s role=%s status=%s",
        user_id, org_id, role_class, status,
    )
    return principal


def _get_membership(user_id: str) -> dict | None:
    """
    Strongly consistent GetItem from fast_user_memberships.

    Returns None if the item does not exist.
    Raises MembershipError on DynamoDB error.
    """
    table_name = os.environ.get(MEMBERSHIPS_TABLE_ENV, "")
    if not table_name:
        raise MembershipError(
            f"Environment variable {MEMBERSHIPS_TABLE_ENV} is not set. "
            "Cannot resolve org membership."
        )

    table = _dynamodb.Table(table_name)
    try:
        resp = table.get_item(
            Key={"user_sub": user_id},
            ConsistentRead=True,   # authorization-critical — must be strongly consistent
        )
    except Exception as e:
        logger.error("[AUTH] DynamoDB GetItem failed for user=%s: %s", user_id, e)
        raise MembershipError(
            f"Failed to read membership for user={user_id}: {e}"
        ) from e

    return resp.get("Item")
