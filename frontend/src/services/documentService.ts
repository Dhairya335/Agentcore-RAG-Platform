let DOCS_API_BASE = ""

async function loadDocsApiBase(): Promise<string> {
  if (DOCS_API_BASE) return DOCS_API_BASE

  try {
    const response = await fetch("/aws-exports.json")
    const config   = await response.json()
    if (!config.docsApiUrl) throw new Error("docsApiUrl not found in aws-exports.json")
    DOCS_API_BASE = config.docsApiUrl.endsWith("/")
      ? config.docsApiUrl
      : `${config.docsApiUrl}/`
    return DOCS_API_BASE
  } catch (error) {
    console.error("Failed to load docs API URL:", error)
    throw new Error("Document API URL not configured — check aws-exports.json")
  }
}

// Legacy alias — existing callers use loadDocsApiUrl() which pointed at the presign endpoint. Keep returning the full presign URL to avoid breaking them.
async function loadDocsApiUrl(): Promise<string> {
  const base = await loadDocsApiBase()
  return `${base}documents/presign`
}
export interface PresignRequest {
  fileName: string
  contentType: string
  tenantId: string       // v1: same as Cognito userId extracted from token
  docId?: string         // omit for new doc, pass to upload new version
  metadata?: Record<string, string>
}

export interface PresignResponse {
  uploadUrl: string      // presigned S3 PUT URL (15 min expiry)
  docId: string
  version: number
  s3Key: string
}

export interface UploadedDocument {
  docId: string
  version: number
  fileName: string
  s3Key: string
  status: "uploading" | "uploaded" | "error"
}


/**
 * Detect MIME type from file extension.
 * S3 needs the correct ContentType for the presigned URL to work.
 */
export function getContentType(file: File): string {
  const ext = file.name.split(".").pop()?.toLowerCase() ?? ""
  const map: Record<string, string> = {
    txt:  "text/plain",
    pdf:  "application/pdf",
    docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    doc:  "application/msword",
    csv:  "text/csv",
    md:   "text/markdown",
    json: "application/json",
    xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  }
  return map[ext] ?? file.type ?? "application/octet-stream"
}

/**
 * Step 1: Ask Lambda for a presigned S3 URL.
 * Lambda simultaneously creates a DynamoDB record (status: UPLOADED).
 *
 * Uses id_token (not access_token) — same as feedbackService,
 * because API Gateway Cognito authorizer validates ID tokens.
 */
export async function getPresignedUploadUrl(
  request: PresignRequest,
  idToken: string
): Promise<PresignResponse> {
  const apiUrl = await loadDocsApiUrl()

  const response = await fetch(apiUrl, {
    method: "POST",
    headers: {
      "Content-Type":  "application/json",
      Authorization:   `Bearer ${idToken}`,
    },
    body: JSON.stringify(request),
  })

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}))
    throw new Error(errorData.error || `Presign failed: HTTP ${response.status}`)
  }

  return response.json() as Promise<PresignResponse>
}

/**
 * Step 2: PUT the file directly to S3 using the presigned URL.
 * No auth header — the presigned URL itself IS the auth credential.
 * No Lambda involved — file goes straight from browser to S3.
 *
 * onProgress: called with 0–100 as the upload progresses.
 */
export async function uploadFileToS3(
  file: File,
  uploadUrl: string,
  contentType: string,
  onProgress?: (percent: number) => void
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()

    // Track upload progress
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) {
        const percent = Math.round((event.loaded / event.total) * 100)
        onProgress(percent)
      }
    })

    xhr.addEventListener("load", () => {
      // S3 returns 200 for successful PUT
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100)
        resolve()
      } else {
        reject(new Error(`S3 upload failed: HTTP ${xhr.status}`))
      }
    })

    xhr.addEventListener("error", () => {
      reject(new Error("S3 upload failed: network error"))
    })

    xhr.open("PUT", uploadUrl)
    xhr.setRequestHeader("Content-Type", contentType)
    xhr.send(file)
  })
}

/**
 * Full upload flow — combines Step 1 + Step 2.
 * Call this from the UI component.
 *
 * @param file       - The File object from the file picker
 * @param tenantId   - The Cognito user's sub (userId)
 * @param idToken    - Cognito ID token for API Gateway auth
 * @param onProgress - Optional 0–100 progress callback
 * @returns          - The uploaded document metadata
 */
export async function uploadDocument(
  file: File,
  tenantId: string,
  idToken: string,
  onProgress?: (percent: number) => void
): Promise<UploadedDocument> {
  const contentType = getContentType(file)

  // Step 1: Get presigned URL from Lambda
  // Lambda creates DynamoDB record with status: UPLOADED
  onProgress?.(0)
  const presign = await getPresignedUploadUrl(
    {
      fileName:    file.name,
      contentType,
      tenantId,
    },
    idToken
  )

  // Step 2: PUT file directly to S3
  // No Lambda bottleneck — browser → S3 directly
  await uploadFileToS3(file, presign.uploadUrl, contentType, (pct) => {
    // Map 0–100 upload progress to 10–100 overall
    // (0–10 is reserved for the presign step above)
    onProgress?.(10 + Math.round(pct * 0.9))
  })

  return {
    docId:    presign.docId,
    version:  presign.version,
    fileName: file.name,
    s3Key:    presign.s3Key,
    status:   "uploaded",
  }
}

//Document Status Polling (Phase 2D)

export type IndexingStatus = "uploading" | "indexing" | "ready" | "failed"

export interface DocumentStatus {
  docId:         string
  status:        "UPLOADED" | "READY" | "FAILED"
  fileName?:     string
  chunkCount?:   number
  errorMessage?: string
  updatedAt?:    string
}

/**
 * Poll GET /documents/{docId}/status until the document is READY or FAILED.
 *
 * Called by DocumentUploadPanel immediately after a successful S3 upload.
 * The ingestion pipeline is asynchronous (S3 → SQS → Lambda → Aurora), so
 * the frontend polls every 3 seconds until one of three things happens:
 *   1. status === "READY"  → chunks are in Aurora, document is searchable
 *   2. status === "FAILED" → ingestion errored, show error message
 *   3. timeout reached     → give up gracefully, treat as unknown
 *
 * The AbortController allows the caller to cancel polling on component unmount
 * (avoids setState on an unmounted component / memory leaks).
 *
 * @param docId        - The document ID returned by the presign Lambda
 * @param tenantId     - The user's Cognito sub
 * @param idToken      - Cognito ID token for API Gateway Cognito authorizer
 * @param onStatus     - Called on every poll with the current IndexingStatus
 * @param signal       - AbortController signal to cancel polling on unmount
 * @param intervalMs   - How often to poll (default: 3000 ms)
 * @param timeoutMs    - Give up after this many ms (default: 60 000 ms)
 */
export async function pollDocumentStatus(
  docId:       string,
  tenantId:    string,
  idToken:     string,
  onStatus:    (status: IndexingStatus, meta?: DocumentStatus) => void,
  signal?:     AbortSignal,
  intervalMs   = 3_000,
  timeoutMs    = 60_000,
): Promise<void> {
  const base      = await loadDocsApiBase()
  const url       = `${base}documents/${encodeURIComponent(docId)}/status?tenantId=${encodeURIComponent(tenantId)}`
  const deadline  = Date.now() + timeoutMs

  onStatus("indexing")

  while (Date.now() < deadline) {
    // Respect AbortController (component unmount)
    if (signal?.aborted) return

    try {
      const resp = await fetch(url, {
        headers: { Authorization: `Bearer ${idToken}` },
        signal,
      })

      if (resp.ok) {
        const data: DocumentStatus = await resp.json()

        if (data.status === "READY") {
          onStatus("ready", data)
          return
        }

        if (data.status === "FAILED") {
          onStatus("failed", data)
          return
        }

        // Still UPLOADED — keep polling
        onStatus("indexing", data)
      }
      // Non-2xx: transient API error — keep polling silently
    } catch (err) {
      // fetch throws on network error or abort
      if (signal?.aborted) return
      console.warn("[pollDocumentStatus] fetch error (will retry):", err)
    }

    // Wait before next poll — use a cancellable sleep
    await _sleep(intervalMs, signal)
  }

  // Timeout — emit "indexing" and let the caller handle it
  console.warn(`[pollDocumentStatus] timed out after ${timeoutMs}ms for docId=${docId}`)
  onStatus("indexing")
}

/** Cancellable sleep — resolves after ms or when signal is aborted */
function _sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms)
    signal?.addEventListener("abort", () => { clearTimeout(timer); resolve() }, { once: true })
  })
}