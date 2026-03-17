"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useAuth } from "react-oidc-context"
import { useIsInternal } from "@/hooks/useUserRole"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { DocumentCard } from "@/components/knowledge/DocumentCard"
import { DocumentDetailPanel } from "@/components/knowledge/DocumentDetailPanel"
import {
  listDocuments,
  getDocument,
  type DocumentListItem,
  type DocumentDetail,
} from "@/services/documentService"
import { ArrowLeft, BookOpen, RefreshCw, AlertCircle } from "lucide-react"

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

export default function DocumentLibraryPage() {
  const auth       = useAuth()
  const navigate   = useNavigate()
  const isInternal = useIsInternal()

  useEffect(() => { if (!isInternal) navigate("/", { replace: true }) }, [isInternal, navigate])

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
      const result = await listDocuments(tenantId, idToken, 30, pageToken)
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
    if (hasFetchedRef.current) return
    hasFetchedRef.current = true
    fetchList()
  }, [fetchList])

  const openDetail = useCallback(async (doc: DocumentListItem) => {
    const { idToken, tenantId } = credentials()
    setDetail({ doc, detail: null, loading: true, error: null })

    try {
      const result = await getDocument(doc.docId, tenantId, idToken)
      setDetail(prev => ({ ...prev, detail: result, loading: false }))
    } catch (err) {
      const error = err instanceof Error ? err.message : "Failed to load document"
      setDetail(prev => ({ ...prev, loading: false, error }))
    }
  }, [credentials])

  const closeDetail = () => setDetail({ doc: null, detail: null, loading: false, error: null })

  return (
    <div className="flex flex-col h-screen bg-gray-50">

      <header className="flex items-center justify-between px-6 py-4 border-b bg-white flex-none">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" onClick={() => navigate("/")} title="Back to chat">
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <BookOpen className="h-5 w-5 text-blue-600" />
          <h1 className="text-lg font-semibold text-gray-800">Knowledge Base</h1>
          {!list.loading && list.docs.length > 0 && (
            <span className="text-sm text-gray-400">{list.docs.length} documents</span>
          )}
        </div>
        <Button
          variant="outline" size="sm"
          onClick={() => { hasFetchedRef.current = false; fetchList() }}
          disabled={list.loading}
          className="gap-1.5"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${list.loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </header>

      <div className="flex flex-1 overflow-hidden">

        <div className="flex-1 overflow-y-auto p-6">

          {list.error && !list.loading && (
            <div className="flex items-center gap-3 bg-red-50 border border-red-100 rounded-lg p-4 mb-4">
              <AlertCircle className="h-5 w-5 text-red-500 flex-shrink-0" />
              <p className="text-sm text-red-700">{list.error}</p>
            </div>
          )}

          {list.loading && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {Array.from({ length: 9 }, (_, i) => (
                <div key={i} className="bg-white rounded-lg border p-4 space-y-2">
                  <div className="flex items-start gap-2">
                    <Skeleton className="h-4 w-4 rounded flex-shrink-0 mt-0.5" />
                    <div className="flex-1 space-y-1.5">
                      <Skeleton className="h-3.5 w-3/4 rounded" />
                      <Skeleton className="h-3 w-1/2 rounded" />
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {!list.loading && !list.error && list.docs.length === 0 && (
            <div className="flex flex-col items-center justify-center h-64 text-center">
              <BookOpen className="h-12 w-12 text-gray-200 mb-4" />
              <p className="text-base font-medium text-gray-500">No documents yet</p>
              <p className="text-sm text-gray-400 mt-1">Upload documents using the paperclip button in the chat.</p>
            </div>
          )}

          {!list.loading && list.docs.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {list.docs.map(doc => (
                <div key={doc.docId} className="bg-white rounded-lg border hover:border-blue-300 transition-colors">
                  <DocumentCard doc={doc} isSelected={detail.doc?.docId === doc.docId} onSelect={openDetail} />
                </div>
              ))}
            </div>
          )}

          {list.nextPageToken && !list.loading && (
            <div className="mt-6 text-center">
              <Button
                variant="outline" size="sm"
                onClick={() => fetchList(list.nextPageToken ?? undefined)}
                disabled={list.loadingMore}
              >
                {list.loadingMore ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}
        </div>

        {detail.doc && (
          <div className="w-80 border-l bg-white flex-none overflow-y-auto">
            <DocumentDetailPanel doc={detail.doc} detail={detail.detail} loading={detail.loading} error={detail.error} onBack={closeDetail} />
          </div>
        )}

      </div>
    </div>
  )
}
