# Phase 3 — Plain English Walkthrough
# FAST RAG + AgentCore: Knowledge Operating System

A plain-English explanation of everything built in Phase 3 — who can see what,
how documents are organised into collections, how the AI cites its sources, and
how all of this gets deployed without circular dependency errors.

---

## The Big Picture

Before Phase 3 the app had one type of user. After Phase 3 it has two:

| User type | Who they are | What they can do |
|-----------|-------------|-----------------|
| **INTERNAL** | Your company's staff | Chat + full document library + collections + source viewer |
| **EXTERNAL** | Client users who self-register | Chat only — no document library visible |

The AI knows which type is talking to it and adjusts its behaviour accordingly:
- INTERNAL users get cited answers: `[Source: report.pdf, page 4, chunk 3/21]` with a clickable chip that opens the exact passage.
- EXTERNAL users get clean answers with no internal file references exposed.

---

## Checkpoint A — Who Are You? (RBAC Foundation)

### The problem it solves
Without roles, every user sees everything. That's fine for a demo but not for a
real product where internal documents shouldn't be visible to clients.

### How it works — plain English

1. **Two Cognito groups are created**: `internal` (precedence 1) and `external` (precedence 10).
   Lower precedence number = higher priority, so if someone is accidentally in
   both groups they get INTERNAL access, not EXTERNAL.

2. **Self-registering users land in `external` automatically.** A Lambda called
   `post-confirmation` fires every time someone confirms their email after
   self-registering. It calls `AdminAddUserToGroup` to put them in the `external` group.
   If the Lambda fails for any reason, it logs the error silently — the sign-up still
   completes, and the role utility defaults unknown users to EXTERNAL (safe default).

3. **Admin-created users go into `internal` manually.** When an admin creates a user
   via the console or config, a CDK `CfnUserPoolUserToGroupAttachment` resource
   automatically assigns them to `internal`.

4. **The agent checks the role on every request** by decoding the Cognito JWT that
   arrives with each API call. No database lookup needed — the group is embedded in
   the token.

5. **The React frontend checks the role** by decoding the same JWT stored in the
   browser. If INTERNAL: the knowledge sidebar renders. If EXTERNAL: only the chat
   renders.

### The Cognito circular dependency — and how it was fixed

**The problem:** Wiring a Lambda trigger to an existing Cognito UserPool via CDK
always hits a CloudFormation circular dependency:

```
UserPool (update LambdaConfig) ←→ Lambda::Permission (references UserPool ARN)
```

CloudFormation cannot figure out which to create first.

**Three failed approaches:**
1. `lambdaTriggers` in the UserPool constructor — CDK auto-creates an intermediate
   resource that cycles back to the Lambda.
2. `CfnUserPool.addPropertyOverride("LambdaConfig.PostConfirmation", ...)` — still
   cycles because `Lambda::Permission` is in the same changeset update.
3. `addPermission` with `sourceAccount` instead of UserPool ARN — CFN still detects
   the implicit ordering dependency within the nested stack update.

**The fix that works — `AwsCustomResource`:**

Instead of wiring the trigger as a CloudFormation resource property, two CDK
`AwsCustomResource` constructs make direct AWS SDK calls **after** both the UserPool
and the Lambda are fully deployed:

```
Lambda deployed (no UserPool reference)
      ↓
UserPool deployed (no LambdaConfig yet — clean)
      ↓
Custom Resource 1: SDK call → UpdateUserPool(LambdaConfig.PostConfirmation = Lambda ARN)
Custom Resource 2: SDK call → AddPermission(cognito-idp.amazonaws.com can invoke Lambda)
```

The Custom Resources have explicit `node.addDependency()` calls so CloudFormation
always runs them last. On stack teardown, they reverse the calls (clear the trigger,
remove the permission).

**Important detail — `SourceArn` not `SourceAccount`:**

The Lambda resource-based policy must use `SourceArn` (the exact user pool ARN),
not `SourceAccount`. Cognito requires the invoking user pool ARN to match exactly
before it will invoke the trigger Lambda. Using `SourceAccount` alone is insufficient
and the trigger will silently not fire.

**Important detail — CDK construct ID uniqueness:**

If you change only `physicalResourceId` without changing the CDK construct ID,
CloudFormation treats it as an UPDATE (not a CREATE), so `onCreate` never re-runs.
Changing the CDK construct ID (e.g. `"PostConfirmationInvokePermissionV2"` →
`"PostConfirmationInvokePermissionV3"`) forces CloudFormation to treat it as a
brand-new resource and always runs `onCreate`.

---

## Checkpoint B — Document Library API

### The problem it solves
Users need to see their uploaded documents with status (uploading → processing → ready),
and drill into a specific document to see its metadata.

### How it works — plain English

Two new Lambda functions sit behind the existing Documents API Gateway:

- **`list-documents`** — returns a paginated list of all documents for the current
  tenant, sorted newest first. It queries DynamoDB's GSI (`tenantId-updatedAt-index`),
  filters for `SK = "LATEST"` rows only (skipping the version history rows), and
  returns up to N results per page with a `nextPageToken` for infinite scroll.

- **`get-document`** — returns full metadata for one document. It does a single
  `BatchGetItem` call to fetch both the `LATEST` record (status, chunk count) and
  the `VER#000001` record (S3 key, content type, upload timestamp) in one round-trip.

Both Lambdas extract the tenant ID from the Cognito JWT claims in the request
context — no separate lookup needed, the API Gateway Cognito authorizer already
validated the token.

---

## Checkpoint C — Knowledge Base Sidebar (Frontend)

### The problem it solves
INTERNAL users need a panel to browse their documents, see ingestion status,
and open document details — all without leaving the chat.

### How it works — plain English

A collapsible sidebar appears on the right side of the chat for INTERNAL users only.
It has two views:

- **List view**: shows all documents with their status badges. Scrolling to the
  bottom automatically loads the next page (`hasFetchedRef` pattern prevents
  double-fetching on React re-renders).

- **Detail view**: clicking a document switches to a panel showing file name,
  status, chunk count, S3 key, and upload time.

The `credentials()` callback is defined as a function (not a value) so the auth
token is read at the moment each API call fires — never a stale token from when
the component first mounted.

---

## Checkpoint D — Collections

### The problem it solves
Documents need to be organised into named groups (collections) so users can point
the AI at a specific subset: "answer only from the Q4 reports collection."

### How it works — plain English

Collections live in the **same DynamoDB table** as documents, using a different PK
prefix pattern (`TENANT#...#COL#...`). Each collection has:
- A `METADATA` record: name, description, visibility mode, doc count, timestamps
- One `DOC#...` membership record per document added to it

**Visibility modes:**
- `INTERNAL_ONLY` — only INTERNAL users can retrieve chunks from this collection
- `EXTERNAL_ALLOWED` — both user types can query it

**Adding/removing documents is atomic.** A `TransactWriteItems` call simultaneously:
- Puts or deletes the membership record
- Increments or decrements `docCount` on the collection's METADATA record

If either part fails, both roll back. You never get a membership record with a
wrong count.

Three new Lambdas handle collections:
- `create-collection` — validates visibility mode, generates UUID, writes with
  `attribute_not_exists(PK)` condition to guard against UUID collision
- `list-collections` — queries the `entityType-tenantId-index` GSI, filters for
  `SK = "METADATA"`, sorts client-side by name
- `collection-membership` — routes `POST` to add, `DELETE` to remove

---

## Checkpoint E — RAG Retrieval with Roles, Collections, and Citations

### The problem it solves
When the AI answers a question, it needs to:
1. Only retrieve chunks the current user is allowed to see (role + visibility mode)
2. Optionally scope the search to specific documents or collections
3. Cite its sources in a way that's useful to INTERNAL users and safe for EXTERNAL users

### How it works — plain English

**Step 1 — Resolve who can see what.**
The `rag-retrieve` Lambda receives the user's role, optional document IDs, and
optional collection ID. It builds two SQL filter clauses:

```
AND visibility_mode IN ('INTERNAL_ONLY', 'EXTERNAL_ALLOWED')   ← for INTERNAL
AND visibility_mode IN ('EXTERNAL_ALLOWED')                     ← for EXTERNAL
```

**Step 2 — Resolve collection members.**
If a collection ID is provided, a DynamoDB query fetches all `DOC#...` keys in
that collection. Those doc IDs are merged with any explicitly provided doc IDs
(union, deduplicated). The SQL then adds:

```
AND doc_id IN (:doc_0, :doc_1, ...)
```

**Step 3 — Vector search.**
The query text is embedded via Titan Embed V2 (1024 dimensions), then Aurora
pgvector runs a cosine similarity search with all the filters applied:

```sql
SELECT ... FROM fast_chunks
WHERE tenant_id = :tenant
AND visibility_mode IN (...)
AND doc_id IN (...)        -- only if scoped
ORDER BY embedding <=> :query_vec
LIMIT :k
```

**Step 4 — Format context with role-aware citations.**
- INTERNAL: `[Source: report.pdf, docId:abc-123, page 4, chunk 3/21]`
- EXTERNAL: `[Source: Internal Knowledge Base]`

**Step 5 — Frontend renders citations as chips.**
`MarkdownRenderer` scans the AI response for citation patterns using a regex.
For INTERNAL users, each citation with a `docId` becomes a clickable blue chip.
Clicking opens the `SourceViewerDrawer` — a right-side panel showing the actual
chunk text with the cited chunk highlighted in blue.

The drawer has two modes:
- **Preview mode** (no chunk index): shows the first 5 chunks of the document
- **Anchor mode** (chunk index present): fetches chunks surrounding the cited
  chunk and highlights it

---

## Q&A

**Q: What happens if a self-registering user's post-confirmation Lambda fails?**
The Lambda catches all exceptions, logs them to CloudWatch, and returns the Cognito
event unchanged. The sign-up succeeds. The user won't be in any group — `role.py`
defaults them to EXTERNAL. An admin can manually add them to a group later.

**Q: Can an EXTERNAL user ever see INTERNAL_ONLY chunks?**
No. The visibility filter is enforced in SQL on the Aurora side, not in the Lambda
or frontend. Even if someone forged a request with `userRole=INTERNAL`, the Lambda
reads the role from the validated Cognito JWT, not the request body.

**Q: Why is `docCount` stored on the collection instead of being counted live?**
Counting collection members live would require a DynamoDB Scan or a separate GSI
query on every list call. Denormalising the count as an attribute on the collection's
METADATA record makes the list-collections response O(1) per collection.

**Q: Why does the citation regex need `CITATION_RE.lastIndex = 0` before every use?**
The regex uses the `g` (global) flag. In JavaScript, a global regex maintains state
between calls via `lastIndex`. If you call `.test()` on it (which advances lastIndex),
then immediately call `.exec()`, it starts scanning from the wrong position. Resetting
`lastIndex = 0` before every use is mandatory.

**Q: Why use `useRef` instead of `useState` for the fetch guard in the sidebar?**
`useState` causes a re-render when updated. A re-render would re-trigger the
`useEffect`, creating an infinite fetch loop. `useRef` mutates without triggering
a re-render, making it the right tool for a "did we already fetch?" guard.
