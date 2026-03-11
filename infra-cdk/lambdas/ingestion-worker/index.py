"""
Phase 2C — Ingestion Worker Lambda

Triggered by SQS (batchSize=1) from S3 ObjectCreated events.

Flow per document:
  1. Parse SQS → S3 event → get bucket + key
  2. Read S3 object metadata (doc-id, tenant-id, version stored by presign Lambda)
  3. Download file from S3
  4. Detect type → apply type-specific chunking strategy
  5. For each chunk: call Bedrock Titan Embed V2 → 1024-dim vector
  6. Batch INSERT chunks into Aurora pgvector via RDS Data API
  7. Update DynamoDB: status UPLOADED → READY (or FAILED on error)

Chunking strategy by type:
  PDF   — page-aware, heading/paragraph split, 600 tokens, 100 overlap, tables as single chunk
  DOCX  — heading section grouping, paragraph combine to 600-800 tokens, 100 overlap
  XLSX  — row groups (30 rows), column headers repeated in every chunk
  CSV   — row groups (40 rows), column headers repeated in every chunk
  TXT   — paragraph split → sentence fallback, 600 tokens, 100 overlap
  MD    — H1/H2/H3 section grouping, 600 tokens, 100 overlap
"""

import io
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
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

TOKENIZER = tiktoken.get_encoding("cl100k_base")

# Chunking constants
CHUNK_TOKENS     = 600   
OVERLAP_TOKENS   = 100  
MIN_CHUNK_TOKENS = 20    
CSV_ROWS_PER_CHUNK  = 40
XLSX_ROWS_PER_CHUNK = 30

# SSM cache — read once per Lambda container lifetime 
_ssm_cache: dict[str, str] = {}


def get_ssm(key: str) -> str:
    if key not in _ssm_cache:
        _ssm_cache[key] = ssm.get_parameter(
            Name=f"/{STACK_NAME}/rag/{key}"
        )["Parameter"]["Value"]
    return _ssm_cache[key]


# Chunk dataclass 
@dataclass
class Chunk:
    content:       str
    chunk_index:   int
    # Metadata fields — None if not applicable for this document type
    page_number:   int | None  = None   # PDF: page where chunk starts
    section_title: str | None = None   # DOCX/MD: heading text
    heading_level: int | None = None   # MD only: 1=H1, 2=H2, 3=H3
    sheet_name:    str | None = None   # XLSX/CSV: sheet or file name
    row_start:     int | None = None   # XLSX/CSV: first data row (0-based)
    row_end:       int | None = None   # XLSX/CSV: last data row (inclusive)


def handler(event, context):
    """
    SQS trigger — batchSize=1.
    Each invocation processes exactly one S3 ObjectCreated event.
    Returning normally = SQS deletes the message.
    Raising = SQS retries (up to maxReceiveCount=3 then DLQ).
    """
    for record in event.get("Records", []):
        try:
            process_sqs_record(record)
        except Exception as e:
            print(f"[ERROR] Failed to process record: {e}")
            raise 

def process_sqs_record(record: dict):
    # SQS wraps the S3 notification as a JSON string in record["body"]
    body = json.loads(record["body"])
    s3_records = body.get("Records", [])

    for s3_record in s3_records:
        bucket = s3_record["s3"]["bucket"]["name"]
        key    = urllib.parse.unquote_plus(s3_record["s3"]["object"]["key"])
        print(f"[INGEST] Processing s3://{bucket}/{key}")
        process_document(bucket, key)


# DOCUMENT PROCESSING
def process_document(bucket: str, key: str):
    """
    Full ingestion pipeline for one document.
    Updates DynamoDB status to READY on success, FAILED on any error.
    """
    # 1. Read S3 metadata 
    # doc-id, tenant-id, version were stored by presign Lambda as S3 object metadata.
    # This avoids a DynamoDB lookup and keeps the ingestion worker stateless.
    head = s3.head_object(Bucket=bucket, Key=key)
    obj_meta  = head.get("Metadata", {})
    tenant_id = obj_meta.get("tenant-id")
    doc_id    = obj_meta.get("doc-id")
    version   = int(obj_meta.get("version", "1"))
    file_name = key.split("/")[-1]

    if not tenant_id or not doc_id:
        raise ValueError(
            f"Missing required S3 object metadata (tenant-id, doc-id) on key: {key}"
        )

    print(f"[INGEST] tenant={tenant_id} doc={doc_id} v={version} file={file_name}")

    db_cluster_arn = get_ssm("aurora-cluster-arn")
    db_secret_arn  = get_ssm("aurora-secret-arn")
    db_name        = get_ssm("aurora-db-name")

    try:
        # 2. Download file
        obj      = s3.get_object(Bucket=bucket, Key=key)
        raw_data = obj["Body"].read()

        # 3. Detect type + chunk 
        ext    = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else "txt"
        chunks = chunk_document(raw_data, file_name, ext)

        if not chunks:
            raise ValueError(f"No chunks produced for {file_name} — document may be empty")

        # Set chunk_total now that we know the count
        chunk_total = len(chunks)
        print(f"[INGEST] Produced {chunk_total} chunks for {file_name}")

        # 4. Embed + insert 
        for chunk in chunks:
            embedding = embed_text(chunk.content)
            insert_chunk(
                db_cluster_arn, db_secret_arn, db_name,
                tenant_id, doc_id, key, file_name, ext,
                chunk, chunk_total, embedding
            )

        # 5. Mark READY 
        update_doc_status(tenant_id, doc_id, version, "READY", chunk_total=chunk_total)
        print(f"[INGEST] ✅ Done — {chunk_total} chunks stored for doc {doc_id}")

    except Exception as e:
        print(f"[INGEST ERROR] {e}")
        update_doc_status(tenant_id, doc_id, version, "FAILED", error_message=str(e))
        raise


# CHUNKING — per document type
def chunk_document(raw_data: bytes, file_name: str, ext: str) -> list[Chunk]:
    """Dispatch to the correct chunking strategy based on file extension."""
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
    fn = dispatch.get(ext, chunk_text)
    return fn(raw_data, file_name)


# PDF
def chunk_pdf(raw_data: bytes, file_name: str) -> list[Chunk]:
    """
    Strategy:
    - Extract text with page awareness (preserve page_number metadata)
    - Detect tables: lines with 3+ tab/pipe separators → treat as single chunk
    - Split by heading → paragraph → sentence fallback
    - 600 token chunks with 100 token overlap
    """
    import PyPDF2  # noqa: PLC0415

    reader = PyPDF2.PdfReader(io.BytesIO(raw_data))
    chunks: list[Chunk] = []

    for page_num, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        if not page_text.strip():
            continue

        paragraphs = _split_into_paragraphs(page_text)

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # Table detection: if 3+ cells per line (tabs or pipes), treat as one chunk
            if _looks_like_table(para):
                if _count_tokens(para) >= MIN_CHUNK_TOKENS:
                    chunks.append(Chunk(
                        content=para,
                        chunk_index=len(chunks),
                        page_number=page_num,
                        section_title=None,
                    ))
                continue

            # Regular text: sliding window split
            para_chunks = _sliding_window_split(para, CHUNK_TOKENS, OVERLAP_TOKENS)
            for text in para_chunks:
                if _count_tokens(text) >= MIN_CHUNK_TOKENS:
                    chunks.append(Chunk(
                        content=text,
                        chunk_index=len(chunks),
                        page_number=page_num,
                    ))

    return chunks


def _looks_like_table(text: str) -> bool:
    """Heuristic: table if majority of lines have 3+ tab/pipe separators."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    table_lines = sum(
        1 for l in lines
        if l.count("\t") >= 2 or l.count("|") >= 2
    )
    return table_lines / len(lines) >= 0.5


#    DOCX  
def chunk_docx(raw_data: bytes, file_name: str) -> list[Chunk]:
    """
    Strategy:
    - Iterate paragraphs in document order
    - When a Heading style is encountered, start a new section
    - Accumulate paragraphs within each section until token limit reached
    - Emit chunk with section_title metadata
    - 600-800 tokens per chunk, 100 token overlap between sections
    """
    from docx import Document  # noqa: PLC0415

    doc        = Document(io.BytesIO(raw_data))
    chunks: list[Chunk] = []

    current_section:  str        = ""   # accumulated text for current section
    current_heading:  str | None = None
    pending_overlap:  str        = ""   # last OVERLAP_TOKENS worth of text

    def flush_section(text: str, heading: str | None):
        """Split accumulated section text and emit chunks."""
        parts = _sliding_window_split(text.strip(), CHUNK_TOKENS, OVERLAP_TOKENS)
        for part in parts:
            if _count_tokens(part) >= MIN_CHUNK_TOKENS:
                chunks.append(Chunk(
                    content=part,
                    chunk_index=len(chunks),
                    section_title=heading,
                ))

    for para in doc.paragraphs:
        style_name = para.style.name if para.style else ""
        text       = para.text.strip()

        if not text:
            continue

        is_heading = style_name.startswith("Heading")

        if is_heading:
            # Flush accumulated section before starting new one
            if current_section.strip():
                flush_section(current_section, current_heading)
            current_heading = text
            current_section = pending_overlap  # carry overlap from previous section
        else:
            current_section += "\n" + text

            # If section has grown past CHUNK_TOKENS, flush and carry overlap
            if _count_tokens(current_section) >= CHUNK_TOKENS:
                flush_section(current_section, current_heading)
                # Carry last OVERLAP_TOKENS tokens as overlap for next chunk
                pending_overlap = _get_tail_tokens(current_section, OVERLAP_TOKENS)
                current_section = pending_overlap

    # Flush final section
    if current_section.strip():
        flush_section(current_section, current_heading)

    return chunks


#    XLSX  
def chunk_xlsx(raw_data: bytes, file_name: str) -> list[Chunk]:
    """
    Strategy:
    - One chunk = XLSX_ROWS_PER_CHUNK rows
    - Column headers repeated in every chunk (essential for retrieval context)
    - Metadata: sheet_name, row_start, row_end
    - No overlap — rows are discrete, overlap breaks row boundaries
    """
    import openpyxl  # noqa: PLC0415

    wb     = openpyxl.load_workbook(io.BytesIO(raw_data), read_only=True, data_only=True)
    chunks: list[Chunk] = []

    for sheet in wb.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue

        # First row = headers
        headers = [str(c) if c is not None else "" for c in rows[0]]
        header_line = "Columns: " + " | ".join(headers)
        data_rows   = rows[1:]

        for start in range(0, len(data_rows), XLSX_ROWS_PER_CHUNK):
            batch = data_rows[start : start + XLSX_ROWS_PER_CHUNK]
            lines = [header_line]
            for row in batch:
                lines.append(" | ".join(str(c) if c is not None else "" for c in row))

            content = f"Sheet: {sheet.title}\n" + "\n".join(lines)

            if _count_tokens(content) >= MIN_CHUNK_TOKENS:
                chunks.append(Chunk(
                    content=content,
                    chunk_index=len(chunks),
                    sheet_name=sheet.title,
                    row_start=start + 1,       # +1 to skip header row (1-based)
                    row_end=start + len(batch),
                ))

    return chunks


#    CSV  ─
def chunk_csv(raw_data: bytes, file_name: str) -> list[Chunk]:
    """
    Strategy:
    - One chunk = CSV_ROWS_PER_CHUNK rows
    - Column headers repeated in every chunk
    - Metadata: sheet_name=file_name, row_start, row_end
    - No overlap — discrete rows
    """
    import csv  # noqa: PLC0415

    text    = raw_data.decode("utf-8", errors="replace")
    reader  = csv.reader(io.StringIO(text))
    all_rows = list(reader)

    if not all_rows:
        return []

    headers     = all_rows[0]
    header_line = "Columns: " + " | ".join(headers)
    data_rows   = all_rows[1:]
    chunks: list[Chunk] = []

    for start in range(0, len(data_rows), CSV_ROWS_PER_CHUNK):
        batch = data_rows[start : start + CSV_ROWS_PER_CHUNK]
        lines = [header_line]
        for row in batch:
            lines.append(" | ".join(row))

        content = "\n".join(lines)

        if _count_tokens(content) >= MIN_CHUNK_TOKENS:
            chunks.append(Chunk(
                content=content,
                chunk_index=len(chunks),
                sheet_name=file_name,
                row_start=start + 1,       # 1-based, skipping header
                row_end=start + len(batch),
            ))

    return chunks


#    TXT / JSON                                                                 ─
def chunk_text(raw_data: bytes, file_name: str) -> list[Chunk]:
    """
    Strategy:
    - Split by paragraph (double newline)
    - If paragraph still over limit: sentence split
    - Sliding window: 600 tokens, 100 overlap
    - No mid-sentence breaks
    """
    text       = raw_data.decode("utf-8", errors="replace")
    paragraphs = _split_into_paragraphs(text)
    chunks: list[Chunk] = []

    buffer = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        buffer += "\n\n" + para if buffer else para

        if _count_tokens(buffer) >= CHUNK_TOKENS:
            parts = _sliding_window_split(buffer, CHUNK_TOKENS, OVERLAP_TOKENS)
            for part in parts[:-1]:  # keep last part as overlap seed for next para
                if _count_tokens(part) >= MIN_CHUNK_TOKENS:
                    chunks.append(Chunk(content=part, chunk_index=len(chunks)))
            buffer = _get_tail_tokens(buffer, OVERLAP_TOKENS)

    # Flush remaining buffer
    if buffer.strip() and _count_tokens(buffer) >= MIN_CHUNK_TOKENS:
        chunks.append(Chunk(content=buffer.strip(), chunk_index=len(chunks)))

    return chunks


#    MARKDOWN                                                                   ─
def chunk_markdown(raw_data: bytes, file_name: str) -> list[Chunk]:
    """
    Strategy:
    - Split on H1/H2/H3 headings (## style)
    - Each section becomes a chunk group
    - Carry heading_level and section_title metadata
    - If section > CHUNK_TOKENS: sliding window split within section
    """
    text   = raw_data.decode("utf-8", errors="replace")
    lines  = text.splitlines()
    chunks: list[Chunk] = []

    current_heading:  str | None = None
    current_level:    int | None = None
    current_body:     str        = ""

    def flush(body: str, heading: str | None, level: int | None):
        body = body.strip()
        if not body:
            return
        parts = _sliding_window_split(body, CHUNK_TOKENS, OVERLAP_TOKENS)
        for part in parts:
            if _count_tokens(part) >= MIN_CHUNK_TOKENS:
                chunks.append(Chunk(
                    content=part,
                    chunk_index=len(chunks),
                    section_title=heading,
                    heading_level=level,
                ))

    heading_re = re.compile(r"^(#{1,3})\s+(.*)")

    for line in lines:
        m = heading_re.match(line)
        if m:
            flush(current_body, current_heading, current_level)
            current_level   = len(m.group(1))   # 1, 2, or 3
            current_heading = m.group(2).strip()
            current_body    = ""
        else:
            current_body += "\n" + line

    flush(current_body, current_heading, current_level)
    return chunks


#    
# TEXT HELPERS
#    

def _count_tokens(text: str) -> int:
    return len(TOKENIZER.encode(text))


def _split_into_paragraphs(text: str) -> list[str]:
    """Split on blank lines (double newline)."""
    return re.split(r"\n\s*\n", text)


def _sliding_window_split(text: str, max_tokens: int, overlap: int) -> list[str]:
    """
    Split text into chunks of max_tokens with overlap token overlap.
    Tries to split at sentence boundaries (. ! ?) to avoid mid-sentence cuts.
    """
    tokens = TOKENIZER.encode(text)

    if len(tokens) <= max_tokens:
        return [text]

    chunks  = []
    start   = 0
    step    = max_tokens - overlap

    while start < len(tokens):
        end        = min(start + max_tokens, len(tokens))
        chunk_toks = tokens[start:end]
        chunk_text = TOKENIZER.decode(chunk_toks)

        # Try to end at a sentence boundary within the last 20% of the chunk
        boundary_zone = chunk_text[int(len(chunk_text) * 0.8):]
        for sep in (". ", "! ", "? ", "\n"):
            idx = chunk_text.rfind(sep, int(len(chunk_text) * 0.8))
            if idx != -1:
                chunk_text = chunk_text[: idx + len(sep)].rstrip()
                break

        chunks.append(chunk_text.strip())
        start += step

    return [c for c in chunks if c]


def _get_tail_tokens(text: str, n: int) -> str:
    """Return last n tokens of text as a string (for overlap carry-over)."""
    tokens = TOKENIZER.encode(text)
    tail   = tokens[-n:] if len(tokens) > n else tokens
    return TOKENIZER.decode(tail)
  
# EMBEDDING — Bedrock Titan Embed V2
def embed_text(text: str) -> list[float]:
    """
    Call Bedrock Titan Embed Text V2.
    Output: 1024-dimensional float vector.
    normalize=True → unit vectors, optimised for cosine similarity.
    """
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
    body = json.loads(response["body"].read())
    return body["embedding"]


# AURORA INSERT — RDS Data API
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
    """
    Insert one chunk row into fast_chunks via RDS Data API.
    The embedding vector is passed as a JSON array string and cast to vector type.
    """
    # Convert embedding list to PostgreSQL vector literal: '[0.1, 0.2, ...]'
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

    def _str_param(name: str, value: str | None) -> dict:
        if value is None:
            return {"name": name, "value": {"isNull": True}}
        return {"name": name, "value": {"stringValue": value}}

    def _int_param(name: str, value: int | None) -> dict:
        if value is None:
            return {"name": name, "value": {"isNull": True}}
        return {"name": name, "value": {"longValue": value}}

    params = [
        _str_param("tenant_id",     tenant_id),
        _str_param("doc_id",        doc_id),
        _str_param("s3_key",        s3_key),
        _str_param("file_name",     file_name),
        _str_param("source_type",   source_type),
        _int_param("chunk_index",   chunk.chunk_index),
        _int_param("chunk_total",   chunk_total),
        _str_param("content",       chunk.content),
        _str_param("embedding",     vector_literal),
        _int_param("page_number",   chunk.page_number),
        _str_param("section_title", chunk.section_title),
        _int_param("heading_level", chunk.heading_level),
        _str_param("sheet_name",    chunk.sheet_name),
        _int_param("row_start",     chunk.row_start),
        _int_param("row_end",       chunk.row_end),
    ]

    rds_data.execute_statement(
        resourceArn=db_cluster_arn,
        secretArn=db_secret_arn,
        database=db_name,
        sql=sql,
        parameters=params,
    )
   
# DYNAMODB STATUS UPDATE
def update_doc_status(
    tenant_id:     str,
    doc_id:        str,
    version:       int,
    status:        str,
    chunk_total:   int  = 0,
    error_message: str  = "",
):
    """
    Update the VER#{version} record in DynamoDB.
    status: UPLOADED → READY (success) or FAILED (error)
    """
    pk = f"TENANT#{tenant_id}#DOC#{doc_id}"
    sk = f"VER#{version:06d}"

    expression_parts  = ["#s = :status", "updatedAt = :now"]
    expression_values: dict[str, Any] = {
        ":status": {"S": status},
        ":now":    {"S": _now_iso()},
    }
    expression_names  = {"#s": "status"}

    if status == "READY" and chunk_total:
        expression_parts.append("chunkCount = :cc")
        expression_values[":cc"] = {"N": str(chunk_total)}

    if status == "FAILED" and error_message:
        expression_parts.append("errorMessage = :err")
        expression_values[":err"] = {"S": error_message[:500]}  # DDB 400KB limit guard

    dynamodb.update_item(
        TableName=DOCS_TABLE_NAME,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
        UpdateExpression="SET " + ", ".join(expression_parts),
        ExpressionAttributeValues=expression_values,
        ExpressionAttributeNames=expression_names,
    )

    print(f"[DYNAMO] {pk} {sk} → {status}")


def _now_iso() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415
    return datetime.now(timezone.utc).isoformat()
