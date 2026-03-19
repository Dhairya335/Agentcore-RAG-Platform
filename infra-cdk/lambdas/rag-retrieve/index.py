"""
Phase 2D — RAG Retrieve Lambda

Called by the Strands agent via AgentCore Gateway (MCP tool: rag_retrieve_documents).

Flow:
  1. Receive { query, tenantId, topK? } from agent tool call
  2. Embed query using Amazon Titan Embed V2 (1024-dim, normalised)
     — must match embedding model used at ingest time in ingestion-worker
  3. Run pgvector cosine similarity search against fast_chunks
     — scoped to tenant_id (multi-tenant isolation)
     — two-stage: fetch topK*2 from HNSW, filter in Lambda
  4. Apply similarity threshold >= 0.30
     — discard clearly unrelated vectors without hard-cutting relevant ones
  5. Format surviving chunks as a context block with inline source citations
  6. Return { context_block, chunks_found, query } to agent

Design decisions:
  - No VPC: RDS Data API is HTTPS + IAM SigV4 — same pattern as ingestion-worker
  - No pip dependencies: only boto3 + json (stdlib) — plain lambda.Function, not PythonFunction
  - SSM cache: same 3 keys as ingestion-worker, cached per container lifetime
  - topK default = 5 (~2500 tokens context), hard max = 8 (~4000 tokens)
  - Two-stage retrieval: LIMIT topK*2 from Aurora, Python post-filter to topK
    Reason: HNSW is approximate — fetching extra candidates then filtering
    in Lambda yields better precision than relying solely on the index cutoff.
"""

import json
import os

import boto3
from boto3.dynamodb.conditions import Key

# AWS clients
bedrock      = boto3.client("bedrock-runtime")
rds_data     = boto3.client("rds-data")
ssm          = boto3.client("ssm")
dynamodb_res = boto3.resource("dynamodb")

# Config
STACK_NAME         = os.environ["STACK_NAME"]
DOCS_TABLE_NAME    = os.environ.get("DOCS_TABLE_NAME", "")
TOP_K_DEFAULT      = 5
TOP_K_MAX          = 8
SIMILARITY_CUTOFF  = 0.30   # discard chunks below this cosine similarity score
EMBED_DIMENSIONS   = 1024   # must match ingestion-worker

# SSM cache (per container lifetime, same pattern as ingestion-worker)
_ssm_cache: dict[str, str] = {}


def _get_ssm(key: str) -> str:
    if key not in _ssm_cache:
        _ssm_cache[key] = ssm.get_parameter(
            Name=f"/{STACK_NAME}/rag/{key}"
        )["Parameter"]["Value"]
    return _ssm_cache[key]


# Entry point

def handler(event, context):
    """
    Lambda entry point — called by AgentCore Gateway MCP tool invocation.

    The Gateway wraps the agent's tool call JSON in a Lambda event.
    Input shape accepted from agent:
        { "query": str, "tenantId": str, "topK": int (optional) }

    Returns a Lambda response that the Gateway forwards back to the agent.
    """
    print(f"[RAG] Event: {json.dumps(event)}")

    # Parse input — the Gateway may deliver the payload directly or wrapped in a 'body' key
    # depending on the integration type. Handle both.
    if isinstance(event.get("body"), str):
        try:
            body = json.loads(event["body"])
        except Exception:
            body = {}
    elif isinstance(event.get("body"), dict):
        body = event["body"]
    else:
        body = event  # direct invocation (CLI test)

    query     = body.get("query", "").strip()
    tenant_id = body.get("tenantId", "").strip()   # = org_id (Phase 4)
    user_id   = body.get("userId", "").strip()      # Cognito sub — for OWNER_ONLY filter
    top_k     = min(int(body.get("topK", TOP_K_DEFAULT)), TOP_K_MAX)

    # Phase 3.4 — scoped retrieval
    doc_ids       = body.get("docIds")
    collection_id = body.get("collectionId")

    # Phase 3 RBAC: role passed by agent from JWT claims.
    # Default to EXTERNAL (fail-safe) if not provided.
    user_role = body.get("userRole", "EXTERNAL").strip().upper()
    if user_role not in ("INTERNAL", "EXTERNAL"):
        user_role = "EXTERNAL"

    if not query:
        return _response(400, {"error": "query is required"})
    if not tenant_id:
        return _response(400, {"error": "tenantId is required"})

    print(
        f"[RAG] query='{query[:80]}' org={tenant_id} user={user_id or 'n/a'} "
        f"topK={top_k} role={user_role} scope=docIds:{doc_ids} col:{collection_id}"
    )

    try:
        result = retrieve(
            query, tenant_id, top_k, user_role,
            user_id=user_id,
            doc_ids=doc_ids,
            collection_id=collection_id,
        )
        return _response(200, result)
    except Exception as e:
        print(f"[RAG ERROR] {e}")
        import traceback
        traceback.print_exc()
        return _response(500, {"error": str(e)})


# Core retrieval logic

# RBAC visibility rules (Phase 3):
# INTERNAL role → can see all visibility modes
# EXTERNAL role → restricted to EXTERNAL_ALLOWED only
VISIBILITY_BY_ROLE = {
    "INTERNAL": ("INTERNAL_ONLY", "EXTERNAL_ALLOWED"),
    "EXTERNAL": ("EXTERNAL_ALLOWED",),
}


def retrieve(
    query:         str,
    tenant_id:     str,    # = org_id (Phase 4)
    top_k:         int,
    user_role:     str = "EXTERNAL",
    user_id:       str = "",
    doc_ids:       list | None = None,
    collection_id: str | None = None,
) -> dict:
    """
    Full retrieval pipeline: embed → search → filter → format.

    Args:
        query:     The user's question text.
        tenant_id: org_id — scopes retrieval to this organisation's documents.
        top_k:     Maximum number of chunks to return (capped at TOP_K_MAX=8).
        user_role: "INTERNAL" or "EXTERNAL" — controls visibility_mode filter.
        user_id:   Cognito sub — used to include OWNER_ONLY chunks owned by this user.

    Returns:
        {
            "context_block": str,
            "chunks_found":  int,
            "query":         str,
        }
    """
    # Resolve allowed visibility modes for this role
    allowed_visibility = VISIBILITY_BY_ROLE.get(user_role, VISIBILITY_BY_ROLE["EXTERNAL"])

    # Phase 3.4 — resolve scoped doc_ids
    resolved_doc_ids: list[str] = []
    if collection_id and DOCS_TABLE_NAME:
        resolved_doc_ids = _resolve_collection_members(tenant_id, collection_id)
        print(f"[RAG] collection {collection_id} resolved to {len(resolved_doc_ids)} docs")
    if doc_ids:
        merged = set(resolved_doc_ids) | set(doc_ids)
        resolved_doc_ids = list(merged)

    # Step 1 — Embed the query
    query_vector = _embed_text(query)

    # Step 2 — Two-stage vector retrieval with org-level + sharing_scope filter
    fetch_limit = top_k * 2
    raw_chunks  = _vector_search(
        query_vector, tenant_id, fetch_limit, allowed_visibility,
        resolved_doc_ids, user_id=user_id,
    )

    # Step 3 — Similarity threshold filter
    # cosine similarity = 1 - cosine_distance
    # Aurora returns (1 - distance) as the similarity column.
    filtered = [c for c in raw_chunks if c["similarity"] >= SIMILARITY_CUTOFF]

    # Respect topK after filtering
    final_chunks = filtered[:top_k]

    print(
        f"[RAG] fetched={len(raw_chunks)} after_filter={len(filtered)} "
        f"returned={len(final_chunks)} threshold={SIMILARITY_CUTOFF} "
        f"org={tenant_id} user={user_id or 'n/a'} role={user_role}"
    )

    if not final_chunks:
        print(f"[RAG] No relevant chunks found above threshold for org={tenant_id}")
        return {
            "context_block": "",
            "chunks_found":  0,
            "query":         query,
        }

    # Step 4 — Format context block
    # INTERNAL users get full citations including docId (for Source Viewer in Phase 3.5).
    # EXTERNAL users get masked citations — no knowledge structure exposed.
    context_block = _format_context(final_chunks, user_role)

    return {
        "context_block": context_block,
        "chunks_found":  len(final_chunks),
        "query":         query,
    }


# Collection membership resolver (Phase 3.4)

def _resolve_collection_members(tenant_id: str, collection_id: str) -> list[str]:
    """
    Query DynamoDB for all DOC#{docId} SK items under the collection PK.
    Returns a list of docIds that belong to this collection for this tenant.
    Uses KeyConditionExpression with begins_with on SK = 'DOC#' to retrieve
    only membership records, not the METADATA record.
    O(N) where N = number of docs in the collection — acceptable at personal scale.
    """
    table = dynamodb_res.Table(DOCS_TABLE_NAME)
    pk    = f"TENANT#{tenant_id}#COL#{collection_id}"

    resp = table.query(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("DOC#")
    )
    return [item["SK"][4:] for item in resp.get("Items", [])]  # strip "DOC#" prefix


# Embedding

def _embed_text(text: str) -> list[float]:
    """
    Embed text using Amazon Titan Embed V2.

    Parameters MUST match ingestion-worker exactly:
      - modelId:    amazon.titan-embed-text-v2:0
      - dimensions: 1024
      - normalize:  True

    Any mismatch would put the query vector in a different space
    from the stored chunk vectors, yielding garbage similarity scores.
    """
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "inputText":  text,
            "dimensions": EMBED_DIMENSIONS,
            "normalize":  True,
        }),
    )
    return json.loads(response["body"].read())["embedding"]


# pgvector search via RDS Data API

def _vector_search(
    query_vector:       list[float],
    tenant_id:          str,           # = org_id (Phase 4)
    fetch_limit:        int,
    allowed_visibility: tuple[str, ...] = ("EXTERNAL_ALLOWED",),
    doc_ids:            list[str] | None = None,
    user_id:            str = "",
) -> list[dict]:
    """
    Cosine similarity search using pgvector HNSW index.

    Phase 4 sharing_scope filter:
      For each chunk to be eligible, one of the following must be true:
        (a) sharing_scope = 'ORG_SHARED'     — visible to any active org member
        (b) owner_user_id = :user_id         — user owns this OWNER_ONLY chunk

      SQL condition: (sharing_scope = 'ORG_SHARED' OR owner_user_id = :user_id)

    Combined with:
      - tenant_id = :org_id                  — org isolation
      - visibility_mode IN (...)             — role-class RBAC (INTERNAL/EXTERNAL)

    All enforcement is in SQL — not just in UI — so crafted tool calls cannot bypass.
    """
    db_cluster_arn = _get_ssm("aurora-cluster-arn")
    db_secret_arn  = _get_ssm("aurora-secret-arn")
    db_name        = _get_ssm("aurora-db-name")

    vector_literal = "[" + ",".join(f"{v:.8f}" for v in query_vector) + "]"

    vis_params = [f":vis_{i}" for i in range(len(allowed_visibility))]
    vis_clause = ", ".join(vis_params)

    doc_clause = ""
    if doc_ids:
        doc_params_names = [f":doc_{i}" for i in range(len(doc_ids))]
        doc_clause = f"AND doc_id IN ({', '.join(doc_params_names)})"

    # Phase 4: sharing_scope filter.
    # If user_id is provided, include OWNER_ONLY chunks they own.
    # If user_id is empty (shouldn't happen in production), only ORG_SHARED.
    if user_id:
        scope_clause = "(sharing_scope = 'ORG_SHARED' OR owner_user_id = :user_id)"
    else:
        scope_clause = "sharing_scope = 'ORG_SHARED'"

    sql = f"""
        SELECT
            content,
            file_name,
            doc_id,
            chunk_index,
            chunk_total,
            page_number,
            section_title,
            source_type,
            visibility_mode,
            sharing_scope,
            owner_user_id,
            1 - (embedding <=> :query_vec::vector) AS similarity
        FROM  fast_chunks
        WHERE tenant_id      = :tenant_id
          AND visibility_mode IN ({vis_clause})
          AND {scope_clause}
          {doc_clause}
        ORDER BY embedding <=> :query_vec::vector
        LIMIT :fetch_limit
    """

    params = [
        {"name": "query_vec",   "value": {"stringValue": vector_literal}},
        {"name": "tenant_id",   "value": {"stringValue": tenant_id}},
        {"name": "fetch_limit", "value": {"longValue":   fetch_limit}},
    ]
    for i, vis in enumerate(allowed_visibility):
        params.append({"name": f"vis_{i}", "value": {"stringValue": vis}})
    if user_id:
        params.append({"name": "user_id", "value": {"stringValue": user_id}})
    if doc_ids:
        for i, did in enumerate(doc_ids):
            params.append({"name": f"doc_{i}", "value": {"stringValue": did}})

    response = rds_data.execute_statement(
        resourceArn=db_cluster_arn,
        secretArn=db_secret_arn,
        database=db_name,
        sql=sql,
        parameters=params,
        includeResultMetadata=True,
    )

    return _parse_rds_response(response)


def _parse_rds_response(response: dict) -> list[dict]:
    """
    Convert RDS Data API response into a list of plain dicts.

    RDS Data API returns:
        columnMetadata: [{"name": "content"}, {"name": "file_name"}, ...]
        records: [[{"stringValue": "..."}, ...], ...]

    We zip column names with values to produce dicts matching the SELECT columns.
    """
    columns = [col["name"] for col in response.get("columnMetadata", [])]
    chunks  = []

    for row in response.get("records", []):
        chunk = {}
        for col_name, cell in zip(columns, row):
            # RDS Data API wraps each value in a type key.
            # Extract the actual value regardless of type.
            if "isNull" in cell and cell["isNull"]:
                chunk[col_name] = None
            elif "stringValue" in cell:
                chunk[col_name] = cell["stringValue"]
            elif "longValue" in cell:
                chunk[col_name] = cell["longValue"]
            elif "doubleValue" in cell:
                chunk[col_name] = cell["doubleValue"]
            elif "booleanValue" in cell:
                chunk[col_name] = cell["booleanValue"]
            else:
                chunk[col_name] = None
        chunks.append(chunk)

    return chunks


# Context formatting

def _format_context(chunks: list[dict], user_role: str = "EXTERNAL") -> str:
    """
    Format retrieved chunks into a context block for the agent prompt.

    Citation format by role (Phase 3 RBAC):

    INTERNAL role — full citation with docId for Source Viewer (Phase 3.5):
        [Source: <file_name>, docId:<doc_id>, page <N>, chunk <X>/<Y>]
        <content>

    EXTERNAL role — masked citation, no knowledge structure exposed:
        [Source: Internal Knowledge Base]
        <content>

    Design note: the citation prefix is kept inside the context block
    (not as a separate metadata field) so the LLM sees it as part of
    the text it is summarising. This produces more natural inline citations.

    The docId in INTERNAL citations is parsed by the frontend Source Viewer
    component (Phase 3.5) to fetch and display the specific chunk.
    """
    parts = []

    for chunk in chunks:
        file_name   = chunk.get("file_name")   or "unknown"
        doc_id      = chunk.get("doc_id")       or ""
        page_num    = chunk.get("page_number")
        chunk_index = chunk.get("chunk_index")
        chunk_total = chunk.get("chunk_total")
        content     = (chunk.get("content") or "").strip()
        similarity  = chunk.get("similarity", 0)

        if user_role == "INTERNAL":
            # Full citation — includes docId for Source Viewer linkage
            page_part  = f", page {page_num}" if page_num is not None else ""
            chunk_part = (
                f", chunk {chunk_index + 1}/{chunk_total}"
                if chunk_index is not None and chunk_total is not None
                else ""
            )
            doc_id_part = f", docId:{doc_id}" if doc_id else ""
            header = f"[Source: {file_name}{doc_id_part}{page_part}{chunk_part}]"
        else:
            # Masked citation — external users see answers, not knowledge structure
            header = "[Source: Internal Knowledge Base]"

        print(f"[RAG] including chunk: {header} similarity={similarity:.3f}")

        parts.append(f"{header}\n{content}")

    return "\n---\n".join(parts)


# Response helper

def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(body),
    }
