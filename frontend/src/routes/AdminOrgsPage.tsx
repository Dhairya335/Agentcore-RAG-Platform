"use client"

/**
 * AdminOrgsPage — Phase 3 Org-Tenancy
 *
 * INTERNAL-only: list all client orgs + create new org.
 * Route: /admin/orgs
 *
 * Features:
 *   - List all orgs (sorted by createdAt desc)
 *   - Create new org form (inline)
 *   - Click org row → navigate to OrgDetailPage
 */

import { useCallback, useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useAuth as useOidcAuth } from "react-oidc-context"
import { useIsInternal } from "@/app/context/SessionContext"
import { listOrgs, createOrg, type OrgSummary, type OrgType } from "@/services/sessionService"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ArrowLeft, Building2, Plus, RefreshCw, Loader2, AlertCircle } from "lucide-react"

export default function AdminOrgsPage() {
  const auth       = useOidcAuth()
  const navigate   = useNavigate()
  const isInternal = useIsInternal()

  useEffect(() => {
    if (!isInternal) navigate("/", { replace: true })
  }, [isInternal, navigate])

  const [orgs, setOrgs]       = useState<OrgSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState<string | null>(null)

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName]       = useState("")
  const [newType, setNewType]       = useState<OrgType>("CLIENT")
  const [creating, setCreating]     = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const idToken = auth.user?.id_token ?? ""

  const fetchOrgs = useCallback(async () => {
    if (!idToken) return
    setLoading(true)
    setError(null)
    try {
      const res = await listOrgs(idToken)
      setOrgs(res.orgs)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load orgs")
    } finally {
      setLoading(false)
    }
  }, [idToken])

  useEffect(() => { fetchOrgs() }, [fetchOrgs])

  const handleCreate = async () => {
    if (!newName.trim()) return
    setCreating(true)
    setCreateError(null)
    try {
      const org = await createOrg(newName.trim(), newType, idToken)
      setOrgs(prev => [org, ...prev])
      setNewName("")
      setShowCreate(false)
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Failed to create org")
    } finally {
      setCreating(false)
    }
  }

  const formatDate = (iso: string) => {
    if (!iso) return "—"
    try { return new Date(iso).toLocaleDateString() } catch { return iso }
  }

  return (
    <div className="flex flex-col h-screen bg-gray-50">

      {/* Header */}
      <header className="flex items-center justify-between px-6 py-4 border-b bg-white flex-none">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" onClick={() => navigate("/")} title="Back to chat">
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <Building2 className="h-5 w-5 text-blue-600" />
          <h1 className="text-lg font-semibold text-gray-800">Organisations</h1>
          {!loading && orgs.length > 0 && (
            <span className="text-sm text-gray-400">{orgs.length} orgs</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline" size="sm"
            onClick={fetchOrgs}
            disabled={loading}
            className="gap-1.5"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button
            size="sm"
            onClick={() => setShowCreate(v => !v)}
            className="gap-1.5"
          >
            <Plus className="h-4 w-4" />
            New Org
          </Button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-6 space-y-4">

        {/* Create org form */}
        {showCreate && (
          <div className="bg-white border rounded-xl p-5 space-y-4 shadow-sm">
            <h2 className="text-sm font-semibold text-gray-700">Create New Organisation</h2>
            <div className="flex gap-3 flex-wrap">
              <Input
                placeholder="Organisation name"
                value={newName}
                onChange={e => setNewName(e.target.value)}
                className="flex-1 min-w-48"
                onKeyDown={e => { if (e.key === "Enter") handleCreate() }}
                disabled={creating}
              />
              <select
                value={newType}
                onChange={e => setNewType(e.target.value as OrgType)}
                className="border rounded-md px-3 py-2 text-sm bg-white text-gray-700 disabled:opacity-50"
                disabled={creating}
              >
                <option value="CLIENT">Client</option>
                <option value="INTERNAL_VENDOR">Internal Vendor</option>
              </select>
              <Button onClick={handleCreate} disabled={creating || !newName.trim()} className="gap-1.5">
                {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                Create
              </Button>
              <Button variant="ghost" onClick={() => { setShowCreate(false); setCreateError(null) }} disabled={creating}>
                Cancel
              </Button>
            </div>
            {createError && (
              <div className="flex items-center gap-2 text-sm text-red-600">
                <AlertCircle className="h-4 w-4 flex-shrink-0" />
                {createError}
              </div>
            )}
          </div>
        )}

        {/* Error */}
        {error && !loading && (
          <div className="flex items-center gap-3 bg-red-50 border border-red-100 rounded-lg p-4">
            <AlertCircle className="h-5 w-5 text-red-500 flex-shrink-0" />
            <p className="text-sm text-red-700">{error}</p>
          </div>
        )}

        {/* Loading skeleton */}
        {loading && (
          <div className="space-y-2">
            {Array.from({ length: 4 }, (_, i) => (
              <div key={i} className="bg-white rounded-lg border p-4 h-14 animate-pulse" />
            ))}
          </div>
        )}

        {/* Empty */}
        {!loading && !error && orgs.length === 0 && (
          <div className="flex flex-col items-center justify-center h-64 text-center">
            <Building2 className="h-12 w-12 text-gray-200 mb-4" />
            <p className="text-base font-medium text-gray-500">No organisations yet</p>
            <p className="text-sm text-gray-400 mt-1">Create the first org using the button above.</p>
          </div>
        )}

        {/* Org list */}
        {!loading && orgs.length > 0 && (
          <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 border-b">
                  <th className="text-left px-5 py-3 font-medium text-gray-600">Name</th>
                  <th className="text-left px-5 py-3 font-medium text-gray-600">Type</th>
                  <th className="text-left px-5 py-3 font-medium text-gray-600">Status</th>
                  <th className="text-left px-5 py-3 font-medium text-gray-600">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {orgs.map(org => (
                  <tr
                    key={org.orgId}
                    className="hover:bg-blue-50 cursor-pointer transition-colors"
                    onClick={() => navigate(`/admin/orgs/${org.orgId}`)}
                  >
                    <td className="px-5 py-3 font-medium text-gray-800">{org.orgName}</td>
                    <td className="px-5 py-3 text-gray-500">{org.orgType}</td>
                    <td className="px-5 py-3">
                      <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                        org.status === "ACTIVE"
                          ? "bg-green-50 text-green-700 border border-green-100"
                          : "bg-gray-100 text-gray-600 border border-gray-200"
                      }`}>
                        {org.status}
                      </span>
                    </td>
                    <td className="px-5 py-3 text-gray-400">{formatDate(org.createdAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

      </div>
    </div>
  )
}
