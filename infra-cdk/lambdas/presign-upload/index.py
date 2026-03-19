"""
Presign Upload Lambda — org-level tenancy (Phase 4 rewrite)

Called by API Gateway POST /documents/presign.

Changes from v1:
  - org_id is NEVER accepted from the browser.  It is resolved server-side
    from fast_user_memberships using the validated JWT sub claim.
  - S3 key format: orgs/{org_id}/documents/{doc_id}/v{version}/{fileName}
  - DynamoDB document record extended with: org_id, owner_user_id,
    owner_role_class, sharing_scope
  - tenantId in DynamoDB PK now stores org_id (not user sub)
  - Browser still sends only: fileName, contentType, optional metadata
  - Browser must NOT send tenantId or orgId — those are ignored if present

Security:
  - org_id derived from membership table (DynamoDB, strongly consistent)
  - Fails closed if membership missing or non-ACTIVE (403)
  - S3 key computed server-side from resolved org_id + generated doc_id
  - S3 object metadata carries org-id (not user sub) for ingestion worker
"""

import json
import os
import uuid
import boto3
from datetime import datetime, timezone

s3              = boto3.client("s3")
dynamodb        = boto3.resource("dynamodb")
dynamodb_client = boto3.client("dynamodb")
_ddb_boto3      = boto3.resource("dynamodb")   # for memberships lookup

BUCKET_NAME          = os.environ["DOCS_BUCKET_NAME"]
TABLE_NAME           = os.environ["DOCS_TABLE_NAME"]
MEMBERSHIPS_TABLE    = os.environ["MEMBERSHIPS_TABLE_NAME"]
REGION               = os.environ.get("AWS_REGION", "us-east-1")
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
cors_origins = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]


def handler(event, context):
    """
    POST /documents/presign

    Expected request body (browser):
    {
      "fileName":    "policy-doc.pdf",
      "contentType": "application/pdf",
      "metadata":    {}   <- optional
      // DO NOT send tenantId or orgId — resolved server-side from JWT
    }

    Returns:
    {
      "uploadUrl": "https://...",
      "docId":     "uuid",
      "version":   1,
      "s3Key":     "orgs/{org_id}/documents/{docId}/v1/{fileName}"
    }
    """
    # ── 1. Parse request body ───────────────────────────────────────────────
    try:
        body = json.loads(event.get("body", "{}"))
    except Exception:
        return _error(400, "Invalid JSON body", event)

    file_name    = (body.get("fileName") or "").strip()
    content_type = body.get("contentType", "application/octet-stream")
    metadata     = body.get("metadata") or {}
    sharing_scope = body.get("sharingScope", "ORG_SHARED").upper()

    if not file_name:
        return _error(400, "fileName is required", event)
    if sharing_scope not in ("ORG_SHARED", "OWNER_ONLY"):
        sharing_scope = "ORG_SHARED"

    # ── 2. Resolve principal from JWT claims ────────────────────────────────
    # API Gateway Cognito authorizer has validated the JWT.
    # Claims are in requestContext.authorizer.claims.
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims", {})
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return _error(401, "Cannot determine user identity from token", event)

    groups = claims.get("cognito:groups", "") or ""
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    role_class = "INTERNAL" if "internal" in groups else "EXTERNAL"

    # ── 3. Resolve org_id from membership table ─────────────────────────────
    # org_id is authoritative — never trust browser-supplied value
    try:
        org_id = _resolve_org_id(user_id)
    except PermissionError as e:
        print(f"[PRESIGN][AUTH] {e}")
        return _error(403, str(e), event)
    except Exception as e:
        print(f"[PRESIGN][ERROR] Membership lookup failed: {e}")
        return _error(500, "Failed to resolve organisation membership", event)

    # ── 4. Generate doc_id and compute S3 key ───────────────────────────────
    doc_id  = body.get("docId") or str(uuid.uuid4())

    # Version: check LATEST record for this org+doc
    table     = dynamodb.Table(TABLE_NAME)
    latest_pk = f"TENANT#{org_id}#DOC#{doc_id}"   # tenant_id = org_id from now on
    version   = 1
    try:
        resp = table.get_item(Key={"PK": latest_pk, "SK": "LATEST"})
        if "Item" in resp:
            version = resp["Item"].get("latestVersion", 0) + 1
    except Exception:
        pass

    # New key format: orgs/{org_id}/documents/{doc_id}/v{version}/{fileName}
    s3_key = f"orgs/{org_id}/documents/{doc_id}/v{version}/{file_name}"

    # ── 5. Generate presigned PUT URL ───────────────────────────────────────
    # S3 object metadata passes org-id (not user sub) to the ingestion worker.
    # Ingestion worker reads these from head_object() — stateless at ingest time.
    try:
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket":      BUCKET_NAME,
                "Key":         s3_key,
                "ContentType": content_type,
                "Metadata": {
                    "doc-id":        doc_id,
                    "org-id":        org_id,          # org-level tenant identifier
                    "owner-user-id": user_id,         # uploader's Cognito sub
                    "version":       str(version),
                    "sharing-scope": sharing_scope,
                },
            },
            ExpiresIn=900,
        )
    except Exception as e:
        return _error(500, f"Failed to generate presigned URL: {e}", event)

    now = datetime.now(timezone.utc).isoformat()

    # ── 6. Write DynamoDB document records atomically ───────────────────────
    # PK uses org_id so all members of the org can look up the document.
    try:
        dynamodb_client.transact_write_items(
            TransactItems=[
                # Version history record
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": {
                            "PK":              {"S": latest_pk},
                            "SK":              {"S": f"VER#{version:06d}"},
                            "tenantId":        {"S": org_id},          # org_id = tenant
                            "orgId":           {"S": org_id},
                            "ownerUserId":     {"S": user_id},
                            "ownerRoleClass":  {"S": role_class},
                            "sharingScope":    {"S": sharing_scope},
                            "version":         {"N": str(version)},
                            "status":          {"S": "UPLOADED"},
                            "s3Key":           {"S": s3_key},
                            "fileName":        {"S": file_name},
                            "contentType":     {"S": content_type},
                            "metadata":        {"M": {k: {"S": str(v)} for k, v in metadata.items()}},
                            "createdAt":       {"S": now},
                            "updatedAt":       {"S": now},
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
                # LATEST pointer
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": {
                            "PK":              {"S": latest_pk},
                            "SK":              {"S": "LATEST"},
                            "tenantId":        {"S": org_id},
                            "orgId":           {"S": org_id},
                            "ownerUserId":     {"S": user_id},
                            "ownerRoleClass":  {"S": role_class},
                            "sharingScope":    {"S": sharing_scope},
                            "docId":           {"S": doc_id},
                            "latestVersion":   {"N": str(version)},
                            "fileName":        {"S": file_name},
                            "updatedAt":       {"S": now},
                        },
                    }
                },
            ]
        )
    except dynamodb_client.exceptions.TransactionCanceledException as e:
        return _error(409, f"Version conflict: {e}", event)
    except Exception as e:
        return _error(500, f"Failed to write document records: {e}", event)

    print(
        f"[UPLOAD] created doc metadata docId={doc_id} org={org_id} "
        f"owner={user_id} sharing={sharing_scope} s3Key={s3_key}"
    )

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _cors_origin(event),
        },
        "body": json.dumps({
            "uploadUrl": upload_url,
            "docId":     doc_id,
            "version":   version,
            "s3Key":     s3_key,
        }),
    }


def _resolve_org_id(user_id: str) -> str:
    """
    Look up user's org_id from fast_user_memberships.
    Strongly consistent read — authorization-critical path.

    Raises:
        PermissionError: membership missing, non-ACTIVE, or empty org_id
        Exception: DynamoDB error
    """
    table = _ddb_boto3.Table(MEMBERSHIPS_TABLE)
    resp  = table.get_item(
        Key={"user_sub": user_id},
        ConsistentRead=True,
    )
    membership = resp.get("Item")
    if not membership:
        raise PermissionError(
            f"No membership record for user={user_id}. "
            "Complete registration before uploading documents."
        )
    status = membership.get("membership_status", "")
    if status != "ACTIVE":
        raise PermissionError(
            f"Membership not ACTIVE for user={user_id} (status={status})"
        )
    org_id = (membership.get("org_id") or "").strip()
    if not org_id:
        raise PermissionError(
            f"Membership record for user={user_id} has no org_id"
        )
    return org_id


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
