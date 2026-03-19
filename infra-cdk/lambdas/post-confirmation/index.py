"""
Post-Confirmation Lambda Trigger — Cognito User Pool

Fires after a user self-registers and confirms their email.

Role assignment policy:
  - Every self-registered user is placed in the 'external' group.
  - Internal users are NOT created via self-registration. They are created
    directly by an administrator (Cognito console / CLI / CDK CfnUserPoolUser)
    and manually added to the 'internal' group.
  - If a user has neither group, the role.py utility defaults to EXTERNAL
    (fail-safe, not fail-open).

Why PostConfirmation (not PreSignUp):
  - PostConfirmation fires AFTER the user is confirmed and exists in the pool.
    AdminAddUserToGroup requires the user to exist first.
  - PreSignUp fires before creation — the user object is not yet committed,
    so AdminAddUserToGroup would fail.

Trigger type: PostConfirmation_ConfirmSignUp (self-registration flow only).
Admin-created users confirmed via AdminConfirmUser do NOT trigger this Lambda
with the PostConfirmation_ConfirmSignUp triggerSource, so they won't be
auto-assigned to 'external'.
"""

import os
import boto3

cognito = boto3.client("cognito-idp")

# pool_id is read from event["userPoolId"] — Cognito always provides it in the
# trigger payload. We do NOT read USER_POOL_ID from env because that would
# require a CDK token reference back to the UserPool, causing a circular
# dependency with LambdaConfig.PostConfirmation on the same UserPool resource.
EXTERNAL_GROUP  = os.environ.get("EXTERNAL_GROUP_NAME", "external")


def handler(event, context):
    trigger_source = event.get("triggerSource", "")
    user_name      = event["userName"]
    pool_id        = event["userPoolId"]

    print(f"[POST-CONFIRM] triggerSource={trigger_source} user={user_name}")

    # Only act on self-registration confirmation, not admin-confirmed flows.
    # PostConfirmation_ConfirmSignUp = user clicked the email verification link.
    # PostConfirmation_ConfirmForgotPassword = password reset — skip that.
    if trigger_source != "PostConfirmation_ConfirmSignUp":
        print(f"[POST-CONFIRM] Skipping — triggerSource is not ConfirmSignUp")
        return event

    try:
        cognito.admin_add_user_to_group(
            UserPoolId=pool_id,
            Username=user_name,
            GroupName=EXTERNAL_GROUP,
        )
        print(f"[POST-CONFIRM] Added {user_name} to group '{EXTERNAL_GROUP}'")
    except cognito.exceptions.ResourceNotFoundException:
        # Group doesn't exist yet — log and continue rather than crashing the
        # sign-up flow. This should not happen in normal operation.
        print(f"[POST-CONFIRM] WARNING: group '{EXTERNAL_GROUP}' not found — user NOT grouped")
    except Exception as e:
        # Log but do NOT re-raise. Raising here would block the user's sign-up,
        # which is a worse outcome than them landing in the default-external fallback.
        print(f"[POST-CONFIRM] ERROR adding user to group: {e}")

    # Must return the event object unchanged — Cognito requires this.
    return event
