# Document Upload Flow — Complete Function Call Reference

This document traces every single function call, state change, AWS service interaction,
async/await operation, and React state update from the moment the user clicks the
paperclip button in ChatInput.tsx until the file is confirmed uploaded in S3.

---

## Files Involved

| File | Role |
|------|------|
| `frontend/src/components/chat/ChatInput.tsx` | Renders paperclip button, fires onUploadClick prop |
| `frontend/src/components/chat/ChatInterface.tsx` | Owns isUploadPanelOpen state, renders DocumentUploadPanel |
| `frontend/src/components/chat/DocumentUploadPanel.tsx` | Upload UI, drag/drop, progress bar, calls uploadDocument |
| `frontend/src/services/documentService.ts` | All upload logic: presign request + S3 PUT |
| `infra-cdk/lambdas/presign-upload/index.py` | Lambda: generates presigned URL + writes DynamoDB |
| `infra-cdk/lib/backend-stack.ts` | CDK: provisions S3, DynamoDB, Lambda, API Gateway |

---

## PHASE 0 — CDK Infrastructure (Deploy Time, NOT Runtime)

Before any user interaction, the CDK stack provisions all AWS resources.

```
backend-stack.ts
└── constructor()
    └── this.createDocumentUploadInfra(config, frontendUrl)
        │
        ├── new s3.Bucket()  ──────────────────────────────────────────────────────────────
        │     name:       "{stack_name_base}-raw-docs"
        │     encryption: S3_MANAGED
        │     cors:       PUT allowed from frontendUrl + localhost:3000
        │     blockPublicAccess: BLOCK_ALL
        │     AWS SERVICE: Amazon S3
        │
        ├── new ssm.StringParameter()  ────────────────────────────────────────────────────
        │     parameterName: "/{stack}/rag/docs-bucket-name"
        │     AWS SERVICE: AWS SSM Parameter Store
        │
        ├── new dynamodb.Table()  ─────────────────────────────────────────────────────────
        │     tableName:     "{stack_name_base}-documents"
        │     partitionKey:  PK (STRING)
        │     sortKey:       SK (STRING)
        │     billing:       PAY_PER_REQUEST
        │     encryption:    AWS_MANAGED
        │     PITR:          enabled
        │     AWS SERVICE: Amazon DynamoDB
        │
        ├── docsTable.addGlobalSecondaryIndex()
        │     indexName:    "tenantId-updatedAt-index"
        │     partitionKey: tenantId
        │     sortKey:      updatedAt
        │
        ├── new ssm.StringParameter()
        │     parameterName: "/{stack}/rag/docs-table-name"
        │
        ├── new PythonFunction()  ─────────────────────────────────────────────────────────
        │     functionName: "{stack_name_base}-presign-upload"
        │     runtime:      PYTHON_3_13
        │     entry:        lambdas/presign-upload/
        │     handler:      handler
        │     arch:         ARM_64
        │     timeout:      30s
        │     env vars:
        │       DOCS_BUCKET_NAME = rawDocsBucket.bucketName
        │       DOCS_TABLE_NAME  = docsTable.tableName
        │       AWS_ACCOUNT_ID   = cdk.Aws.ACCOUNT_ID
        │     AWS SERVICE: AWS Lambda
        │
        ├── rawDocsBucket.grantPut(presignLambda)
        │     Grants Lambda s3:PutObject on the bucket
        │     AWS SERVICE: IAM (inline policy on Lambda role)
        │
        ├── docsTable.grantReadWriteData(presignLambda)
        │     Grants Lambda dynamodb:GetItem, PutItem, UpdateItem etc.
        │     AWS SERVICE: IAM
        │
        ├── new apigateway.RestApi()  ─────────────────────────────────────────────────────
        │     restApiName: "{stack_name_base}-docs-api"
        │     stageName:   "prod"
        │     CORS:        frontendUrl + localhost:3000
        │     logging:     INFO level + access logs
        │     tracing:     X-Ray enabled
        │     AWS SERVICE: Amazon API Gateway
        │
        ├── new apigateway.CognitoUserPoolsAuthorizer()
        │     cognitoUserPools: [this.userPool]
        │     identitySource:   method.request.header.Authorization
        │     AWS SERVICE: Amazon Cognito + API Gateway Authorizer
        │
        ├── documentsResource = docsApi.root.addResource("documents")
        ├── presignResource   = documentsResource.addResource("presign")
        ├── presignResource.addMethod("POST", LambdaIntegration(presignLambda))
        │     authorizer:     docsAuthorizer (Cognito)
        │     authType:       COGNITO
        │
        └── new ssm.StringParameter()
              parameterName: "/{stack}/rag/docs-api-url"
              stringValue:   docsApi.url
              AWS SERVICE: AWS SSM Parameter Store
```

---

## PHASE 1 — App Startup (Browser loads, ChatInterface mounts)

```
ChatInterface.tsx mounts
└── useEffect(loadConfig, [])   ← runs once on mount
    └── async loadConfig()
        └── await fetch("/aws-exports.json")
              ↓ reads docsApiUrl from the JSON file
              ↓ (this URL was written by CDK deploy scripts from SSM)
              ↓ also reads agentRuntimeArn, awsRegion, agentPattern
            setClient(new AgentCoreClient(...))   ← React state update
```

React state initialized in ChatInterface:
- `messages` = []
- `input` = ""
- `isUploadPanelOpen` = false
- `isLoading` = false (from GlobalContext)
- `client` = null → AgentCoreClient after loadConfig

---

## PHASE 2 — User Clicks the Paperclip Button

```
ChatInput.tsx
└── <Button onClick={onUploadClick}>   ← user clicks paperclip icon
      │
      │  onUploadClick is a PROP passed down from ChatInterface:
      │  onUploadClick={() => setIsUploadPanelOpen((prev) => !prev)}
      │
      ▼
ChatInterface.tsx
└── setIsUploadPanelOpen((prev) => !prev)
      │
      │  REACT STATE CHANGE:
      │  isUploadPanelOpen: false → true
      │
      ▼
      React re-renders ChatInterface
      └── inputArea JSX now includes:
            {isUploadPanelOpen && (
              <DocumentUploadPanel
                onClose={() => setIsUploadPanelOpen(false)}
                onUploadSuccess={handleUploadSuccess}
              />
            )}
```

Also: ChatInput receives `isUploadPanelOpen={true}` as prop
- paperclip button changes CSS class to blue (active state)
- `aria-label` changes to "Close upload panel"

---

## PHASE 3 — DocumentUploadPanel Mounts

```
DocumentUploadPanel.tsx mounts
│
├── useState<UploadState>({ status: "idle" })
│     UploadState union type:
│       | { status: "idle" }
│       | { status: "selected"; file: File }
│       | { status: "uploading"; file: File; progress: number }
│       | { status: "success"; file: File; doc: UploadedDocument }
│       | { status: "error"; file: File; message: string }
│
├── useState(isDragOver = false)
├── useRef(fileInputRef)   ← hidden <input type="file">
└── useAuth()              ← reads Cognito session from react-oidc-context
      provides: auth.user?.id_token
                auth.user?.profile?.sub  (used as tenantId)
```

Panel renders in "idle" state:
- Drag & drop zone visible
- Hidden `<input type="file" accept=".txt,.pdf,.docx,.doc,.csv,.md,.json,.xlsx,.pptx">`

---

## PHASE 4A — User Drags a File (Drag & Drop path)

```
DocumentUploadPanel.tsx
│
├── onDragOver={handleDragOver}
│     └── handleDragOver(e: DragEvent)
│           e.preventDefault()           ← prevents browser default (open file)
│           setIsDragOver(true)          ← REACT STATE: isDragOver = true
│                                           (turns drop zone blue)
│
├── onDragLeave={handleDragLeave}
│     └── handleDragLeave(e: DragEvent)
│           e.preventDefault()
│           setIsDragOver(false)         ← REACT STATE: isDragOver = false
│
└── onDrop={handleDrop}
      └── handleDrop(e: DragEvent)       ← useCallback, stable reference
            e.preventDefault()
            setIsDragOver(false)         ← REACT STATE: isDragOver = false
            const file = e.dataTransfer.files[0]
            if (file) selectFile(file)   ── calls selectFile()
```

---

## PHASE 4B — User Clicks to Browse (File Picker path)

```
DocumentUploadPanel.tsx
└── onClick={() => fileInputRef.current?.click()}   ← div click
      └── fileInputRef.current.click()              ← programmatically opens OS file picker
            │
            │  user selects file in OS dialog
            │
            ▼
      onChange={handleFileChange}
      └── handleFileChange(e: ChangeEvent<HTMLInputElement>)
            const file = e.target.files?.[0]
            if (file) selectFile(file)
            e.target.value = ""    ← reset so same file can be re-selected
```

---

## PHASE 5 — selectFile() — File Validation

```
DocumentUploadPanel.tsx
└── selectFile(file: File)
      └── validateFile(file)
            │
            ├── checks extension against ACCEPTED_EXTENSIONS:
            │     [".txt",".pdf",".docx",".doc",".csv",".md",".json",".xlsx",".pptx"]
            │
            ├── checks file.size > MAX_FILE_SIZE_MB * 1024 * 1024  (50MB limit)
            │
            └── returns: string (error message) | null (valid)

      IF error:
        setState({ status: "error", file, message: error })
        ← REACT STATE: UploadState = { status: "error", ... }
        Panel renders error UI

      IF valid:
        setState({ status: "selected", file })
        ← REACT STATE: UploadState = { status: "selected", file }
        Panel renders file preview + Upload button
```

---

## PHASE 6 — User Clicks "Upload" Button

```
DocumentUploadPanel.tsx
└── <Button onClick={handleUpload}>Upload</Button>
      └── async handleUpload()
            │
            ├── guard: if (state.status !== "selected") return
            │
            ├── const { file } = state
            │
            ├── const idToken  = auth.user?.id_token
            │     ↑ Cognito ID token from react-oidc-context session
            │     ↑ Used for API Gateway Cognito Authorizer
            │
            ├── const tenantId = auth.user?.profile?.sub
            │     ↑ Cognito user's "sub" claim = unique user ID
            │     ↑ Used as tenantId in DynamoDB PK
            │
            ├── IF !idToken || !tenantId:
            │     setState({ status: "error", message: "Authentication required..." })
            │     return
            │
            ├── setState({ status: "uploading", file, progress: 0 })
            │     ← REACT STATE: UploadState = { status: "uploading", progress: 0 }
            │     Panel renders progress bar at 0%
            │
            └── await uploadDocument(file, tenantId, idToken, onProgress)
                  ↓ calls into documentService.ts
```

---

## PHASE 7 — uploadDocument() in documentService.ts

```
documentService.ts
└── async uploadDocument(file, tenantId, idToken, onProgress)
      │
      ├── getContentType(file)
      │     ↓ pure function, no async
      │     const ext = file.name.split(".").pop()?.toLowerCase()
      │     looks up ext in hardcoded map:
      │       txt  → "text/plain"
      │       pdf  → "application/pdf"
      │       docx → "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
      │       doc  → "application/msword"
      │       csv  → "text/csv"
      │       md   → "text/markdown"
      │       json → "application/json"
      │       xlsx → "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
      │       pptx → "application/vnd.openxmlformats-officedocument.presentationml.presentation"
      │     fallback: file.type ?? "application/octet-stream"
      │     returns: contentType string
      │
      ├── onProgress?.(0)
      │     ← REACT STATE in DocumentUploadPanel:
      │       setState({ status: "uploading", file, progress: 0 })
      │       progress bar renders at 0%
      │
      ├── await getPresignedUploadUrl({ fileName, contentType, tenantId }, idToken)
      │     ↓ see PHASE 8 below
      │     returns: PresignResponse { uploadUrl, docId, version, s3Key }
      │
      ├── await uploadFileToS3(file, presign.uploadUrl, contentType, progressCallback)
      │     progressCallback = (pct) => onProgress?.(10 + Math.round(pct * 0.9))
      │     ↑ maps S3 upload 0-100% to overall 10-100%
      │     ↑ 0-10% was "reserved" for the presign step
      │     ↓ see PHASE 9 below
      │
      └── returns UploadedDocument:
            {
              docId:    presign.docId,
              version:  presign.version,
              fileName: file.name,
              s3Key:    presign.s3Key,
              status:   "uploaded"
            }
```

---

## PHASE 8 — getPresignedUploadUrl() → API Gateway → Lambda

### 8A — loadDocsApiUrl() (module-level cache)

```
documentService.ts
└── async getPresignedUploadUrl(request, idToken)
      └── await loadDocsApiUrl()
            │
            ├── IF DOCS_API_URL already cached → return immediately (no fetch)
            │
            └── IF empty:
                  await fetch("/aws-exports.json")
                  ↓ reads local JSON file served by frontend
                  const config = await response.json()
                  DOCS_API_URL = config.docsApiUrl + "documents/presign"
                  ↑ example: "https://abc123.execute-api.us-east-1.amazonaws.com/prod/documents/presign"
                  returns DOCS_API_URL
```

### 8B — HTTP POST to API Gateway

```
documentService.ts
└── async getPresignedUploadUrl(request, idToken)
      └── await fetch(apiUrl, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Authorization:  "Bearer <id_token>"   ← Cognito ID token
              },
              body: JSON.stringify({
                fileName:    file.name,
                contentType: "application/pdf",   (example)
                tenantId:    "cognito-sub-uuid"
              })
            })

      AWS SERVICE: Amazon API Gateway
      Route: POST /documents/presign
      │
      ├── API Gateway checks Authorization header
      │     └── CognitoUserPoolsAuthorizer validates id_token
      │           ↓ calls Cognito JWKS endpoint to verify JWT signature
      │           ↓ checks token expiry, audience, issuer
      │           AWS SERVICE: Amazon Cognito (token validation)
      │           IF invalid → 401 Unauthorized (never reaches Lambda)
      │           IF valid   → passes event to Lambda
      │
      └── API Gateway invokes presignLambda
            AWS SERVICE: AWS Lambda (synchronous invoke)
```

### 8C — Lambda handler() in index.py

```
infra-cdk/lambdas/presign-upload/index.py
└── handler(event, context)
      │
      ├── json.loads(event.get("body", "{}"))
      │     parses the POST body sent by the browser
      │
      ├── extracts:
      │     file_name    = body["fileName"]
      │     content_type = body["contentType"]
      │     tenant_id    = body["tenantId"]
      │     metadata     = body.get("metadata", {})
      │     doc_id       = body.get("docId") or str(uuid.uuid4())
      │                    ↑ auto-generates UUID if new document
      │
      ├── VALIDATION:
      │     if not file_name or not tenant_id:
      │       return _error(400, "fileName and tenantId are required")
      │
      ├── BUILD DynamoDB KEY:
      │     latest_pk = f"TENANT#{tenant_id}#DOC#{doc_id}"
      │     latest_sk = "LATEST"
      │
      ├── VERSION LOOKUP — DynamoDB GetItem
      │     AWS SERVICE: Amazon DynamoDB
      │     table.get_item(Key={"PK": latest_pk, "SK": "LATEST"})
      │     │
      │     ├── IF item exists:
      │     │     version = item["latestVersion"] + 1
      │     │     (incrementing version for re-upload of same doc)
      │     │
      │     └── IF no item (first upload ever):
      │           version = 1
      │
      ├── BUILD S3 KEY:
      │     s3_key = f"raw-docs/{tenant_id}/{doc_id}/v{version}/{file_name}"
      │     example: "raw-docs/abc-123/doc-uuid/v1/policy.pdf"
      │
      ├── GENERATE PRESIGNED URL — S3 generate_presigned_url
      │     AWS SERVICE: Amazon S3
      │     s3.generate_presigned_url(
      │       "put_object",
      │       Params={
      │         "Bucket":      DOCS_BUCKET_NAME,   ← from env var
      │         "Key":         s3_key,
      │         "ContentType": content_type,
      │       },
      │       ExpiresIn=900   ← URL valid for 15 minutes
      │     )
      │     returns: upload_url (pre-signed HTTPS PUT URL)
      │     NOTE: URL contains AWS SigV4 signature embedded as query params
      │           No Authorization header needed when using this URL
      │
      ├── WRITE DynamoDB — TransactWrite (atomic, 2 items)
      │     AWS SERVICE: Amazon DynamoDB (TransactWriteItems)
      │     dynamodb.meta.client.transact_write(TransactItems=[
      │
      │       Item 1 — Version History Record:
      │         PK:          "TENANT#{tenant_id}#DOC#{doc_id}"
      │         SK:          "VER#000001"   (zero-padded for sort order)
      │         tenantId:    tenant_id
      │         docId:       doc_id
      │         version:     1
      │         status:      "UPLOADED"
      │         s3Key:       s3_key
      │         fileName:    file_name
      │         contentType: content_type
      │         metadata:    {}
      │         createdAt:   ISO timestamp
      │         updatedAt:   ISO timestamp
      │         ConditionExpression: "attribute_not_exists(PK)"
      │           ↑ safety guard: prevents overwriting existing version
      │
      │       Item 2 — LATEST Pointer Record:
      │         PK:            "TENANT#{tenant_id}#DOC#{doc_id}"
      │         SK:            "LATEST"
      │         tenantId:      tenant_id
      │         docId:         doc_id
      │         latestVersion: 1
      │         fileName:      file_name
      │         updatedAt:     ISO timestamp
      │         (no condition — LATEST is always overwritten)
      │     ])
      │
      │     IF TransactionCanceledException:
      │       return _error(409, "Version conflict...")
      │     IF other Exception:
      │       return _error(500, "Failed to write DynamoDB records...")
      │
      └── RETURN HTTP 200:
            {
              "statusCode": 200,
              "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
              },
              "body": JSON.dumps({
                "uploadUrl": upload_url,
                "docId":     doc_id,
                "version":   1,
                "s3Key":     s3_key
              })
            }
```

### 8D — Back in getPresignedUploadUrl()

```
documentService.ts
└── getPresignedUploadUrl()
      └── response = await fetch(...)   ← Lambda response arrives
            │
            ├── IF !response.ok:
            │     const errorData = await response.json().catch(() => ({}))
            │     throw new Error(errorData.error || "Presign failed: HTTP {status}")
            │     ← caught by handleUpload() in DocumentUploadPanel
            │
            └── return response.json() as Promise<PresignResponse>
                  returns: { uploadUrl, docId, version, s3Key }
```

---

## PHASE 9 — uploadFileToS3() — Direct Browser to S3 Upload

```
documentService.ts
└── async uploadFileToS3(file, uploadUrl, contentType, onProgress)
      │
      │  NOTE: This function wraps XHR in a Promise manually
      │        because XHR gives progress events, fetch() does not
      │
      └── return new Promise((resolve, reject) => {
              const xhr = new XMLHttpRequest()

              ── EVENT LISTENER 1: Progress ──────────────────────────────────
              xhr.upload.addEventListener("progress", (event) => {
                if (event.lengthComputable && onProgress) {
                  const percent = Math.round((event.loaded / event.total) * 100)
                  onProgress(percent)
                  │
                  │  onProgress callback chain:
                  │  uploadFileToS3 onProgress(pct)
                  │    → uploadDocument callback: onProgress(10 + Math.round(pct * 0.9))
                  │      → DocumentUploadPanel callback:
                  │          setState({ status: "uploading", file, progress: newPct })
                  │          ← REACT STATE UPDATE on every progress tick
                  │          Panel re-renders progress bar with new percentage
                  │
                }
              })

              ── EVENT LISTENER 2: Load (complete) ───────────────────────────
              xhr.addEventListener("load", () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                  onProgress?.(100)   ← fires final 100% progress
                  resolve()           ← Promise resolves, uploadFileToS3 returns
                } else {
                  reject(new Error("S3 upload failed: HTTP " + xhr.status))
                }
              })

              ── EVENT LISTENER 3: Network Error ─────────────────────────────
              xhr.addEventListener("error", () => {
                reject(new Error("S3 upload failed: network error"))
              })

              ── OPEN + SEND ──────────────────────────────────────────────────
              xhr.open("PUT", uploadUrl)
              │  uploadUrl = presigned S3 URL with embedded SigV4 signature
              │  AWS SERVICE: Amazon S3 (direct PUT, no Lambda involved)
              │
              xhr.setRequestHeader("Content-Type", contentType)
              │  MUST match ContentType used when generating presigned URL
              │  S3 validates this — mismatch = 403 SignatureDoesNotMatch
              │
              xhr.send(file)
              │  sends raw file bytes directly to S3
              │  browser → S3 (no backend server in the middle)
            })
```

---

## PHASE 10 — Upload Complete — State Propagation Back Up

```
documentService.ts
└── uploadDocument() returns UploadedDocument:
      { docId, version, fileName, s3Key, status: "uploaded" }
      ↓
DocumentUploadPanel.tsx
└── handleUpload() receives resolved value
      └── setState({ status: "success", file, doc })
            ← REACT STATE: UploadState = { status: "success", ... }
            Panel renders success UI:
              - green checkmark icon
              - "Uploaded successfully — processing will begin shortly"
              - doc.docId (first 8 chars) + version number
              - "Upload another" button → handleReset() → setState({ status: "idle" })
              - "Done" button → onClose() prop
      │
      └── onUploadSuccess?.(doc)
            ↓ prop callback fires up to ChatInterface.tsx
            └── handleUploadSuccess(doc: UploadedDocument)
                  │
                  ├── setIsUploadPanelOpen(false)
                  │     ← REACT STATE: isUploadPanelOpen = false
                  │     DocumentUploadPanel unmounts
                  │     ChatInput paperclip returns to grey (inactive)
                  │
                  └── setMessages((prev) => [...prev, uploadMsg])
                        ← REACT STATE: messages array gets new entry
                        uploadMsg = {
                          role:      "user",
                          content:   "📎 Uploaded: **{fileName}** (v{version}) — processing will begin shortly...",
                          timestamp: new Date().toISOString()
                        }
                        Chat renders this message in the conversation
```

---

## PHASE 11 — Error Handling Paths

```
── Error in getPresignedUploadUrl() ────────────────────────────────────────────
  fetch() fails OR response.ok = false
    → throw new Error(...)
    → caught in handleUpload() try/catch
    → setState({ status: "error", file, message })
    ← REACT STATE: UploadState = { status: "error" }
    Panel renders red error UI with message
    "Try again" button → handleReset() → setState({ status: "idle" })

── Error in uploadFileToS3() ───────────────────────────────────────────────────
  xhr "error" event fires (network failure)
    → reject(new Error("S3 upload failed: network error"))
    → Promise rejects
    → caught in handleUpload() try/catch
    → setState({ status: "error", file, message })

  xhr "load" event fires but status >= 300
    → reject(new Error("S3 upload failed: HTTP {status}"))
    Common causes:
      403 = ContentType mismatch with presigned URL params
      403 = Presigned URL expired (> 15 min old)
      403 = CORS not configured on S3 bucket

── Lambda DynamoDB TransactionCanceledException ────────────────────────────────
  409 returned from API Gateway
    → getPresignedUploadUrl throws Error("Version conflict...")
    → propagates up through uploadDocument → handleUpload
    → setState({ status: "error", ... })

── Authentication errors ────────────────────────────────────────────────────────
  auth.user?.id_token is null/undefined
    → handleUpload() catches this BEFORE calling uploadDocument
    → setState({ status: "error", message: "Authentication required..." })

  id_token expired → API Gateway Cognito Authorizer rejects
    → 401 response from fetch()
    → getPresignedUploadUrl throws
    → handleUpload catches → setState error
```

---

## PHASE 12 — User Closes Panel Without Uploading

```
DocumentUploadPanel.tsx
└── <button onClick={onClose}>X</button>   ← header X button
      └── onClose()   ← prop from ChatInterface
            └── setIsUploadPanelOpen(false)
                  ← REACT STATE: isUploadPanelOpen = false
                  DocumentUploadPanel unmounts
                  All local state (UploadState, isDragOver) is destroyed
```

Also triggered by:
- ChatInput paperclip click when panel is already open
  → `setIsUploadPanelOpen((prev) => !prev)` → false
- `startNewChat()` in ChatInterface
  → `setIsUploadPanelOpen(false)` directly

---

## Complete State Machine — UploadState

```
                    ┌─────────────────────────────────────────┐
                    │                                         │
                    ▼                                         │
              ┌──────────┐                                    │
              │   idle   │ ◄── handleReset() ─────────────────┤
              └────┬─────┘                                    │
                   │ selectFile(file) — validation passes     │
                   ▼                                          │
           ┌──────────────┐                                   │
           │   selected   │ ──── validateFile fails ──────────┤
           └──────┬───────┘                                   │
                  │ handleUpload() called                     │
                  ▼                                           │
          ┌───────────────┐                                   │
          │   uploading   │ ──── fetch/XHR error ─────────────┤
          │  progress 0→  │                                   │
          │     100%      │                                   │
          └──────┬────────┘                                   │
                 │ uploadDocument resolves                    │
                 ▼                                            │
          ┌─────────────┐                                     │
          │   success   │ ──── "Upload another" ─────────────►┘
          └─────────────┘
                 │
                 └── "Done" → onClose() → panel unmounts

          ┌─────────────┐
          │    error    │ ──── "Try again" → handleReset() → idle
          └─────────────┘
```

---

## React State Summary — All State Variables

| Component | State Variable | Type | Changes When |
|-----------|---------------|------|--------------|
| ChatInterface | `isUploadPanelOpen` | boolean | paperclip click, onClose, startNewChat, handleUploadSuccess |
| ChatInterface | `messages` | Message[] | handleUploadSuccess adds upload confirmation message |
| ChatInterface | `isLoading` | boolean (GlobalContext) | not changed during upload flow |
| DocumentUploadPanel | `state` (UploadState) | union type | file select, validation, upload start, progress, success, error |
| DocumentUploadPanel | `isDragOver` | boolean | dragover/dragleave/drop events |

---

## AWS Services Interaction Summary

| AWS Service | When | What |
|-------------|------|------|
| Amazon Cognito | Phase 8B | API Gateway validates id_token JWT signature + expiry |
| Amazon API Gateway | Phase 8B | Routes POST /documents/presign to Lambda |
| AWS Lambda | Phase 8C | Runs handler(), generates presigned URL, writes DynamoDB |
| Amazon DynamoDB | Phase 8C (GetItem) | Looks up existing doc to determine version number |
| Amazon DynamoDB | Phase 8C (TransactWrite) | Atomically writes VER record + LATEST pointer |
| Amazon S3 | Phase 8C | generate_presigned_url() creates signed PUT URL |
| Amazon S3 | Phase 9 | Browser PUTs file bytes directly using presigned URL |
| AWS SSM Parameter Store | Deploy time | Stores bucket name, table name, API URL |
| AWS IAM | Deploy time | Lambda role gets s3:PutObject + dynamodb:ReadWriteData |
| AWS X-Ray | Runtime | Traces API Gateway + Lambda invocations |

---

## Key Design Decisions

**Why presigned URL instead of uploading through Lambda?**
Lambda has a 6MB payload limit for synchronous invocations via API Gateway.
A presigned URL lets the browser PUT directly to S3 — no size limit, no Lambda bottleneck.

**Why id_token and not access_token for API Gateway?**
API Gateway's Cognito Authorizer validates ID tokens (contains user identity claims like `sub`).
Access tokens are for authorizing API calls to resource servers, not for identity.

**Why XHR instead of fetch() for S3 upload?**
The Fetch API does not expose upload progress events. XHR's `xhr.upload.progress` event
fires repeatedly during the upload, enabling the 0-100% progress bar.

**Why TransactWrite for DynamoDB?**
Two records (VER + LATEST) must be written atomically. If one fails, both fail.
This prevents a state where LATEST points to a version record that doesn't exist.

**Why `attribute_not_exists(PK)` condition on VER record?**
Version numbers must be unique. The condition prevents a race condition where two
simultaneous uploads of the same doc could create duplicate VER#000001 records.
