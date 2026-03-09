"use client"

/**
 * DocumentUploadPanel
 *
 * A collapsible panel that sits ABOVE the ChatInput bar.
 * Triggered by a paperclip button added to ChatInput.
 *
 * Features:
 *   - Drag & drop or click-to-browse file picker
 *   - Shows file name, size, and type icon
 *   - Live upload progress bar (0–100%)
 *   - Success / error states with clear feedback
 *   - Supports: txt, pdf, docx, doc, csv, md, json, xlsx, pptx
 */

import { useState, useRef, useCallback, DragEvent, ChangeEvent } from "react"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import {
  FileText,
  FileSpreadsheet,
  File,
  X,
  Upload,
  CheckCircle2,
  AlertCircle,
  Loader2,
} from "lucide-react"
import { uploadDocument, UploadedDocument } from "@/services/documentService"
import { useAuth } from "react-oidc-context"

// ─── Accepted file types ──────────────────────────────────────────────────────

const ACCEPTED_EXTENSIONS = [
  ".txt", ".pdf", ".docx", ".doc",
  ".csv", ".md", ".json", ".xlsx", ".pptx",
]
const ACCEPTED_MIME = ACCEPTED_EXTENSIONS.join(",")
const MAX_FILE_SIZE_MB = 50

// ─── Types ────────────────────────────────────────────────────────────────────

type UploadState =
  | { status: "idle" }
  | { status: "selected"; file: File }
  | { status: "uploading"; file: File; progress: number }
  | { status: "success"; file: File; doc: UploadedDocument }
  | { status: "error"; file: File; message: string }

interface DocumentUploadPanelProps {
  /** Called when panel should be closed (e.g. user clicks X) */
  onClose: () => void
  /** Optional callback when upload succeeds — e.g. to add a chat message */
  onUploadSuccess?: (doc: UploadedDocument) => void
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function FileIcon({ fileName }: { fileName: string }) {
  const ext = fileName.split(".").pop()?.toLowerCase() ?? ""
  if (["xlsx", "csv"].includes(ext)) return <FileSpreadsheet className="h-8 w-8 text-green-500" />
  if (["pdf"].includes(ext)) return <FileText className="h-8 w-8 text-red-500" />
  if (["docx", "doc"].includes(ext)) return <FileText className="h-8 w-8 text-blue-500" />
  return <File className="h-8 w-8 text-gray-400" />
}

// ─── Component ────────────────────────────────────────────────────────────────

export function DocumentUploadPanel({ onClose, onUploadSuccess }: DocumentUploadPanelProps) {
  const [state, setState] = useState<UploadState>({ status: "idle" })
  const [isDragOver, setIsDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const auth = useAuth()

  // ── File validation ─────────────────────────────────────────────────────────

  function validateFile(file: File): string | null {
    const ext = "." + (file.name.split(".").pop()?.toLowerCase() ?? "")
    if (!ACCEPTED_EXTENSIONS.includes(ext)) {
      return `File type not supported. Accepted: ${ACCEPTED_EXTENSIONS.join(", ")}`
    }
    if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
      return `File too large. Maximum size: ${MAX_FILE_SIZE_MB}MB`
    }
    return null
  }

  function selectFile(file: File) {
    const error = validateFile(file)
    if (error) {
      setState({ status: "error", file, message: error })
      return
    }
    setState({ status: "selected", file })
  }

  // ── Drag and drop ───────────────────────────────────────────────────────────

  const handleDragOver = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    setIsDragOver(true)
  }, [])

  const handleDragLeave = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    setIsDragOver(false)
  }, [])

  const handleDrop = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    setIsDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file) selectFile(file)
  }, [])

  // ── File input ──────────────────────────────────────────────────────────────

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) selectFile(file)
    // Reset input so same file can be re-selected after error
    e.target.value = ""
  }

  // ── Upload ──────────────────────────────────────────────────────────────────

  async function handleUpload() {
    if (state.status !== "selected") return

    const { file } = state

    // Get Cognito ID token — same as feedbackService pattern
    const idToken = auth.user?.id_token
    // Get userId from Cognito sub claim — used as tenantId (v1)
    const tenantId = auth.user?.profile?.sub

    if (!idToken || !tenantId) {
      setState({
        status: "error",
        file,
        message: "Authentication required. Please log in again.",
      })
      return
    }

    setState({ status: "uploading", file, progress: 0 })

    try {
      const doc = await uploadDocument(
        file,
        tenantId,
        idToken,
        (progress) => {
          setState({ status: "uploading", file, progress })
        }
      )

      setState({ status: "success", file, doc })
      onUploadSuccess?.(doc)
    } catch (err) {
      const message = err instanceof Error ? err.message : "Upload failed"
      setState({ status: "error", file, message })
    }
  }

  // ── Reset ───────────────────────────────────────────────────────────────────

  function handleReset() {
    setState({ status: "idle" })
  }

  // ─── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="mx-4 mb-2 bg-white border border-gray-200 rounded-xl shadow-md overflow-hidden">

      {/* Panel header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100 bg-gray-50">
        <div className="flex items-center gap-2">
          <Upload className="h-4 w-4 text-gray-600" />
          <span className="text-sm font-semibold text-gray-700">Upload Document</span>
        </div>
        <button
          onClick={onClose}
          className="text-gray-400 hover:text-gray-600 transition-colors rounded-md p-1 hover:bg-gray-100"
          aria-label="Close upload panel"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="p-4">

        {/* ── IDLE: drag & drop zone ─────────────────────────────────────── */}
        {state.status === "idle" && (
          <div
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            className={`
              border-2 border-dashed rounded-lg p-8 text-center cursor-pointer
              transition-all duration-200
              ${isDragOver
                ? "border-blue-400 bg-blue-50"
                : "border-gray-200 hover:border-gray-300 hover:bg-gray-50"
              }
            `}
          >
            <Upload className="h-8 w-8 text-gray-400 mx-auto mb-3" />
            <p className="text-sm font-medium text-gray-700 mb-1">
              Drop a file here or <span className="text-blue-600">browse</span>
            </p>
            <p className="text-xs text-gray-400">
              PDF, DOCX, TXT, CSV, MD, XLSX, JSON, PPTX — up to {MAX_FILE_SIZE_MB}MB
            </p>
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPTED_MIME}
              onChange={handleFileChange}
              className="hidden"
            />
          </div>
        )}

        {/* ── SELECTED: file preview + upload button ─────────────────────── */}
        {state.status === "selected" && (
          <div className="space-y-4">
            <div className="flex items-center gap-3 p-3 bg-gray-50 rounded-lg border border-gray-100">
              <FileIcon fileName={state.file.name} />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-800 truncate">{state.file.name}</p>
                <p className="text-xs text-gray-500">{formatFileSize(state.file.size)}</p>
              </div>
              <button
                onClick={handleReset}
                className="text-gray-400 hover:text-gray-600 p-1 rounded"
                aria-label="Remove file"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex gap-2 justify-end">
              <Button variant="outline" size="sm" onClick={handleReset}>
                Cancel
              </Button>
              <Button size="sm" onClick={handleUpload} className="gap-2">
                <Upload className="h-4 w-4" />
                Upload
              </Button>
            </div>
          </div>
        )}

        {/* ── UPLOADING: progress bar ────────────────────────────────────── */}
        {state.status === "uploading" && (
          <div className="space-y-3">
            <div className="flex items-center gap-3 p-3 bg-gray-50 rounded-lg border border-gray-100">
              <Loader2 className="h-8 w-8 text-blue-500 animate-spin flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-800 truncate">{state.file.name}</p>
                <p className="text-xs text-gray-500">{formatFileSize(state.file.size)}</p>
              </div>
            </div>
            <div className="space-y-1">
              <div className="flex justify-between text-xs text-gray-500">
                <span>Uploading...</span>
                <span>{state.progress}%</span>
              </div>
              <Progress value={state.progress} className="h-2" />
            </div>
          </div>
        )}

        {/* ── SUCCESS ────────────────────────────────────────────────────── */}
        {state.status === "success" && (
          <div className="space-y-4">
            <div className="flex items-center gap-3 p-3 bg-green-50 rounded-lg border border-green-100">
              <CheckCircle2 className="h-8 w-8 text-green-500 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-800 truncate">{state.file.name}</p>
                <p className="text-xs text-green-600 font-medium">
                  Uploaded successfully — processing will begin shortly
                </p>
                <p className="text-xs text-gray-400 font-mono mt-0.5">
                  ID: {state.doc.docId.slice(0, 8)}… · v{state.doc.version}
                </p>
              </div>
            </div>
            <div className="flex gap-2 justify-end">
              <Button variant="outline" size="sm" onClick={handleReset}>
                Upload another
              </Button>
              <Button size="sm" onClick={onClose}>
                Done
              </Button>
            </div>
          </div>
        )}

        {/* ── ERROR ──────────────────────────────────────────────────────── */}
        {state.status === "error" && (
          <div className="space-y-4">
            <div className="flex items-center gap-3 p-3 bg-red-50 rounded-lg border border-red-100">
              <AlertCircle className="h-8 w-8 text-red-500 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-800 truncate">{state.file.name}</p>
                <p className="text-xs text-red-600">{state.message}</p>
              </div>
            </div>
            <div className="flex gap-2 justify-end">
              <Button variant="outline" size="sm" onClick={handleReset}>
                Try again
              </Button>
            </div>
          </div>
        )}

      </div>
    </div>
  )
}