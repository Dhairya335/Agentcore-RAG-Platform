"use client"

import { FileText, FileSpreadsheet, FileJson, File, CheckCircle2, Clock, AlertCircle } from "lucide-react"
import { DocumentListItem } from "@/services/documentService"
import { cn } from "@/lib/utils"

const EXT_ICON: Record<string, React.ReactNode> = {
  pdf:  <FileText        className="h-4 w-4 text-red-500"    />,
  docx: <FileText        className="h-4 w-4 text-blue-500"   />,
  doc:  <FileText        className="h-4 w-4 text-blue-500"   />,
  md:   <FileText        className="h-4 w-4 text-blue-400"   />,
  txt:  <FileText        className="h-4 w-4 text-gray-400"   />,
  xlsx: <FileSpreadsheet className="h-4 w-4 text-green-600"  />,
  xls:  <FileSpreadsheet className="h-4 w-4 text-green-600"  />,
  csv:  <FileSpreadsheet className="h-4 w-4 text-green-500"  />,
  json: <FileJson        className="h-4 w-4 text-yellow-500" />,
}

const DEFAULT_ICON = <File className="h-4 w-4 text-gray-400" />

const STATUS_CONFIG: Record<DocumentListItem["status"], { icon: React.ReactNode; label: string; className: string }> = {
  READY:    { icon: <CheckCircle2 className="h-3 w-3" />,                     label: "Ready",    className: "text-green-700 bg-green-50"   },
  FAILED:   { icon: <AlertCircle  className="h-3 w-3" />,                     label: "Failed",   className: "text-red-700   bg-red-50"     },
  UPLOADED: { icon: <Clock        className="h-3 w-3 animate-pulse" />,        label: "Indexing", className: "text-yellow-700 bg-yellow-50" },
}

function getFileIcon(fileName: string): React.ReactNode {
  const ext = fileName.split(".").pop()?.toLowerCase() ?? ""
  return EXT_ICON[ext] ?? DEFAULT_ICON
}

function formatRelativeDate(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const rtf  = new Intl.RelativeTimeFormat("en", { numeric: "auto" })
  const cuts: [number, Intl.RelativeTimeFormatUnit][] = [
    [60_000,        "minute"],
    [3_600_000,     "hour"  ],
    [86_400_000,    "day"   ],
    [2_592_000_000, "month" ],
  ]
  for (const [ms, unit] of cuts) {
    if (diff < ms) return rtf.format(-Math.round(diff / (ms / (unit === "minute" ? 1 : 60))), unit)
  }
  return rtf.format(-Math.round(diff / 2_592_000_000), "month")
}

interface DocumentCardProps {
  doc:        DocumentListItem
  isSelected: boolean
  onSelect:   (doc: DocumentListItem) => void
}

export function DocumentCard({ doc, isSelected, onSelect }: DocumentCardProps) {
  const status = STATUS_CONFIG[doc.status]

  return (
    <button
      onClick={() => onSelect(doc)}
      className={cn(
        "w-full text-left px-3 py-2.5 rounded-md transition-colors",
        "hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-400",
        isSelected && "bg-blue-50 hover:bg-blue-50"
      )}
    >
      <div className="flex items-start gap-2">
        <span className="mt-0.5 flex-shrink-0">{getFileIcon(doc.fileName)}</span>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium text-gray-800 truncate">{doc.fileName}</p>
          <div className="flex items-center gap-2 mt-0.5">
            <span className={cn("inline-flex items-center gap-1 text-xs font-medium rounded-full px-1.5 py-0.5", status.className)}>
              {status.icon}{status.label}
            </span>
            {doc.chunkCount != null && (
              <span className="text-xs text-gray-400">{doc.chunkCount} chunks</span>
            )}
          </div>
          <p className="text-xs text-gray-400 mt-0.5">{formatRelativeDate(doc.updatedAt)}</p>
        </div>
      </div>
    </button>
  )
}
