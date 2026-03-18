"use client"

/**
 * SourceViewerDrawer — Phase 3.5
 *
 * Right-side Sheet that opens when an INTERNAL user clicks a citation chip
 * in the chat. Fetches the cited chunk plus neighbours via preview-chunks
 * Lambda in anchor mode (chunkIndex present → BETWEEN query).
 *
 * Props driven by the parsed citation from MarkdownRenderer:
 *   docId, tenantId, chunkIndex, fileName
 *
 * Anchor window: fetches 5 chunks centred on chunkIndex (2 before, anchor, 2 after).
 * The anchor chunk is visually highlighted.
 */

import { useCallback, useEffect, useState } from "react"
import { useAuth } from "react-oidc-context"
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { previewChunks, type ChunkPreview } from "@/services/documentService"
import { FileText, AlertCircle } from "lucide-react"
import { cn } from "@/lib/utils"

export interface SourceViewerTarget {
  docId:      string
  fileName:   string
  chunkIndex: number
}

interface SourceViewerDrawerProps {
  target:    SourceViewerTarget | null
  onClose:   () => void
}

export function SourceViewerDrawer({ target, onClose }: SourceViewerDrawerProps) {
  const auth = useAuth()

  const [chunks,  setChunks]  = useState<ChunkPreview[]>([])
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState<string | null>(null)

  const fetch = useCallback(async (t: SourceViewerTarget) => {
    const idToken  = auth.user?.id_token     ?? ""
    const tenantId = auth.user?.profile?.sub ?? ""
    if (!idToken || !tenantId) return

    setLoading(true)
    setError(null)
    setChunks([])

    try {
      const result = await previewChunks(t.docId, tenantId, idToken, 5, t.chunkIndex)
      setChunks(result.chunks)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load chunk")
    } finally {
      setLoading(false)
    }
  }, [auth.user])

  useEffect(() => {
    if (target) fetch(target)
    else { setChunks([]); setError(null) }
  }, [target, fetch])

  return (
    <Sheet open={!!target} onOpenChange={v => { if (!v) onClose() }}>
      <SheetContent side="right" className="w-[480px] sm:max-w-[480px] p-0 flex flex-col">

        <SheetHeader className="px-4 py-3 border-b flex-none">
          <SheetTitle className="flex items-center gap-2 text-sm font-semibold text-gray-800 truncate">
            <FileText className="h-4 w-4 text-blue-600 flex-shrink-0" />
            <span className="truncate">{target?.fileName ?? "Source"}</span>
          </SheetTitle>
          {target && (
            <p className="text-xs text-gray-400 mt-0.5">
              Chunk {target.chunkIndex + 1} · surrounding context
            </p>
          )}
        </SheetHeader>

        <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">

          {loading && Array.from({ length: 3 }, (_, i) => (
            <div key={i} className="space-y-2 p-3 rounded-lg border border-gray-100">
              <Skeleton className="h-3 w-1/4 rounded" />
              <Skeleton className="h-3.5 w-full rounded" />
              <Skeleton className="h-3.5 w-5/6 rounded" />
              <Skeleton className="h-3.5 w-3/4 rounded" />
            </div>
          ))}

          {error && !loading && (
            <div className="flex items-start gap-2 text-red-600 bg-red-50 rounded-md p-3 text-xs">
              <AlertCircle className="h-4 w-4 flex-shrink-0 mt-0.5" />
              <p>{error}</p>
            </div>
          )}

          {!loading && chunks.map(chunk => (
            <div
              key={chunk.chunkIndex}
              className={cn(
                "p-3 rounded-lg border text-sm leading-relaxed whitespace-pre-wrap transition-colors",
                chunk.chunkIndex === target?.chunkIndex
                  ? "border-blue-300 bg-blue-50 text-gray-900"
                  : "border-gray-100 bg-white text-gray-600"
              )}
            >
              <div className="flex items-center gap-2 mb-2 text-xs text-gray-400">
                <span>Chunk {chunk.chunkIndex + 1}/{chunk.chunkTotal}</span>
                {chunk.pageNumber != null && <span>· Page {chunk.pageNumber}</span>}
                {chunk.sectionTitle && <span className="truncate">· {chunk.sectionTitle}</span>}
                {chunk.chunkIndex === target?.chunkIndex && (
                  <span className="ml-auto text-blue-600 font-medium">cited</span>
                )}
              </div>
              {chunk.content}
            </div>
          ))}

        </div>

      </SheetContent>
    </Sheet>
  )
}
