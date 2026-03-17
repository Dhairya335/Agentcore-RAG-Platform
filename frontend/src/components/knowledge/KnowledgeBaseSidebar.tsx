"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useAuth } from "react-oidc-context"
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { DocumentCard } from "./DocumentCard"
import { DocumentDetailPanel } from "./DocumentDetailPanel"
import {
  listDocuments,
  getDocument,
  type DocumentListItem,
  type DocumentDetail,
} from "@/services/documentService"
import { BookOpen, RefreshCw, AlertCircle } from "lucide-react"

type View = "list" | "detail"

interface ListState {
  docs:          DocumentListItem[]
  nextPageToken: string | null
  loading:       boolean
  loadingMore:   boolean
  error:         string | null
}

interface DetailState {
  doc:     DocumentListItem | null
  detail:  DocumentDetail | null
  loading: boolean
  error:   string | null
}

interface KnowledgeBaseSidebarProps {
  open:    boolean
  onClose: () => void
}

export function KnowledgeBaseSidebar({ open, onClose }: KnowledgeBaseSidebarProps) {
  const auth = useAuth()

  const [view, setView]     = useState<View>("list")
  const [list, setList]     = useState<ListState>({ docs: [], nextPageToken: null, loading: false, loadingMore: false, error: null })
  const [detail, setDetail] = useState<DetailState>({ doc: null, detail: null, loading: false, error: null })

  const hasFetchedRef = useRef(false)

  const credentials = useCallback(() => ({
    idToken:  auth.user?.id_token     ?? "",
    tenantId: auth.user?.profile?.sub ?? "",
  }), [auth.user])

  const fetchList = useCallback(async (pageToken?: string) => {
    const { idToken, tenantId } = credentials()
    if (!idToken || !tenantId) return

    const isFirstPage = !pageToken
    setList(prev => ({ ...prev, loading: isFirstPage, loadingMore: !isFirstPage, error: isFirstPage ? null : prev.error }))

    try {
      const result = await listDocuments(tenantId, idToken, 20, pageToken)
      setList(prev => ({
        docs:          isFirstPage ? result.documents : [...prev.docs, ...result.documents],
        nextPageToken: result.nextPageToken,
        loading:       false,
        loadingMore:   false,
        error:         null,
      }))
    } catch (err) {
      const error = err instanceof Error ? err.message : "Failed to load documents"
      setList(prev => ({ ...prev, loading: false, loadingMore: false, error }))
    }
  }, [credentials])

  useEffect(() => {
    if (!open) { hasFetchedRef.current = false; return }
    if (hasFetchedRef.current) return
    hasFetchedRef.current = true
    fetchList()
  }, [open, fetchList])

  useEffect(() => { if (!open) setView("list") }, [open])

  const openDetail = useCallback(async (doc: DocumentListItem) => {
    const { idToken, tenantId } = credentials()
    setView("detail")
    setDetail({ doc, detail: null, loading: true, error: null })

    try {
      const result = await getDocument(doc.docId, tenantId, idToken)
      setDetail(prev => ({ ...prev, detail: result, loading: false }))
    } catch (err) {
      const error = err instanceof Error ? err.message : "Failed to load document"
      setDetail(prev => ({ ...prev, loading: false, error }))
    }
  }, [credentials])

  return (
    <Sheet open={open} onOpenChange={v => { if (!v) onClose() }}>
      <SheetContent side="left" className="w-80 sm:max-w-sm p-0 flex flex-col">

        <SheetHeader className="px-4 py-3 border-b flex-none">
          <div className="flex items-center justify-between">
            <SheetTitle className="flex items-center gap-2 text-sm font-semibold text-gray-800">
              <BookOpen className="h-4 w-4 text-blue-600" />
              Knowledge Base
            </SheetTitle>
            {view === "list" && (
              <Button
                variant="ghost" size="icon"
                className="h-7 w-7 text-gray-400 hover:text-gray-600"
                onClick={() => { hasFetchedRef.current = false; fetchList() }}
                disabled={list.loading}
                title="Refresh"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${list.loading ? "animate-spin" : ""}`} />
              </Button>
            )}
          </div>
        </SheetHeader>

        <div className="flex-1 overflow-y-auto">
          {view === "detail"
            ? <DocumentDetailPanel doc={detail.doc!} detail={detail.detail} loading={detail.loading} error={detail.error} onBack={() => setView("list")} />
            : <ListView list={list} onSelect={openDetail} onLoadMore={() => fetchList(list.nextPageToken ?? undefined)} />
          }
        </div>

        {view === "list" && !list.loading && list.docs.length > 0 && (
          <div className="px-4 py-2 border-t flex-none">
            <p className="text-xs text-gray-400">
              {list.docs.length} document{list.docs.length !== 1 ? "s" : ""}
            </p>
          </div>
        )}

      </SheetContent>
    </Sheet>
  )
}

function ListView({ list, onSelect, onLoadMore }: { list: ListState; onSelect: (doc: DocumentListItem) => void; onLoadMore: () => void }) {
  return (
    <div className="px-2 py-2 space-y-0.5">

      {list.loading && Array.from({ length: 5 }, (_, i) => (
        <div key={i} className="px-3 py-2.5 flex items-start gap-2">
          <Skeleton className="h-4 w-4 rounded mt-0.5 flex-shrink-0" />
          <div className="flex-1 space-y-1.5">
            <Skeleton className="h-3.5 w-3/4 rounded" />
            <Skeleton className="h-3 w-1/2 rounded" />
          </div>
        </div>
      ))}

      {list.error && !list.loading && (
        <div className="px-3 py-4 text-center">
          <AlertCircle className="h-5 w-5 text-red-400 mx-auto mb-2" />
          <p className="text-xs text-red-600">{list.error}</p>
        </div>
      )}

      {!list.loading && !list.error && list.docs.length === 0 && (
        <div className="px-3 py-8 text-center">
          <BookOpen className="h-8 w-8 text-gray-200 mx-auto mb-3" />
          <p className="text-sm font-medium text-gray-500">No documents yet</p>
          <p className="text-xs text-gray-400 mt-1">Upload a document using the paperclip in chat.</p>
        </div>
      )}

      {!list.loading && list.docs.map(doc => (
        <DocumentCard key={doc.docId} doc={doc} isSelected={false} onSelect={onSelect} />
      ))}

      {list.nextPageToken && !list.loading && (
        <div className="px-3 pt-2 pb-3">
          <Button variant="ghost" size="sm" className="w-full text-xs text-gray-500" onClick={onLoadMore} disabled={list.loadingMore}>
            {list.loadingMore ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}

    </div>
  )
}
