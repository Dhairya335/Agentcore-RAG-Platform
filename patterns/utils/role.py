"""
Role extraction utility for FAST RAG + AgentCore.

Reads the user's role from their Cognito JWT claims and returns a normalised
role string ("INTERNAL" or "EXTERNAL").

Role model:
  - INTERNAL: Company A (vendor/owner). Full access to document library,
              collections, chunk previews, source viewer, debug panels.
  - EXTERNAL: Company B (client). Chat-only. No access to knowledge structure.

Source of truth: Cognito Groups (cognito:groups claim in the ID token).
  - "internal" group  → INTERNAL
  - "external" group  → EXTERNAL
  - neither group     → EXTERNAL (fail-safe default)

Usage in Lambda handlers:
    from utils.role import get_user_role
    role = get_user_role(claims)          # claims = decoded JWT dict
    if role == "EXTERNAL":
        return _forbidden()

Usage in basic_agent.py (via RequestContext):
    from utils.role import get_user_role_from_context
    role = get_user_role_from_context(context)
"""

from __future__ import annotations

import logging
from typing import Any

import jwt
from bedrock_agentcore.runtime import RequestContext

logger = logging.getLogger(__name__)

# Group names — must match what is created in Cognito CDK stack
INTERNAL_GROUP = "internal"
EXTERNAL_GROUP = "external"

ROLE_INTERNAL = "INTERNAL"
ROLE_EXTERNAL = "EXTERNAL"


def get_user_role(claims: dict[str, Any]) -> str:
    """
    Derive role from decoded JWT claims dict.

    Args:
        claims: Decoded JWT payload (dict). Must have been validated upstream.
                The AgentCore Runtime validates before calling the agent.
                API Gateway Cognito Authorizer validates before calling Lambdas.

    Returns:
        "INTERNAL" if the user is in the 'internal' Cognito group.
        "EXTERNAL" for all other cases (external group or no group).

    Note:
        The cognito:groups claim is a list of group names, e.g.:
            ["internal", "some-other-group"]
        If the user has no groups, the claim is absent (not an empty list).
        Always default to EXTERNAL — fail-safe, not fail-open.
    """
    groups: list[str] = claims.get("cognito:groups", []) or []

    if INTERNAL_GROUP in groups:
        logger.debug("User role resolved: INTERNAL (groups=%s)", groups)
        return ROLE_INTERNAL

    logger.debug("User role resolved: EXTERNAL (groups=%s)", groups)
    return ROLE_EXTERNAL


def get_user_role_from_context(context: RequestContext) -> str:
    """
    Extract role from AgentCore RequestContext (used in basic_agent.py).

    The AgentCore Runtime passes the validated JWT in the Authorization header.
    We decode without signature verification because the Runtime has already
    validated the token — same pattern as extract_user_id_from_context in auth.py.

    Args:
        context: AgentCore RequestContext with request_headers.

    Returns:
        "INTERNAL" or "EXTERNAL".

    Raises:
        ValueError: If Authorization header is missing.
    """
    request_headers = context.request_headers
    if not request_headers:
        raise ValueError(
            "No request headers found in context. "
            "Ensure the Runtime is configured with Authorization header allowlist."
        )

    auth_header = request_headers.get("Authorization", "")
    if not auth_header:
        raise ValueError("No Authorization header in request context.")

    token = (
        auth_header.replace("Bearer ", "")
        if auth_header.startswith("Bearer ")
        else auth_header
    )

    claims = jwt.decode(
        jwt=token,
        options={"verify_signature": False},
        algorithms=["RS256"],
    )

    return get_user_role(claims)


def get_user_role_from_token_string(id_token: str) -> str:
    """
    Extract role from a raw JWT string.
    Used by Lambda handlers that receive the token directly (e.g. from an
    API Gateway context or test invocations).

    Args:
        id_token: Raw JWT string (with or without "Bearer " prefix).

    Returns:
        "INTERNAL" or "EXTERNAL".
    """
    token = (
        id_token.replace("Bearer ", "")
        if id_token.startswith("Bearer ")
        else id_token
    )

    claims = jwt.decode(
        jwt=token,
        options={"verify_signature": False},
        algorithms=["RS256"],
    )

    return get_user_role(claims)
