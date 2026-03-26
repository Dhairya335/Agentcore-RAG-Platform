"""
Pre-Signup Lambda Trigger — Cognito User Pool

Fires synchronously before a new Cognito account is created (self-registration flow).
If this Lambda raises an exception, Cognito blocks the sign-up and shows the user
an error. If it returns the event unchanged, sign-up proceeds.

Purpose:
  Enforce invite-only sign-up for external users. Only a registering email that
  has at least one valid PENDING, unexpired invite record in the invites table
  is allowed to create a Cognito account.

Behaviour by case:
  - Valid PENDING invite for this email   → allow sign-up (return event)
  - No invite record for this email       → block sign-up (raise Exception)
  - Invite exists but CONSUMED            → block sign-up (raise Exception)
  - Invite exists but expired             → block sign-up (raise Exception)
  - Multiple invites: any one valid       → allow sign-up (return event)
  - DynamoDB error                        → block sign-up (fail closed, raise Exception)
  - SSM read error                        → block sign-up (fail closed, raise Exception)
  - Internal user tries to self-register  → block (no invite exists for them — correct)

Trigger type: PreSignUp_SignUp (self-registration only).
AdminCreateUser flows (used for internal/admin CDK-created users) do NOT fire
PreSignUp_SignUp — they fire PreSignUp_AdminCreateUser, which we skip immediately.

Environment variables (required — missing value crashes at startup):
  INVITES_TABLE_SSM_PARAM: SSM parameter path that holds the DynamoDB invites table name.
                           Written by BackendStack at deploy time.
                           Path: /{stack_name_base}/rag/invites-table-name
                           We read via SSM at cold-start to avoid a circular CDK
                           nested-stack dependency (CognitoStack ↔ BackendStack).
"""

import os
import time
import boto3
from boto3.dynamodb.conditions import Key

dynamodb  = boto3.resource("dynamodb")
ssm_client = boto3.client("ssm")

# Required — crashes at cold start if missing (fail loud, no default)
INVITES_TABLE_SSM_PARAM: str = os.environ["INVITES_TABLE_SSM_PARAM"]

# GSI name on the invites table that indexes by invited_email
INVITED_EMAIL_INDEX = "invited_email-index"


def _get_invites_table_name() -> str:
    """
    Fetch the DynamoDB invites table name from SSM Parameter Store.

    Called once at cold-start. Raises if the parameter does not exist or
    SSM is unreachable — fail closed so sign-up is blocked on infrastructure error.

    Returns:
        str: The DynamoDB invites table name.

    Raises:
        Exception: If SSM GetParameter fails for any reason.
    """
    try:
        response = ssm_client.get_parameter(Name=INVITES_TABLE_SSM_PARAM)
        return response["Parameter"]["Value"]
    except Exception as exc:
        # Fail closed — if we can't read the table name, block all sign-ups
        raise Exception(
            f"[PRE-SIGNUP] Cannot read invites table name from SSM "
            f"param={INVITES_TABLE_SSM_PARAM}: {exc}"
        )


# Read at cold-start — SSM is cached for the lifetime of this Lambda container.
# If SSM is unavailable, the Lambda crashes and Cognito blocks the sign-up (fail closed).
INVITES_TABLE_NAME: str = _get_invites_table_name()


def handler(event: dict, context: object) -> dict:
    """
    Cognito PreSignUp trigger handler.

    Called synchronously by Cognito before creating a new user account.
    Raises Exception to block sign-up; returns event to allow it.

    Args:
        event   (dict): Cognito trigger event. Key fields:
                          event["triggerSource"]                       — trigger type string
                          event["request"]["userAttributes"]["email"]  — registering email
        context (object): Lambda context (unused)

    Returns:
        dict: The original event unchanged (signals Cognito to allow sign-up)

    Raises:
        Exception: With a user-visible message when sign-up should be blocked.
    """
    trigger_source: str = event.get("triggerSource", "")
    print(f"[PRE-SIGNUP] triggerSource={trigger_source}")

    # Only gate self-registration. AdminCreateUser flows (used for internal/CDK users)
    # fire PreSignUp_AdminCreateUser — skip them immediately so internal users are
    # never blocked by this invite check.
    if trigger_source != "PreSignUp_SignUp":
        print(f"[PRE-SIGNUP] Skipping — triggerSource is not PreSignUp_SignUp")
        return event

    email: str = (
        event.get("request", {})
             .get("userAttributes", {})
             .get("email", "")
    ).lower().strip()

    if not email:
        print("[PRE-SIGNUP] Blocked — no email in userAttributes")
        raise Exception(
            "Sign-up requires a valid email address."
        )

    print(f"[PRE-SIGNUP] Checking invite for email={email}")

    _assert_valid_invite_exists(email=email)

    print(f"[PRE-SIGNUP] Allowed — valid invite found for email={email}")
    return event


def _assert_valid_invite_exists(email: str) -> None:
    """
    Query the invites table by email and assert that at least one valid
    PENDING, unexpired invite exists for this email address.

    Raises Exception (blocking sign-up) if:
      - DynamoDB query fails (fail closed — never allow on error)
      - No invite records exist for this email
      - All invite records are CONSUMED or expired

    Args:
        email (str): Lowercase normalised email address of the registering user.

    Returns:
        None — returns normally if a valid invite exists.

    Raises:
        Exception: With a user-visible error message if sign-up should be blocked.
    """
    table = dynamodb.Table(INVITES_TABLE_NAME)

    try:
        response = table.query(
            IndexName=INVITED_EMAIL_INDEX,
            KeyConditionExpression=Key("invited_email").eq(email),
        )
    except Exception as exc:
        # DynamoDB failure — fail closed. Never allow sign-up when we can't verify.
        print(f"[PRE-SIGNUP] DynamoDB query failed for email={email}: {exc}")
        raise Exception(
            "Unable to verify your invite at this time. Please try again or contact your administrator."
        )

    items: list[dict] = response.get("Items", [])

    if not items:
        print(f"[PRE-SIGNUP] Blocked — no invite records found for email={email}")
        raise Exception(
            "Sign-up is by invitation only. "
            "Please contact your administrator to request access."
        )

    now_epoch: int = int(time.time())

    # Check each invite — allow if ANY one is PENDING and not expired
    for invite in items:
        status: str     = invite.get("status", "")
        expires_at: int = int(invite.get("expires_at", 0))
        org_id: str     = invite.get("org_id", "unknown")

        if status == "CONSUMED":
            print(f"[PRE-SIGNUP] Invite for email={email} org={org_id} is CONSUMED — skipping")
            continue

        if now_epoch > expires_at:
            print(f"[PRE-SIGNUP] Invite for email={email} org={org_id} is expired — skipping")
            continue

        if status == "PENDING":
            print(f"[PRE-SIGNUP] Valid PENDING invite found for email={email} org={org_id}")
            return

        # Unknown status — treat as invalid, log for visibility
        print(f"[PRE-SIGNUP] Unknown invite status={status} for email={email} org={org_id} — skipping")

    # All invite records were either consumed or expired
    print(f"[PRE-SIGNUP] Blocked — all invites for email={email} are consumed or expired")
    raise Exception(
        "Your invite has expired or has already been used. "
        "Please contact your administrator to request a new invite."
    )
