"""
Phase 2C — Ingestion Worker Lambda

Triggered by SQS (batchSize=1) from S3 ObjectCreated events.

Flow per document:
  1. Parse SQS → S3 event → get bucket + key
  2. Read S3 object metadata (doc-id, tenant-id, version stored by presign Lambda)
  3. Download file from S3
  4. Detect type → apply type-specific chunking strategy
  5. For each chunk: call Bedrock Titan Embed V2 → 1024-dim vector
  6. INSERT chunks into Aurora pgvector via RDS Data API
  7. Update DynamoDB: status UPLOADED → READY (or FAILED on error)

Chunking strategy by type:
  PDF   — page-aware, paragraph split, table detection, 600 tokens / 100 overlap
  DOCX  — heading section grouping, paragraph accumulation, 600 tokens / 100 overlap
  XLSX  — 30-row groups, column headers repeated in every chunk
  CSV   — 40-row groups, column headers repeated in every chunk
  TXT   — paragraph accumulation, 600 tokens / 100 overlap
  MD    — H1/H2/H3 section grouping, 600 tokens / 100 overlap

Complexity target: O(N) per document where N = total tokens in the document.
All token counting is done on integer token-id lists, not on strings.
"""

import csv as csv_module
import io
import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

import boto3
import tiktoken

s3         = boto3.client("s3")
bedrock    = boto3.client("bedrock-runtime")
rds_data   = boto3.client("rds-data")
dynamodb   = boto3.client("dynamodb")
ssm        = boto3.client("ssm")

STACK_NAME      = os.environ["STACK_NAME"]
DOCS_TABLE_NAME = os.environ["DOCS_TABLE_NAME"]

# Tokenizer loaded once at cold start, reused for every document in this container
TOKENIZER = tiktoken.get_encoding("cl100k_base")

CHUNK_TOKENS        = 600
OVERLAP_TOKENS      = 100
MIN_CHUNK_TOKENS    = 20
CSV_ROWS_PER_CHUNK  = 40
XLSX_ROWS_PER_CHUNK = 30

# SSM values read once per container lifetime, cached in memory
_ssm_cache: dict[str, str] = {}


def get_ssm(key: str) -> str:
    if key not in _ssm_cache:
        _ssm_cache[key] = ssm.get_parameter(
            Name=f"/{STACK_NAME}/rag/{key}"
        )["Parameter"]["Value"]
    return _ssm_cache[key]


@dataclass
class Chunk:
    content:       str
    chunk_index:   int
    page_number:   int | None = None
    section_title: str | None = None
    heading_level: int | None = None
    sheet_name:    str | None = None
    row_start:     int | None = None
    row_end:       int | None = None


# Entry point

def handler(event, context):
    for record in event.get("Records", []):
        try:
            process_sqs_record(record)
        except Exception as e:
            print(f"[ERROR] {e}")
            raise


def process_sqs_record(record: dict):
    body       = json.loads(record["body"])
    s3_records = body.get("Records", [])
    for s3_record in s3_records:
        bucket = s3_record["s3"]["bucket"]["name"]
        key    = urllib.parse.unquote_plus(s3_record["s3"]["object"]["key"])
        print(f"[INGEST] s3://{bucket}/{key}")
        process_document(bucket, key)


def process_document(bucket: str, key: str):
    head     = s3.head_object(Bucket=bucket, Key=key)
    obj_meta = head.get("Metadata", {})
    tenant_id = obj_meta.get("tenant-id")
    doc_id    = obj_meta.get("doc-id")
    version   = int(obj_meta.get("version", "1"))
    file_name = key.split("/")[-1]

    if not tenant_id or not doc_id:
        raise ValueError(f"Missing S3 metadata (tenant-id, doc-id) on key: {key}")

    print(f"[INGEST] tenant={tenant_id} doc={doc_id} v={version} file={file_name}")

    db_cluster_arn = get_ssm("aurora-cluster-arn")
    db_secret_arn  = get_ssm("aurora-secret-arn")
    db_name        = get_ssm("aurora-db-name")

    try:
        obj      = s3.get_object(Bucket=bucket, Key=key)
        raw_data = obj["Body"].read()

        ext    = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else "txt"
        chunks = chunk_document(raw_data, file_name, ext)

        if not chunks:
            raise ValueError(f"No chunks produced for {file_name}")

        chunk_total = len(chunks)
        print(f"[INGEST] {chunk_total} chunks for {file_name}")

        for chunk in chunks:
            embedding = embed_text(chunk.content)
            insert_chunk(
                db_cluster_arn, db_secret_arn, db_name,
                tenant_id, doc_id, key, file_name, ext,
                chunk, chunk_total, embedding
            )

        update_doc_status(tenant_id, doc_id, version, "READY", chunk_total=chunk_total)
        print(f"[INGEST] Done — {chunk_total} chunks stored for doc {doc_id}")

    except Exception as e:
        print(f"[INGEST ERROR] {e}")
        update_doc_status(tenant_id, doc_id, version, "FAILED", error_message=str(e))
        raise


# Chunking dispatch

def chunk_document(raw_data: bytes, file_name: str, ext: str) -> list[Chunk]:
    dispatch = {
        "pdf":  chunk_pdf,
        "docx": chunk_docx,
        "doc":  chunk_docx,
        "xlsx": chunk_xlsx,
        "xls":  chunk_xlsx,
        "csv":  chunk_csv,
        "txt":  chunk_text,
        "md":   chunk_markdown,
        "json": chunk_text,
    }
    return dispatch.get(ext, chunk_text)(raw_data, file_name)


# PDF chunking

def chunk_pdf(raw_data: bytes, file_name: str) -> list[Chunk]:
    import PyPDF2

    reader = PyPDF2.PdfReader(io.BytesIO(raw_data))
    chunks: list[Chunk] = []

    for page_num, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        if not page_text.strip():
            continue

        for para in _split_paragraphs(page_text):
            if not para:
                continue

            # Encode once per paragraph, reuse the token list for both checks — O(N) not O(2N)
            para_ids = TOKENIZER.encode(para)

            if len(para_ids) < MIN_CHUNK_TOKENS:
                continue

            if _ids_look_like_table(para):
                chunks.append(Chunk(
                    content=para,
                    chunk_index=len(chunks),
                    page_number=page_num,
                ))
                continue

            for window_ids in _token_windows(para_ids, CHUNK_TOKENS, OVERLAP_TOKENS):
                if len(window_ids) >= MIN_CHUNK_TOKENS:
                    chunks.append(Chunk(
                        content=TOKENIZER.decode(window_ids),
                        chunk_index=len(chunks),
                        page_number=page_num,
                    ))

    return chunks


def _ids_look_like_table(text: str) -> bool:
    # Single pass over lines — O(N) where N = number of lines
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    table_count = sum(1 for l in lines if l.count("\t") >= 2 or l.count("|") >= 2)
    return table_count / len(lines) >= 0.5


# DOCX chunking

def chunk_docx(raw_data: bytes, file_name: str) -> list[Chunk]:
    from docx import Document

    doc    = Document(io.BytesIO(raw_data))
    chunks: list[Chunk] = []

    current_heading:     str | None  = None
    # Store token ids directly instead of re-encoding an ever-growing string — O(N) total
    current_ids:         list[int]   = []
    current_token_count: int         = 0
    overlap_ids:         list[int]   = []

    def flush(heading: str | None):
        if not current_ids:
            return
        for window in _token_windows(current_ids, CHUNK_TOKENS, OVERLAP_TOKENS):
            if len(window) >= MIN_CHUNK_TOKENS:
                chunks.append(Chunk(
                    content=TOKENIZER.decode(window),
                    chunk_index=len(chunks),
                    section_title=heading,
                ))

    for para in doc.paragraphs:
        style_name = para.style.name if para.style else ""
        text       = para.text.strip()
        if not text:
            continue

        if style_name.startswith("Heading"):
            flush(current_heading)
            current_heading     = text
            current_ids         = list(overlap_ids)
            current_token_count = len(overlap_ids)
        else:
            # Encode paragraph once, append ids — never re-encode accumulated buffer
            para_ids             = TOKENIZER.encode("\n" + text)
            current_ids         += para_ids
            current_token_count += len(para_ids)

            if current_token_count >= CHUNK_TOKENS:
                flush(current_heading)
                overlap_ids         = current_ids[-OVERLAP_TOKENS:]
                current_ids         = list(overlap_ids)
                current_token_count = len(overlap_ids)

    flush(current_heading)
    return chunks


# XLSX chunking

def chunk_xlsx(raw_data: bytes, file_name: str) -> list[Chunk]:
    import openpyxl

    wb     = openpyxl.load_workbook(io.BytesIO(raw_data), read_only=True, data_only=True)
    chunks: list[Chunk] = []

    for sheet in wb.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue

        headers     = [str(c) if c is not None else "" for c in rows[0]]
        # Build header line once, reuse as prefix for every chunk — O(1) per chunk
        header_line = "Columns: " + " | ".join(headers)
        data_rows   = rows[1:]

        for start in range(0, len(data_rows), XLSX_ROWS_PER_CHUNK):
            batch   = data_rows[start: start + XLSX_ROWS_PER_CHUNK]
            lines   = [f"Sheet: {sheet.title}", header_line]
            lines  += [" | ".join(str(c) if c is not None else "" for c in row) for row in batch]
            content = "\n".join(lines)

            if len(TOKENIZER.encode(content)) >= MIN_CHUNK_TOKENS:
                chunks.append(Chunk(
                    content=content,
                    chunk_index=len(chunks),
                    sheet_name=sheet.title,
                    row_start=start + 1,
                    row_end=start + len(batch),
                ))

    return chunks


# CSV chunking

def chunk_csv(raw_data: bytes, file_name: str) -> list[Chunk]:
    text     = raw_data.decode("utf-8", errors="replace")
    reader   = csv_module.reader(io.StringIO(text))
    all_rows = list(reader)

    if not all_rows:
        return []

    headers     = all_rows[0]
    # Build header line once, reuse as prefix for every chunk — O(1) per chunk
    header_line = "Columns: " + " | ".join(headers)
    data_rows   = all_rows[1:]
    chunks: list[Chunk] = []

    for start in range(0, len(data_rows), CSV_ROWS_PER_CHUNK):
        batch   = data_rows[start: start + CSV_ROWS_PER_CHUNK]
        lines   = [header_line] + [" | ".join(row) for row in batch]
        content = "\n".join(lines)

        if len(TOKENIZER.encode(content)) >= MIN_CHUNK_TOKENS:
            chunks.append(Chunk(
                content=content,
                chunk_index=len(chunks),
                sheet_name=file_name,
                row_start=start + 1,
                row_end=start + len(batch),
            ))

    return chunks


# TXT / JSON chunking

def chunk_text(raw_data: bytes, file_name: str) -> list[Chunk]:
    text   = raw_data.decode("utf-8", errors="replace")
    chunks: list[Chunk] = []

    # Accumulate paragraph token ids directly — never re-encode the buffer string
    buffer_ids: list[int] = []

    for para in _split_paragraphs(text):
        if not para:
            continue

        para_ids     = TOKENIZER.encode(para)
        buffer_ids  += para_ids

        if len(buffer_ids) >= CHUNK_TOKENS:
            for window in _token_windows(buffer_ids, CHUNK_TOKENS, OVERLAP_TOKENS):
                if len(window) >= MIN_CHUNK_TOKENS:
                    chunks.append(Chunk(content=TOKENIZER.decode(window), chunk_index=len(chunks)))
            buffer_ids = buffer_ids[-OVERLAP_TOKENS:]

    if len(buffer_ids) >= MIN_CHUNK_TOKENS:
        chunks.append(Chunk(content=TOKENIZER.decode(buffer_ids), chunk_index=len(chunks)))

    return chunks


# Markdown chunking

def chunk_markdown(raw_data: bytes, file_name: str) -> list[Chunk]:
    text       = raw_data.decode("utf-8", errors="replace")
    chunks: list[Chunk] = []
    heading_re = re.compile(r"^(#{1,3})\s+(.*)")

    current_heading: str | None  = None
    current_level:   int | None  = None
    # Accumulate body token ids directly — never re-encode the body string
    current_ids:     list[int]   = []

    def flush(heading: str | None, level: int | None):
        if not current_ids:
            return
        for window in _token_windows(current_ids, CHUNK_TOKENS, OVERLAP_TOKENS):
            if len(window) >= MIN_CHUNK_TOKENS:
                chunks.append(Chunk(
                    content=TOKENIZER.decode(window),
                    chunk_index=len(chunks),
                    section_title=heading,
                    heading_level=level,
                ))

    for line in text.splitlines():
        m = heading_re.match(line)
        if m:
            flush(current_heading, current_level)
            current_level   = len(m.group(1))
            current_heading = m.group(2).strip()
            current_ids     = []
        else:
            current_ids += TOKENIZER.encode("\n" + line)

    flush(current_heading, current_level)
    return chunks


# Token-space helpers

def _split_paragraphs(text: str) -> list[str]:
    # Split on blank lines — O(N) single pass
    return [p.strip() for p in re.split(r"\n\s*\n", text)]


def _token_windows(ids: list[int], max_tokens: int, overlap: int) -> list[list[int]]:
    """
    Sliding window over a pre-encoded token id list.
    Operates entirely in integer space — no string operations inside the loop.
    O(N) total: each token is visited a constant number of times proportional to
    overlap ratio. With 100/600 overlap (~16%) each token appears in at most 2 windows.

    Sentence boundary snapping: scans backward from window end in token-id space
    looking for period/newline tokens. Single backward scan, max 20% of window = O(1)
    per window, not O(N).
    """
    if not ids:
        return []

    # Period and newline token ids in cl100k_base (precomputed — O(1) lookup)
    _PERIOD_IDS = {
        TOKENIZER.encode(s)[0]
        for s in (".", ".\n", "! ", "? ", "!\n", "?\n")
        if TOKENIZER.encode(s)
    }

    step    = max_tokens - overlap
    windows = []
    start   = 0

    while start < len(ids):
        end   = min(start + max_tokens, len(ids))
        chunk = ids[start:end]

        # Snap to sentence boundary: scan back at most 20% of window — O(1) per window
        snap_start = max(0, len(chunk) - max_tokens // 5)
        for i in range(len(chunk) - 1, snap_start, -1):
            if chunk[i] in _PERIOD_IDS:
                chunk = chunk[: i + 1]
                break

        windows.append(chunk)
        start += step

    return windows


# Embedding — Bedrock Titan Embed V2

def embed_text(text: str) -> list[float]:
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "inputText":  text,
            "dimensions": 1024,
            "normalize":  True,
        }),
    )
    return json.loads(response["body"].read())["embedding"]


# Aurora INSERT via RDS Data API

def insert_chunk(
    db_cluster_arn: str,
    db_secret_arn:  str,
    db_name:        str,
    tenant_id:      str,
    doc_id:         str,
    s3_key:         str,
    file_name:      str,
    source_type:    str,
    chunk:          Chunk,
    chunk_total:    int,
    embedding:      list[float],
):
    vector_literal = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

    sql = """
        INSERT INTO fast_chunks (
            tenant_id, doc_id, s3_key, file_name, source_type,
            chunk_index, chunk_total, content, embedding,
            page_number, section_title, heading_level,
            sheet_name, row_start, row_end
        ) VALUES (
            :tenant_id, :doc_id, :s3_key, :file_name, :source_type,
            :chunk_index, :chunk_total, :content, :embedding::vector,
            :page_number, :section_title, :heading_level,
            :sheet_name, :row_start, :row_end
        )
    """

    def _str(name: str, value: str | None) -> dict:
        return {"name": name, "value": {"isNull": True} if value is None else {"stringValue": value}}

    def _int(name: str, value: int | None) -> dict:
        return {"name": name, "value": {"isNull": True} if value is None else {"longValue": value}}

    rds_data.execute_statement(
        resourceArn=db_cluster_arn,
        secretArn=db_secret_arn,
        database=db_name,
        sql=sql,
        parameters=[
            _str("tenant_id",     tenant_id),
            _str("doc_id",        doc_id),
            _str("s3_key",        s3_key),
            _str("file_name",     file_name),
            _str("source_type",   source_type),
            _int("chunk_index",   chunk.chunk_index),
            _int("chunk_total",   chunk_total),
            _str("content",       chunk.content),
            _str("embedding",     vector_literal),
            _int("page_number",   chunk.page_number),
            _str("section_title", chunk.section_title),
            _int("heading_level", chunk.heading_level),
            _str("sheet_name",    chunk.sheet_name),
            _int("row_start",     chunk.row_start),
            _int("row_end",       chunk.row_end),
        ],
    )


# DynamoDB status update

def update_doc_status(
    tenant_id:     str,
    doc_id:        str,
    version:       int,
    status:        str,
    chunk_total:   int = 0,
    error_message: str = "",
):
    from datetime import datetime, timezone

    pk = f"TENANT#{tenant_id}#DOC#{doc_id}"
    sk = f"VER#{version:06d}"

    expr_parts:  list[str]       = ["#s = :status", "updatedAt = :now"]
    expr_values: dict[str, Any]  = {
        ":status": {"S": status},
        ":now":    {"S": datetime.now(timezone.utc).isoformat()},
    }

    if status == "READY" and chunk_total:
        expr_parts.append("chunkCount = :cc")
        expr_values[":cc"] = {"N": str(chunk_total)}

    if status == "FAILED" and error_message:
        expr_parts.append("errorMessage = :err")
        expr_values[":err"] = {"S": error_message[:500]}

    dynamodb.update_item(
        TableName=DOCS_TABLE_NAME,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeValues=expr_values,
        ExpressionAttributeNames={"#s": "status"},
    )
    print(f"[DYNAMO] {pk} {sk} → {status}")
