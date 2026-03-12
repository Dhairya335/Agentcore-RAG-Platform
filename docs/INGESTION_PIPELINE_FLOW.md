# Phase 2C — Ingestion Pipeline Flow: Complete Function Call Reference

This document traces every single function call, AWS service interaction, state change,
retry mechanism, and data transformation from the moment a file lands in S3 after a
presigned upload, all the way until vector embeddings are stored in Aurora pgvector and
the document status is marked READY in DynamoDB.

This document is a direct continuation of DOCUMENT_UPLOAD_FLOW.md. Phase 2B ends when
the browser completes the S3 PUT. Phase 2C begins at that exact moment asynchronously.

---

## Files Involved

| File | Role |
|------|------|
| `infra-cdk/lib/backend-stack.ts` | CDK: provisions VPC, Aurora, SQS, DLQ, ingestion Lambda, S3 notification, IAM |
| `infra-cdk/lambdas/pgvector-setup/index.py` | Custom Resource Lambda: runs schema SQL on every deploy |
| `infra-cdk/lambdas/ingestion-worker/index.py` | SQS-triggered Lambda: parse, chunk, embed, insert into Aurora |
| `infra-cdk/lambdas/presign-upload/index.py` | Modified in Phase 2C: now adds S3 object metadata to presigned URL |
| `infra-cdk/lambdas/ingestion-worker/requirements.txt` | Python deps: PyPDF2, python-docx, openpyxl, tiktoken |

---

## Phase 0 — CDK Infrastructure (Deploy Time, NOT Runtime)

Everything in this section happens once when you run `make deploy`. No user action needed.

### Phase 0A — Aurora Serverless v2 + pgvector (createVectorStore)

```
backend-stack.ts
└── constructor()
    └── this.createVectorStore(config)
        |
        ├── new ec2.Vpc()
        |     vpcName:    "FAST-stack-rag-vpc"
        |     maxAzs:     2
        |     natGateways: 0        (no NAT deployed, $0 cost)
        |     subnetConfiguration:
        |       name:       "rag-private"
        |       subnetType: PRIVATE_WITH_EGRESS
        |       cidrMask:   24
        |     AWS SERVICE: Amazon VPC
        |     Creates: 2 private subnets (one per AZ), route tables, subnet groups
        |     No internet gateway attached to these subnets
        |
        ├── new ec2.SecurityGroup()
        |     securityGroupName: "FAST-stack-aurora-sg"
        |     description: "Aurora pgvector port 5432 from VPC CIDR only, no outbound"
        |     allowAllOutbound: false    (explicit outbound deny)
        |     AWS SERVICE: Amazon EC2 (Security Groups)
        |
        ├── auroraSg.addIngressRule()
        |     peer:        ec2.Peer.ipv4(vpc.vpcCidrBlock)
        |     port:        ec2.Port.tcp(5432)
        |     description: "PostgreSQL from VPC CIDR only"
        |     Allows only internal VPC traffic to reach Aurora on port 5432
        |     The internet cannot reach Aurora at all — no public IP is assigned
        |
        ├── new secretsmanager.Secret()
        |     secretName: "FAST-stack/aurora-pgvector"
        |     Auto-generates a 32-char alphanumeric password
        |     Username: "pgadmin"
        |     AWS SERVICE: AWS Secrets Manager
        |     This secret is what RDS Data API uses to authenticate SQL calls
        |
        ├── new rds.DatabaseCluster()
        |     clusterIdentifier:       "FAST-stack-pgvector"
        |     engine:                  AuroraPostgres VER_16_4
        |     serverlessV2MinCapacity: 0.5    (near-zero idle, approx $0.06/hr floor)
        |     serverlessV2MaxCapacity: 4      (4 ACUs = ~8 GB RAM under load)
        |     writer:                  ClusterInstance.serverlessV2("writer")
        |     vpc:                     ragVpc
        |     vpcSubnets:              PRIVATE_WITH_EGRESS
        |     securityGroups:          [auroraSg]
        |     defaultDatabaseName:     "ragdb"
        |     credentials:             from dbSecret above
        |     enableDataApi:           true   (allows HTTPS calls without VPC membership)
        |     storageEncrypted:        true   (AES-256 at rest)
        |     removalPolicy:           DESTROY
        |     AWS SERVICE: Amazon Aurora Serverless v2
        |
        ├── new lambda.Function()  (PgvectorSetupLambda)
        |     functionName: "FAST-stack-pgvector-setup"
        |     runtime:      PYTHON_3_13
        |     code:         lambdas/pgvector-setup/
        |     handler:      index.handler
        |     architecture: ARM_64
        |     timeout:      5 minutes
        |     env vars:
        |       DB_CLUSTER_ARN = dbCluster.clusterArn
        |       DB_SECRET_ARN  = dbSecret.secretArn
        |       DB_NAME        = "ragdb"
        |     AWS SERVICE: AWS Lambda
        |
        ├── dbCluster.grantDataApiAccess(pgvectorSetupLambda)
        |     Grants: rds-data:ExecuteStatement, rds-data:BatchExecuteStatement
        |             secretsmanager:GetSecretValue on the cluster secret
        |     AWS SERVICE: IAM (inline policy on Lambda execution role)
        |
        ├── dbSecret.grantRead(pgvectorSetupLambda)
        |     Grants: secretsmanager:GetSecretValue, DescribeSecret
        |     AWS SERVICE: IAM
        |
        ├── new cdk.CustomResource()  (PgvectorSetupResource)
        |     serviceToken: pgvectorSetupLambda.functionArn
        |     properties:
        |       SchemaVersion: "1"
        |     CloudFormation calls this Lambda synchronously during deploy
        |     Waits up to 5 minutes for Lambda to respond via ResponseURL
        |     If Lambda does not respond, CloudFormation marks it FAILED
        |
        ├── pgvectorSetupResource.node.addDependency(dbCluster)
        |     Ensures Aurora cluster is fully AVAILABLE before schema init runs
        |
        ├── new ssm.StringParameter()  "AuroraClusterArnParam"
        |     parameterName: "/FAST-stack/rag/aurora-cluster-arn"
        |     stringValue:   dbCluster.clusterArn
        |     AWS SERVICE: AWS SSM Parameter Store
        |
        ├── new ssm.StringParameter()  "AuroraSecretArnParam"
        |     parameterName: "/FAST-stack/rag/aurora-secret-arn"
        |     stringValue:   dbSecret.secretArn
        |
        └── new ssm.StringParameter()  "AuroraDbNameParam"
              parameterName: "/FAST-stack/rag/aurora-db-name"
              stringValue:   "ragdb"
```

### Phase 0B — pgvector Schema Init (pgvector-setup/index.py runs at deploy time)

This Lambda is invoked by CloudFormation Custom Resource during `cdk deploy`. It runs
before any document is ever uploaded. It sets up the database schema idempotently.

```
pgvector-setup/index.py
└── handler(event, context)
      |
      ├── Reads from event:
      |     event["RequestType"]         = "Create" (first deploy) or "Update" (re-deploy)
      |     event["ResponseURL"]         = presigned S3 URL CloudFormation is listening on
      |     event["StackId"]             = CloudFormation stack ARN
      |     event["RequestId"]           = unique ID for this CFN request
      |     event["LogicalResourceId"]   = "PgvectorSetupResource"
      |     event["PhysicalResourceId"]  = "pgvector-schema" (stable across updates)
      |
      ├── IF RequestType == "Create" or "Update":
      |     └── setup_schema()
      |           |
      |           ├── Statement 1: rds_data.execute_statement()
      |           |     sql: "CREATE EXTENSION IF NOT EXISTS vector"
      |           |     Loads the pgvector extension into the "ragdb" database
      |           |     This must run before any vector() column can be created
      |           |     AWS SERVICE: RDS Data API → Aurora PostgreSQL 16.4
      |           |
      |           ├── Statement 2: rds_data.execute_statement()
      |           |     sql: CREATE TABLE IF NOT EXISTS fast_chunks (...)
      |           |     Creates the main chunks table with all columns:
      |           |
      |           |     Column layout:
      |           |       id            UUID PRIMARY KEY DEFAULT gen_random_uuid()
      |           |       tenant_id     TEXT NOT NULL
      |           |       doc_id        TEXT NOT NULL
      |           |       s3_key        TEXT NOT NULL
      |           |       file_name     TEXT NOT NULL
      |           |       source_type   TEXT NOT NULL   (pdf|docx|xlsx|csv|txt|md)
      |           |       chunk_index   INTEGER NOT NULL
      |           |       chunk_total   INTEGER NOT NULL
      |           |       content       TEXT NOT NULL
      |           |       embedding     vector(1024)    (Titan Embed V2 output dimension)
      |           |       page_number   INTEGER         (PDF only, NULL otherwise)
      |           |       section_title TEXT            (DOCX/MD headings, NULL otherwise)
      |           |       heading_level INTEGER         (MD: 1=H1, 2=H2, 3=H3, NULL otherwise)
      |           |       sheet_name    TEXT            (XLSX/CSV, NULL otherwise)
      |           |       row_start     INTEGER         (XLSX/CSV, NULL otherwise)
      |           |       row_end       INTEGER         (XLSX/CSV, NULL otherwise)
      |           |       created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
      |           |
      |           ├── Statement 3: rds_data.execute_statement()
      |           |     sql: CREATE INDEX IF NOT EXISTS fast_chunks_embedding_hnsw_idx
      |           |          ON fast_chunks USING hnsw (embedding vector_cosine_ops)
      |           |          WITH (m = 16, ef_construction = 64)
      |           |     HNSW index for approximate nearest-neighbour vector search
      |           |     vector_cosine_ops = cosine similarity scoring
      |           |     m=16 = connections per node in the graph (recall vs memory tradeoff)
      |           |     ef_construction=64 = search width during build (quality vs build speed)
      |           |
      |           ├── Statement 4: fast_chunks_tenant_idx ON fast_chunks (tenant_id)
      |           ├── Statement 5: fast_chunks_doc_idx ON fast_chunks (doc_id)
      |           ├── Statement 6: fast_chunks_source_idx ON fast_chunks (source_type)
      |           └── Statement 7: fast_chunks_tenant_doc_idx ON fast_chunks (tenant_id, doc_id)
      |                 These four B-tree indexes support hybrid queries:
      |                 vector similarity + WHERE tenant_id = X AND doc_id = Y
      |
      ├── IF RequestType == "Delete":
      |     No-op. Schema stays in place. Aurora cluster deletion is handled by CDK.
      |
      └── send_cfn_response(response_url, ..., "SUCCESS" or "FAILED")
            Constructs JSON body:
              Status:             "SUCCESS"
              PhysicalResourceId: "pgvector-schema"
              Data:               { SchemaStatus: "initialized" }
            urllib.request.Request(url=response_url, method="PUT", data=body)
            urlopen(req) — sends the HTTP PUT to CloudFormation's ResponseURL
            CloudFormation unblocks and marks the Custom Resource CREATE_COMPLETE
```

### Phase 0C — Ingestion Pipeline CDK (createIngestionPipeline)

```
backend-stack.ts
└── this.createIngestionPipeline(config)
      |
      ├── new sqs.Queue()  (IngestionDlq)
      |     queueName:       "FAST-stack-ingestion-dlq"
      |     retentionPeriod: 14 days
      |     encryption:      SQS_MANAGED
      |     AWS SERVICE: Amazon SQS
      |     Purpose: receives messages that failed ingestion after 3 retries
      |
      ├── new sqs.Queue()  (IngestionQueue)
      |     queueName:         "FAST-stack-ingestion-queue"
      |     visibilityTimeout: 15 minutes   (must match Lambda timeout exactly)
      |     retentionPeriod:   4 days
      |     encryption:        SQS_MANAGED
      |     deadLetterQueue:
      |       queue:           ingestionDlq
      |       maxReceiveCount: 3    (retry count before DLQ)
      |     AWS SERVICE: Amazon SQS
      |
      |     Why visibilityTimeout must match Lambda timeout:
      |       When SQS delivers a message to Lambda, it hides the message for
      |       visibilityTimeout seconds. If Lambda finishes before that, SQS
      |       deletes the message. If Lambda is still running when the timeout
      |       expires, SQS makes the message visible again and retries it.
      |       If visibilityTimeout < Lambda timeout, you get duplicate processing.
      |
      ├── s3.Bucket.fromBucketName()  (RawDocsBucketRef)
      |     bucketName: "fast-stack-raw-docs"
      |     Imports the existing bucket by name (no re-creation)
      |     Needed to attach the S3 event notification
      |
      ├── rawDocsBucket.addEventNotification()
      |     eventType:   s3.EventType.OBJECT_CREATED
      |     destination: new s3notify.SqsDestination(ingestionQueue)
      |     AWS SERVICE: Amazon S3 (Event Notifications)
      |     Every PUT to the bucket sends a notification message to ingestionQueue
      |     Message format: standard S3 event notification JSON
      |
      ├── new lambda.Function()  (IngestionWorkerLambda)
      |     functionName: "FAST-stack-ingestion-worker"
      |     runtime:      PYTHON_3_13
      |     code:         lambdas/ingestion-worker/
      |     handler:      index.handler
      |     architecture: ARM_64
      |     timeout:      15 minutes
      |     memorySize:   1024 MB   (PDF parsing + tiktoken + embedding is memory heavy)
      |     env vars:
      |       STACK_NAME:      "FAST-stack"
      |       DOCS_TABLE_NAME: "FAST-stack-documents"
      |     AWS SERVICE: AWS Lambda
      |
      ├── rawDocsBucket.grantRead(ingestionLambda)
      |     Grants: s3:GetObject, s3:ListBucket on the raw-docs bucket
      |
      ├── ingestionLambda.addToRolePolicy()  (DynamoDB)
      |     Actions:   dynamodb:UpdateItem, dynamodb:GetItem
      |     Resources: arn:aws:dynamodb:...:table/FAST-stack-documents
      |
      ├── ingestionLambda.addToRolePolicy()  (Bedrock)
      |     Actions:   bedrock:InvokeModel
      |     Resources: arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0
      |
      ├── ingestionLambda.addToRolePolicy()  (RDS Data API)
      |     Actions:   rds-data:ExecuteStatement, rds-data:BatchExecuteStatement
      |     Resources: *   (cluster ARN is read from SSM at runtime)
      |
      ├── ingestionLambda.addToRolePolicy()  (Secrets Manager)
      |     Actions:   secretsmanager:GetSecretValue
      |     Resources: *   (secret ARN is read from SSM at runtime)
      |
      ├── ingestionLambda.addToRolePolicy()  (SSM)
      |     Actions:   ssm:GetParameter
      |     Resources: arn:aws:ssm:...:parameter/FAST-stack/rag/*
      |
      └── ingestionLambda.addEventSource(new SqsEventSource(ingestionQueue))
            batchSize:               1    (one document per invocation)
            maxConcurrency:          5    (max 5 parallel ingestion jobs)
            reportBatchItemFailures: true (partial batch failure support)
            AWS SERVICE: Lambda Event Source Mapping (SQS trigger)
```

---

## Phase 1 — File Lands in S3 (Continuation from Phase 2B Upload Flow)

At the end of DOCUMENT_UPLOAD_FLOW.md Phase 9, the browser completed an XHR PUT to S3.
The file now exists in the raw-docs bucket. Everything below happens automatically with
no further browser interaction.

```
S3 Bucket: fast-stack-raw-docs
  Object key: {tenantId}/v{version}/{fileName}
  Example:    "abc-123-user-sub/v1/policy.pdf"

  Object metadata (set by presign Lambda when generating the URL):
    x-amz-meta-doc-id:    "doc-uuid-here"
    x-amz-meta-tenant-id: "abc-123-user-sub"
    x-amz-meta-version:   "1"

S3 detects OBJECT_CREATED event
    |
    └── S3 Event Notification fires automatically
          Destination: IngestionQueue (SQS)
          Delay: typically under 1 second from PUT completion
```

---

## Phase 2 — S3 Sends Event to SQS

```
S3 Event Notification → SQS SendMessage
  Queue: FAST-stack-ingestion-queue
  Message body (S3 standard format):
    {
      "Records": [
        {
          "eventSource": "aws:s3",
          "eventName":   "ObjectCreated:Put",
          "s3": {
            "bucket": {
              "name": "fast-stack-raw-docs"
            },
            "object": {
              "key":  "abc-123-user-sub/v1/policy.pdf",
              "size": 204800,
              "eTag": "abc123..."
            }
          }
        }
      ]
    }

SQS message state after receipt:
  messageId:         "msg-uuid"
  visibilityTimeout: 15 minutes (message hidden from other consumers)
  receiveCount:      1 (increments on each delivery attempt)
  AWS SERVICE: Amazon SQS
```

---

## Phase 3 — SQS Triggers Ingestion Worker Lambda

```
SQS Event Source Mapping detects message in IngestionQueue
  batchSize = 1 → exactly 1 message per Lambda invocation
  |
  └── Lambda service invokes FAST-stack-ingestion-worker
        event shape passed to handler():
          {
            "Records": [
              {
                "messageId":     "msg-uuid",
                "receiptHandle": "long-opaque-string",
                "body":          "{...S3 event JSON as string...}",
                "attributes": {
                  "ApproximateReceiveCount": "1",
                  "SentTimestamp":           "1234567890"
                },
                "eventSource":   "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:...:FAST-stack-ingestion-queue"
              }
            ]
          }
```

---

## Phase 4 — handler() Entry Point

```
ingestion-worker/index.py
└── handler(event, context)
      |
      ├── Module-level initialization (runs once per container cold start):
      |     s3       = boto3.client("s3")
      |     bedrock  = boto3.client("bedrock-runtime")
      |     rds_data = boto3.client("rds-data")
      |     dynamodb = boto3.client("dynamodb")
      |     ssm      = boto3.client("ssm")
      |     TOKENIZER = tiktoken.get_encoding("cl100k_base")
      |       tiktoken loads the cl100k_base BPE vocabulary from the package
      |       This is used for counting tokens, not for generating embeddings
      |
      ├── for record in event.get("Records", []):
      |     (batchSize=1 so this loop runs exactly once)
      |     |
      |     └── try:
      |           process_sqs_record(record)
      |         except Exception as e:
      |           print(f"[ERROR] Failed to process record: {e}")
      |           raise   ← re-raise so SQS retries this message
      |
      └── Returning from handler() normally:
            SQS Event Source Mapping deletes the message from the queue
            receiveCount is NOT incremented again
```

---

## Phase 5 — process_sqs_record()

```
ingestion-worker/index.py
└── process_sqs_record(record: dict)
      |
      ├── body = json.loads(record["body"])
      |     Parses the outer SQS message body (which contains the S3 event as JSON string)
      |
      ├── s3_records = body.get("Records", [])
      |     Extracts the list of S3 event records
      |     In practice: always 1 record per S3 PUT event
      |
      └── for s3_record in s3_records:
            bucket = s3_record["s3"]["bucket"]["name"]
            key    = urllib.parse.unquote_plus(s3_record["s3"]["object"]["key"])
              unquote_plus handles URL encoding in the key
              Example: "abc%20123/v1/my%20file.pdf" → "abc 123/v1/my file.pdf"
            print(f"[INGEST] Processing s3://{bucket}/{key}")
            └── process_document(bucket, key)
```

---

## Phase 6 — process_document() — Main Pipeline

```
ingestion-worker/index.py
└── process_document(bucket: str, key: str)
      |
      ├── STEP 1: Read S3 object metadata
      |     s3.head_object(Bucket=bucket, Key=key)
      |     AWS SERVICE: Amazon S3 (HeadObject — metadata only, no file download)
      |     Returns: head["Metadata"] dict with lowercase keys
      |     |
      |     ├── tenant_id = obj_meta.get("tenant-id")
      |     |     Value: "abc-123-user-sub"  (Cognito sub claim stored by presign Lambda)
      |     ├── doc_id    = obj_meta.get("doc-id")
      |     |     Value: "doc-uuid-here"
      |     ├── version   = int(obj_meta.get("version", "1"))
      |     |     Value: 1 (or 2, 3... for re-uploads)
      |     └── file_name = key.split("/")[-1]
      |           Value: "policy.pdf"  (last segment of S3 key)
      |
      |     VALIDATION:
      |       if not tenant_id or not doc_id:
      |         raise ValueError("Missing required S3 object metadata...")
      |         This causes SQS to retry the message (or DLQ after 3 attempts)
      |
      ├── STEP 2: Read SSM parameters (cached after first call per container)
      |     get_ssm("aurora-cluster-arn")
      |       → ssm.get_parameter(Name="/FAST-stack/rag/aurora-cluster-arn")
      |       AWS SERVICE: AWS SSM Parameter Store
      |       Returns: "arn:aws:rds:us-east-1:897675553288:cluster:fast-stack-pgvector"
      |     get_ssm("aurora-secret-arn")
      |       → ssm.get_parameter(Name="/FAST-stack/rag/aurora-secret-arn")
      |       Returns: "arn:aws:secretsmanager:us-east-1:...:FAST-stack/aurora-pgvector"
      |     get_ssm("aurora-db-name")
      |       → ssm.get_parameter(Name="/FAST-stack/rag/aurora-db-name")
      |       Returns: "ragdb"
      |
      |     _ssm_cache stores all three values so warm Lambda containers skip SSM calls
      |
      ├── STEP 3: Download file from S3
      |     s3.get_object(Bucket=bucket, Key=key)
      |     AWS SERVICE: Amazon S3 (GetObject — full download)
      |     raw_data = obj["Body"].read()
      |     File is loaded entirely into Lambda memory (1024 MB allocated)
      |
      ├── STEP 4: Detect type and chunk
      |     ext = file_name.lower().rsplit(".", 1)[-1]
      |       "policy.pdf"        → ext = "pdf"
      |       "report.docx"       → ext = "docx"
      |       "data.xlsx"         → ext = "xlsx"
      |       "records.csv"       → ext = "csv"
      |       "readme.txt"        → ext = "txt"
      |       "notes.md"          → ext = "md"
      |       "config.json"       → ext = "json"  (treated as txt)
      |       "legacy.doc"        → ext = "doc"   (treated as docx)
      |       no extension        → fallback "txt"
      |     chunks = chunk_document(raw_data, file_name, ext)
      |       see Phases 7A through 7F below for per-type chunking detail
      |
      |     if not chunks:
      |       raise ValueError("No chunks produced...")
      |       Causes SQS retry
      |
      ├── STEP 5: Embed each chunk and insert into Aurora
      |     chunk_total = len(chunks)
      |     for chunk in chunks:
      |       embedding = embed_text(chunk.content)   ← see Phase 8
      |       insert_chunk(..., chunk, chunk_total, embedding)  ← see Phase 9
      |
      ├── STEP 6: Mark document READY in DynamoDB
      |     update_doc_status(tenant_id, doc_id, version, "READY", chunk_total=chunk_total)
      |     see Phase 10
      |
      └── except Exception as e:
            update_doc_status(tenant_id, doc_id, version, "FAILED", error_message=str(e))
            raise   ← re-raises so SQS retries
```

---

## Phase 7A — Chunking: PDF

```
ingestion-worker/index.py
└── chunk_pdf(raw_data: bytes, file_name: str) -> list[Chunk]
      |
      ├── import PyPDF2
      ├── reader = PyPDF2.PdfReader(io.BytesIO(raw_data))
      |     Parses the PDF binary in-memory, no temp files written to disk
      |
      └── for page_num, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
              PyPDF2 extracts all text from this page including headers/footers
              If page is purely image-based (scanned), returns empty string
              Empty pages are skipped (continue)
            |
            ├── paragraphs = _split_into_paragraphs(page_text)
            |     re.split(r"\n\s*\n", page_text)
            |     Splits on blank lines between paragraphs
            |
            └── for para in paragraphs:
                  |
                  ├── _looks_like_table(para)
                  |     Counts lines with 2+ tab or pipe characters
                  |     If 50% or more of lines look tabular: treat as single chunk
                  |     Reason: splitting a table mid-row destroys query context
                  |     Table chunk: stored as-is with page_number, section_title=None
                  |
                  └── Regular paragraph:
                        para_chunks = _sliding_window_split(para, 600, 100)
                          Tokenizes with tiktoken cl100k_base
                          Produces chunks of up to 600 tokens with 100 token overlap
                          Tries to break at sentence boundaries (. ! ? \n)
                          in the last 20% of each chunk window
                        for text in para_chunks:
                          if _count_tokens(text) >= 20:    (MIN_CHUNK_TOKENS guard)
                            Chunk(
                              content=text,
                              chunk_index=len(chunks),
                              page_number=page_num,        (1-based)
                              section_title=None,
                            )

Metadata stored per PDF chunk:
  page_number:   int (which page the text came from)
  section_title: None (PDF has no structured headings accessible via PyPDF2)
  heading_level: None
  sheet_name:    None
  row_start:     None
  row_end:       None
```

### Phase 7B — Chunking: DOCX

```
ingestion-worker/index.py
└── chunk_docx(raw_data: bytes, file_name: str) -> list[Chunk]
      |
      ├── from docx import Document
      ├── doc = Document(io.BytesIO(raw_data))
      |     python-docx parses the .docx ZIP/XML structure in-memory
      |
      ├── Iterates doc.paragraphs in document order
      |     Each paragraph has:
      |       para.text       = the text content
      |       para.style.name = style name ("Heading 1", "Heading 2", "Normal", etc.)
      |
      ├── Heading detection: style_name.startswith("Heading")
      |     When a heading is found:
      |       1. flush_section(current_section, current_heading) emits pending chunks
      |       2. current_heading = heading text
      |       3. current_section = pending_overlap (carry-over from previous section)
      |
      ├── Body paragraph: append to current_section
      |     if _count_tokens(current_section) >= 600:
      |       flush_section() called
      |       pending_overlap = _get_tail_tokens(current_section, 100)
      |         Takes last 100 tokens as overlap for the next chunk
      |       current_section = pending_overlap
      |
      ├── flush_section(text, heading):
      |     _sliding_window_split(text, 600, 100)
      |     for each part: Chunk(content=part, section_title=heading)
      |
      └── Final flush: remaining current_section emitted

Metadata stored per DOCX chunk:
  page_number:   None (python-docx does not expose page breaks)
  section_title: "Introduction" / "Security Requirements" etc. (heading text)
  heading_level: None (DOCX style name not converted to numeric level here)
  sheet_name:    None
  row_start:     None
  row_end:       None
```

### Phase 7C — Chunking: XLSX

```
ingestion-worker/index.py
└── chunk_xlsx(raw_data: bytes, file_name: str) -> list[Chunk]
      |
      ├── import openpyxl
      ├── wb = openpyxl.load_workbook(io.BytesIO(raw_data), read_only=True, data_only=True)
      |     read_only=True: streaming mode, efficient for large files
      |     data_only=True: returns cell values not formulas
      |
      └── for sheet in wb.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
              Returns all rows as tuples of raw values
              First row treated as headers
            |
            ├── headers     = [str(c) for c in rows[0]]
            ├── header_line = "Columns: Region | Product | Revenue | ..."
            ├── data_rows   = rows[1:]
            |
            └── for start in range(0, len(data_rows), 30):
                  batch = data_rows[start : start + 30]
                    Each chunk = up to 30 data rows
                    Headers are prepended to EVERY chunk
                    This ensures each chunk is self-contained for retrieval
                  |
                  content = f"Sheet: {sheet.title}\n{header_line}\nrow1...\nrow2..."
                  if _count_tokens(content) >= 20:
                    Chunk(
                      content=content,
                      chunk_index=len(chunks),
                      sheet_name=sheet.title,
                      row_start=start + 1,    (1-based, skipping header row)
                      row_end=start + len(batch),
                    )

Metadata stored per XLSX chunk:
  page_number:   None
  section_title: None
  heading_level: None
  sheet_name:    "Sales_Q1" (the worksheet tab name)
  row_start:     1          (first data row in this chunk, 1-based)
  row_end:       30         (last data row in this chunk)
```

### Phase 7D — Chunking: CSV

```
ingestion-worker/index.py
└── chunk_csv(raw_data: bytes, file_name: str) -> list[Chunk]
      |
      ├── import csv
      ├── text     = raw_data.decode("utf-8", errors="replace")
      ├── reader   = csv.reader(io.StringIO(text))
      ├── all_rows = list(reader)
      |
      ├── headers     = all_rows[0]
      ├── header_line = "Columns: Date | User | Event | ..."
      ├── data_rows   = all_rows[1:]
      |
      └── for start in range(0, len(data_rows), 40):
            batch = data_rows[start : start + 40]
              Each chunk = up to 40 data rows
              No overlap — CSV rows are discrete and self-contained
            headers prepended to every chunk
            content = "Columns: ...\nrow1\nrow2\n..."
            Chunk(
              content=content,
              chunk_index=len(chunks),
              sheet_name=file_name,   (CSV has no sheet name, use filename)
              row_start=start + 1,
              row_end=start + len(batch),
            )

Metadata stored per CSV chunk:
  sheet_name:  "records.csv"  (the file name itself)
  row_start:   1
  row_end:     40
```

### Phase 7E — Chunking: TXT and JSON

```
ingestion-worker/index.py
└── chunk_text(raw_data: bytes, file_name: str) -> list[Chunk]
      |
      ├── text = raw_data.decode("utf-8", errors="replace")
      ├── paragraphs = _split_into_paragraphs(text)
      |     re.split(r"\n\s*\n", text)
      |     Blank lines are the primary split point
      |
      ├── buffer = ""
      └── for para in paragraphs:
            buffer += para (appending paragraphs)
            if _count_tokens(buffer) >= 600:
              parts = _sliding_window_split(buffer, 600, 100)
              for part in parts[:-1]:    (all but last, keep last as overlap seed)
                if _count_tokens(part) >= 20:
                  Chunk(content=part, chunk_index=len(chunks))
              buffer = _get_tail_tokens(buffer, 100)
        Flush remaining buffer at end

Metadata stored per TXT chunk:
  All metadata fields are None (plain text has no structure)
```

### Phase 7F — Chunking: Markdown

```
ingestion-worker/index.py
└── chunk_markdown(raw_data: bytes, file_name: str) -> list[Chunk]
      |
      ├── text  = raw_data.decode("utf-8", errors="replace")
      ├── lines = text.splitlines()
      ├── heading_re = re.compile(r"^(#{1,3})\s+(.*)")
      |     Matches H1 (#), H2 (##), H3 (###)
      |
      └── for line in lines:
            m = heading_re.match(line)
            IF heading:
              flush(current_body, current_heading, current_level)
              current_level   = len(m.group(1))    1, 2, or 3
              current_heading = m.group(2).strip()  "Authentication"
              current_body    = ""
            ELSE:
              current_body += "\n" + line

      flush(body, heading, level):
        _sliding_window_split(body, 600, 100)
        Chunk(
          content=part,
          section_title=heading,
          heading_level=level,     1=H1, 2=H2, 3=H3
        )

Metadata stored per MD chunk:
  section_title: "Authentication"  (the heading text)
  heading_level: 2                  (H2 section)
```

---

## Phase 8 — embed_text() — Bedrock Titan Embed V2

Called once per chunk. For a 20-chunk document, this is 20 Bedrock API calls.

```
ingestion-worker/index.py
└── embed_text(text: str) -> list[float]
      |
      ├── bedrock.invoke_model(
      |     modelId:      "amazon.titan-embed-text-v2:0"
      |     contentType:  "application/json"
      |     accept:       "application/json"
      |     body: JSON.dumps({
      |       "inputText":  text,      (the chunk content string)
      |       "dimensions": 1024,      (output vector size — Titan V2 supports 256/512/1024)
      |       "normalize":  True       (unit vector output — optimal for cosine similarity)
      |     })
      |   )
      |   AWS SERVICE: Amazon Bedrock (synchronous model invocation)
      |   Typical latency: 100-300ms per call depending on text length
      |
      ├── response["body"].read()
      |     Reads the streaming response body
      |
      ├── json.loads(response_body)
      |     Parses response JSON
      |
      └── return body["embedding"]
            Returns: list of 1024 float values
            Example: [0.02341234, -0.01234567, 0.00823456, ...]
            These floats represent the semantic position of the text
            in 1024-dimensional embedding space
```

---

## Phase 9 — insert_chunk() — RDS Data API Aurora Insert

Called once per chunk, after embed_text() returns the embedding.

```
ingestion-worker/index.py
└── insert_chunk(db_cluster_arn, db_secret_arn, db_name, tenant_id, doc_id,
                 s3_key, file_name, source_type, chunk, chunk_total, embedding)
      |
      ├── vector_literal = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"
      |     Converts Python list of floats to PostgreSQL vector literal string
      |     Example: "[0.02341234,-0.01234567,0.00823456,...]"
      |     The ::vector cast in the SQL converts this string to pgvector's native type
      |
      ├── SQL statement:
      |     INSERT INTO fast_chunks (
      |       tenant_id, doc_id, s3_key, file_name, source_type,
      |       chunk_index, chunk_total, content, embedding,
      |       page_number, section_title, heading_level,
      |       sheet_name, row_start, row_end
      |     ) VALUES (
      |       :tenant_id, :doc_id, :s3_key, :file_name, :source_type,
      |       :chunk_index, :chunk_total, :content, :embedding::vector,
      |       :page_number, :section_title, :heading_level,
      |       :sheet_name, :row_start, :row_end
      |     )
      |
      ├── Parameter binding:
      |     _str_param(name, value):
      |       if value is None: {"name": name, "value": {"isNull": True}}
      |       else:             {"name": name, "value": {"stringValue": value}}
      |     _int_param(name, value):
      |       if value is None: {"name": name, "value": {"isNull": True}}
      |       else:             {"name": name, "value": {"longValue": value}}
      |
      |     Full parameter list sent to RDS Data API:
      |       tenant_id     → stringValue
      |       doc_id        → stringValue
      |       s3_key        → stringValue
      |       file_name     → stringValue
      |       source_type   → stringValue ("pdf" / "docx" / "xlsx" etc.)
      |       chunk_index   → longValue   (0-based position)
      |       chunk_total   → longValue   (total chunks for this doc version)
      |       content       → stringValue (the actual text of this chunk)
      |       embedding     → stringValue (vector literal, cast with ::vector in SQL)
      |       page_number   → longValue or isNull
      |       section_title → stringValue or isNull
      |       heading_level → longValue or isNull
      |       sheet_name    → stringValue or isNull
      |       row_start     → longValue or isNull
      |       row_end       → longValue or isNull
      |
      └── rds_data.execute_statement(
            resourceArn=db_cluster_arn,
            secretArn=db_secret_arn,
            database=db_name,
            sql=sql,
            parameters=params,
          )
          AWS SERVICE: RDS Data API (HTTPS request to regional endpoint)
          RDS Data API authenticates to Aurora using the secret in Secrets Manager
          Lambda never makes a direct TCP connection to port 5432
          Aurora receives the INSERT and appends the row to fast_chunks
          The HNSW index is updated incrementally for the new embedding vector
```

---

## Phase 10 — update_doc_status() — DynamoDB UPLOADED to READY

Called once after all chunks are successfully inserted.

```
ingestion-worker/index.py
└── update_doc_status(tenant_id, doc_id, version, "READY", chunk_total=N)
      |
      ├── pk = f"TENANT#{tenant_id}#DOC#{doc_id}"
      |     Example: "TENANT#abc-123#DOC#doc-uuid"
      ├── sk = f"VER#{version:06d}"
      |     Example: "VER#000001"
      |
      ├── Builds UpdateExpression dynamically:
      |     Base:    "SET #s = :status, updatedAt = :now"
      |     +READY:  ", chunkCount = :cc"
      |     +FAILED: ", errorMessage = :err"
      |
      └── dynamodb.update_item(
            TableName=DOCS_TABLE_NAME,
            Key={
              "PK": {"S": "TENANT#abc-123#DOC#doc-uuid"},
              "SK": {"S": "VER#000001"}
            },
            UpdateExpression="SET #s = :status, updatedAt = :now, chunkCount = :cc",
            ExpressionAttributeValues={
              ":status": {"S": "READY"},
              ":now":    {"S": "2026-03-11T20:05:00+00:00"},
              ":cc":     {"N": "23"}
            },
            ExpressionAttributeNames={"#s": "status"}
          )
          AWS SERVICE: Amazon DynamoDB (UpdateItem)
          Updates the existing VER#000001 record in-place
          status field: "UPLOADED" → "READY"
          chunkCount field added: 23 (how many chunks were stored)
          updatedAt field updated to current timestamp
```

---

## Phase 11 — Error Handling and Retry Flow

```
Retry Scenario: Bedrock call fails (throttle / model unavailable)
  |
  ├── embed_text() raises an exception
  ├── process_document() except block catches it
  ├── update_doc_status(... "FAILED", error_message="...bedrock error...")
  |     DynamoDB VER record: status = "FAILED", errorMessage = "ThrottlingException..."
  ├── process_document() re-raises the exception
  ├── process_sqs_record() propagates the raise
  ├── handler() catches, prints, re-raises
  └── Lambda exits with error
        |
        └── SQS Event Source Mapping sees Lambda failure
              Message becomes VISIBLE again in IngestionQueue
              receiveCount increments to 2
              Lambda is invoked again (retry 2)
              If retry 2 also fails:
                receiveCount = 3
                Lambda invoked again (retry 3)
              If retry 3 also fails:
                receiveCount = 4 > maxReceiveCount (3)
                SQS moves message to FAST-stack-ingestion-dlq
                Message stays in DLQ for 14 days
                DynamoDB record shows status = "FAILED"

Retry Scenario: S3 metadata missing (doc-id or tenant-id not in object metadata)
  |
  ├── process_document() raises ValueError
  ├── DynamoDB cannot be updated (no doc_id to build the PK)
  ├── Exception propagates up to handler()
  ├── Lambda exits with error
  ├── SQS retries the message 3 times
  └── Message goes to DLQ

Non-Retry Scenario: Empty document (no chunks produced)
  |
  ├── chunk_document() returns []
  ├── raises ValueError("No chunks produced...")
  ├── update_doc_status sets FAILED with error message
  └── SQS retries but will keep failing → DLQ after 3 attempts
```

---

## Complete End-to-End Flow Arrow Diagram

```
BROWSER (Phase 2B end)
  |
  | XHR PUT file bytes directly to S3 presigned URL
  v
S3 BUCKET: fast-stack-raw-docs
  Object:   {tenantId}/v{version}/{fileName}
  Metadata: doc-id, tenant-id, version
  |
  | S3 Event Notification (OBJECT_CREATED) fires automatically
  v
SQS QUEUE: FAST-stack-ingestion-queue
  Message:  { Records: [{ s3: { bucket, object: { key } } }] }
  State:    visible for 15 minutes (visibilityTimeout)
  |
  | SQS Event Source Mapping triggers Lambda
  v
LAMBDA: FAST-stack-ingestion-worker
  |
  ├── s3.head_object() → read doc-id, tenant-id, version from metadata
  ├── ssm.get_parameter() x3 → aurora-cluster-arn, aurora-secret-arn, aurora-db-name
  ├── s3.get_object() → download raw file bytes
  |
  ├── chunk_document() → dispatch by extension
  |     PDF   → chunk_pdf()    → PyPDF2 + page-aware + table detection
  |     DOCX  → chunk_docx()   → python-docx + heading sections
  |     XLSX  → chunk_xlsx()   → openpyxl + 30-row groups + repeat headers
  |     CSV   → chunk_csv()    → csv.reader + 40-row groups + repeat headers
  |     TXT   → chunk_text()   → paragraph split + sliding window
  |     MD    → chunk_markdown() → H1/H2/H3 sections + sliding window
  |     (all token-counted via tiktoken cl100k_base, 600 tokens, 100 overlap)
  |
  ├── for each Chunk:
  |     bedrock.invoke_model("amazon.titan-embed-text-v2:0")
  |       → 1024-dimensional float vector (normalized for cosine similarity)
  |     rds_data.execute_statement(INSERT INTO fast_chunks ...)
  |       → Aurora PostgreSQL 16.4 stores row + updates HNSW index
  |
  └── dynamodb.update_item(VER#000001 status: UPLOADED → READY, chunkCount: N)
        |
        v
      DynamoDB record updated

S3: file remains stored indefinitely (source of truth)
Aurora: fast_chunks table now has N rows for this document
DynamoDB: VER#000001.status = "READY", chunkCount = N
SQS: message deleted automatically (Lambda returned normally)
```

---

## AWS Services Interaction Summary

| AWS Service | Phase | What Happens |
|-------------|-------|--------------|
| Amazon S3 | Phase 1 | File written by browser via presigned PUT |
| Amazon S3 | Phase 1 | ObjectCreated event fires to SQS |
| Amazon SQS | Phase 2 | Receives S3 notification, queues message |
| AWS Lambda | Phase 3-10 | Ingestion worker invoked by SQS |
| Amazon S3 | Phase 6 Step 1 | head_object reads doc metadata |
| AWS SSM Parameter Store | Phase 6 Step 2 | Reads Aurora ARNs and DB name |
| Amazon S3 | Phase 6 Step 3 | get_object downloads file bytes |
| Amazon Bedrock | Phase 8 | Titan Embed V2 generates 1024-dim vector |
| Amazon RDS Data API | Phase 9 | INSERT into fast_chunks via HTTPS |
| Aurora PostgreSQL 16.4 | Phase 9 | Stores chunk row, updates HNSW index |
| Amazon DynamoDB | Phase 10 | UpdateItem sets status READY + chunkCount |
| Amazon SQS DLQ | Phase 11 | Receives messages after 3 failed attempts |
| AWS CloudWatch Logs | Always | Lambda prints [INGEST] logs to log group |

---

## Validation Guide — How to Confirm Ingestion Worked

After uploading a document, use these steps to verify the entire pipeline ran correctly.

### Step 1 — Check DynamoDB document status

The quickest check. If ingestion succeeded, the status field changes from UPLOADED to READY.

Open AWS Console → DynamoDB → Tables → FAST-stack-documents → Explore items

Query by partition key:
```
PK = "TENANT#{your-cognito-sub}#DOC#{doc-id}"
SK = "VER#000001"
```

You should see:
```json
{
  "PK":         "TENANT#abc-123#DOC#doc-uuid",
  "SK":         "VER#000001",
  "status":     "READY",
  "chunkCount": 23,
  "updatedAt":  "2026-03-11T20:05:30.123456+00:00"
}
```

If status is still UPLOADED after a minute: Lambda has not run yet or is still processing.
If status is FAILED: check the errorMessage field and CloudWatch logs.

From AWS CLI:
```bash
aws dynamodb get-item \
  --table-name FAST-stack-documents \
  --key '{"PK":{"S":"TENANT#YOUR_SUB#DOC#YOUR_DOC_ID"},"SK":{"S":"VER#000001"}}' \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

### Step 2 — Check CloudWatch Lambda logs

Open AWS Console → CloudWatch → Log Groups → /aws/lambda/FAST-stack-ingestion-worker

Find the most recent log stream. You should see:
```
[INGEST] Processing s3://fast-stack-raw-docs/abc-123/v1/policy.pdf
[INGEST] tenant=abc-123 doc=doc-uuid v=1 file=policy.pdf
[INGEST] Produced 23 chunks for policy.pdf
[INGEST] Done — 23 chunks stored for doc doc-uuid
[DYNAMO] TENANT#abc-123#DOC#doc-uuid VER#000001 → READY
```

If you see [INGEST ERROR] lines, the error message tells you what failed.

From AWS CLI (latest 50 log events from most recent stream):
```bash
aws logs describe-log-streams \
  --log-group-name /aws/lambda/FAST-stack-ingestion-worker \
  --order-by LastEventTime \
  --descending \
  --max-items 1 \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```
Then:
```bash
aws logs get-log-events \
  --log-group-name /aws/lambda/FAST-stack-ingestion-worker \
  --log-stream-name "YOUR_STREAM_NAME" \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

### Step 3 — Check SQS queue depth

Open AWS Console → SQS → FAST-stack-ingestion-queue

Check:
- Messages Available: should be 0 after processing completes
- Messages In Flight: should be 0 after Lambda finishes
- Messages Not Visible: > 0 means Lambda is currently processing

If FAST-stack-ingestion-dlq has messages: ingestion failed after 3 retries.

From AWS CLI:
```bash
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/897675553288/FAST-stack-ingestion-queue \
  --attribute-names ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

### Step 4 — Query Aurora pgvector to see chunks directly

This is the definitive check. If rows exist in fast_chunks, the pipeline worked end-to-end.

Use the RDS Data API via AWS CLI (no psql client needed):

```bash
# Count all chunks for your document
aws rds-data execute-statement \
  --resource-arn "arn:aws:rds:us-east-1:897675553288:cluster:fast-stack-pgvector" \
  --secret-arn "arn:aws:secretsmanager:us-east-1:897675553288:secret:FAST-stack/aurora-pgvector-XXXXXX" \
  --database "ragdb" \
  --sql "SELECT COUNT(*) FROM fast_chunks WHERE doc_id = 'YOUR_DOC_ID'" \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

```bash
# See first 5 chunks with their metadata (no embedding column — too large to display)
aws rds-data execute-statement \
  --resource-arn "arn:aws:rds:us-east-1:897675553288:cluster:fast-stack-pgvector" \
  --secret-arn "arn:aws:secretsmanager:us-east-1:897675553288:secret:FAST-stack/aurora-pgvector-XXXXXX" \
  --database "ragdb" \
  --sql "SELECT id, tenant_id, doc_id, file_name, source_type, chunk_index, chunk_total, page_number, section_title FROM fast_chunks WHERE doc_id = 'YOUR_DOC_ID' ORDER BY chunk_index LIMIT 5" \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

```bash
# See the actual text content of chunk 0
aws rds-data execute-statement \
  --resource-arn "arn:aws:rds:us-east-1:897675553288:cluster:fast-stack-pgvector" \
  --secret-arn "arn:aws:secretsmanager:us-east-1:897675553288:secret:FAST-stack/aurora-pgvector-XXXXXX" \
  --database "ragdb" \
  --sql "SELECT chunk_index, LEFT(content, 300) as content_preview, chunk_total FROM fast_chunks WHERE doc_id = 'YOUR_DOC_ID' ORDER BY chunk_index" \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

```bash
# Check the embedding vector exists (shows first 5 dimensions)
aws rds-data execute-statement \
  --resource-arn "arn:aws:rds:us-east-1:897675553288:cluster:fast-stack-pgvector" \
  --secret-arn "arn:aws:secretsmanager:us-east-1:897675553288:secret:FAST-stack/aurora-pgvector-XXXXXX" \
  --database "ragdb" \
  --sql "SELECT id, embedding[1:5] as first_5_dims FROM fast_chunks WHERE doc_id = 'YOUR_DOC_ID' LIMIT 1" \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

```bash
# Count all chunks across all documents for your tenant
aws rds-data execute-statement \
  --resource-arn "arn:aws:rds:us-east-1:897675553288:cluster:fast-stack-pgvector" \
  --secret-arn "arn:aws:secretsmanager:us-east-1:897675553288:secret:FAST-stack/aurora-pgvector-XXXXXX" \
  --database "ragdb" \
  --sql "SELECT doc_id, file_name, source_type, COUNT(*) as chunk_count FROM fast_chunks WHERE tenant_id = 'YOUR_COGNITO_SUB' GROUP BY doc_id, file_name, source_type ORDER BY chunk_count DESC" \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

```bash
# Check the table schema is correct
aws rds-data execute-statement \
  --resource-arn "arn:aws:rds:us-east-1:897675553288:cluster:fast-stack-pgvector" \
  --secret-arn "arn:aws:secretsmanager:us-east-1:897675553288:secret:FAST-stack/aurora-pgvector-XXXXXX" \
  --database "ragdb" \
  --sql "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'fast_chunks' ORDER BY ordinal_position" \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

```bash
# Check the HNSW index was created
aws rds-data execute-statement \
  --resource-arn "arn:aws:rds:us-east-1:897675553288:cluster:fast-stack-pgvector" \
  --secret-arn "arn:aws:secretsmanager:us-east-1:897675553288:secret:FAST-stack/aurora-pgvector-XXXXXX" \
  --database "ragdb" \
  --sql "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'fast_chunks'" \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

To get the actual secret ARN, run:
```bash
aws secretsmanager list-secrets \
  --filter Key=name,Values=FAST-stack/aurora-pgvector \
  --profile bedrock-agentcore-rag \
  --region us-east-1 \
  --query "SecretList[0].ARN" \
  --output text
```

### Step 5 — Run a test vector similarity query

This confirms embeddings are semantically meaningful and the HNSW index works.

```bash
aws rds-data execute-statement \
  --resource-arn "arn:aws:rds:us-east-1:897675553288:cluster:fast-stack-pgvector" \
  --secret-arn "YOUR_SECRET_ARN" \
  --database "ragdb" \
  --sql "SELECT chunk_index, LEFT(content, 200) as preview, 1 - (embedding <=> '[0.02,0.01,...YOUR_TEST_VECTOR...]'::vector) as similarity FROM fast_chunks WHERE tenant_id = 'YOUR_SUB' ORDER BY embedding <=> '[0.02,0.01,...]'::vector LIMIT 5" \
  --profile bedrock-agentcore-rag \
  --region us-east-1
```

The <=> operator is pgvector's cosine distance operator. Ordering by it ascending gives
the most similar chunks first. Similarity score = 1 - cosine_distance (higher = more similar).

---

## Key Design Decisions

**Why S3 metadata instead of DynamoDB lookup in the ingestion worker?**
The presign Lambda stores doc-id, tenant-id, version as S3 object metadata when generating
the presigned URL. The ingestion worker reads this via head_object. This avoids a DynamoDB
read inside the ingestion worker, keeps it fully stateless, and removes a dependency between
the two Lambdas. If DynamoDB were unavailable during ingestion, the worker could still
identify the document.

**Why SQS between S3 and Lambda instead of a direct S3 trigger?**
S3 event notifications support only one direct Lambda target per event type per prefix.
SQS as a middle layer allows adding future consumers (analytics, antivirus scan, thumbnail
generation) without changing the S3 configuration. SQS also provides automatic retries with
a DLQ, whereas a direct S3-to-Lambda trigger has no retry mechanism on Lambda failure.

**Why batchSize=1 for the SQS event source?**
Each document is processed independently. With batchSize > 1, if one document fails, the
entire batch message is retried, potentially re-processing documents that already succeeded.
batchSize=1 gives clean per-document error isolation.

**Why visibilityTimeout matches Lambda timeout (both 15 minutes)?**
When SQS delivers a message to Lambda, it hides the message for visibilityTimeout seconds.
If Lambda is still running when visibilityTimeout expires, SQS makes the message visible
again and retries it, causing the same document to be processed twice. Setting both to the
same value ensures SQS never prematurely retries a still-running ingestion job.

**Why token-based chunking (tiktoken) instead of character-based?**
Embedding models have token limits, not character limits. A 600-character chunk of dense
code is far more tokens than 600 characters of plain prose. Tiktoken counts actual BPE
tokens so the chunk sizes are predictable and consistent across different text types.

**Why 600 tokens with 100 token overlap?**
600 tokens is large enough to contain meaningful semantic context (a full paragraph or
table section) but small enough to stay within Bedrock Titan Embed V2 input limits (8192
tokens). 100 token overlap (roughly 16% of chunk size) ensures that sentences crossing
chunk boundaries are captured by both adjacent chunks during retrieval.

**Why HNSW instead of IVFFlat for the vector index?**
IVFFlat requires a training step where you provide sample vectors to build cluster centroids.
It cannot be indexed until you have enough data. HNSW builds incrementally and supports
immediate queries from the first inserted row. For this project where document counts are
unpredictable, HNSW is the correct choice.

**Why vector(1024) and not vector(1536)?**
1536 is the output dimension of OpenAI text-embedding-ada-002. Amazon Bedrock Titan Embed
Text V2 outputs 1024 dimensions when dimensions=1024 is passed in the request body. Using
1536 with Titan V2 would cause a dimension mismatch error on INSERT.

**Why normalize=True in the Bedrock call?**
Normalized vectors (unit vectors) allow the cosine similarity calculation to be simplified
to a dot product, which is faster. pgvector's <=> operator (cosine distance) works correctly
with both normalized and unnormalized vectors, but normalized inputs give more consistent
similarity scores across documents of different lengths.

**Why Lambda stays outside the VPC while Aurora is inside a private VPC?**
RDS Data API is a regional AWS HTTPS endpoint. Lambda calls it via IAM-authenticated HTTPS
without needing a direct TCP connection to port 5432. Aurora's security comes from its
private subnet and locked security group, not from where Lambda lives. Putting Lambda inside
the VPC would require VPC Endpoints (~$29/month for 4 services) or a NAT Gateway (~$32/month)
to reach Bedrock, SSM, and DynamoDB. Lambda outside the VPC costs nothing extra.
