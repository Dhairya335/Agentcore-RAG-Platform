"use client"

import { useCallback, useEffect, useState } from "react"
import { useAuth } from "react-oidc-context"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import {
  listCollections,
  createCollection,
  addDocumentToCollection,
  removeDocumentFromCollection,
  type Collection,
  type VisibilityMode,
  type DocumentListItem,
} from "@/services/documentService"
import { FolderOpen, Plus, X, Globe, Lock, AlertCircle, ChevronDown, ChevronRight } from "lucide-react"
import { cn } from "@/lib/utils"

const COLOR_OPTIONS = ["blue", "purple", "green", "orange", "red", "gray"] as const
type Color = typeof COLOR_OPTIONS[number]

const COLOR_CLASS: Record<Color, string> = {
  blue:   "bg-blue-100   text-blue-700",
  purple: "bg-purple-100 text-purple-700",
  green:  "bg-green-100  text-green-700",
  orange: "bg-orange-100 text-orange-700",
  red:    "bg-red-100    text-red-700",
  gray:   "bg-gray-100   text-gray-600",
}

const DOT_CLASS: Record<Color, string> = {
  blue:   "bg-blue-500",
  purple: "bg-purple-500",
  green:  "bg-green-500",
  orange: "bg-orange-500",
  red:    "bg-red-500",
  gray:   "bg-gray-400",
}

interface CollectionManagerProps {
  docs: DocumentListItem[]
}

export function CollectionManager({ docs }: CollectionManagerProps) {
  const auth = useAuth()

  const [collections, setCollections] = useState<Collection[]>([])
  const [loading, setLoading]         = useState(false)
  const [error, setError]             = useState<string | null>(null)
  const [expanded, setExpanded]       = useState(true)
  const [showForm, setShowForm]       = useState(false)

  const credentials = useCallback(() => ({
    idToken:  auth.user?.id_token     ?? "",
    tenantId: auth.user?.profile?.sub ?? "",
  }), [auth.user])

  const fetchCollections = useCallback(async () => {
    const { idToken, tenantId } = credentials()
    if (!idToken || !tenantId) return
    setLoading(true)
    setError(null)
    try {
      const result = await listCollections(tenantId, idToken)
      setCollections(result.collections)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load collections")
    } finally {
      setLoading(false)
    }
  }, [credentials])

  useEffect(() => { fetchCollections() }, [fetchCollections])

  const handleCreated = (col: Collection) => {
    setCollections(prev => [...prev, col].sort((a, b) => a.name.localeCompare(b.name)))
    setShowForm(false)
  }

  const handleAddDoc = useCallback(async (colId: string, docId: string) => {
    const { idToken, tenantId } = credentials()
    await addDocumentToCollection(colId, docId, tenantId, idToken)
    setCollections(prev => prev.map(c =>
      c.colId === colId ? { ...c, docCount: c.docCount + 1 } : c
    ))
  }, [credentials])

  const handleRemoveDoc = useCallback(async (colId: string, docId: string) => {
    const { idToken, tenantId } = credentials()
    await removeDocumentFromCollection(colId, docId, tenantId, idToken)
    setCollections(prev => prev.map(c =>
      c.colId === colId ? { ...c, docCount: Math.max(0, c.docCount - 1) } : c
    ))
  }, [credentials])

  return (
    <div className="border-t mt-1 pt-1">
      <button
        onClick={() => setExpanded(v => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-xs font-semibold text-gray-500 uppercase tracking-wide hover:text-gray-700"
      >
        {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <FolderOpen className="h-3.5 w-3.5" />
        Collections
        <span className="ml-auto font-normal normal-case text-gray-400">{collections.length}</span>
      </button>

      {expanded && (
        <div className="px-2 pb-2 space-y-0.5">
          {loading && Array.from({ length: 2 }, (_, i) => (
            <div key={i} className="px-3 py-2 flex items-center gap-2">
              <Skeleton className="h-3 w-3 rounded-full flex-shrink-0" />
              <Skeleton className="h-3.5 flex-1 rounded" />
            </div>
          ))}

          {error && !loading && (
            <div className="px-3 py-2 flex items-center gap-2 text-xs text-red-500">
              <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
              {error}
            </div>
          )}

          {!loading && collections.map(col => (
            <CollectionRow
              key={col.colId}
              collection={col}
              docs={docs}
              onAddDoc={handleAddDoc}
              onRemoveDoc={handleRemoveDoc}
            />
          ))}

          {!loading && collections.length === 0 && !error && (
            <p className="px-3 py-2 text-xs text-gray-400">No collections yet.</p>
          )}

          {showForm
            ? <CreateCollectionForm credentials={credentials} onCreated={handleCreated} onCancel={() => setShowForm(false)} />
            : (
              <button
                onClick={() => setShowForm(true)}
                className="w-full flex items-center gap-1.5 px-3 py-1.5 text-xs text-gray-400 hover:text-blue-600 rounded-md hover:bg-gray-50"
              >
                <Plus className="h-3.5 w-3.5" /> New collection
              </button>
            )
          }
        </div>
      )}
    </div>
  )
}

function CollectionRow({
  collection,
  docs,
  onAddDoc,
  onRemoveDoc,
}: {
  collection:  Collection
  docs:        DocumentListItem[]
  onAddDoc:    (colId: string, docId: string) => Promise<void>
  onRemoveDoc: (colId: string, docId: string) => Promise<void>
}) {
  const [open, setOpen]     = useState(false)
  const [busy, setBusy]     = useState<string | null>(null)
  const color = (collection.color || "blue") as Color

  const toggle = async (docId: string, inCollection: boolean) => {
    setBusy(docId)
    try {
      inCollection
        ? await onRemoveDoc(collection.colId, docId)
        : await onAddDoc(collection.colId, docId)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="rounded-md overflow-hidden">
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-sm rounded-md hover:bg-gray-50"
      >
        <span className={cn("h-2.5 w-2.5 rounded-full flex-shrink-0", DOT_CLASS[color])} />
        <span className="flex-1 text-left font-medium text-gray-700 truncate">{collection.name}</span>
        <span className="text-xs text-gray-400">{collection.docCount}</span>
        {collection.visibilityMode === "EXTERNAL_ALLOWED"
          ? <Globe className="h-3 w-3 text-green-500 flex-shrink-0" />
          : <Lock  className="h-3 w-3 text-gray-300 flex-shrink-0" />
        }
        {open ? <ChevronDown className="h-3 w-3 text-gray-400" /> : <ChevronRight className="h-3 w-3 text-gray-400" />}
      </button>

      {open && (
        <div className="px-3 pb-2 space-y-0.5">
          {docs.length === 0 && (
            <p className="text-xs text-gray-400 py-1">No documents available.</p>
          )}
          {docs.map(doc => {
            const inCol = false // TODO Phase 3.4: track membership per collection
            return (
              <div key={doc.docId} className="flex items-center gap-2 py-0.5">
                <input
                  type="checkbox"
                  checked={inCol}
                  disabled={busy === doc.docId}
                  onChange={() => toggle(doc.docId, inCol)}
                  className="h-3.5 w-3.5 rounded border-gray-300 text-blue-600 flex-shrink-0 cursor-pointer"
                />
                <span className="text-xs text-gray-700 truncate flex-1">{doc.fileName}</span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function CreateCollectionForm({
  credentials,
  onCreated,
  onCancel,
}: {
  credentials: () => { idToken: string; tenantId: string }
  onCreated:   (col: Collection) => void
  onCancel:    () => void
}) {
  const [name, setName]               = useState("")
  const [color, setColor]             = useState<Color>("blue")
  const [visibility, setVisibility]   = useState<VisibilityMode>("INTERNAL_ONLY")
  const [submitting, setSubmitting]   = useState(false)
  const [error, setError]             = useState<string | null>(null)

  const submit = async () => {
    const trimmed = name.trim()
    if (!trimmed) return
    const { idToken, tenantId } = credentials()
    setSubmitting(true)
    setError(null)
    try {
      const col = await createCollection(tenantId, trimmed, "", color, visibility, idToken)
      onCreated(col)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create collection")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="px-1 pt-1 pb-2 space-y-2">
      <Input
        value={name}
        onChange={e => setName(e.target.value)}
        placeholder="Collection name"
        className="h-7 text-xs"
        onKeyDown={e => { if (e.key === "Enter") submit(); if (e.key === "Escape") onCancel() }}
        autoFocus
      />

      <div className="flex items-center gap-1.5">
        {COLOR_OPTIONS.map(c => (
          <button
            key={c}
            onClick={() => setColor(c)}
            className={cn("h-4 w-4 rounded-full border-2 transition-transform", DOT_CLASS[c], color === c ? "border-gray-700 scale-110" : "border-transparent")}
          />
        ))}
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={() => setVisibility(v => v === "INTERNAL_ONLY" ? "EXTERNAL_ALLOWED" : "INTERNAL_ONLY")}
          className={cn("flex items-center gap-1 text-xs px-2 py-1 rounded-full border transition-colors",
            visibility === "EXTERNAL_ALLOWED"
              ? "border-green-300 text-green-700 bg-green-50"
              : "border-gray-200 text-gray-500 bg-white"
          )}
        >
          {visibility === "EXTERNAL_ALLOWED"
            ? <><Globe className="h-3 w-3" /> External</>
            : <><Lock  className="h-3 w-3" /> Internal</>
          }
        </button>
      </div>

      {error && <p className="text-xs text-red-500">{error}</p>}

      <div className="flex items-center gap-1.5">
        <Button size="sm" className="h-6 text-xs px-2" onClick={submit} disabled={!name.trim() || submitting}>
          {submitting ? "Creating…" : "Create"}
        </Button>
        <Button size="sm" variant="ghost" className="h-6 text-xs px-2" onClick={onCancel}>
          <X className="h-3 w-3" />
        </Button>
      </div>
    </div>
  )
}
