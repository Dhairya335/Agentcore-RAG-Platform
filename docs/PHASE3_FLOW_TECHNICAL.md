# Phase 3 — Complete Technical Reference
# FAST RAG + AgentCore: Knowledge Operating System

Every function call, IAM grant, DynamoDB key pattern, SQL clause, React hook lifecycle,
event flow, state machine, and design decision for every Phase 3 checkpoint.

---

## Files Created / Modified in Phase 3

| File | Status | Checkpoint |
|------|--------|------------|
| `infra-cdk/lib/cognito-stack.ts` | Modified | A |
| `infra-cdk/lambdas/post-confirmation/index.py` | Created | A |
| `patterns/utils/role.py` | Created | A |
| `frontend/src/hooks/useUserRole.ts` | Created | A |
| `infra-cdk/lambdas/pgvector-setup/index.py` | Modified | A |
| `infra-cdk/lambdas/ingestion-worker/index.py` | Modified | A |
| `infra-cdk/lib/backend-stack.ts` | Modified | B, C, D, E |
| `infra-cdk/lambdas/list-documents/index.py` | Created | B |
| `infra-cdk/lambdas/get-document/index.py` | Created | B |
| `frontend/src/services/documentService.ts` | Modified | B, C, D, E |
| `frontend/src/components/knowledge/KnowledgeBaseSidebar.tsx` | Created | C |
| `frontend/src/components/knowledge/DocumentCard.tsx` | Created | C |
| `frontend/src/components/knowledge/DocumentDetailPanel.tsx` | Created | C |
| `frontend/src/components/knowledge/CollectionManager.tsx` | Created | D |
| `infra-cdk/lambdas/create-collection/index.py` | Created | D |
| `infra-cdk/lambdas/list-collections/index.py` | Created | D |
| `infra-cdk/lambdas/collection-membership/index.py` | Created | D |
| `infra-cdk/lambdas/rag-retrieve/index.py` | Modified | E |
| `infra-cdk/lambdas/rag-retrieve/tool_spec.json` | Modified | E |
| `infra-cdk/lambdas/preview-chunks/index.py` | Created | E |
| `patterns/strands-single-agent/basic_agent.py` | Modified | E |
| `frontend/src/components/knowledge/SourceViewerDrawer.tsx` | Created | E |
| `frontend/src/components/chat/MarkdownRenderer.tsx` | Modified | E |
| `frontend/src/components/chat/ChatMessages.tsx` | Modified | E |
| `frontend/src/components/chat/ChatMessage.tsx` | Modified | E |
| `frontend/src/components/chat/ChatInterface.tsx` | Modified | A, C, E |

---

## CHECKPOINT A — RBAC Foundation

### Phase 0 — CDK: Cognito Groups + PostConfirmation Trigger (cognito-stack.ts)

```
CognitoStack.createCognitoUserPool()
    ↓
new lambda.Function(this, "PostConfirmationLambda", {
    functionName: "{stack}-post-confirmation",
    runtime:      PYTHON_3_13,
    code:         fromAsset("lambdas/post-confirmation/"),
    handler:      "index.handler",
    architecture: ARM_64,
    timeout:      Duration.seconds(10),
    environment: {
        USER_POOL_ID:        "PLACEHOLDER",  ← overwritten below after pool creation
        EXTERNAL_GROUP_NAME: "external",
    },
})
    AWS SERVICE: AWS Lambda
    NOTE: USER_POOL_ID starts as "PLACEHOLDER" because the pool doesn't exist yet.
    CDK resolves it as a CloudFormation token reference after pool creation.
    ↓
new cognito.UserPool(this, "UserPool", {
    selfSignUpEnabled: true,
    signInAliases: { email: true },
    autoVerify: { email: true },
    passwordPolicy: { minLength:8, upper, lower, digits, symbols },
    accountRecovery: EMAIL_ONLY,
    removalPolicy: DESTROY,
    // NOTE: lambdaTriggers intentionally NOT set here — circular dependency.
    // See "Cognito Circular Dependency" section below for full explanation.
})
    AWS SERVICE: Amazon Cognito User Pool
    ↓
postConfirmationLambda.addEnvironment("USER_POOL_ID", userPool.userPoolId)
    Injects real pool ID as a CFN token reference — resolved at deploy time, not synth.
    ↓
postConfirmationLambda.addToRolePolicy(new iam.PolicyStatement({
    actions:   ["cognito-idp:AdminAddUserToGroup"],
    resources: ["*"],   ← "*" intentional — avoids circular dep on userPool.userPoolArn
}))
    AWS SERVICE: IAM
    Minimum-necessary grant: only AdminAddUserToGroup.
```

#### Cognito Circular Dependency — Root Cause and Fix

**Problem:** Three approaches were attempted and all produced `UPDATE_FAILED: Circular dependency`:

| Approach | Why it cycles |
|----------|--------------|
| `lambdaTriggers` in UserPool constructor | CDK auto-generates `UserPoolPostConfirmationCognito` resource that references both Lambda and UserPool in the same changeset |
| `CfnUserPool.addPropertyOverride("LambdaConfig.PostConfirmation", ...)` | `Lambda::Permission` resource in same changeset creates implicit CFN ordering cycle |
| `addPermission` with `sourceAccount` only | CFN dependency analyser still ties `Lambda::Permission` to the nested stack update containing the UserPool |

**Root cause:** CloudFormation cannot update a UserPool's `LambdaConfig` in the same
changeset as creating the `Lambda::Permission` that allows Cognito to invoke that Lambda.
It is a chicken-and-egg ordering problem within a single CFN changeset.

**Fix: `AwsCustomResource` (CDK custom-resources module)**

Two `AwsCustomResource` constructs make direct AWS SDK calls **after** both the
UserPool and Lambda are fully deployed. They are separate CFN resources with
explicit `node.addDependency()` ordering:

```
PostConfirmationLambda (CREATE) — no UserPool reference
    ↓
UserPool (CREATE/UPDATE) — no LambdaConfig set yet
    ↓
TriggerWirerRole (CREATE) — IAM role for the custom resource Lambda
    ↓
PostConfirmationTriggerWirer (AwsCustomResource)
    onCreate/onUpdate: CognitoIdentityProvider.updateUserPool({
        UserPoolId: <pool-id>,
        LambdaConfig: { PostConfirmation: <lambda-arn> }
    })
    onDelete: CognitoIdentityProvider.updateUserPool({
        UserPoolId: <pool-id>,
        LambdaConfig: {}    ← clears the trigger on stack teardown
    })
    installLatestAwsSdk: false   ← uses bundled SDK, avoids download at deploy time
    ↓
PostConfirmationInvokePermission (AwsCustomResource)
    onCreate: Lambda.addPermission({
        FunctionName:  <lambda-arn>,
        StatementId:   "CognitoInvokePermission",
        Action:        "lambda:InvokeFunction",
        Principal:     "cognito-idp.amazonaws.com",
        SourceAccount: <account-id>,
    })
    ignoreErrorCodesMatching: "ResourceConflictException"  ← idempotent on re-deploy
    onDelete: Lambda.removePermission({
        FunctionName: <lambda-arn>,
        StatementId:  "CognitoInvokePermission",
    })
    ignoreErrorCodesMatching: "ResourceNotFoundException"  ← safe if already removed
    installLatestAwsSdk: false
```

TriggerWirerRole inline policy:
```json
{
  "cognito-idp:UpdateUserPool":   [userPool.userPoolArn],
  "cognito-idp:DescribeUserPool": [userPool.userPoolArn],
  "lambda:AddPermission":         [postConfirmationLambda.functionArn],
  "lambda:RemovePermission":      [postConfirmationLambda.functionArn]
}
```

Explicit dependency edges (ensures correct CFN ordering):
```typescript
triggerWirer.node.addDependency(postConfirmationLambda)
triggerWirer.node.addDependency(userPool)
addPermission.node.addDependency(postConfirmationLambda)
```

#### Cognito Groups

```
new cognito.CfnUserPoolGroup(this, "InternalGroup", {
    userPoolId:  userPool.userPoolId,
    groupName:   "internal",
    description: "Internal users — full knowledge base access",
})
    AWS SERVICE: Amazon Cognito User Pool
    Creates a named group. Group membership is stored by Cognito and included
    automatically in every ID token the user receives post-login.

new cognito.CfnUserPoolGroup(this, "ExternalGroup", {
    userPoolId:  userPool.userPoolId,
    groupName:   "external",
    description: "External users — chat-only access",
})

internalGroup.node.addDependency(userPool)
externalGroup.node.addDependency(userPool)
```

#### Admin User (if config.admin_user_email set)

```
new cognito.CfnUserPoolUserToGroupAttachment(this, "AdminToInternalGroup", {
    userPoolId: userPool.userPoolId,
    username:   adminUser.ref,
    groupName:  "internal",
})
    Hardwires the CDK-defined admin user to the internal group at deploy time.
    All other admin users must be manually added via Cognito console or CLI.
    Admin-created users do NOT trigger PostConfirmation_ConfirmSignUp —
    the post-confirmation Lambda will NOT run for them automatically.
```

### Phase 0B — CDK: pgvector-setup Lambda — visibility_mode column (pgvector-setup/index.py)

Phase 3 adds a `visibility_mode` column to `fast_chunks` so the retrieval layer can enforce
RBAC at the SQL level. The pgvector-setup Custom Resource Lambda now runs these additional
statements on every deploy (all idempotent):

```
Statement N+1:
    ALTER TABLE fast_chunks
    ADD COLUMN IF NOT EXISTS visibility_mode TEXT NOT NULL DEFAULT 'INTERNAL_ONLY';
    AWS SERVICE: RDS Data API → Aurora PostgreSQL 16.4
    DEFAULT 'INTERNAL_ONLY': retroactively applies to all existing chunks (safe).
    IF NOT EXISTS: idempotent across re-deploys.

Statement N+2:
    CREATE INDEX IF NOT EXISTS fast_chunks_visibility_idx
    ON fast_chunks (visibility_mode);
    B-tree index on visibility_mode alone.
    Supports queries filtering by a single visibility value.

Statement N+3:
    CREATE INDEX IF NOT EXISTS fast_chunks_tenant_vis_idx
    ON fast_chunks (tenant_id, visibility_mode);
    Composite index.
    Supports the full Phase 3 SQL pattern:
        WHERE tenant_id = :t AND visibility_mode IN (:v1, :v2)
    PostgreSQL query planner uses this index for the combined predicate.
```

### Phase 0C — CDK: ingestion-worker — visibility_mode write (ingestion-worker/index.py)

The ingestion-worker now writes visibility_mode for each chunk:

```
batch_insert_chunks():
    ...
    INSERT INTO fast_chunks (..., visibility_mode, ...)
    VALUES (..., :visibility_mode, ...)

    :visibility_mode → {"name": "visibility_mode", "value": {"stringValue": "INTERNAL_ONLY"}}
```

Default is always INTERNAL_ONLY. Phase 3 does not yet expose a UI for users to change
per-chunk visibility — that is a future feature. Collections have their own visibilityMode
which controls scoped retrieval, not individual chunk visibility.

Also: `update_doc_status` now denormalizes status + chunkCount into the LATEST record:

```
update_doc_status(tenant_id, doc_id, version, "READY", chunk_total=N)
    ↓
dynamodb.update_item(
    Key = {PK: "TENANT#...#DOC#...", SK: "VER#000001"},
    UpdateExpression = "SET #s = :status, updatedAt = :now, chunkCount = :cc",
    ExpressionAttributeValues = {":status": READY, ":cc": N}
)

ALSO updates the LATEST pointer:
dynamodb.update_item(
    Key = {PK: same, SK: "LATEST"},
    UpdateExpression = "SET #s = :status, chunkCount = :cc, updatedAt = :now",
)
```
Why: The list-documents Lambda queries the LATEST record. Without this update,
LATEST would show status=UPLOADED forever even after ingestion completes.
The list-documents Lambda would show incorrect status to users.

---

## CHECKPOINT A — Runtime: role.py (patterns/utils/role.py)

```
Module-level constants:
    INTERNAL_GROUP = "internal"
    EXTERNAL_GROUP = "external"
    ROLE_INTERNAL  = "INTERNAL"
    ROLE_EXTERNAL  = "EXTERNAL"

get_user_role(claims: dict) -> str
    groups = claims.get("cognito:groups", []) or []
        ↑ The 'or []' handles the case where the key exists but is None.
        ↑ JWT spec says absent claims = key missing; Cognito omits the key entirely
          when the user has no groups. .get() returns [] default.
    if INTERNAL_GROUP in groups → return ROLE_INTERNAL
    else                        → return ROLE_EXTERNAL (fail-safe)
    Called by: Lambda handlers that receive decoded JWT claims dict.

get_user_role_from_context(context: RequestContext) -> str
    request_headers = context.request_headers
        ↑ AgentCore Runtime passes headers from the original browser request.
    auth_header = request_headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "") if startswith("Bearer ") else auth_header
    claims = jwt.decode(
        jwt=token,
        options={"verify_signature": False},  ← Runtime already validated; no double-check
        algorithms=["RS256"],
    )
    return get_user_role(claims)
    Called by: basic_agent.py agent_stream() to extract role before creating agent.
    Why decode without verification: The Runtime validates the JWT before calling agent_stream.
    The JWT signature is already verified. Re-verifying requires fetching the Cognito JWKS
    endpoint — an extra network call per request for no security gain.

get_user_role_from_token_string(id_token: str) -> str
    Strips "Bearer " prefix if present.
    jwt.decode(verify_signature=False) → get_user_role(claims)
    Called by: Lambda handlers that have the raw token string (future use / test invocations).
```

---

## CHECKPOINT A — Runtime: useUserRole.ts (frontend/src/hooks/useUserRole.ts)

```
decodeJwtPayload(token: string) -> Record<string, unknown>
    splits on "."
    if parts.length !== 3 → return {} (malformed token guard)
    parts[1] = base64url encoded payload
    base64url → base64: replace /-/g → "+", /_/g → "/"
    atob(base64) → JSON.parse()
    try/catch → {} on any error
    Why: oidc-client-ts sometimes strips non-standard claims from profile.
    The id_token raw string is always available as auth.user.id_token.

useUserRole() -> UserRole
    auth = useOidcAuth()
    useMemo([auth?.isAuthenticated, auth?.user]):
        if (!auth?.isAuthenticated || !auth.user) → return "EXTERNAL"
        Try profile["cognito:groups"] (oidc-client-ts parsed version)
        If absent or empty → decodeJwtPayload(auth.user.id_token)
                             → raw JWT payload has "cognito:groups"
        groups.includes("internal") → "INTERNAL", else "EXTERNAL"
    useMemo deps: [auth?.isAuthenticated, auth?.user]
        Re-computes only when authentication state changes — not on every render.

useIsInternal() -> boolean
    return useUserRole() === "INTERNAL"
    Used in ChatInterface.tsx as the single conditional gate for all INTERNAL features.

ChatInterface.tsx additions:
    const isInternal = useIsInternal()
    const [isSidebarOpen, setIsSidebarOpen] = useState(false)
    const [sourceTarget, setSourceTarget]   = useState<SourceViewerTarget | null>(null)

    In JSX:
    {isInternal && <KnowledgeBaseSidebar open={isSidebarOpen} onClose={...} />}
    {isInternal && <SourceViewerDrawer target={sourceTarget} onClose={...} />}
    onCitationClick={isInternal ? setSourceTarget : undefined}
        ↑ undefined prevents MarkdownRenderer from entering citation rendering mode
```

---

## CHECKPOINT B — Document Management APIs

### CDK: New Lambda Registrations and Routes (backend-stack.ts)

```
createDocumentUploadInfra(config, frontendUrl):
    ...existing presign Lambda...

    ── ListDocumentsLambda    ────
    new lambda.Function(this, "ListDocumentsLambda", {
        functionName: "{stack}-list-documents",
        runtime:      PYTHON_3_13, architecture: ARM_64, timeout: 15s,
        code:         fromAsset("lambdas/list-documents/"),
        handler:      "index.handler",
        environment: {
            DOCS_TABLE_NAME:       docsTable.tableName,
            CORS_ALLOWED_ORIGINS:  "{frontendUrl},http://localhost:3000",
        },
    })
    docsTable.grantReadData(listDocumentsLambda)
        Grants: dynamodb:GetItem, Query, Scan, DescribeTable (read-only).

    documentsResource.addMethod("GET", LambdaIntegration(listDocumentsLambda), {
        authorizer:        docsAuthorizer,
        authorizationType: COGNITO,
    })
        Route: GET /documents

    ── GetDocumentLambda    ──────
    new lambda.Function(this, "GetDocumentLambda", {
        functionName: "{stack}-get-document",
        runtime:      PYTHON_3_13, architecture: ARM_64, timeout: 10s,
        code:         fromAsset("lambdas/get-document/"),
        handler:      "index.handler",
        environment:  { DOCS_TABLE_NAME, CORS_ALLOWED_ORIGINS },
    })
    docsTable.grantReadData(getDocumentLambda)

    docItemResource.addMethod("GET", LambdaIntegration(getDocumentLambda), {
        authorizer:        docsAuthorizer,
        authorizationType: COGNITO,
    })
        Route: GET /documents/{docId}

    ── GSI for collections (added to existing docsTable)  ─
    docsTable.addGlobalSecondaryIndex({
        indexName:            "entityType-tenantId-index",
        partitionKey:         { name: "entityType", type: STRING },
        sortKey:              { name: "tenantId",   type: STRING },
        projectionType:       ALL,
    })
        AWS SERVICE: Amazon DynamoDB GSI
        Why: list-collections queries entityType="COLLECTION" + tenantId=X.
        The existing tenantId-updatedAt-index does not have entityType as a partition key.
        Without this GSI, finding collections requires a full table scan.
```

### list-documents Lambda — Full Flow (lambdas/list-documents/index.py)

```
Module-level initialization (cold start):
    dynamodb   = boto3.client("dynamodb")
    TABLE_NAME = os.environ["DOCS_TABLE_NAME"]
    GSI_NAME   = "tenantId-updatedAt-index"
    _cors_list = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

handler(event, context):
    ── RBAC   ────
    role = _extract_role(event)
        claims = event["requestContext"]["authorizer"]["claims"]
            ↑ API Gateway Cognito Authorizer decodes JWT and injects claims here.
            ↑ Never trust event["body"] or query params for role — those can be forged.
        groups = claims.get("cognito:groups", "")
            ↑ Cognito Authorizer delivers groups as a comma-separated string, not an array.
        group_list = [g.strip() for g in groups.split(",") if g.strip()]
        return "INTERNAL" if "internal" in group_list else "EXTERNAL"
    if role != "INTERNAL" → return 403

    ── Input parsing      ─
    query_params = event.get("queryStringParameters") or {}
    tenant_id    = query_params.get("tenantId", "").strip()
    if not tenant_id → 400
    limit        = min(int(query_params.get("limit", 20)), 100)
    next_page_token = query_params.get("nextPageToken")

    ── DynamoDB GSI Query    ─────
    query_kwargs = {
        TableName:              TABLE_NAME,
        IndexName:              "tenantId-updatedAt-index",
        KeyConditionExpression: "tenantId = :tid",
        FilterExpression:       "SK = :latest",
        ExpressionAttributeValues: {
            ":tid":    {"S": tenant_id},
            ":latest": {"S": "LATEST"},
        },
        ScanIndexForward: False,     ← Sort on updatedAt DESC via GSI SK
        Limit:            limit * 3, ← Over-fetch: VER# records also have tenantId,
                                        so the GSI returns both LATEST and VER records.
                                        Filtering SK=LATEST means many items discarded.
                                        Fetching 3x the limit ensures enough LATEST items.
    }

    if next_page_token:
        lek = json.loads(base64.b64decode(next_page_token).decode())
        query_kwargs["ExclusiveStartKey"] = lek
            ↑ DynamoDB pagination: client sends back LastEvaluatedKey from previous page.
            ↑ base64 encoding hides the DynamoDB key structure from the client.

    resp = dynamodb.query(**query_kwargs)

    ── Shape response      ─
    _shape_doc(item):
        pk     = item["PK"]["S"]  → "TENANT#abc#DOC#uuid"
        doc_id = pk.split("#DOC#")[-1]    ← extracts just the UUID suffix
        return {
            "docId":         doc_id,
            "fileName":      item["fileName"]["S"],
            "latestVersion": int(item["latestVersion"]["N"]) if present else None,
            "status":        item["status"]["S"],       ← denormalized by ingestion-worker
            "chunkCount":    int(item["chunkCount"]["N"]) if present else None,
            "updatedAt":     item["updatedAt"]["S"],
            "contentType":   item["contentType"]["S"] if present else None,
            "errorMessage":  item["errorMessage"]["S"] if present else None,
        }

    ── Pagination token encoding     
    if "LastEvaluatedKey" in resp:
        new_token = base64.b64encode(
            json.dumps(resp["LastEvaluatedKey"]).encode()
        ).decode()
    else:
        new_token = None

    return 200 { documents: [...], count: N, nextPageToken: new_token }
```

### get-document Lambda — Full Flow (lambdas/get-document/index.py)

```
handler(event, context):
    Role check → 403 if not INTERNAL

    doc_id    = event["pathParameters"]["docId"]
    tenant_id = event["queryStringParameters"]["tenantId"]
    pk = f"TENANT#{tenant_id}#DOC#{doc_id}"

    ── BatchGetItem: 2 keys, 1 round-trip    ───────
    resp = dynamodb.batch_get_item(
        RequestItems={
            TABLE_NAME: {
                "Keys": [
                    {"PK": {"S": pk}, "SK": {"S": "LATEST"}},
                    {"PK": {"S": pk}, "SK": {"S": "VER#000001"}},
                ],
                "ConsistentRead": False,
                    ↑ Eventually consistent read. LATEST was written by ingestion-worker.
                    ↑ For a status poll use case, eventual consistency is acceptable.
                    ↑ Consistent read costs 2x RCUs.
            }
        }
    )

    items = resp["Responses"][TABLE_NAME]
    by_sk = {item["SK"]["S"]: item for item in items}
    latest = by_sk.get("LATEST", {})
    ver1   = by_sk.get("VER#000001", {})

    ── Merge strategy      ─
    body = {
        "fileName":      _s(latest, "fileName") or _s(ver1, "fileName"),
            ↑ LATEST has fileName (set by ingestion-worker update). VER1 is fallback.
        "latestVersion": _n(latest, "latestVersion") or _n(ver1, "version"),
        "status":        _s(latest, "status")   or _s(ver1, "status"),
        "chunkCount":    _n(latest, "chunkCount"),   ← only in LATEST after ingestion
        "s3Key":         _s(ver1,   "s3Key"),         ← only in VER1 (set at upload time)
        "contentType":   _s(ver1,   "contentType"),   ← only in VER1
        "createdAt":     _s(ver1,   "createdAt"),     ← only in VER1 (creation time)
        "updatedAt":     _s(latest, "updatedAt") or _s(ver1, "updatedAt"),
        "errorMessage":  _s(latest, "errorMessage") or _s(ver1, "errorMessage"),
    }
    Why split across two records: The presign-upload Lambda writes VER1 with upload
    metadata (s3Key, contentType, createdAt). The ingestion-worker updates VER1 AND
    LATEST with runtime state (status, chunkCount, errorMessage). This merge combines
    both into one response without re-reading the full table.
```

---

## CHECKPOINT C — Frontend Library UI

### KnowledgeBaseSidebar.tsx — State Machines and Data Flow

```
type View = "list" | "detail"
    ↑ Discrete union type. No boolean flags, no if/else chains for navigation.

interface ListState {
    docs:          DocumentListItem[]
    nextPageToken: string | null     ← null when no more pages
    loading:       boolean           ← first-page load in progress
    loadingMore:   boolean           ← subsequent-page load in progress
    error:         string | null
}

interface DetailState {
    doc:     DocumentListItem | null  ← summary data from list (available immediately)
    detail:  DocumentDetail | null    ← full data from get-document (loads after navigation)
    loading: boolean
    error:   string | null
}
```

**hasFetchedRef guard — why useRef not useState:**
```
const hasFetchedRef = useRef(false)

useEffect(() => {
    if (!open) { hasFetchedRef.current = false; return }
    if (hasFetchedRef.current) return
    hasFetchedRef.current = true
    fetchList()
}, [open, fetchList])
```
- `open` goes false → reset ref so next open re-fetches
- `open` goes true → check ref before fetching
- Setting a ref does NOT trigger re-render → no infinite loop
- Using useState would: setState(true) → re-render → useEffect runs → setState again → loop

**credentials() callback — why useCallback with [auth.user]:**
```
const credentials = useCallback(() => ({
    idToken:  auth.user?.id_token     ?? "",
    tenantId: auth.user?.profile?.sub ?? "",
}), [auth.user])
```
Each data function calls `credentials()` at invocation time, not at definition time.
If idToken were captured in a closure at definition time, a token refresh between
definition and invocation would use a stale token. This pattern always reads the
current token from the OIDC auth context at the moment the API call is made.

**fetchList() — pagination flow:**
```
fetchList(pageToken?: string)
    isFirstPage = !pageToken
    setList(prev => ({
        ...prev,
        loading:     isFirstPage,    ← show skeleton on first page
        loadingMore: !isFirstPage,   ← show "Loading..." button on subsequent pages
        error:       isFirstPage ? null : prev.error,
    }))
    ↓
    result = await listDocuments(tenantId, idToken, 20, pageToken)
    ↓
    setList(prev => ({
        docs:          isFirstPage ? result.documents : [...prev.docs, ...result.documents],
            ↑ First page: replace. Subsequent pages: append. Standard infinite-scroll pattern.
        nextPageToken: result.nextPageToken,
        loading:       false, loadingMore: false, error: null,
    }))
```

**openDetail() — optimistic navigation:**
```
openDetail(doc: DocumentListItem):
    setView("detail")            ← immediate navigation (no wait for data)
    setDetail({ doc, detail: null, loading: true, error: null })
        ↑ doc is the list-level summary: available immediately, rendered as skeleton backdrop
    result = await getDocument(doc.docId, tenantId, idToken)
    setDetail(prev => ({ ...prev, detail: result, loading: false }))
        ↑ Full detail arrives and fills in the panel
```

**JSX render — state-driven (no if/else for view):**
```
{view === "detail"
    ? <DocumentDetailPanel doc={detail.doc!} detail={detail.detail} loading={detail.loading} ... />
    : <>
        <ListView list={list} onSelect={openDetail} onLoadMore={...} />
        <CollectionManager docs={list.docs} />   ← receives docs for checkbox population
    </>
}
```

---

## CHECKPOINT D — Collections: Single-Table Design

### DynamoDB Key Patterns — Complete Reference

```
── Existing document items (Phase 2)  ───
PK = "TENANT#{tenantId}#DOC#{docId}"
SK = "VER#000001"              (version history)
SK = "LATEST"                  (pointer to current state)

── New collection items (Phase 3)  ──────
PK = "TENANT#{tenantId}#COL#{colId}"
SK = "METADATA"                (collection descriptor — name, color, visibilityMode, docCount)
SK = "DOC#{docId}"             (membership: one item per document in this collection)

── GSI indexes   ──
tenantId-updatedAt-index
    PK = tenantId    SK = updatedAt
    Used by: list-documents (SK=LATEST filter post-query)

entityType-tenantId-index
    PK = entityType  SK = tenantId
    Used by: list-collections (entityType="COLLECTION" + tenantId=X)
    Also present on: COLLECTION_MEMBER items (entityType="COLLECTION_MEMBER")
    FilterExpression SK="METADATA" in list-collections excludes DOC# membership rows.
```

### create-collection Lambda — Full Flow (lambdas/create-collection/index.py)

```
handler(event, context):
    _get_groups(event) → [g.strip() for g in claims["cognito:groups"].split(",")]
    if "internal" not in groups → 403

    body = json.loads(event["body"] or "{}")
    tenant_id   = body["tenantId"].strip()
    name        = body["name"].strip()
    description = body.get("description", "").strip()
    color       = body.get("color", "blue").strip()
    visibility  = body.get("visibilityMode", "INTERNAL_ONLY")

    if visibility not in ("INTERNAL_ONLY", "EXTERNAL_ALLOWED") → 400

    col_id = str(uuid.uuid4())    ← collision probability: ~1 in 5.3 × 10^36
    now    = datetime.now(timezone.utc).isoformat()

    dynamodb.put_item(
        TableName: TABLE_NAME,
        Item: {
            "PK":             "TENANT#{tenant_id}#COL#{col_id}",
            "SK":             "METADATA",
            "entityType":     "COLLECTION",    ← written for GSI partition key
            "tenantId":       tenant_id,       ← written for GSI sort key
            "colId":          col_id,
            "name":           name,
            "description":    description,
            "color":          color,           ← UI display: "blue", "purple" etc.
            "visibilityMode": visibility,      ← INTERNAL_ONLY or EXTERNAL_ALLOWED
            "docCount":       0,               ← starts at 0, maintained by transact_write
            "createdAt":      now,
            "updatedAt":      now,
        },
        ConditionExpression: "attribute_not_exists(PK)",
            ↑ Prevents UUID collision. If two concurrent creates used the same UUID
              (extraordinarily unlikely) the second would fail cleanly with
              ConditionalCheckFailedException rather than silently overwriting.
    )
    return 201 { colId, name, description, color, visibilityMode, docCount: 0, createdAt }
```

### list-collections Lambda — Full Flow (lambdas/list-collections/index.py)

```
Module-level:
    dynamodb_res = boto3.resource("dynamodb")    ← higher-level resource client
    _table = dynamodb_res.Table(TABLE_NAME)
        ↑ boto3 resource API uses Python-native types (str, int) instead of
          DynamoDB typed format ({"S": "..."}).
        ↑ list-documents uses boto3.client for explicit typed format control.
          list-collections uses boto3.resource for cleaner query syntax.
          Both are correct; the difference is developer preference per Lambda.

handler(event, context):
    Role check → 403 if not INTERNAL

    _table.query(
        IndexName: "entityType-tenantId-index",
        KeyConditionExpression: Key("entityType").eq("COLLECTION") & Key("tenantId").eq(tenant_id),
        FilterExpression: Key("SK").eq("METADATA"),
            ↑ KeyConditionExpression applies during index traversal (uses index structure).
            ↑ FilterExpression applies AFTER index traversal, before returning to caller.
            ↑ We need FilterExpression because the GSI also contains COLLECTION_MEMBER
              items (they also have entityType and tenantId). SK="METADATA" excludes them.
    )

    items.sort(key=lambda c: c["name"].lower())
        ↑ Client-side sort after GSI query. GSI sort key is tenantId, not name.
        ↑ At personal/small-team scale, N (collection count) is small. O(N log N) in Lambda.
        ↑ Alternative: another GSI with name as SK — overkill for this use case.

    _shape(item):
        Returns { colId, name, description, color, visibilityMode, docCount, createdAt, updatedAt }
        int(item.get("docCount", 0)) ← boto3.resource returns Decimal for numbers; cast to int.
```

### collection-membership Lambda — Full Flow (lambdas/collection-membership/index.py)

```
handler(event, context):
    Role check → 403 if not INTERNAL
    method = event["httpMethod"]
    col_id = event["pathParameters"]["colId"]

    method == "POST"   → _add(event, col_id)
    method == "DELETE" → _remove(event, col_id, doc_id, tenant_id)
    else               → 405

_add(event, col_id):
    body      = json.loads(event["body"])
    tenant_id = body["tenantId"]
    doc_id    = body["docId"]
    col_pk    = f"TENANT#{tenant_id}#COL#{col_id}"
    mem_sk    = f"DOC#{doc_id}"
    now       = datetime.now(timezone.utc).isoformat()

    dynamodb.transact_write_items(Items=[
        {
            "Put": {
                TableName: TABLE_NAME,
                Item: {
                    PK: col_pk, SK: mem_sk,
                    entityType: "COLLECTION_MEMBER",
                    tenantId, colId: col_id, docId: doc_id, addedAt: now,
                },
                ConditionExpression: "attribute_not_exists(PK)",
                    ↑ If this DOC# item already exists → TransactionCanceledException.
                    ↑ Prevents duplicate membership without a pre-read.
            }
        },
        {
            "Update": {
                TableName: TABLE_NAME,
                Key: { PK: col_pk, SK: "METADATA" },
                UpdateExpression: "SET docCount = docCount + :one, updatedAt = :now",
                ExpressionAttributeValues: { ":one": 1, ":now": now },
                ConditionExpression: "attribute_exists(PK)",
                    ↑ Collection must exist. If someone deletes the collection between
                    ↑ the UI render and this API call → TransactionCanceledException.
            }
        },
    ])
    ← If either condition fails → entire transaction cancelled → 409 returned.
    ← TransactWrite is atomic: either both succeed or both fail. No partial state.

_remove(event, col_id, doc_id, tenant_id):
    dynamodb.transact_write_items(Items=[
        {
            "Delete": {
                TableName: TABLE_NAME,
                Key: { PK: col_pk, SK: mem_sk },
                ConditionExpression: "attribute_exists(PK)",
                    ↑ Membership must exist before deleting it.
                    ↑ Prevents double-decrement of docCount if remove is called twice.
            }
        },
        {
            "Update": {
                TableName: TABLE_NAME,
                Key: { PK: col_pk, SK: "METADATA" },
                UpdateExpression: "SET docCount = if_not_exists(docCount, :zero) - :one, updatedAt = :now",
                    ↑ if_not_exists(:zero): if docCount was somehow missing, treat as 0.
                    ↑ Prevents AttributeNotExistsException on the arithmetic.
                    ↑ Note: this can make docCount go to -1 if called on a count-0 collection.
                    ↑ The optimistic UI update uses Math.max(0, ...) to floor at 0 client-side.
                ExpressionAttributeValues: { ":one": 1, ":zero": 0, ":now": now },
                ConditionExpression: "attribute_exists(PK)",
            }
        },
    ])
```

---

## CHECKPOINT E — Scoped Retrieval, Chunk Preview, Source Viewer, Citations

### rag-retrieve Lambda — Phase 3 Additions (lambdas/rag-retrieve/index.py)

```
Module-level additions:
    dynamodb_res  = boto3.resource("dynamodb")    ← for _resolve_collection_members
    DOCS_TABLE_NAME = os.environ.get("DOCS_TABLE_NAME", "")
        ↑ Empty string default: if not set, collection resolution is skipped gracefully.

VISIBILITY_BY_ROLE = {
    "INTERNAL": ("INTERNAL_ONLY", "EXTERNAL_ALLOWED"),
    "EXTERNAL": ("EXTERNAL_ALLOWED",),
}
    ↑ Tuple, not list. Immutable. Used as a lookup table.
    ↑ INTERNAL sees both visibility modes → all chunks.
    ↑ EXTERNAL sees only EXTERNAL_ALLOWED → restricted content.

handler(event, context):
    ...existing parsing...
    user_role     = body.get("userRole", "EXTERNAL").strip().upper()
    if user_role not in ("INTERNAL", "EXTERNAL") → user_role = "EXTERNAL"
        ↑ Normalisation. Agent passes correct value but any deviation defaults to EXTERNAL.
    doc_ids       = body.get("docIds")       # list[str] | None
    collection_id = body.get("collectionId") # str | None

retrieve(query, tenant_id, top_k, user_role, doc_ids, collection_id):

    ── Phase 3.4: collection resolution  
    resolved_doc_ids = []
    if collection_id and DOCS_TABLE_NAME:
        resolved_doc_ids = _resolve_collection_members(tenant_id, collection_id)
    if doc_ids:
        merged = set(resolved_doc_ids) | set(doc_ids)
        resolved_doc_ids = list(merged)
            ↑ set union: deduplicates doc_ids that appear in both the collection
            ↑ and the explicit docIds list.
            ↑ list() preserves determinism for SQL param binding order.

    ── Step 1: embed      ──
    query_vector = _embed_text(query)
        Unchanged from Phase 2D.

    ── Step 2: two-stage retrieval with visibility filter  ─
    fetch_limit = top_k * 2
    raw_chunks  = _vector_search(
        query_vector, tenant_id, fetch_limit,
        allowed_visibility=VISIBILITY_BY_ROLE[user_role],
        doc_ids=resolved_doc_ids,
    )

    ── Step 3: similarity threshold  ────
    filtered = [c for c in raw_chunks if c["similarity"] >= 0.30]
    final_chunks = filtered[:top_k]

    ── Step 4: role-aware context formatting    ────
    context_block = _format_context(final_chunks, user_role)

_resolve_collection_members(tenant_id, collection_id):
    table = dynamodb_res.Table(DOCS_TABLE_NAME)
    pk    = f"TENANT#{tenant_id}#COL#{collection_id}"

    resp = table.query(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("DOC#")
            ↑ begins_with on the SK uses the PK+SK B-tree index.
            ↑ Returns only membership items (SK=DOC#{docId}).
            ↑ Excludes the METADATA item (SK="METADATA") — begins_with("DOC#") fails for "METADATA".
    )
    return [item["SK"][4:] for item in resp["Items"]]
        ↑ item["SK"] = "DOC#abc-123-uuid"
        ↑ [4:] strips the "DOC#" prefix → "abc-123-uuid"
        ↑ O(N) list comprehension, N = members in collection.

_vector_search(query_vector, tenant_id, fetch_limit, allowed_visibility, doc_ids):
    ...existing SSM reads and vector literal serialization...

    ── Visibility IN clause    ───
    vis_params = [f":vis_{i}" for i in range(len(allowed_visibility))]
    vis_clause = ", ".join(vis_params)
        INTERNAL: vis_clause = ":vis_0, :vis_1"
        EXTERNAL: vis_clause = ":vis_0"

    ── doc_id scope clause    ────
    doc_clause = ""
    if doc_ids:
        doc_params_names = [f":doc_{i}" for i in range(len(doc_ids))]
        doc_clause = f"AND doc_id IN ({', '.join(doc_params_names)})"
            ↑ Only appended when doc_ids is non-empty.
            ↑ When empty: no doc filter → full tenant search (Phase 2D behaviour preserved).

    sql = f"""
        SELECT content, file_name, doc_id, chunk_index, chunk_total,
               page_number, section_title, source_type, visibility_mode,
               1 - (embedding <=> :query_vec::vector) AS similarity
        FROM  fast_chunks
        WHERE tenant_id      = :tenant_id
          AND visibility_mode IN ({vis_clause})
          {doc_clause}
        ORDER BY embedding <=> :query_vec::vector
        LIMIT :fetch_limit
    """
        ↑ doc_id now selected (new in Phase 3 — needed for INTERNAL citations).
        ↑ visibility_mode selected (new in Phase 3 — used for audit logging).

    params = [
        {"name": "query_vec",   "value": {"stringValue": vector_literal}},
        {"name": "tenant_id",   "value": {"stringValue": tenant_id}},
        {"name": "fetch_limit", "value": {"longValue":   fetch_limit}},
    ]
    for i, vis in enumerate(allowed_visibility):
        params.append({"name": f"vis_{i}", "value": {"stringValue": vis}})
    if doc_ids:
        for i, did in enumerate(doc_ids):
            params.append({"name": f"doc_{i}", "value": {"stringValue": did}})
        ↑ RDS Data API does not support array-typed parameters.
        ↑ Each value in the IN clause must be a separate named parameter.
        ↑ Named params prevent SQL injection (values never interpolated into SQL string).

_format_context(chunks, user_role):
    for chunk in chunks:
        file_name   = chunk["file_name"] or "unknown"
        doc_id      = chunk["doc_id"] or ""    ← now available (added to SELECT)
        page_num    = chunk["page_number"]
        chunk_index = chunk["chunk_index"]
        chunk_total = chunk["chunk_total"]
        content     = chunk["content"].strip()

        if user_role == "INTERNAL":
            page_part  = f", page {page_num}" if page_num is not None else ""
            chunk_part = f", chunk {chunk_index + 1}/{chunk_total}"
                ↑ chunk_index + 1: converts 0-based storage to 1-based display
                ↑ Frontend will convert back: parseInt(m[3]) - 1 to get 0-based for API
            doc_id_part = f", docId:{doc_id}" if doc_id else ""
            header = f"[Source: {file_name}{doc_id_part}{page_part}{chunk_part}]"
                → "[Source: attention.pdf, docId:abc-123, page 3, chunk 3/8]"
        else:
            header = "[Source: Internal Knowledge Base]"
                → External users see no file names, no structure

        parts.append(f"{header}\n{content}")

    return "\n---\n".join(parts)
```

### tool_spec.json — Phase 3 Additions

```
Original Phase 2D parameters: query (required), tenantId (required), topK (optional)

Phase 3 additions:
    "userRole": {
        "type": "string",
        "enum": ["INTERNAL", "EXTERNAL"],
        "description": "The user's access role... Always pass from user's JWT claims."
    }
        ↑ enum forces Claude to only use exactly these two values.
        ↑ "Always pass" is directive language — Claude reads this as a hard rule.

    "docIds": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Optional. Restrict to these specific document UUIDs..."
    }

    "collectionId": {
        "type": "string",
        "description": "Optional. Restrict to documents in this collection..."
    }

Required fields remain: ["query", "tenantId"]
    userRole, docIds, collectionId are optional — backward compatible with Phase 2D.
```

### CDK: RAG Retrieve Lambda additions (backend-stack.ts, createRagRetrieve)

```
ragRetrieveLambda environment additions:
    DOCS_TABLE_NAME: docsTable.tableName
        ↑ Needed by _resolve_collection_members to read collection memberships.

docsTable.grantReadData(ragRetrieveLambda)
    Grants: dynamodb:GetItem, Query, Scan, DescribeTable on the documents table.
    ↑ Required for _resolve_collection_members DynamoDB query call.

CDK: Preview Chunks Lambda:
    new lambda.Function(this, "PreviewChunksLambda", {
        functionName: "{stack}-preview-chunks",
        runtime:      PYTHON_3_13, architecture: ARM_64, timeout: 15s,
        memorySize:   256,
        code:         fromAsset("lambdas/preview-chunks/"),
        handler:      "index.handler",
        environment: {
            STACK_NAME:           stackName,
            CORS_ALLOWED_ORIGINS: "{frontendUrl},http://localhost:3000",
        },
    })

    previewChunksLambda.addToRolePolicy({
        actions:   ["rds-data:ExecuteStatement"],
        resources: ["*"],
    })
    previewChunksLambda.addToRolePolicy({
        actions:   ["secretsmanager:GetSecretValue"],
        resources: ["*"],
    })
    previewChunksLambda.addToRolePolicy({
        actions:   ["ssm:GetParameter"],
        resources: [`arn:aws:ssm:...parameter/${stackName}/rag/*`],
    })

    docItemResource.addResource("chunks")
        → adds /documents/{docId}/chunks path node

    chunksResource.addMethod("GET", LambdaIntegration(previewChunksLambda), {
        authorizer:        docsAuthorizer,
        authorizationType: COGNITO,
    })
```

### preview-chunks Lambda — Full Flow (lambdas/preview-chunks/index.py)

```
Module-level:
    rds_data   = boto3.client("rds-data")
    ssm        = boto3.client("ssm")
    _ssm_cache = {}           ← same pattern as ingestion-worker/rag-retrieve
    CHUNK_LIMIT_MAX = 20      ← hard cap to prevent huge responses

handler(event, context):
    Role check → 403 if not INTERNAL

    doc_id    = event["pathParameters"]["docId"]
    tenant_id = event["queryStringParameters"]["tenantId"]
    raw_limit  = event["queryStringParameters"].get("limit", "5")
    raw_anchor = event["queryStringParameters"].get("chunkIndex")   ← optional

    limit = min(int(raw_limit) if str(raw_limit).isdigit() else 5, CHUNK_LIMIT_MAX)

    db_cluster_arn = _get_ssm("aurora-cluster-arn")
    db_secret_arn  = _get_ssm("aurora-secret-arn")
    db_name        = _get_ssm("aurora-db-name")

    ── MODE SELECTION      ─
    if raw_anchor is not None and str(raw_anchor).lstrip("-").isdigit():
        ── ANCHOR MODE (SourceViewerDrawer)    ─────
        anchor = int(raw_anchor)           ← 0-based chunk index
        half   = limit // 2               ← e.g., limit=5 → half=2
        lo     = max(0, anchor - half)    ← floor at 0 (no negative indexes)
        hi     = anchor + half            ← no upper cap (chunk_total known in DB)

        sql = """
            SELECT chunk_index, chunk_total, content, page_number, section_title, source_type
            FROM   fast_chunks
            WHERE  tenant_id   = :tenant_id
              AND  doc_id      = :doc_id
              AND  chunk_index BETWEEN :lo AND :hi
            ORDER  BY chunk_index
        """
        params = [tenant_id, doc_id, lo, hi]
            ↑ BETWEEN returns a contiguous window [lo, hi] inclusive.
            ↑ One SQL round-trip for the entire window.
            ↑ ORDER BY chunk_index ensures anchor is always in the middle.
    else:
        ── PREVIEW MODE (DocumentDetailPanel)    ────
        sql = """
            SELECT chunk_index, chunk_total, content, page_number, section_title, source_type
            FROM   fast_chunks
            WHERE  tenant_id = :tenant_id
              AND  doc_id    = :doc_id
            ORDER  BY chunk_index
            LIMIT  :limit
        """
        params = [tenant_id, doc_id, limit]
            ↑ Returns first N chunks of the document.

    _row(columns, row):
        Converts RDS Data API typed cells to plain Python values.
        isNull → None
        stringValue → str
        longValue → int
        doubleValue → float
        ↑ Same pattern as _parse_rds_response in rag-retrieve.

    return 200 { chunks: [...], count: N }
```

### basic_agent.py — Phase 3 Changes

```
Import addition:
    from utils.role import get_user_role_from_context

create_basic_agent(user_id, session_id, user_role="EXTERNAL"):
    ← New parameter: user_role

    ── Role-aware system prompt    
    if user_role == "INTERNAL":
        citation_format_instruction = """CITATION FORMAT (internal user, full citations):
      [Source: <file_name>, docId:<doc_id>, page <page_number>, chunk <X>/<Y>]
      Example: "The encoder maps input... [Source: attention-paper.pdf, docId:abc-123, page 3, chunk 2/8]."
      The docId field enables the Source Viewer. Always include it when present."""
        retrieval_role_instruction = f"""Always pass tenantId="{user_id}" and userRole="INTERNAL" ..."""
    else:
        citation_format_instruction = """CITATION FORMAT (external user, masked):
      Answer based on retrieved knowledge. Do not expose document names or structure."""
        retrieval_role_instruction = f"""Always pass tenantId="{user_id}" and userRole="EXTERNAL" ..."""

    system_prompt = f"""
    ...
    DOCUMENT RETRIEVAL RULES:
    2. {retrieval_role_instruction}      ← bakes exact userRole into the LLM's instructions
    {citation_format_instruction}       ← bakes citation format into the LLM's instructions
    """
        ↑ The f-string is evaluated at agent creation time, not at tool call time.
        ↑ Claude reads the literal string "userRole='INTERNAL'" or "userRole='EXTERNAL'".
        ↑ This means Claude cannot accidentally omit userRole or use the wrong value.

    trace_attributes = {
        "user.id":    user_id,
        "user.role":  user_role,    ← NEW: role appears in CloudWatch traces
        "session.id": session_id,
    }

agent_stream(payload, context: RequestContext):
    user_id   = extract_user_id_from_context(context)       ← unchanged
    user_role = get_user_role_from_context(context)         ← NEW: extract role from JWT
        ↑ context.request_headers["Authorization"] = Bearer {id_token}
        ↑ jwt.decode(verify_signature=False) → claims → get_user_role(claims)
        ↑ INTERNAL/EXTERNAL from cognito:groups
    agent = create_basic_agent(user_id, session_id, user_role)  ← passes role
```

### MarkdownRenderer.tsx — Phase 3 Changes

```
CITATION_RE = /\[Source:\s*([^,\]]+?)(?:,\s*docId:([a-f0-9-]+))?(?:,\s*page\s*\d+)?(?:,\s*chunk\s*(\d+)\/\d+)?\]/g

Regex breakdown:
    \[Source:\s*             → literal "[Source:" + optional whitespace
    ([^,\]]+?)               → capture group 1: file name (lazy, stops at first comma or ])
    (?:,\s*docId:([a-f0-9-]+))?   → optional capture group 2: docId UUID
    (?:,\s*page\s*\d+)?     → optional non-capturing: page number (matched but not captured)
    (?:,\s*chunk\s*(\d+)\/\d+)? → optional capture group 3: chunk numerator (X in X/Y)
    \]                       → literal "]"
    /g                       → global flag: match all occurrences

interface ParsedCitation {
    raw:        string    ← full match text e.g. "[Source: attention.pdf, docId:xxx, page 3, chunk 3/8]"
    fileName:   string    ← m[1] = "attention.pdf"
    docId:      string    ← m[2] = "abc-123-uuid"
    chunkIndex: number    ← parseInt(m[3]) - 1 = 2  (0-based, for API call)
}

renderWithCitations(text, onClick):
    CITATION_RE.lastIndex = 0    ← CRITICAL RESET
        ↑ /g flag makes the regex object stateful.
        ↑ Multiple calls to exec() on the same regex advance lastIndex.
        ↑ Without reset: second call on a different string may start mid-string,
        ↑ skipping the first citation entirely.

    while ((m = CITATION_RE.exec(text)) !== null):
        docId = (m[2] ?? "").trim()

        push text from [lastIndex, m.index) as plain string

        if docId:    ← INTERNAL citation with docId
            citation = { raw: m[0], fileName: m[1].trim(), docId, chunkIndex: parseInt(m[3])-1 }
            push <button onClick={() => onClick(citation)}
                         className="... text-blue-600 bg-blue-50 ...">
                📄 {citation.fileName}
            </button>
        else:        ← EXTERNAL masked citation "[Source: Internal Knowledge Base]"
            push <span className="text-xs text-gray-400">{m[0]}</span>
                ↑ No button, no onClick, no docId → cannot open Source Viewer

        lastIndex = m.index + m[0].length

    push text from [lastIndex, text.length) as plain string

MarkdownRenderer({ content, onCitationClick }):
    hasCitations = onCitationClick && CITATION_RE.test(content)
    CITATION_RE.lastIndex = 0    ← reset after .test() (which also advances lastIndex)

    if hasCitations:
        return <p>{renderWithCitations(content, onCitationClick!)}</p>
    else:
        return <ReactMarkdown ...>{content}</ReactMarkdown>

    ↑ Two rendering paths:
    ↑ Citation path: plain <p> with interleaved text nodes and React button elements.
    ↑ Normal path:   ReactMarkdown with full AST rendering (GFM, syntax highlighting).
    ↑ Cannot mix: React button elements cannot be injected into ReactMarkdown's AST.
```

### ChatInterface.tsx — prop threading for citations

```
ChatMessages:
    onCitationClick={isInternal ? setSourceTarget : undefined}
        ↑ setSourceTarget has type (target: SourceViewerTarget | null) => void
        ↑ SourceViewerTarget = { docId, fileName, chunkIndex }
        ↑ matches ParsedCitation fields exactly — no adapter needed

ChatMessages → ChatMessage:
    onCitationClick?: OnCitationClick    ← optional prop threading

ChatMessage → MarkdownRenderer:
    onCitationClick={onCitationClick}
    ↑ When undefined: MarkdownRenderer skips citation rendering entirely.
    ↑ When defined (INTERNAL): citation chips render and clicking calls setSourceTarget.
```

### SourceViewerDrawer — Full Flow

```
Props: target: SourceViewerTarget | null, onClose: () => void

Sheet open condition: !!target
    ↑ null → false → Sheet closed
    ↑ non-null → true → Sheet open, slide in from right

useEffect [target, fetch]:
    if (target) → fetch(target)
    else        → setChunks([]); setError(null)   ← reset on close

fetch(target):
    idToken  = auth.user?.id_token ?? ""
    tenantId = auth.user?.profile?.sub ?? ""
    if (!idToken || !tenantId) return

    setLoading(true); setError(null); setChunks([])

    result = await previewChunks(target.docId, tenantId, idToken, 5, target.chunkIndex)
        ↑ limit=5: returns anchor ± 2 chunks (half = 5//2 = 2 in Lambda)
        ↑ chunkIndex: 0-based (already converted in MarkdownRenderer: parseInt(m[3])-1)
        → GET /documents/{docId}/chunks?tenantId=xxx&limit=5&chunkIndex=N
        → preview-chunks Lambda ANCHOR MODE
        → BETWEEN query: [max(0, N-2), N+2]

    setChunks(result.chunks)

Render per chunk:
    key={chunk.chunkIndex}
    className={cn(
        "p-3 rounded-lg border text-sm",
        chunk.chunkIndex === target?.chunkIndex
            ? "border-blue-300 bg-blue-50 text-gray-900"    ← cited chunk (anchor)
            : "border-gray-100 bg-white text-gray-600"       ← context chunks
    )}

    Header: "Chunk {chunk.chunkIndex + 1}/{chunk.chunkTotal}"
        ↑ +1 converts 0-based back to 1-based for display
    If chunk.pageNumber != null: "· Page {chunk.pageNumber}"
    If chunk.sectionTitle: "· {chunk.sectionTitle}" (truncated via CSS)
    If cited: <span className="ml-auto text-blue-600">cited</span>
```

---

## Complete AWS Service Interaction Summary — Phase 3

| AWS Service | Phase 3 Usage | Lambda/Component |
|-------------|---------------|-----------------|
| Amazon Cognito | Creates internal/external groups at deploy time | CDK (cognito-stack.ts) |
| Amazon Cognito | PostConfirmation trigger assigns external group on sign-up | post-confirmation Lambda |
| Amazon Cognito | JWT carries cognito:groups claim on every authenticated request | All API calls |
| API Gateway | Routes 7 new endpoints with Cognito Authorizer | backend-stack.ts |
| AWS Lambda | post-confirmation (Cognito trigger) | Checkpoint A |
| AWS Lambda | list-documents (GET /documents) | Checkpoint B |
| AWS Lambda | get-document (GET /documents/{docId}) | Checkpoint B |
| AWS Lambda | create-collection (POST /collections) | Checkpoint D |
| AWS Lambda | list-collections (GET /collections) | Checkpoint D |
| AWS Lambda | collection-membership (POST+DELETE /collections/{id}/documents) | Checkpoint D |
| AWS Lambda | preview-chunks (GET /documents/{docId}/chunks) | Checkpoint E |
| Amazon DynamoDB | Single table: documents + collection metadata + memberships | All backend |
| DynamoDB TransactWrite | Atomic membership add/remove + docCount update | collection-membership |
| DynamoDB BatchGetItem | Fetch LATEST + VER1 in one call | get-document |
| DynamoDB GSI (tenantId-updatedAt-index) | List documents per tenant newest-first | list-documents |
| DynamoDB GSI (entityType-tenantId-index) | List collections per tenant (NEW) | list-collections |
| Aurora Serverless v2 pgvector | BETWEEN query for chunk window (anchor mode) | preview-chunks |
| Aurora Serverless v2 pgvector | visibility_mode IN clause for RBAC filter | rag-retrieve |
| Aurora Serverless v2 pgvector | doc_id IN clause for scoped retrieval | rag-retrieve |
| RDS Data API | HTTPS+IAM access from preview-chunks and rag-retrieve | Checkpoint E |
| AWS SSM Parameter Store | aurora-cluster-arn, aurora-secret-arn, aurora-db-name | preview-chunks |
| Amazon Bedrock (Titan V2) | Query embedding — unchanged from Phase 2D | rag-retrieve |
| AgentCore Runtime | Passes user_role from JWT to agent | basic_agent.py |
| AgentCore Gateway | Routes rag_retrieve_documents with new userRole, docIds, collectionId params | tool_spec.json |
| AWS IAM | Minimum-necessary grants per Lambda | backend-stack.ts |

---

## DynamoDB Item Lifecycle — Dual-Table Tracking

```
DOCUMENT LIFECYCLE:
Upload → presign-upload Lambda → VER#000001 (status=UPLOADED) + LATEST (pointer)
Ingest → ingestion-worker Lambda → VER#000001 (status=READY, chunkCount=N) + LATEST (status=READY, chunkCount=N)
List   → list-documents Lambda → reads LATEST (status, chunkCount, updatedAt)
Detail → get-document Lambda → reads LATEST + VER#000001 (s3Key, contentType, createdAt)
Preview→ preview-chunks Lambda → reads Aurora fast_chunks (NOT DynamoDB)
Delete → not implemented (Phase 4 scope)

COLLECTION LIFECYCLE:
Create → create-collection Lambda → METADATA item (docCount=0)
List   → list-collections Lambda → reads all METADATA items for tenant
AddDoc → collection-membership Lambda → DOC#{docId} item + docCount+1 (atomic)
RemDoc → collection-membership Lambda → deletes DOC#{docId} + docCount-1 (atomic)
Scope  → rag-retrieve Lambda → reads all DOC# items for collection → extracts docIds
Delete → not implemented (Phase 4 scope)
```

---

## Critical Design Decisions — Phase 3 Specific

**Why visibility_mode is enforced in SQL (not just UI):**
The SQL WHERE clause `AND visibility_mode IN (:vis_0, ...)` runs inside Aurora.
Even if a crafted API call bypasses the frontend role check, the database-level filter
ensures EXTERNAL users cannot retrieve INTERNAL_ONLY content. Defense in depth.

**Why the agent passes userRole as a tool parameter:**
The rag-retrieve Lambda is invoked by the AgentCore Gateway with only the tool call payload.
The original browser HTTP request headers are not forwarded. The agent is the only component
with access to the user's JWT at that point in the call chain. The agent extracts role
from its own context (already validated by AgentCore Runtime) and injects it into the tool call.

**Why docCount is denormalized into METADATA instead of counted live:**
Counting membership items requires a DynamoDB query (begins_with "DOC#") then len() —
O(N) reads. Reading METADATA.docCount is O(1). At collection creation time and every
add/remove, the transact_write atomically keeps docCount accurate. The trade-off:
eventually consistent on crash between the two transact_write operations, but DynamoDB
TransactWrite is atomic — both items succeed or both fail. No partial update possible.

**Why hasFetchedRef uses useRef not useState in KnowledgeBaseSidebar:**
useState triggers re-renders on every setState call. Setting state inside a useEffect
dependency creates an infinite loop: effect runs → state changes → re-render → effect runs.
useRef mutations are invisible to React's reconciler. The guard flag can be set without
causing a re-render.

**Why CITATION_RE is a module-level constant (not created inside the function):**
Creating a regex inside a render function or callback would allocate a new regex object
on every call. Module-level means one object for the lifetime of the module. The trade-off:
the /g flag makes it stateful (lastIndex persists across calls). This is why lastIndex=0
resets are mandatory before every use.

**Why the Source Viewer converts chunkIndex: parseInt(m[3])-1 in the frontend:**
The Lambda stores chunks 0-indexed (chunk_index column: 0, 1, 2, ...).
The citation string displays 1-indexed for readability (chunk 1/8, not chunk 0/8).
The regex captures the display number. Subtracting 1 converts back to storage index.
The preview-chunks Lambda uses the 0-based index in the BETWEEN clause.
Keeping the conversion in one place (MarkdownRenderer) means no other component
needs to know about the indexing mismatch.
