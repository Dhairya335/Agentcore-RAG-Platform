"""
Phase 4 Backfill Script — Org-level tenancy migration

Run ONCE after deploying Phase 4 CDK changes.

What this script does:
  1. Creates the org-internal org record in fast_orgs (idempotent)
  2. Seeds fast_user_memberships for all existing INTERNAL Cognito users
  3. Prints a report of any EXTERNAL users that still need manual invite flow
  4. Updates existing fast_chunks rows where tenant_id = user_sub → org-internal
     (for internal users only — external user chunks cannot be migrated without
      knowing their org, which requires invite flow completion)

Usage:
  python scripts/backfill_org_tenancy.py \
    --profile bedrock-agentcore-rag \
    --region us-east-1 \
    --stack-name FAST-stack \
    --user-pool-id us-east-1_xhskqm1HR \
    [--dry-run]

ALWAYS run with --dry-run first to verify.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import boto3

NOW = datetime.now(timezone.utc).isoformat()
ORG_INTERNAL_ID = "org-internal"


def parse_args():
    p = argparse.ArgumentParser(description="Phase 4 org-level tenancy backfill")
    p.add_argument("--profile",       required=True, help="AWS profile name")
    p.add_argument("--region",        default="us-east-1")
    p.add_argument("--stack-name",    required=True)
    p.add_argument("--user-pool-id",  required=True)
    p.add_argument("--dry-run",       action="store_true",
                   help="Print what would be done without writing anything")
    return p.parse_args()


def get_ssm_param(ssm, stack_name: str, key: str) -> str:
    resp = ssm.get_parameter(Name=f"/{stack_name}/{key}")
    return resp["Parameter"]["Value"]


def ensure_org_internal(dynamodb, orgs_table_name: str, dry_run: bool):
    """Create org-internal org record if it doesn't exist."""
    table = dynamodb.Table(orgs_table_name)
    resp  = table.get_item(Key={"org_id": ORG_INTERNAL_ID}, ConsistentRead=True)
    if "Item" in resp:
        print(f"  ✓ org-internal already exists in {orgs_table_name}")
        return

    item = {
        "org_id":     ORG_INTERNAL_ID,
        "org_name":   "Internal Vendor Organisation",
        "org_type":   "INTERNAL_VENDOR",
        "status":     "ACTIVE",
        "created_at": NOW,
        "created_by": "backfill-script",
        "settings":   {},
    }
    if dry_run:
        print(f"  [DRY-RUN] Would create org: {json.dumps(item, indent=2)}")
    else:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(org_id)")
        print(f"  ✓ Created org-internal in {orgs_table_name}")


def list_cognito_users_by_group(cognito, user_pool_id: str, group_name: str) -> list[dict]:
    users = []
    kwargs = {"UserPoolId": user_pool_id, "GroupName": group_name, "Limit": 60}
    while True:
        resp = cognito.list_users_in_group(**kwargs)
        users.extend(resp.get("Users", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return users


def get_user_email(user: dict) -> str:
    for attr in user.get("Attributes", []):
        if attr["Name"] == "email":
            return attr["Value"].lower()
    return ""


def seed_internal_memberships(
    dynamodb,
    cognito,
    memberships_table_name: str,
    user_pool_id: str,
    dry_run: bool,
):
    """Write fast_user_memberships for all internal Cognito users."""
    table = dynamodb.Table(memberships_table_name)
    users = list_cognito_users_by_group(cognito, user_pool_id, "internal")
    print(f"\n  Found {len(users)} users in 'internal' Cognito group")

    seeded = 0
    skipped = 0
    for user in users:
        sub   = user["Username"]  # sub is the username for sub-based UUIDs
        # Try to get the actual sub from attributes
        for attr in user.get("Attributes", []):
            if attr["Name"] == "sub":
                sub = attr["Value"]
                break
        email = get_user_email(user)

        # Check existing
        resp = table.get_item(Key={"user_sub": sub}, ConsistentRead=True)
        if "Item" in resp:
            existing = resp["Item"]
            if existing.get("org_id") == ORG_INTERNAL_ID and existing.get("membership_status") == "ACTIVE":
                print(f"    ✓ Already seeded: sub={sub} email={email}")
                skipped += 1
                continue

        item = {
            "user_sub":          sub,
            "email":             email,
            "org_id":            ORG_INTERNAL_ID,
            "role_class":        "INTERNAL",
            "membership_status": "ACTIVE",
            "invited_by":        "backfill-script",
            "created_at":        NOW,
            "updated_at":        NOW,
        }
        if dry_run:
            print(f"    [DRY-RUN] Would write membership: sub={sub} email={email} org={ORG_INTERNAL_ID}")
        else:
            table.put_item(Item=item)
            print(f"    ✓ Seeded membership: sub={sub} email={email} org={ORG_INTERNAL_ID}")
        seeded += 1

    print(f"  Seeded: {seeded}  |  Already present: {skipped}")


def report_external_users(cognito, user_pool_id: str):
    """List external users that need invite flow to get org membership."""
    users = list_cognito_users_by_group(cognito, user_pool_id, "external")
    print(f"\n  External users requiring invite-based org assignment: {len(users)}")
    for user in users:
        sub   = user["Username"]
        email = get_user_email(user)
        print(f"    - sub={sub} email={email}")
    if users:
        print("\n  ACTION REQUIRED: Each external user needs to:")
        print("  1. Admin creates org via POST /admin/orgs")
        print("  2. Admin creates invite via POST /admin/orgs/{orgId}/invites")
        print("  3. External user calls POST /auth/complete-external-registration with invite token")


def backfill_aurora_chunks(
    rds_data,
    aurora_cluster_arn: str,
    aurora_secret_arn: str,
    aurora_db_name: str,
    internal_user_subs: list[str],
    dry_run: bool,
):
    """
    For each internal user sub that is currently stored as tenant_id in fast_chunks,
    update tenant_id to org-internal.

    This is only safe for internal users because we know their org_id = org-internal.
    External user chunks cannot be migrated without knowing their org_id.
    """
    if not internal_user_subs:
        print("\n  No internal user subs to backfill in Aurora")
        return

    print(f"\n  Backfilling Aurora fast_chunks for {len(internal_user_subs)} internal user subs")
    for sub in internal_user_subs:
        # Count chunks for this sub
        count_sql = "SELECT COUNT(*) AS cnt FROM fast_chunks WHERE tenant_id = :sub"
        resp = rds_data.execute_statement(
            resourceArn=aurora_cluster_arn,
            secretArn=aurora_secret_arn,
            database=aurora_db_name,
            sql=count_sql,
            parameters=[{"name": "sub", "value": {"stringValue": sub}}],
            includeResultMetadata=True,
        )
        cnt = resp["records"][0][0].get("longValue", 0) if resp.get("records") else 0
        if cnt == 0:
            print(f"    ✓ No chunks found for sub={sub} (nothing to migrate)")
            continue

        if dry_run:
            print(f"    [DRY-RUN] Would update {cnt} chunks: tenant_id {sub} → {ORG_INTERNAL_ID}")
        else:
            update_sql = """
                UPDATE fast_chunks
                SET tenant_id     = :org_id,
                    owner_user_id = :sub,
                    sharing_scope = 'ORG_SHARED'
                WHERE tenant_id = :sub
            """
            rds_data.execute_statement(
                resourceArn=aurora_cluster_arn,
                secretArn=aurora_secret_arn,
                database=aurora_db_name,
                sql=update_sql,
                parameters=[
                    {"name": "org_id", "value": {"stringValue": ORG_INTERNAL_ID}},
                    {"name": "sub",    "value": {"stringValue": sub}},
                ],
            )
            print(f"    ✓ Migrated {cnt} chunks: sub={sub} → org={ORG_INTERNAL_ID}")


def main():
    args = parse_args()

    session  = boto3.Session(profile_name=args.profile, region_name=args.region)
    ssm      = session.client("ssm")
    dynamodb = session.resource("dynamodb")
    cognito  = session.client("cognito-idp")
    rds_data = session.client("rds-data")

    print(f"\n{'='*60}")
    print(f"Phase 4 Org-Level Tenancy Backfill")
    print(f"Stack:    {args.stack_name}")
    print(f"Region:   {args.region}")
    print(f"Dry-run:  {args.dry_run}")
    print(f"{'='*60}\n")

    # Resolve table names from SSM
    try:
        orgs_table        = get_ssm_param(ssm, args.stack_name, "rag/orgs-table-name")
        memberships_table = get_ssm_param(ssm, args.stack_name, "rag/memberships-table-name")
        aurora_cluster    = get_ssm_param(ssm, args.stack_name, "rag/aurora-cluster-arn")
        aurora_secret     = get_ssm_param(ssm, args.stack_name, "rag/aurora-secret-arn")
        aurora_db         = get_ssm_param(ssm, args.stack_name, "rag/aurora-db-name")
    except Exception as e:
        print(f"ERROR: Failed to read SSM parameters: {e}")
        print("Ensure CDK has been deployed with Phase 4 changes first.")
        sys.exit(1)

    # Step 1: Ensure org-internal exists
    print("Step 1: Ensuring org-internal record exists in fast_orgs")
    ensure_org_internal(dynamodb, orgs_table, args.dry_run)

    # Step 2: Seed internal user memberships
    print("\nStep 2: Seeding fast_user_memberships for internal Cognito users")
    seed_internal_memberships(
        dynamodb, cognito, memberships_table, args.user_pool_id, args.dry_run
    )

    # Step 3: Report external users that need invite flow
    print("\nStep 3: Reporting external users that need invite-based registration")
    report_external_users(cognito, args.user_pool_id)

    # Step 4: Backfill Aurora chunks for internal users
    print("\nStep 4: Backfilling Aurora fast_chunks (internal users only)")
    internal_users = list_cognito_users_by_group(cognito, args.user_pool_id, "internal")
    internal_subs  = []
    for user in internal_users:
        for attr in user.get("Attributes", []):
            if attr["Name"] == "sub":
                internal_subs.append(attr["Value"])
                break
        else:
            internal_subs.append(user["Username"])

    backfill_aurora_chunks(
        rds_data, aurora_cluster, aurora_secret, aurora_db,
        internal_subs, args.dry_run,
    )

    print(f"\n{'='*60}")
    print("Backfill complete." if not args.dry_run else "Dry-run complete. Re-run without --dry-run to apply.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
