"""
Org Seed Lambda — CDK Custom Resource

Runs on every CDK deploy (triggered by SchemaVersion property change).

Responsibilities:
  1. Ensure the vendor org record ("org-internal") exists in the OrgsTable.
  2. List all Cognito users in the "internal" group and write an ACTIVE membership
     record for each into MembershipsTable (idempotent — skips existing ACTIVE members).

This replaces the manual backfill script. It is NOT a user-facing API endpoint.
CloudFormation Custom Resource lifecycle:
  - Create / Update  → seed org + memberships
  - Delete           → no-op (tables persist independently)

Environment variables (required — missing value crashes at startup):
  ORGS_TABLE_NAME:        DynamoDB table name for org registry
  MEMBERSHIPS_TABLE_NAME: DynamoDB table name for user→org memberships
  USER_POOL_ID:           Cognito User Pool ID
  STACK_NAME:             Stack name prefix (e.g. "FAST-stack") for logging
"""

import json
import os
import time
import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

# ── Module-level clients (reused across warm Lambda invocations)   ──────
dynamodb  = boto3.resource("dynamodb")
cognito   = boto3.client("cognito-idp")

# ── Required env vars — crash at startup if missing  ───
ORGS_TABLE_NAME        = os.environ["ORGS_TABLE_NAME"]
MEMBERSHIPS_TABLE_NAME = os.environ["MEMBERSHIPS_TABLE_NAME"]
USER_POOL_ID           = os.environ["USER_POOL_ID"]
STACK_NAME             = os.environ["STACK_NAME"]

VENDOR_ORG_ID   = "org-internal"
VENDOR_ORG_NAME = "Internal Organisation"
INTERNAL_GROUP  = "internal"


def handler(event: dict, context: object) -> dict:
    """
    CloudFormation Custom Resource handler.

    Called by CDK on Create, Update, and Delete.
    On Delete: returns success immediately (tables are not dropped here).
    On Create/Update: seeds org-internal record + all internal Cognito users.

    Args:
        event   (dict): CloudFormation Custom Resource event
        context (object): Lambda context (unused)

    Returns:
        dict: {"PhysicalResourceId": str, "Data": dict}
              Data includes counts of org_created, memberships_created, memberships_skipped.
    """
    request_type: str = event.get("RequestType", "")
    print(f"[ORG-SEED] RequestType={request_type} StackName={STACK_NAME}")

    if request_type == "Delete":
        # Seed data is preserved — tables are managed separately by CDK.
        return _cfn_success(event=event, data={"message": "Delete is a no-op — seed data preserved"})

    # Create or Update — run full seed
    org_created        = _seed_vendor_org()
    created, skipped   = _seed_internal_memberships()

    print(
        f"[ORG-SEED] Done: org_created={org_created} "
        f"memberships_created={created} memberships_skipped={skipped}"
    )

    return _cfn_success(
        event=event,
        data={
            "org_created":           org_created,
            "memberships_created":   created,
            "memberships_skipped":   skipped,
        },
    )


def _seed_vendor_org() -> bool:
    """
    Write the vendor org record to OrgsTable if it does not already exist.

    Uses a conditional PutItem so concurrent deploys are safe (idempotent).

    Returns:
        bool: True if the record was created, False if it already existed.
    """
    orgs_table = dynamodb.Table(ORGS_TABLE_NAME)
    now        = _iso_now()

    try:
        orgs_table.put_item(
            Item={
                "org_id":      VENDOR_ORG_ID,
                "org_name":    VENDOR_ORG_NAME,
                "org_type":    "INTERNAL",
                "status":      "ACTIVE",
                "created_at":  now,
                "updated_at":  now,
            },
            # Only write if the record does not already exist
            ConditionExpression=Attr("org_id").not_exists(),
        )
        print(f"[ORG-SEED] Created org record: org_id={VENDOR_ORG_ID}")
        return True
    except ClientError as exc:
        # ConditionalCheckFailedException means the record already exists — that is fine
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(f"[ORG-SEED] Org record already exists: org_id={VENDOR_ORG_ID} — skipping")
            return False
        # Any other DynamoDB error is a real failure — crash loudly
        raise


def _seed_internal_memberships() -> tuple[int, int]:
    """
    List all users in the Cognito "internal" group and write ACTIVE membership
    records to MembershipsTable for any user who does not already have one.

    Membership records written here use:
        membership_status = "ACTIVE"
        role_class        = "INTERNAL"
        org_id            = "org-internal"

    The seed is idempotent: existing ACTIVE records are left untouched.

    Returns:
        tuple[int, int]: (created_count, skipped_count)
    """
    memberships_table = dynamodb.Table(MEMBERSHIPS_TABLE_NAME)
    users             = _list_all_users_in_group(group_name=INTERNAL_GROUP)

    created = 0
    skipped = 0
    now     = _iso_now()

    for user in users:
        user_sub   = _get_user_attribute(user=user, attribute_name="sub")
        user_email = _get_user_attribute(user=user, attribute_name="email")

        if not user_sub:
            # User has no sub — should never happen in a valid Cognito pool
            print(f"[ORG-SEED][WARN] User in internal group has no sub attribute: {user.get('Username')}")
            continue

        try:
            memberships_table.put_item(
                Item={
                    "user_sub":          user_sub,
                    "org_id":            VENDOR_ORG_ID,
                    "membership_status": "ACTIVE",
                    "role_class":        "INTERNAL",
                    "email":             user_email,
                    "created_at":        now,
                    "updated_at":        now,
                },
                # Only write if no membership record exists for this user yet
                ConditionExpression=Attr("user_sub").not_exists(),
            )
            print(f"[ORG-SEED] Created membership: user_sub={user_sub} email={user_email}")
            created += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Membership already exists — idempotent skip
                skipped += 1
            else:
                # Real DynamoDB error — crash loudly so the deploy fails visibly
                raise

    return created, skipped


def _list_all_users_in_group(group_name: str) -> list[dict]:
    """
    Paginate through all Cognito users in the given group.

    Args:
        group_name (str): Cognito group name to list users from

    Returns:
        list[dict]: List of Cognito user objects (each has Username + Attributes list)
    """
    users: list[dict] = []
    kwargs: dict = {
        "UserPoolId": USER_POOL_ID,
        "GroupName":  group_name,
        "Limit":      60,  # Cognito max per page
    }

    while True:
        response   = cognito.list_users_in_group(**kwargs)
        users     += response.get("Users", [])
        next_token = response.get("NextToken")

        if not next_token:
            break

        # Pass pagination token for the next page
        kwargs["NextToken"] = next_token

    print(f"[ORG-SEED] Found {len(users)} users in Cognito group '{group_name}'")
    return users


def _get_user_attribute(user: dict, attribute_name: str) -> str:
    """
    Extract a named attribute value from a Cognito user object.

    Cognito user objects have an "Attributes" list of {"Name": str, "Value": str} dicts.

    Args:
        user           (dict): Cognito user object from list_users_in_group
        attribute_name (str):  Attribute name to extract (e.g. "sub", "email")

    Returns:
        str: Attribute value, or empty string if not found
    """
    for attr in user.get("Attributes", []):
        if attr.get("Name") == attribute_name:
            return attr.get("Value", "")
    return ""


def _iso_now() -> str:
    """
    Return the current UTC time as an ISO 8601 string.

    Returns:
        str: e.g. "2026-03-26T15:04:05.123456+00:00"
    """
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat()


def _cfn_success(event: dict, data: dict) -> dict:
    """
    Return a CloudFormation Custom Resource success response.

    The PhysicalResourceId is stable across Create/Update so CloudFormation
    does not treat Update as a replacement.

    Args:
        event (dict): Original CloudFormation Custom Resource event
        data  (dict): Key/value pairs returned to CloudFormation as resource attributes

    Returns:
        dict: Custom Resource response payload
    """
    return {
        "PhysicalResourceId": f"{STACK_NAME}-org-seed",
        "Data":               data,
    }
