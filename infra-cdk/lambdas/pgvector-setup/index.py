"""
Custom Resource Lambda: Idempotent pgvector Schema Initialiser

  Called by CloudFormation (via CDK Custom Resource) on every cdk deploy.
  Uses RDS Data API — Lambda does NOT need to be inside the VPC.

  What it does:
    Create  -> runs full schema init (extension + table + indexes)
    Update  -> re-runs schema init (all statements use IF NOT EXISTS — safe)
    Delete  -> no-op (stack delete keeps schema; Aurora cluster deleted by CFN)

  Schema layout:
    fast_chunks table — one row per chunk, stores:
      - vector embedding (1024 dims — Titan Embed V2)
      - full metadata for hybrid search (page, section, sheet, etc.)
      - source_type for per-type filtering
"""

import json
import os
import urllib.request

import boto3

rds_data = boto3.client("rds-data")

DB_CLUSTER_ARN = os.environ["DB_CLUSTER_ARN"]
DB_SECRET_ARN  = os.environ["DB_SECRET_ARN"]
DB_NAME        = os.environ["DB_NAME"]

SCHEMA_STATEMENTS = [

    # 1. pgvector extension — must be first
    "CREATE EXTENSION IF NOT EXISTS vector",

    # 2. Main chunks table
    # WHY these columns:
    #   embedding vector(1024) — Titan Embed V2 output dimension (NOT 1536/OpenAI)
    #   source_type            — pdf | docx | xlsx | csv | txt | md
    #   page_number            — PDF page awareness (NULL for non-PDF)
    #   section_title          — DOCX/MD heading text (NULL for others)
    #   heading_level          — MD heading depth: 1=H1, 2=H2, 3=H3 (NULL if not MD)
    #   sheet_name             — XLSX/CSV sheet or file name (NULL for others)
    #   row_start / row_end    — XLSX/CSV row range for this chunk (NULL for others)
    #   chunk_index            — position within this document version (0-based)
    #   chunk_total            — total chunks for this document version
    """
    CREATE TABLE IF NOT EXISTS fast_chunks (
        id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id     TEXT        NOT NULL,
        doc_id        TEXT        NOT NULL,
        s3_key        TEXT        NOT NULL,
        file_name     TEXT        NOT NULL,
        source_type   TEXT        NOT NULL,
        chunk_index   INTEGER     NOT NULL,
        chunk_total   INTEGER     NOT NULL,
        content       TEXT        NOT NULL,
        embedding     vector(1024),
        page_number   INTEGER,
        section_title TEXT,
        heading_level INTEGER,
        sheet_name    TEXT,
        row_start     INTEGER,
        row_end       INTEGER,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # 3. HNSW index for approximate nearest-neighbour vector search
    # WHY HNSW over IVFFlat:
    #   HNSW has better recall at query time and doesn't require a training step.
    #   vector_cosine_ops = cosine similarity — best for text embeddings.
    #   m=16, ef_construction=64 = good balance of build speed vs recall.
    """
    CREATE INDEX IF NOT EXISTS fast_chunks_embedding_hnsw_idx
    ON fast_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    """,

    # 4. Supporting indexes for hybrid search filters
    # These let the query planner do: vector similarity + WHERE tenant_id = X
    # which is the core RAG retrieval pattern.
    "CREATE INDEX IF NOT EXISTS fast_chunks_tenant_idx   ON fast_chunks (tenant_id)",
    "CREATE INDEX IF NOT EXISTS fast_chunks_doc_idx      ON fast_chunks (doc_id)",
    "CREATE INDEX IF NOT EXISTS fast_chunks_source_idx   ON fast_chunks (source_type)",
    "CREATE INDEX IF NOT EXISTS fast_chunks_tenant_doc_idx ON fast_chunks (tenant_id, doc_id)",
]


def handler(event, context):
    print(f"Event RequestType: {event.get('RequestType')}")
    request_type = event["RequestType"]
    response_url = event["ResponseURL"]
    stack_id     = event["StackId"]
    request_id   = event["RequestId"]
    logical_id   = event["LogicalResourceId"]
    physical_id  = event.get("PhysicalResourceId", "pgvector-schema")

    try:
        if request_type in ("Create", "Update"):
            setup_schema()
            send_cfn_response(
                response_url, stack_id, request_id, logical_id,
                physical_id, "SUCCESS", {"SchemaStatus": "initialized"}
            )
        elif request_type == "Delete":
            # No-op: we keep the schema. Aurora cluster itself is deleted by CDK.
            print("Delete event — no schema action taken.")
            send_cfn_response(
                response_url, stack_id, request_id, logical_id,
                physical_id, "SUCCESS", {}
            )
    except Exception as e:
        print(f"ERROR during schema setup: {e}")
        send_cfn_response(
            response_url, stack_id, request_id, logical_id,
            physical_id, "FAILED", {}, reason=str(e)
        )


def setup_schema():
    """
    Execute all schema statements via RDS Data API.
    Each statement is run independently so partial failures are visible in logs.
    All use IF NOT EXISTS — fully idempotent.
    """
    print(f"Setting up pgvector schema on cluster: {DB_CLUSTER_ARN}")
    print(f"Database: {DB_NAME}")

    for i, statement in enumerate(SCHEMA_STATEMENTS):
        stmt_preview = statement.strip().split("\n")[0][:80]
        print(f"[{i+1}/{len(SCHEMA_STATEMENTS)}] Executing: {stmt_preview}...")

        rds_data.execute_statement(
            resourceArn=DB_CLUSTER_ARN,
            secretArn=DB_SECRET_ARN,
            database=DB_NAME,
            sql=statement.strip(),
        )

    print("Schema setup complete.")


def send_cfn_response(response_url, stack_id, request_id, logical_id,
                      physical_id, status, data, reason=""):
    """
    Send result back to CloudFormation via the presigned S3 ResponseURL.
    CloudFormation blocks until it receives this PUT.
    """
    body = json.dumps({
        "Status":             status,
        "Reason":             reason or "See CloudWatch logs for details",
        "PhysicalResourceId": physical_id,
        "StackId":            stack_id,
        "RequestId":          request_id,
        "LogicalResourceId":  logical_id,
        "Data":               data,
    }).encode("utf-8")

    req = urllib.request.Request(
        url=response_url,
        data=body,
        method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(body))},
    )

    with urllib.request.urlopen(req) as resp:
        print(f"CloudFormation response sent: HTTP {resp.status}")
