// Cached docs API URL — loaded once from aws-exports.json
let DOCS_API_URL = ""

async function loadDocsApiUrl(): Promise<string> {
  if (DOCS_API_URL) return DOCS_API_URL

  try {
    const response = await fetch("/aws-exports.json")
    const config = await response.json()
    // docsApiUrl comes from CDK output stored in aws-exports.json
    // Value example: "https://abc123.execute-api.us-east-1.amazonaws.com/prod/"
    DOCS_API_URL = config.docsApiUrl ? `${config.docsApiUrl}documents/presign` : ""
    if (!DOCS_API_URL) throw new Error("docsApiUrl not found in aws-exports.json")
    return DOCS_API_URL
  } catch (error) {
    console.error("Failed to load docs API URL:", error)
    throw new Error("Document API URL not configured — check aws-exports.json")
  }
}

// ─── Types ───────────────────────────────────────────────────────────────────

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

// ─── Helpers ─────────────────────────────────────────────────────────────────

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

// ─── Main Functions ───────────────────────────────────────────────────────────

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