# AgentCore RAG Platform

A production-grade AI Knowledge Operating System built on AWS Bedrock AgentCore, designed for secure, multi-tenant document ingestion, retrieval, and reasoning.

This project extends the AWS FAST (Fullstack AgentCore Solution Template) into a fully customized Retrieval-Augmented Generation (RAG) platform with advanced retrieval control, tenant isolation, and scalable ingestion pipelines.

This project is based on AWS FAST (Fullstack Solution Template for AgentCore), licensed under Apache 2.0, with significant architectural modifications.

---

## What This Is

This is not a basic chatbot.

This system is designed as:
- A private AI knowledge system
- A document ingestion and retrieval engine
- A reasoning layer over structured and unstructured data

It enables:
- Secure document uploads per tenant
- Semantic retrieval using vector embeddings
- Context-aware response generation using LLMs
- Source-grounded answers with traceability

---

## Architecture Overview

High-level flow:

Browser → Cognito Authentication → API Gateway → Presign Lambda → S3 Upload  
→ S3 Event → SQS → Ingestion Lambda → Embedding (Titan V2) → Aurora pgvector  
→ Query → Agent Runtime → Retrieval Lambda → Vector Search → Filter → LLM Response

### Key Components

- **Frontend**: React-based chat interface with document upload  
- **Authentication**: AWS Cognito (JWT-based)  
- **Storage**:  
  - S3 (document storage)  
  - DynamoDB (metadata + status)  
  - Aurora PostgreSQL (pgvector for embeddings)  
- **Ingestion Pipeline**: SQS + Lambda (asynchronous processing)  
- **Embedding Model**: Amazon Titan Embed V2 (1024 dimensions)  
- **Agent Runtime**: AWS Bedrock AgentCore  
- **LLM**: Claude (via Bedrock)  

---

## Core Features

- Multi-tenant document isolation  
- Secure upload using presigned URLs  
- Asynchronous ingestion pipeline (SQS + Lambda)  
- Vector-based semantic retrieval (pgvector)  
- Similarity filtering and controlled top-K retrieval  
- LLM reasoning with grounded context  
- Source attribution (chunk-level metadata)  

---

## Retrieval Strategy

- Query embedding using Titan V2 (1024 dimensions)  
- Candidate fetch: topK * 2 (SQL LIMIT)  
- SQL fetch ceiling: 16  
- Post-filtering in Lambda using similarity threshold (0.30)  
- Final response context capped at topK (max 8)  

This ensures:
- high precision retrieval  
- reduced hallucination  
- controlled token usage  

---

## Project Structure

frontend/ → UI and chat interface
infra-cdk/ → AWS infrastructure (CDK)
lambdas/ → ingestion + retrieval logic
database/ → schema definitions
docs/ → architecture and design


---

## Setup & Deployment

### Prerequisites

- AWS account with Bedrock + AgentCore access  
- Node.js  
- AWS CDK  
- Docker  

### Deploy Infrastructure

```bash
cd infra-cdk
npm install
cdk bootstrap
cdk deploy
```

### Run Frontend

```bash
cd frontend
npm install
npm run dev
```

### Security

- JWT-based authentication via Cognito
- Tenant isolation enforced at query layer
- Private S3 buckets (no public access)
- IAM least-privilege policies

Note: This project is not production-hardened by default. Security, compliance, and data protection must be implemented based on your use case.

### Limitations

- No keyword fallback (vector-only retrieval)
- No reranking (planned future enhancement)
- Optimized for controlled datasets, not open web search

### Roadmap

- Document management layer (collections, UI)
- Advanced retrieval (section-aware, deduplication)
- Observability and evaluation framework
- Workflow automation (agent-driven tasks)
- Multi-agent orchestration

### License

This project includes components derived from AWS FAST (Fullstack Solution Template for AgentCore), licensed under the Apache License 2.0.

All modifications and extensions are the work of this repository.