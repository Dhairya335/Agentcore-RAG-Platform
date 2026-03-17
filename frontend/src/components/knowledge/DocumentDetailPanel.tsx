"use client"

import { ArrowLeft, Layers, Tag, File, Calendar, Clock, AlertCircle, CheckCircle2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { DocumentListItem, DocumentDetail } from "@/services/documentService"
import { cn } from "@/lib/utils"

const STATUS_CONFIG: Record<DocumentListItem["status"], { icon: React.ReactNode; label: string; className: string }> = {
  READY:    { icon: <CheckCircle2 className="h-3 w-3" />,              label: "Ready",    className: "text-green-700 bg-green-50"   },
  FAILED:   { icon: <AlertCircle  className="h-3 w-3" />,              label: "Failed",   className: "text-red-700   bg-red-50"     },
  UPLOADED: { icon: <Clock        className="h-3 w-3 animate-pulse" />, label: "Indexing", className: "text-yellow-700 bg-yellow-50" },
}

function MetaRow({ icon, label, value }: { icon: React.ReactNode; label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2.5 py-2 border-b border-gray-50 last:border-0">
      <span className="text-gray-400 mt-0.5 flex-shrink-0">{icon}</span>
      <div className="flex-1 min-w-0">
        <p className="text-xs text-gray-400 leading-none mb-0.5">{label}</p>
        <div className="text-sm text-gray-800 font-medium break-all">{value}</div>
      </div>
    </div>
  )
}

function formatDate(iso: string | null): string {
  return iso
    ? new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : "—"
}

interface DocumentDetailPanelProps {
  doc:     DocumentListItem
  detail:  DocumentDetail | null
  loading: boolean
  error:   string | null
  onBack:  () => void
}

export function DocumentDetailPanel({ doc, detail, loading, error, onBack }: DocumentDetailPanelProps) {
  const status   = STATUS_CONFIG[detail?.status ?? doc.status]
  const fileName = detail?.fileName ?? doc.fileName

  const metaRows: { icon: React.ReactNode; label: string; value: React.ReactNode; show: boolean }[] = [
    { icon: <Layers   className="h-3.5 w-3.5" />, label: "Chunks indexed", value: `${detail?.chunkCount} chunk${detail?.chunkCount !== 1 ? "s" : ""}`, show: detail?.chunkCount != null },
    { icon: <Tag      className="h-3.5 w-3.5" />, label: "Version",        value: `v${detail?.latestVersion ?? 1}`,                                      show: !!detail              },
    { icon: <File     className="h-3.5 w-3.5" />, label: "Content type",   value: detail?.contentType,                                                   show: !!detail?.contentType },
    { icon: <Calendar className="h-3.5 w-3.5" />, label: "Uploaded",       value: formatDate(detail?.createdAt ?? null),                                  show: !!detail?.createdAt   },
    { icon: <Clock    className="h-3.5 w-3.5" />, label: "Last updated",   value: formatDate(detail?.updatedAt ?? null),                                  show: !!detail?.updatedAt   },
  ]

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b flex-none">
        <Button variant="ghost" size="sm" className="text-xs text-gray-500 px-1 -ml-1" onClick={onBack}>
          <ArrowLeft className="h-3.5 w-3.5 mr-1" /> Back to library
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        <div className="mb-4">
          <p className="text-sm font-semibold text-gray-900 break-all leading-snug">{fileName}</p>
          <span className={cn("mt-1.5 inline-flex items-center gap-1 text-xs font-medium rounded-full px-2 py-0.5", status.className)}>
            {status.icon}{status.label}
          </span>
        </div>

        {loading && (
          <div className="space-y-3">
            {Array.from({ length: 4 }, (_, i) => (
              <div key={i} className="flex items-start gap-2.5 py-2">
                <Skeleton className="h-3.5 w-3.5 rounded flex-shrink-0 mt-0.5" />
                <div className="flex-1 space-y-1">
                  <Skeleton className="h-3 w-1/3 rounded" />
                  <Skeleton className="h-3.5 w-2/3 rounded" />
                </div>
              </div>
            ))}
          </div>
        )}

        {error && !loading && (
          <div className="flex items-start gap-2 text-red-600 bg-red-50 rounded-md p-3 text-xs">
            <AlertCircle className="h-4 w-4 flex-shrink-0 mt-0.5" />
            <p>{error}</p>
          </div>
        )}

        {detail && !loading && (
          <div>
            {metaRows.filter(r => r.show).map(r => (
              <MetaRow key={r.label} icon={r.icon} label={r.label} value={r.value} />
            ))}
            {detail.status === "FAILED" && detail.errorMessage && (
              <div className="mt-3 p-3 bg-red-50 rounded-md border border-red-100">
                <p className="text-xs font-medium text-red-700 mb-1">Ingestion error</p>
                <p className="text-xs text-red-600 break-all font-mono leading-relaxed">{detail.errorMessage}</p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
