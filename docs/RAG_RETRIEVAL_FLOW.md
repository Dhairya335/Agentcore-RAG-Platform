# Phase 2D — RAG Retrieval Flow: Complete Function Call Reference

This document traces every function call, AWS service interaction, state change, IAM grant,
and data transformation from the moment the user sends a chat message, all the way through
the Strands agent calling the rag_retrieve_documents tool, until a grounded answer with
inline source citations is streamed back to the browser.

This document is a continuation of INGESTION_PIPELINE_FLOW.md. Phase 2C ends when Aurora
has rows in fast_chunks and DynamoDB VER#000001.status = "READY". Phase 2D begins when the
user types a question into the chat input.

---

## Files Involved

| File | Role |
|------|------|
| `frontend/src/components/chat/ChatInterface.tsx` | Sends user query to AgentCore Runtime, streams response |
| `frontend/src/services/documentService.ts` | pollDocumentStatus() — polls doc-status API after upload |
| `frontend/src/components/chat/DocumentUploadPanel.tsx` | IndexingStatusBanner — real-time status display |
| `infra-cdk/lambdas/doc-status/index.py` | GET /documents/{docId}/status endpoint |
| `infra-cdk/lambdas/rag-retrieve/index.py` | Core RAG Lambda: embed + search + format |
| `infra-cdk/lambdas/rag-retrieve/tool_spec.json` | MCP tool schema registered with AgentCore Gateway |
| `patterns/strands-single-agent/basic_agent.py` | Strands agent: system prompt + MCPClient + tool dispatch |
| `infra-cdk/lib/backend-stack.ts` | CDK: createRagRetrieve(), DocStatusLambda, Gateway target |

---

## PHASE 0 — CDK Infrastructure (Deploy Time, NOT Runtime)

### Phase 0A — Doc Status Lambda and API Route (createDocumentUploadInfra)

```
backend-stack.ts
└── createDocumentUploadInfra(config, frontendUrl)
    |
    |   ...existing presign Lambda and POST /documents/presign route...
    |
    ├── new lambda.Function()  (DocStatusLambda)
    |     functionName: "FAST-stack-doc-status"
    |     runtime:      PYTHON_3_13
    |     code:         lambdas/doc-status/
    |     handler:      index.handler
    |     arch:         ARM_64
    |     timeout:      10s
    |     memorySize:   256 MB
    |     env vars:
    |       DOCS_TABLE_NAME:      "FAST-stack-documents"
    |       CORS_ALLOWED_ORIGINS: "{frontendUrl},http://localhost:3000"
    |     logGroup: /aws/lambda/FAST-stack-doc-status (1 week retention)
    |     AWS SERVICE: AWS Lambda
    |
    ├── docsTable.grantReadData(docStatusLambda)
    |     Grants: dynamodb:GetItem, Query, Scan, DescribeTable (read-only)
    |     AWS SERVICE: IAM (inline policy on Lambda execution role)
    |
    ├── documentsResource.addResource("{docId}")   ← adds /documents/{docId} path
    |     {docId} is a path parameter captured by API Gateway
    |
    ├── docItemResource.addResource("status")      ← adds /documents/{docId}/status path
    |
    └── statusResource.addMethod("GET", LambdaIntegration(docStatusLambda))
          authorizer:         docsAuthorizer (Cognito)
          authorizationType:  COGNITO
          requestParameters:
            "method.request.querystring.tenantId": true
              ← API Gateway rejects requests missing tenantId query string
          AWS SERVICE: Amazon API Gateway
```

### Phase 0B — RAG Retrieve Lambda and Gateway Target (createRagRetrieve)

Called from the BackendStack constructor after createIngestionPipeline().
Requires this.gateway and this.gatewayRole to already be set by createAgentCoreGateway().

```
backend-stack.ts
└── createRagRetrieve(config)
    |
    ├── new lambda.Function()  (RagRetrieveLambda)
    |     functionName: "FAST-stack-rag-retrieve"
    |     runtime:      PYTHON_3_13
    |     code:         lambdas/rag-retrieve/   (plain fromAsset, not PythonFunction)
    |     handler:      index.handler
    |     arch:         ARM_64
    |     timeout:      30s
    |     memorySize:   512 MB
    |     env vars:
    |       STACK_NAME: "FAST-stack"
    |     logGroup: /aws/lambda/FAST-stack-rag-retrieve (1 week retention)
    |     AWS SERVICE: AWS Lambda
    |
    |     NOTE: plain lambda.Function (not PythonFunction) because index.py has
    |     zero pip dependencies — only boto3 which ships in the Python 3.13 runtime.
    |
    ├── ragRetrieveLambda.addToRolePolicy()  (Bedrock)
    |     effect:    ALLOW
    |     actions:   bedrock:InvokeModel
    |     resources: arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0
    |     Scope: exactly the one model used at ingest time — nothing else
    |     AWS SERVICE: IAM
    |
    ├── ragRetrieveLambda.addToRolePolicy()  (RDS Data API)
    |     effect:    ALLOW
    |     actions:   rds-data:ExecuteStatement
    |     resources: "*"
    |     NOTE: "*" because the cluster ARN is read from SSM at Lambda runtime.
    |     CDK does not know the final ARN at synth time (it is a CloudFormation token).
    |     AWS SERVICE: IAM
    |
    ├── ragRetrieveLambda.addToRolePolicy()  (Secrets Manager)
    |     effect:    ALLOW
    |     actions:   secretsmanager:GetSecretValue
    |     resources: "*"
    |     Needed by RDS Data API to authenticate SQL calls to Aurora.
    |     AWS SERVICE: IAM
    |
    ├── ragRetrieveLambda.addToRolePolicy()  (SSM)
    |     effect:    ALLOW
    |     actions:   ssm:GetParameter
    |     resources: arn:aws:ssm:us-east-1:897675553288:parameter/FAST-stack/rag/*
    |     Reads aurora-cluster-arn, aurora-secret-arn, aurora-db-name at runtime.
    |     AWS SERVICE: IAM
    |
    ├── ragRetrieveLambda.grantInvoke(this.gatewayRole)
    |     Adds lambda:InvokeFunction to the Gateway's IAM role.
    |     Without this, the Gateway cannot call the Lambda when the agent uses the tool.
    |     AWS SERVICE: IAM (resource-based policy on the Lambda)
    |
    ├── Load tool_spec.json
    |     path.join(__dirname, "..", "lambdas", "rag-retrieve", "tool_spec.json")
    |     fs.readFileSync() + JSON.parse()
    |     Result: bare JSON array (not wrapped in a "tools" key)
    |       [{ "name": "rag_retrieve_documents", "description": "...", "inputSchema": {...} }]
    |
    └── new bedrockagentcore.CfnGatewayTarget()  (RagRetrieveTarget)
          gatewayIdentifier: this.gateway.attrGatewayIdentifier
          name:              "rag-retrieve-target"
          targetConfiguration:
            mcp:
              lambda:
                lambdaArn: ragRetrieveLambda.functionArn
                toolSchema:
                  inlinePayload: ragToolSpec   ← the bare array from tool_spec.json
          credentialProviderConfigurations:
            - credentialProviderType: "GATEWAY_IAM_ROLE"
          ragRetrieveTarget.addDependency(this.gateway)
            Ensures the Gateway exists before the target is created.
          AWS SERVICE: Amazon Bedrock AgentCore Gateway (CfnGatewayTarget L1)
```

---

## PHASE 0C — Gateway Class Properties (createAgentCoreGateway)

```
backend-stack.ts
└── createAgentCoreGateway(config)
    |
    ├── new lambda.Function()  (SampleToolLambda)
    |     ...existing sample tool setup...
    |
    ├── this.gatewayRole = new iam.Role()
    |     assumedBy: bedrock-agentcore.amazonaws.com service principal
    |     Stored as class property so createRagRetrieve() can call
    |     ragRetrieveLambda.grantInvoke(this.gatewayRole) without forward-reference issues.
    |
    ├── toolLambda.grantInvoke(this.gatewayRole)
    |     Allows Gateway to invoke the sample tool Lambda.
    |
    ├── this.gateway = new bedrockagentcore.CfnGateway()
    |     name:          "FAST-stack-gateway"
    |     protocolType:  "MCP"
    |     authorizerType: "CUSTOM_JWT"
    |     authorizerConfiguration:
    |       customJwtAuthorizer:
    |         allowedClients: [this.machineClient.userPoolClientId]
    |         discoveryUrl:   Cognito OIDC discovery endpoint
    |     Stored as class property so createRagRetrieve() can reference
    |     this.gateway.attrGatewayIdentifier when registering the RAG target.
    |
    └── new bedrockagentcore.CfnGatewayTarget()  (GatewayTarget — sample tool)
          ...existing sample tool target setup...
```

---

## PHASE 1 — Post-Upload: Frontend Starts Polling Doc Status

This phase runs in parallel with any chat activity. It begins immediately after
the browser S3 upload completes (Phase 2B in DOCUMENT_UPLOAD_FLOW.md).

### Phase 1A — useEffect Polling Hook Fires

```
DocumentUploadPanel.tsx
└── useEffect(
      deps: [state.status === "success" ? state.doc.docId : null]
    )
    |
    ├── Guard: if (state.status !== "success") return
    |     Effect only runs when upload transitions to "success".
    |
    ├── const idToken  = auth.user?.id_token
    ├── const tenantId = auth.user?.profile?.sub
    |
    ├── Guard: if (!idToken || !tenantId) return
    |
    ├── const controller = new AbortController()
    |     Creates a cancellation handle.
    |     controller.signal is passed to pollDocumentStatus so polling
    |     stops cleanly if the component unmounts before the doc is READY.
    |
    ├── pollDocumentStatus(
    |     state.doc.docId,
    |     tenantId,
    |     idToken,
    |     (status, meta) => {
    |       setIndexing(status)   ← REACT STATE: IndexingStatus
    |       setIndexMeta(meta)    ← REACT STATE: DocumentStatus | undefined
    |     },
    |     controller.signal,
    |   )
    |   ↓ async, does not block render
    |
    └── return () => controller.abort()
          Cleanup function: aborts polling when panel unmounts or docId changes.
          Prevents calling setState on an unmounted component.
```

### Phase 1B — pollDocumentStatus() Loop

```
documentService.ts
└── async pollDocumentStatus(docId, tenantId, idToken, onStatus, signal, intervalMs=3000, timeoutMs=60000)
    |
    ├── const base = await loadDocsApiBase()
    |     Returns cached DOCS_API_BASE (no fetch if already loaded).
    |     Example: "https://abc123.execute-api.us-east-1.amazonaws.com/prod/"
    |
    ├── const url = `${base}documents/${encodeURIComponent(docId)}/status?tenantId=${encodeURIComponent(tenantId)}`
    |     Example: ".../documents/3b398eaf-1234/status?tenantId=abc-sub-uuid"
    |
    ├── const deadline = Date.now() + 60000
    |
    ├── onStatus("indexing")
    |     Immediately tells the panel to show the "Indexing..." spinner
    |     before the first HTTP response arrives.
    |     REACT STATE: indexing = "indexing"
    |
    └── while (Date.now() < deadline):
          |
          ├── if (signal?.aborted) return    ← check on every iteration
          |
          ├── await fetch(url, { headers: { Authorization: `Bearer ${idToken}` }, signal })
          |     GET /documents/{docId}/status?tenantId={sub}
          |     Authorization: Bearer {Cognito id_token}
          |     AWS SERVICE: Amazon API Gateway (Cognito-authorized)
          |
          ├── IF resp.ok:
          |     const data: DocumentStatus = await resp.json()
          |       { docId, status, fileName, updatedAt, chunkCount?, errorMessage? }
          |     |
          |     ├── IF data.status === "READY":
          |     |     onStatus("ready", data)
          |     |     REACT STATE: indexing = "ready", indexMeta = data
          |     |     return   ← polling stops
          |     |
          |     ├── IF data.status === "FAILED":
          |     |     onStatus("failed", data)
          |     |     REACT STATE: indexing = "failed", indexMeta = data
          |     |     return   ← polling stops
          |     |
          |     └── IF data.status === "UPLOADED":
          |           onStatus("indexing", data)
          |           REACT STATE: indexing = "indexing" (unchanged, but meta updated)
          |
          ├── ELSE (non-2xx): silent retry, do not update state
          |
          ├── CATCH (fetch error or abort):
          |     if (signal?.aborted) return
          |     console.warn(...)
          |     ← transient network errors retry silently
          |
          └── await _sleep(3000, signal)
                Waits 3 seconds.
                _sleep(): setTimeout inside a Promise, with abort listener
                that resolves immediately if signal fires during the sleep.
```

### Phase 1C — Doc Status Lambda Handles the GET Request

```
API Gateway: GET /documents/{docId}/status?tenantId={sub}
  ├── Cognito Authorizer validates Bearer id_token
  |     Checks JWT signature, expiry, audience, issuer
  |     IF invalid: 401 Unauthorized (Lambda never invoked)
  |     AWS SERVICE: Amazon Cognito
  |
  └── Invokes doc-status Lambda
        AWS SERVICE: AWS Lambda (synchronous)

infra-cdk/lambdas/doc-status/index.py
└── handler(event, context)
    |
    ├── path_params  = event.get("pathParameters") or {}
    ├── query_params = event.get("queryStringParameters") or {}
    |
    ├── doc_id    = path_params.get("docId", "").strip()
    ├── tenant_id = query_params.get("tenantId", "").strip()
    |
    ├── Validation:
    |     if not doc_id:    return _error(400, "docId path parameter is required")
    |     if not tenant_id: return _error(400, "tenantId query parameter is required")
    |
    ├── Build DynamoDB key:
    |     pk = f"TENANT#{tenant_id}#DOC#{doc_id}"
    |     sk = "VER#000001"
    |     Version is hardcoded to 1 for Phase 2D.
    |
    ├── dynamodb.get_item(
    |     TableName=TABLE_NAME,
    |     Key={ "PK": {"S": pk}, "SK": {"S": sk} },
    |     ProjectionExpression="#s, chunkCount, errorMessage, fileName, updatedAt",
    |     ExpressionAttributeNames={"#s": "status"},
    |   )
    |   AWS SERVICE: Amazon DynamoDB (GetItem)
    |   ProjectionExpression limits the response to only the 5 needed attributes.
    |   ExpressionAttributeNames: "status" is a reserved word in DynamoDB expression
    |   syntax; #s is an alias that avoids the conflict.
    |
    ├── item = resp.get("Item")
    |     if not item: return _error(404, f"Document not found: {doc_id}")
    |
    ├── Extract fields from DynamoDB typed format:
    |     status    = item.get("status",    {}).get("S", "UNKNOWN")
    |     file_name = item.get("fileName",  {}).get("S")
    |     updated   = item.get("updatedAt", {}).get("S")
    |
    ├── Build response body:
    |     body = { "docId": doc_id, "status": status, "fileName": file_name, "updatedAt": updated }
    |     IF "chunkCount" in item: body["chunkCount"] = int(item["chunkCount"]["N"])
    |     IF "errorMessage" in item: body["errorMessage"] = item["errorMessage"]["S"]
    |
    └── Return HTTP 200:
          headers:
            Content-Type: "application/json"
            Access-Control-Allow-Origin: matched origin from CORS_ALLOWED_ORIGINS
            Cache-Control: "no-store"   ← prevents API Gateway or browser caching
          body: JSON string of body dict
```

### Phase 1D — IndexingStatusBanner Renders

```
DocumentUploadPanel.tsx
└── <IndexingStatusBanner status={indexing} meta={indexMeta} />

IndexingStatusBanner({ status, meta })
|
├── status === "uploading":
|     return null
|     Transient state between S3 upload completing and first poll arriving.
|     Nothing is shown so the UI does not flash an intermediate message.
|
├── status === "indexing":
|     <Loader2 animate-spin />
|     "Indexing document..."
|     "Chunks are being embedded and stored. This usually takes 5-30 seconds."
|
├── status === "ready":
|     chunkInfo = meta?.chunkCount != null ? ` · ${meta.chunkCount} chunks indexed` : ""
|     <CheckCircle2 />
|     "Ready to query{chunkInfo}"
|     "You can now ask questions about this document in the chat."
|     Example display: "Ready to query · 21 chunks indexed"
|
└── status === "failed":
      errMsg = meta?.errorMessage ? `: ${meta.errorMessage}` : ". Please try uploading again."
      <AlertCircle />
      "Indexing failed{errMsg}"
      "The file was uploaded to S3 but could not be processed. Try uploading again."
```

---

## PHASE 2 — User Sends a Chat Message

```
ChatInterface.tsx
└── handleSendMessage()
    |
    ├── user_query = input.trim()
    ├── setMessages([...messages, { role: "user", content: user_query }])
    |     REACT STATE: messages array gains a new user entry
    |
    └── client.streamResponse(user_query, runtimeSessionId)
          ↓ calls AgentCore Runtime via HTTPS
          AWS SERVICE: Amazon Bedrock AgentCore Runtime
```

---

## PHASE 3 — AgentCore Runtime Dispatches to Agent Container

```
AgentCore Runtime
├── Validates the request JWT (Cognito id_token)
|     AWS SERVICE: Amazon Cognito
|
├── Extracts user sub claim → user_id (RequestContext.identity)
|
└── Invokes the agent container (Docker image on Runtime)
      ↓ calls basic_agent.py agent_stream() entrypoint
```

---

## PHASE 4 — agent_stream() — Main Entrypoint

```
patterns/strands-single-agent/basic_agent.py
└── @app.entrypoint
    async def agent_stream(payload, context: RequestContext)
    |
    ├── user_query = payload.get("prompt")
    ├── session_id = payload.get("runtimeSessionId")
    |
    ├── Guard: if not all([user_query, session_id]):
    |     yield { "status": "error", "error": "Missing required fields..." }
    |     return
    |
    ├── user_id = extract_user_id_from_context(context)
    |     Reads the validated JWT sub claim from RequestContext.
    |     This is the user's Cognito sub (UUID), used as tenantId everywhere.
    |     NOT read from the payload body (which could be forged).
    |
    ├── agent = create_basic_agent(user_id, session_id)
    |     ↓ see Phase 5
    |
    └── async for event in agent.stream_async(user_query):
          yield json.loads(json.dumps(dict(event), default=str))
            Streams each token/event back to the Runtime as a JSON dict.
            json.dumps(default=str) handles any non-serializable types.
```

---

## PHASE 5 — create_basic_agent() — Agent Construction

```
patterns/strands-single-agent/basic_agent.py
└── create_basic_agent(user_id, session_id)
    |
    ├── system_prompt = f"""..."""
    |     An f-string with user_id baked in at agent creation time.
    |     Key rules injected:
    |       - Call rag_retrieve_documents FIRST for document questions
    |       - Always pass tenantId="{user_id}"  ← exact Cognito sub, not a variable
    |       - Cite sources as [Source: file_name, page N]
    |       - If chunks_found == 0, answer from general knowledge
    |       - Never fabricate citations
    |     The system prompt is rendered once per request (not shared across users).
    |
    ├── bedrock_model = BedrockModel(
    |     model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    |     temperature=0.1
    |   )
    |   temperature=0.1: near-deterministic for document-grounded answers.
    |
    ├── memory_id = os.environ.get("MEMORY_ID")
    |
    ├── agentcore_memory_config = AgentCoreMemoryConfig(
    |     memory_id=memory_id, session_id=session_id, actor_id=user_id
    |   )
    |   session_manager = AgentCoreMemorySessionManager(...)
    |   Conversation history is stored and retrieved via AgentCore Memory.
    |
    ├── access_token = get_gateway_access_token()
    |     Calls Cognito OAuth2 token endpoint using the machine client credentials
    |     stored in SSM (client_id + client_secret).
    |     Returns a short-lived Bearer access_token for M2M communication.
    |     AWS SERVICE: Amazon Cognito (client_credentials OAuth2 flow)
    |
    ├── gateway_client = create_gateway_mcp_client(access_token)
    |     |
    |     ├── gateway_url = get_ssm_parameter(f"/{stack_name}/gateway_url")
    |     |     Fetches the AgentCore Gateway HTTPS endpoint from SSM.
    |     |     AWS SERVICE: AWS SSM Parameter Store
    |     |
    |     └── MCPClient(
    |           lambda: streamablehttp_client(
    |             url=gateway_url,
    |             headers={"Authorization": f"Bearer {access_token}"}
    |           ),
    |           prefix="gateway",
    |         )
    |         MCPClient wraps the HTTP connection as a tool provider.
    |         prefix="gateway" namespaces tools as "gateway_rag_retrieve_documents" etc.
    |         The lambda factory is called lazily when the agent needs a tool.
    |         AWS SERVICE: Amazon Bedrock AgentCore Gateway
    |
    ├── code_tools = StrandsCodeInterpreterTools(region)
    |
    └── agent = Agent(
          name="BasicAgent",
          system_prompt=system_prompt,
          tools=[gateway_client, code_tools.execute_python_securely],
          model=bedrock_model,
          session_manager=session_manager,
          trace_attributes={ "user.id": user_id, "session.id": session_id },
        )
        Both the gateway MCP client and the code interpreter are registered as tools.
        Strands resolves available tools by calling the MCP tool list endpoint on
        the Gateway when the agent starts reasoning.
```

---

## PHASE 6 — Agent Reasons and Decides to Call rag_retrieve_documents

```
Agent.stream_async(user_query)
|
├── Strands calls the MCPClient to enumerate available tools from the Gateway.
|     HTTP GET to Gateway MCP endpoint → lists all registered targets
|     Gateway returns: [ "rag_retrieve_documents", ...sample_tool... ]
|
├── Agent sends the user query + system_prompt + tool descriptions to Claude Sonnet.
|     AWS SERVICE: Amazon Bedrock (InvokeModelWithResponseStream)
|
├── Claude reads the system prompt rule:
|     "Whenever the user asks a question that could be answered by their
|      uploaded documents, you MUST call the rag_retrieve_documents tool FIRST"
|
└── Claude emits a tool_use block:
      {
        "type": "tool_use",
        "name": "gateway_rag_retrieve_documents",
        "input": {
          "query":    "what is multi-head attention",
          "tenantId": "abc-123-user-sub",   ← from system prompt, exact user_id
          "topK":     5
        }
      }
```

---

## PHASE 7 — Strands Dispatches Tool Call to Gateway

```
Strands MCPClient
└── Receives tool_use block from Claude
    |
    └── Sends MCP tool invocation to AgentCore Gateway
          HTTP POST to gateway_url/mcp
          Headers: Authorization: Bearer {access_token}
          Body: { "tool": "rag_retrieve_documents", "input": { query, tenantId, topK } }
          AWS SERVICE: Amazon Bedrock AgentCore Gateway

AgentCore Gateway
├── Validates the Bearer access_token (machine client JWT)
|     AWS SERVICE: Amazon Cognito (token validation)
|
├── Looks up which Lambda target handles "rag_retrieve_documents"
|     Checks registered GatewayTargets by tool name from inlinePayload schema
|
├── Assumes this.gatewayRole via STS
|     AWS SERVICE: AWS STS
|
└── Invokes FAST-stack-rag-retrieve Lambda
      Authorization: Gateway IAM role (lambda:InvokeFunction granted by grantInvoke)
      AWS SERVICE: AWS Lambda (synchronous invocation)
      Payload:
        {
          "body": "{\"query\":\"what is multi-head attention\",\"tenantId\":\"abc-123\",\"topK\":5}"
        }
```

---

## PHASE 8 — rag-retrieve Lambda: handler()

```
infra-cdk/lambdas/rag-retrieve/index.py
└── handler(event, context)
    |
    ├── Module-level initialization (once per container cold start):
    |     bedrock  = boto3.client("bedrock-runtime")
    |     rds_data = boto3.client("rds-data")
    |     ssm      = boto3.client("ssm")
    |     STACK_NAME = os.environ["STACK_NAME"]   ← "FAST-stack"
    |     TOP_K_DEFAULT = 5
    |     TOP_K_MAX     = 8
    |     SIMILARITY_CUTOFF = 0.30
    |     EMBED_DIMENSIONS  = 1024
    |     _ssm_cache = {}   ← per-container cache for SSM values
    |
    ├── Parse input — dual-format handling:
    |     IF event.get("body") is str:
    |       body = json.loads(event["body"])
    |         Gateway wraps the payload in a "body" string key.
    |     ELIF event.get("body") is dict:
    |       body = event["body"]
    |     ELSE:
    |       body = event
    |         Direct invocation path (CLI test: aws lambda invoke ...).
    |
    ├── query     = body.get("query", "").strip()       ← "what is multi-head attention"
    ├── tenant_id = body.get("tenantId", "").strip()    ← "abc-123-user-sub"
    ├── top_k     = min(int(body.get("topK", 5)), 8)    ← capped at TOP_K_MAX
    |
    ├── Validation:
    |     if not query:     return _response(400, {"error": "query is required"})
    |     if not tenant_id: return _response(400, {"error": "tenantId is required"})
    |
    └── result = retrieve(query, tenant_id, top_k)
          ↓ see Phase 9
          return _response(200, result)
```

---

## PHASE 9 — retrieve() — Full Retrieval Pipeline

```
infra-cdk/lambdas/rag-retrieve/index.py
└── retrieve(query, tenant_id, top_k)
    |
    ├── STEP 1: query_vector = _embed_text(query)
    |     ↓ see Phase 10
    |
    ├── STEP 2: fetch_limit = top_k * 2
    |     Two-stage design: fetch double candidates from Aurora, filter in Lambda.
    |     Reason: HNSW is an approximate index. Fetching extra candidates and then
    |     filtering precisely in Python gives better precision than relying solely
    |     on the database to cut off at topK.
    |     Example with top_k=5: fetch_limit = 10
    |
    ├── STEP 3: raw_chunks = _vector_search(query_vector, tenant_id, fetch_limit)
    |     ↓ see Phase 11
    |     Returns up to 10 chunks with similarity scores from Aurora.
    |
    ├── STEP 4: Similarity threshold filter
    |     filtered = [c for c in raw_chunks if c["similarity"] >= 0.30]
    |     Discards chunks below 0.30 cosine similarity.
    |     0.30 is chosen to cut clearly unrelated vectors without dropping
    |     relevant ones that happen to score in the 0.30-0.50 range.
    |     cosine similarity = 1 - cosine_distance
    |     (Aurora returns this pre-computed as the "similarity" column)
    |
    ├── STEP 5: final_chunks = filtered[:top_k]
    |     Respect topK after threshold filtering.
    |     If threshold eliminated some candidates, returns fewer than topK.
    |
    ├── IF not final_chunks:
    |     return { "context_block": "", "chunks_found": 0, "query": query }
    |     Agent receives empty context_block and follows the system prompt rule:
    |     "answer from general knowledge, do NOT fabricate document sources"
    |
    ├── STEP 6: context_block = _format_context(final_chunks)
    |     ↓ see Phase 12
    |
    └── return {
          "context_block": context_block,   ← formatted text ready for LLM prompt
          "chunks_found":  len(final_chunks),
          "query":         query,
        }
```

---

## PHASE 10 — _embed_text() — Titan V2 Query Embedding

```
infra-cdk/lambdas/rag-retrieve/index.py
└── _embed_text(text: str) -> list[float]
    |
    ├── bedrock.invoke_model(
    |     modelId="amazon.titan-embed-text-v2:0",
    |     contentType="application/json",
    |     accept="application/json",
    |     body=json.dumps({
    |       "inputText":  "what is multi-head attention",
    |       "dimensions": 1024,
    |       "normalize":  True,
    |     }),
    |   )
    |   AWS SERVICE: Amazon Bedrock (synchronous invoke)
    |   Typical latency: 100-200ms
    |
    |   CRITICAL: parameters must exactly match ingestion-worker:
    |     dimensions=1024  — if ingestion used 1024 and retrieval uses 512,
    |                        the two vectors live in different spaces and
    |                        cosine similarity scores will be meaningless.
    |     normalize=True   — produces unit vectors. Aurora's cosine distance
    |                        operator (<=> ) works correctly with both, but
    |                        normalised inputs give consistent scores across
    |                        documents of different lengths.
    |
    ├── response["body"].read()
    |     Reads the streaming response body bytes.
    |
    ├── json.loads(response_body)
    |
    └── return body["embedding"]
          Returns: list of 1024 floats
          Example: [0.02341234, -0.01234567, 0.00823456, ...] (1024 values total)
          This is the query's position in the same 1024-dimensional space
          as the chunk embeddings stored in Aurora during ingestion.
```

---

## PHASE 11 — _vector_search() — pgvector HNSW Similarity Search

```
infra-cdk/lambdas/rag-retrieve/index.py
└── _vector_search(query_vector, tenant_id, fetch_limit)
    |
    ├── db_cluster_arn = _get_ssm("aurora-cluster-arn")
    |     _get_ssm() checks _ssm_cache dict first.
    |     IF not cached: ssm.get_parameter(Name="/FAST-stack/rag/aurora-cluster-arn")
    |     Caches the value for the container's lifetime.
    |     AWS SERVICE: AWS SSM Parameter Store (only on cold start per key)
    |
    ├── db_secret_arn  = _get_ssm("aurora-secret-arn")
    ├── db_name        = _get_ssm("aurora-db-name")   ← "ragdb"
    |
    ├── vector_literal = "[" + ",".join(f"{v:.8f}" for v in query_vector) + "]"
    |     Converts 1024 Python floats to a PostgreSQL vector literal string.
    |     Example: "[0.02341234,-0.01234567,0.00823456,...]"
    |     The "::vector" cast in the SQL converts this string to pgvector's native type.
    |     8 decimal places (.8f) preserves enough precision for cosine similarity.
    |
    ├── SQL:
    |     SELECT
    |         content,
    |         file_name,
    |         chunk_index,
    |         chunk_total,
    |         page_number,
    |         section_title,
    |         source_type,
    |         1 - (embedding <=> :query_vec::vector) AS similarity
    |     FROM  fast_chunks
    |     WHERE tenant_id = :tenant_id
    |     ORDER BY embedding <=> :query_vec::vector
    |     LIMIT :fetch_limit
    |
    |     SQL notes:
    |       embedding <=> :query_vec::vector   = cosine DISTANCE (0=identical, 2=opposite)
    |       1 - (distance)                     = cosine SIMILARITY (1=identical, -1=opposite)
    |       WHERE tenant_id = :tenant_id        = multi-tenant isolation at DB level
    |       ORDER BY distance ASC               = most similar chunks first
    |       LIMIT :fetch_limit                  = fetch only top N candidates
    |       The HNSW index on (embedding vector_cosine_ops) is used automatically
    |       by the Aurora query planner for ORDER BY ... LIMIT queries.
    |
    ├── params = [
    |     {"name": "query_vec",   "value": {"stringValue": vector_literal}},
    |     {"name": "tenant_id",   "value": {"stringValue": tenant_id}},
    |     {"name": "fetch_limit", "value": {"longValue":   fetch_limit}},
    |   ]
    |
    ├── rds_data.execute_statement(
    |     resourceArn=db_cluster_arn,
    |     secretArn=db_secret_arn,
    |     database=db_name,
    |     sql=sql,
    |     parameters=params,
    |     includeResultMetadata=True,    ← returns column names alongside values
    |   )
    |   AWS SERVICE: RDS Data API (HTTPS + IAM SigV4)
    |   Lambda authenticates to Aurora via the secret in Secrets Manager.
    |   Lambda makes zero direct TCP connections to port 5432.
    |   Aurora applies the HNSW approximate nearest-neighbour search.
    |   Typical latency: 50-150ms for a 1000-row table.
    |
    └── return _parse_rds_response(response)
          ↓ see Phase 11A
```

### Phase 11A — _parse_rds_response()

```
infra-cdk/lambdas/rag-retrieve/index.py
└── _parse_rds_response(response: dict) -> list[dict]
    |
    ├── RDS Data API response structure:
    |     response["columnMetadata"] = [{"name": "content"}, {"name": "file_name"}, ...]
    |     response["records"]        = [[{"stringValue": "..."}, ...], ...]
    |     includeResultMetadata=True is required to get columnMetadata.
    |     Without it: only records are returned and column order is ambiguous.
    |
    ├── columns = [col["name"] for col in response.get("columnMetadata", [])]
    |     Example: ["content", "file_name", "chunk_index", "chunk_total",
    |               "page_number", "section_title", "source_type", "similarity"]
    |
    └── for row in response.get("records", []):
          chunk = {}
          for col_name, cell in zip(columns, row):
            |
            ├── IF cell["isNull"] == True: chunk[col_name] = None
            |     page_number is None for DOCX/XLSX/CSV/TXT/MD chunks.
            |
            ├── ELIF "stringValue" in cell: chunk[col_name] = cell["stringValue"]
            |     content, file_name, section_title, source_type
            |
            ├── ELIF "longValue" in cell: chunk[col_name] = cell["longValue"]
            |     chunk_index, chunk_total, page_number (when not null)
            |
            ├── ELIF "doubleValue" in cell: chunk[col_name] = cell["doubleValue"]
            |     similarity (computed by SQL: 1 - distance)
            |
            └── ELSE: chunk[col_name] = None
          chunks.append(chunk)
        return chunks
          Returns a plain Python list of dicts, one dict per matching chunk.
```

---

## PHASE 12 — _format_context() — Build Citation Context Block

```
infra-cdk/lambdas/rag-retrieve/index.py
└── _format_context(chunks: list[dict]) -> str
    |
    └── for chunk in chunks:
          |
          ├── file_name   = chunk.get("file_name")   or "unknown"
          ├── page_num    = chunk.get("page_number")    ← int or None
          ├── chunk_index = chunk.get("chunk_index")    ← 0-based int
          ├── chunk_total = chunk.get("chunk_total")
          ├── content     = (chunk.get("content") or "").strip()
          ├── similarity  = chunk.get("similarity", 0)
          |
          ├── page_part  = f", page {page_num}" if page_num is not None else ""
          ├── chunk_part = f", chunk {chunk_index + 1}/{chunk_total}"
          |                  (chunk_index + 1 converts 0-based to 1-based for display)
          |
          ├── header = f"[Source: {file_name}{page_part}{chunk_part}]"
          |     Example: "[Source: NIPS-2017-attention-is-all-you-need-Paper.pdf, page 3, chunk 2/8]"
          |
          └── parts.append(f"{header}\n{content}")
                Header is prepended INSIDE the context block text, not as metadata.
                This ensures the LLM reads the citation as part of the passage
                and naturally includes it in its inline citations.

    return "\n---\n".join(parts)
      Chunks are joined with "---" as a visual separator.
      The LLM sees the complete context_block as a single text string.
```

Example context_block output:

```
[Source: NIPS-2017-attention-is-all-you-need-Paper.pdf, page 3, chunk 2/8]
Multi-head attention allows the model to jointly attend to information from
different representation subspaces at different positions...
---
[Source: NIPS-2017-attention-is-all-you-need-Paper.pdf, page 4, chunk 3/8]
The encoder-decoder attention layers allow every position in the decoder to
attend over all positions in the input sequence...
```

---

## PHASE 13 — Tool Result Returns to Agent

```
AgentCore Gateway
└── Returns Lambda response to Strands MCPClient
      Response body: {
        "context_block": "[Source: ...]...",
        "chunks_found":  2,
        "query":         "what is multi-head attention"
      }

Strands MCPClient
└── Passes tool_result back to Agent as a tool_result content block
      { "type": "tool_result", "tool_use_id": "...", "content": "..." }
```

---

## PHASE 14 — Agent Composes Grounded Answer

```
Agent.stream_async
└── Agent sends to Claude Sonnet:
      1. system_prompt (with RAG rules + citation format)
      2. user message: "what is multi-head attention"
      3. tool_use block (the call it made)
      4. tool_result block (the context_block it received)
    |
    └── Claude reads the context_block and system_prompt rules:
          "base your answer on the returned passages"
          "cite every source inline using: [Source: <file_name>, page <N>]"
          "never claim a document says something not in the context_block"
        |
        └── Claude generates a grounded response with inline citations:
              "Multi-head attention allows the model to jointly attend to information
               from different representation subspaces at different positions
               [Source: NIPS-2017-attention-is-all-you-need-Paper.pdf, page 3].
               Each head learns a different attention pattern, enabling the model to
               capture various types of relationships between tokens simultaneously
               [Source: NIPS-2017-attention-is-all-you-need-Paper.pdf, page 4]."

Agent streams response tokens back through:
  agent.stream_async() → agent_stream() yield → AgentCore Runtime → ChatInterface.tsx
```

---

## PHASE 15 — Response Streams to Browser

```
ChatInterface.tsx
└── client.streamResponse() receives streaming token events
    |
    └── setMessages(prev => [...prev, { role: "assistant", content: assembled_text }])
          REACT STATE: messages array gains the assistant reply
          Browser renders the grounded answer with inline citations
```

---

## Complete End-to-End Flow (RAG Query Path)

```
BROWSER
  user types: "what is multi-head attention"
  |
  | HTTPS POST to AgentCore Runtime
  v
AGENTCORE RUNTIME
  validates JWT, extracts user_id
  |
  | invokes agent container
  v
AGENT (basic_agent.py)
  1. get_gateway_access_token() → Cognito machine client token
  2. MCPClient connects to Gateway
  3. Agent sends query to Claude Sonnet (Bedrock)
  |
  | Claude decides to call rag_retrieve_documents
  v
AGENTCORE GATEWAY
  validates access_token
  |
  | invokes FAST-stack-rag-retrieve Lambda
  v
RAG-RETRIEVE LAMBDA
  1. Parse input: { query, tenantId, topK }
  2. _get_ssm() x3 → aurora-cluster-arn, aurora-secret-arn, aurora-db-name  (SSM)
  3. _embed_text(query) → 1024-dim float vector                              (Bedrock)
  4. _vector_search()   → cosine similarity search, fetch topK*2 rows       (RDS Data API → Aurora)
  5. filter: similarity >= 0.30, take [:topK]
  6. _format_context()  → context_block string with [Source: ...] headers
  |
  | returns { context_block, chunks_found, query }
  v
GATEWAY → STRANDS MCPClient → tool_result block
  |
  v
CLAUDE SONNET (Bedrock)
  reads context_block + system_prompt rules
  generates grounded answer with inline citations
  |
  | streams tokens back
  v
AGENTCORE RUNTIME → BROWSER
  ChatInterface renders the answer with citations
```

---

## Post-Upload Status Polling Flow (Parallel to Chat)

```
BROWSER (after S3 upload completes)
  DocumentUploadPanel state = "success"
  useEffect fires → AbortController created
  |
  | loop every 3s up to 60s
  v
GET /documents/{docId}/status?tenantId={sub}
  Authorization: Bearer {id_token}
  |
  | API Gateway Cognito Authorizer validates token
  v
DOC-STATUS LAMBDA
  dynamodb.get_item(PK=TENANT#{sub}#DOC#{docId}, SK=VER#000001)
  returns { status: "UPLOADED" | "READY" | "FAILED", chunkCount?, errorMessage? }
  |
  v
BROWSER
  onStatus callback updates React state
  IndexingStatusBanner renders:
    "UPLOADED" → "Indexing..." spinner
    "READY"    → green "Ready to query · N chunks indexed"
    "FAILED"   → red error message

Polling stops on READY or FAILED or timeout (60s) or component unmount.
```

---

## AWS Services Interaction Summary

| AWS Service | Phase | What Happens |
|-------------|-------|--------------|
| Amazon Cognito | Phase 4 | Runtime validates user JWT, extracts sub |
| Amazon Cognito | Phase 5 | Machine client OAuth2 token for Gateway auth |
| AWS SSM Parameter Store | Phase 5 | gateway_url fetched by MCPClient |
| Amazon Bedrock AgentCore Runtime | Phase 3-4 | Runs agent container, streams response |
| Amazon Bedrock AgentCore Gateway | Phase 7 | Routes MCP tool call to Lambda |
| Amazon Bedrock (Sonnet) | Phase 6, 14 | LLM reasoning + answer generation |
| AWS Lambda | Phase 8-12 | rag-retrieve executes the retrieval pipeline |
| Amazon Bedrock (Titan Embed V2) | Phase 10 | Embeds the query into a 1024-dim vector |
| AWS SSM Parameter Store | Phase 11 | Aurora ARNs fetched by rag-retrieve Lambda |
| Amazon RDS Data API | Phase 11 | pgvector HNSW cosine similarity search |
| Aurora PostgreSQL 16.4 | Phase 11 | Returns ranked chunks from fast_chunks table |
| IAM / STS | Phase 7 | Gateway assumes gatewayRole to invoke Lambda |
| API Gateway | Phase 1C | Routes GET /documents/{docId}/status |
| Amazon Cognito | Phase 1C | Validates id_token on status poll request |
| AWS Lambda | Phase 1C | doc-status reads DynamoDB VER record |
| Amazon DynamoDB | Phase 1C | GetItem returns current ingestion status |

---

## Key Design Decisions

**Why embed the query with the same model and settings as ingestion?**
Amazon Titan Embed V2 can output different vector sizes (256, 512, 1024). The embedding
space is specific to the model, version, and dimension setting. If ingestion stored 1024-dim
vectors and retrieval queried with 512-dim vectors, the two sets of coordinates describe
different geometric spaces. Cosine similarity between mismatched vectors is meaningless.

**Why fetch topK*2 from Aurora and filter in Lambda?**
The HNSW index uses an approximate algorithm. It finds neighbours quickly but may omit
some relevant items in its candidate set. Fetching twice as many candidates gives the
similarity threshold filter a larger pool to work with, improving precision without a
second database round-trip.

**Why similarity threshold 0.30?**
A score of 0.0 means the vectors are orthogonal (no relationship). A score of 1.0 means
identical. Scores below 0.30 in practice correspond to chunks that share some surface-level
vocabulary but not semantic meaning. The threshold cuts noise while keeping low-scoring
but genuinely relevant chunks that appear in adjacent sections of a document.

**Why is tenantId baked into the system prompt at agent creation time?**
The user_id is extracted from the validated JWT in RequestContext — it cannot be forged
by the payload body. Embedding it into the f-string system_prompt means the LLM cannot
accidentally omit it or use a wrong value when calling the tool.

**Why context_block is a single text string instead of a structured array?**
The citation header ([Source: file_name, page N]) is placed inside the text block rather
than as a separate metadata field. This makes the LLM see the header as part of the passage
it is reading, which produces more natural inline citations in the final answer. If metadata
were separate, the LLM would need to explicitly look up the source for each snippet.

**Why does doc-status use Cache-Control: no-store?**
The document status changes from UPLOADED to READY within seconds. If API Gateway or the
browser cached a 200 response with status=UPLOADED, the polling loop would never see READY
and would time out. no-store forces every poll to hit the Lambda.

**Why tenantId is a query string parameter on doc-status instead of reading from the JWT?**
API Gateway's Cognito Authorizer validates the JWT signature but does not pass the sub claim
to the Lambda event. The Lambda would need to decode the JWT itself to extract the sub. Passing
tenantId as a query string is simpler and still secure because the DynamoDB key combines both
tenantId and docId — a user cannot read another tenant's document even if they supply a wrong
tenantId, because they would also need that tenant's docId, which is a UUID they cannot guess.
