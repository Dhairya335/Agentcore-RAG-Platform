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

# AWS clients
bedrock  = boto3.client("bedrock-runtime")
rds_data = boto3.client("rds-data")
ssm      = boto3.client("ssm")

# Config
STACK_NAME         = os.environ["STACK_NAME"]
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
    tenant_id = body.get("tenantId", "").strip()
    top_k     = min(int(body.get("topK", TOP_K_DEFAULT)), TOP_K_MAX)

    if not query:
        return _response(400, {"error": "query is required"})
    if not tenant_id:
        return _response(400, {"error": "tenantId is required"})

    print(f"[RAG] query='{query[:80]}' tenant={tenant_id} topK={top_k}")

    try:
        result = retrieve(query, tenant_id, top_k)
        return _response(200, result)
    except Exception as e:
        print(f"[RAG ERROR] {e}")
        import traceback
        traceback.print_exc()
        return _response(500, {"error": str(e)})


# Core retrieval logic

def retrieve(query: str, tenant_id: str, top_k: int) -> dict:
    """
    Full retrieval pipeline: embed → search → filter → format.

    Returns:
        {
            "context_block": str,   # formatted context for agent prompt
            "chunks_found":  int,   # number of chunks after filtering
            "query":         str,   # echo of original query
        }
    """
    # Step 1 — Embed the query
    # Uses the same model + settings as ingestion-worker to ensure
    # vectors are comparable (same space, same normalisation).
    query_vector = _embed_text(query)

    # Step 2 — Two-stage vector retrieval
    # Fetch topK*2 candidates from Aurora (HNSW approximate search),
    # then filter in Lambda to topK by similarity threshold.
    # This improves precision without a second DB round-trip.
    fetch_limit = top_k * 2
    raw_chunks  = _vector_search(query_vector, tenant_id, fetch_limit)

    # Step 3 — Similarity threshold filter
    # cosine similarity = 1 - cosine_distance
    # Aurora returns (1 - distance) as the similarity column.
    filtered = [c for c in raw_chunks if c["similarity"] >= SIMILARITY_CUTOFF]

    # Respect topK after filtering
    final_chunks = filtered[:top_k]

    print(
        f"[RAG] fetched={len(raw_chunks)} after_filter={len(filtered)} "
        f"returned={len(final_chunks)} threshold={SIMILARITY_CUTOFF}"
    )

    if not final_chunks:
        print("[RAG] No relevant chunks found above threshold")
        return {
            "context_block": "",
            "chunks_found":  0,
            "query":         query,
        }

    # Step 4 — Format context block
    context_block = _format_context(final_chunks)

    return {
        "context_block": context_block,
        "chunks_found":  len(final_chunks),
        "query":         query,
    }


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
    query_vector: list[float],
    tenant_id:    str,
    fetch_limit:  int,
) -> list[dict]:
    """
    Cosine similarity search using pgvector HNSW index.

    SQL notes:
      - `embedding <=> :query_vec::vector` = cosine DISTANCE (0 = identical, 2 = opposite)
      - `1 - (embedding <=> ...)` = cosine SIMILARITY (1 = identical, -1 = opposite)
      - WHERE tenant_id = :tenant_id enforces multi-tenant isolation at the DB level
      - ORDER BY distance ASC (closest first) + LIMIT = top-K approximate neighbours
      - The HNSW index on (embedding vector_cosine_ops) is used automatically

    We do NOT use OFFSET or pagination — retrieval is always a fresh top-K query.
    """
    db_cluster_arn = _get_ssm("aurora-cluster-arn")
    db_secret_arn  = _get_ssm("aurora-secret-arn")
    db_name        = _get_ssm("aurora-db-name")

    # Serialise vector as PostgreSQL literal: [v1,v2,...,v1024]
    vector_literal = "[" + ",".join(f"{v:.8f}" for v in query_vector) + "]"

    sql = """
        SELECT
            content,
            file_name,
            chunk_index,
            chunk_total,
            page_number,
            section_title,
            source_type,
            1 - (embedding <=> :query_vec::vector) AS similarity
        FROM  fast_chunks
        WHERE tenant_id = :tenant_id
        ORDER BY embedding <=> :query_vec::vector
        LIMIT :fetch_limit
    """

    params = [
        {"name": "query_vec",   "value": {"stringValue": vector_literal}},
        {"name": "tenant_id",   "value": {"stringValue": tenant_id}},
        {"name": "fetch_limit", "value": {"longValue":   fetch_limit}},
    ]

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

def _format_context(chunks: list[dict]) -> str:
    """
    Format retrieved chunks into a context block for the agent prompt.

    Each chunk is prefixed with a source citation line so the agent can
    produce inline citations in the format [Source: file_name, page N].

    Format per chunk:
        [Source: <file_name>, page <page_number>, chunk <index+1>/<total>]
        <content>

    Chunks are separated by "---" to visually delimit boundaries.

    Design note: the citation prefix is kept inside the context block
    (not as a separate metadata field) so the LLM sees it as part of
    the text it is summarising. This produces more natural inline citations.
    """
    parts = []

    for chunk in chunks:
        file_name   = chunk.get("file_name")   or "unknown"
        page_num    = chunk.get("page_number")
        chunk_index = chunk.get("chunk_index")
        chunk_total = chunk.get("chunk_total")
        content     = (chunk.get("content") or "").strip()
        similarity  = chunk.get("similarity", 0)

        # Build citation header
        page_part  = f", page {page_num}" if page_num is not None else ""
        chunk_part = (
            f", chunk {chunk_index + 1}/{chunk_total}"
            if chunk_index is not None and chunk_total is not None
            else ""
        )
        header = f"[Source: {file_name}{page_part}{chunk_part}]"

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
