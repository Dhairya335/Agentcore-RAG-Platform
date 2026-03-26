"use client"

/**
 * OrgDetailPage — Phase 3 Org-Tenancy
 *
 * INTERNAL-only: full detail for one org.
 * Route: /admin/orgs/:orgId
 *
 * Sections:
 *   - Org summary (name, type, status, ID)
 *   - Members table (userId, email, joinedAt)
 *   - Invites section: create invite form + invites table
 *
 * Invite flow:
 *   1. Admin enters email → clicks "Create Invite"
 *   2. Backend returns raw token (shown once in a copyable box)
 *   3. Admin copies/shares the link — we show the full invite URL
 *   4. Table of past invites below (consumed status, expiry)
 */

import { useCallback, useEffect, useState } from "react"
import { useNavigate, useParams } from "react-router-dom"
import { useAuth as useOidcAuth } from "react-oidc-context"
import { useIsInternal } from "@/app/context/SessionContext"
import {
  getOrgDetail,
  createInvite,
  type OrgDetail,
  type OrgInvite,
  type OrgMember,
  type CreateInviteResponse,
} from "@/services/sessionService"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  ArrowLeft, Building2, RefreshCw, Loader2,
  AlertCircle, UserPlus, Copy, CheckCheck, Mail,
  Users, Link2
} from "lucide-react"

export default function OrgDetailPage() {
  const auth       = useOidcAuth()
  const navigate   = useNavigate()
  const isInternal = useIsInternal()
  const { orgId }  = useParams<{ orgId: string }>()

  useEffect(() => {
    if (!isInternal) navigate("/", { replace: true })
  }, [isInternal, navigate])

  const [org, setOrg]         = useState<OrgDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState<string | null>(null)

  // Invite form
  const [inviteEmail, setInviteEmail]   = useState("")
  const [inviting, setInviting]         = useState(false)
  const [inviteError, setInviteError]   = useState<string | null>(null)
  const [newInvite, setNewInvite]       = useState<CreateInviteResponse | null>(null)
  const [copied, setCopied]             = useState(false)

  const idToken = auth.user?.id_token ?? ""

  const fetchDetail = useCallback(async () => {
    if (!idToken || !orgId) return
    setLoading(true)
    setError(null)
    try {
      const data = await getOrgDetail(orgId, idToken)
      setOrg(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load org")
    } finally {
      setLoading(false)
    }
  }, [idToken, orgId])

  useEffect(() => { fetchDetail() }, [fetchDetail])

  const handleCreateInvite = async () => {
    if (!inviteEmail.trim() || !orgId) return
    setInviting(true)
    setInviteError(null)
    setNewInvite(null)
    try {
      const result = await createInvite(orgId, inviteEmail.trim(), idToken)
      setNewInvite(result)
      setInviteEmail("")
      // Refresh to show invite in the table
      fetchDetail()
    } catch (err) {
      setInviteError(err instanceof Error ? err.message : "Failed to create invite")
    } finally {
      setInviting(false)
    }
  }

  const copyLink = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // fallback
    }
  }

  const formatDate = (iso: string) => {
    if (!iso) return "—"
    try { return new Date(iso).toLocaleString() } catch { return iso }
  }

  const formatShortDate = (iso: string) => {
    if (!iso) return "—"
    try { return new Date(iso).toLocaleDateString() } catch { return iso }
  }

  if (loading && !org) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <Loader2 className="h-8 w-8 animate-spin text-blue-500" />
      </div>
    )
  }

  if (error && !org) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen gap-4 px-4">
        <AlertCircle className="h-10 w-10 text-red-400" />
        <p className="text-sm text-gray-600">{error}</p>
        <Button variant="outline" onClick={() => navigate("/admin/orgs")}>
          <ArrowLeft className="h-4 w-4 mr-2" /> Back to Orgs
        </Button>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-screen bg-gray-50">

      {/* Header */}
      <header className="flex items-center justify-between px-6 py-4 border-b bg-white flex-none">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" onClick={() => navigate("/admin/orgs")} title="Back">
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <Building2 className="h-5 w-5 text-blue-600" />
          <h1 className="text-lg font-semibold text-gray-800">{org?.orgName ?? orgId}</h1>
          {org && (
            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
              org.status === "ACTIVE"
                ? "bg-green-50 text-green-700 border border-green-100"
                : "bg-gray-100 text-gray-600"
            }`}>
              {org.status}
            </span>
          )}
        </div>
        <Button
          variant="outline" size="sm"
          onClick={fetchDetail}
          disabled={loading}
          className="gap-1.5"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </header>

      <div className="flex-1 overflow-y-auto p-6 space-y-6">

        {/* Org meta */}
        {org && (
          <div className="bg-white rounded-xl border shadow-sm p-5 grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
            <div>
              <p className="text-gray-400 text-xs mb-1">Org ID</p>
              <p className="font-mono text-gray-700 text-xs truncate">{org.orgId}</p>
            </div>
            <div>
              <p className="text-gray-400 text-xs mb-1">Type</p>
              <p className="text-gray-700">{org.orgType}</p>
            </div>
            <div>
              <p className="text-gray-400 text-xs mb-1">Members</p>
              <p className="text-gray-700 font-medium">{org.members.length}</p>
            </div>
            <div>
              <p className="text-gray-400 text-xs mb-1">Created</p>
              <p className="text-gray-700">{formatShortDate(org.createdAt)}</p>
            </div>
          </div>
        )}

        {/* ── Invite section  ─────── */}
        <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
          <div className="px-5 py-4 border-b flex items-center gap-2">
            <Link2 className="h-4 w-4 text-blue-500" />
            <h2 className="text-sm font-semibold text-gray-700">Invite External User</h2>
          </div>
          <div className="p-5 space-y-4">

            {/* Create invite form */}
            <div className="flex gap-3 flex-wrap">
              <Input
                type="email"
                placeholder="user@example.com"
                value={inviteEmail}
                onChange={e => setInviteEmail(e.target.value)}
                className="flex-1 min-w-56"
                onKeyDown={e => { if (e.key === "Enter") handleCreateInvite() }}
                disabled={inviting}
              />
              <Button
                onClick={handleCreateInvite}
                disabled={inviting || !inviteEmail.trim()}
                className="gap-1.5"
              >
                {inviting
                  ? <Loader2 className="h-4 w-4 animate-spin" />
                  : <UserPlus className="h-4 w-4" />
                }
                Create Invite
              </Button>
            </div>

            {inviteError && (
              <div className="flex items-center gap-2 text-sm text-red-600">
                <AlertCircle className="h-4 w-4 flex-shrink-0" />
                {inviteError}
              </div>
            )}

            {/* New invite result — shown once */}
            {newInvite && (
              <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 space-y-3">
                <div className="flex items-center gap-2 text-sm font-medium text-blue-800">
                  <Mail className="h-4 w-4" />
                  Invite created for {newInvite.invitedEmail}
                </div>
                <p className="text-xs text-blue-600">
                  Share this link — it expires {new Date(newInvite.expiresAt).toLocaleDateString()}.
                  <strong> This is the only time you'll see it.</strong>
                </p>
                <div className="flex items-center gap-2">
                  <code className="flex-1 text-xs bg-white border border-blue-200 rounded px-3 py-2 font-mono truncate text-gray-700">
                    {newInvite.inviteUrl}
                  </code>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => copyLink(newInvite.inviteUrl)}
                    className="gap-1.5 flex-none"
                  >
                    {copied
                      ? <><CheckCheck className="h-4 w-4 text-green-500" /> Copied</>
                      : <><Copy className="h-4 w-4" /> Copy</>
                    }
                  </Button>
                </div>
                <Button variant="ghost" size="sm" className="text-blue-600" onClick={() => setNewInvite(null)}>
                  Dismiss
                </Button>
              </div>
            )}

            {/* Invites table */}
            {org && org.invites.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b bg-gray-50">
                      <th className="text-left px-3 py-2 font-medium text-gray-500">Created</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-500">Expires</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-500">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {org.invites.map((inv: OrgInvite) => (
                      <tr key={inv.inviteId} className="hover:bg-gray-50">
                        <td className="px-3 py-2 text-gray-600">{formatDate(inv.createdAt)}</td>
                        <td className="px-3 py-2 text-gray-600">{formatDate(inv.expiresAt)}</td>
                        <td className="px-3 py-2">
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                            inv.consumed
                              ? "bg-green-50 text-green-700 border border-green-100"
                              : new Date(inv.expiresAt) < new Date()
                              ? "bg-gray-100 text-gray-500"
                              : "bg-amber-50 text-amber-700 border border-amber-100"
                          }`}>
                            {inv.consumed ? "Consumed" : new Date(inv.expiresAt) < new Date() ? "Expired" : "Pending"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {org && org.invites.length === 0 && (
              <p className="text-sm text-gray-400 text-center py-4">
                No invites created yet for this org.
              </p>
            )}
          </div>
        </div>

        {/* ── Members section  ────── */}
        <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
          <div className="px-5 py-4 border-b flex items-center gap-2">
            <Users className="h-4 w-4 text-blue-500" />
            <h2 className="text-sm font-semibold text-gray-700">
              Members
              {org && <span className="text-gray-400 font-normal ml-1">({org.members.length})</span>}
            </h2>
          </div>

          {org && org.members.length === 0 && (
            <div className="p-8 text-center text-sm text-gray-400">
              No members yet. Send an invite link above.
            </div>
          )}

          {org && org.members.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 border-b">
                    <th className="text-left px-5 py-3 font-medium text-gray-600">Email</th>
                    <th className="text-left px-5 py-3 font-medium text-gray-600">Status</th>
                    <th className="text-left px-5 py-3 font-medium text-gray-600">Joined</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {org.members.map((m: OrgMember) => (
                    <tr key={m.userId} className="hover:bg-gray-50">
                      <td className="px-5 py-3 text-gray-700">{m.email || m.userId}</td>
                      <td className="px-5 py-3">
                        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                          m.membershipStatus === "ACTIVE"
                            ? "bg-green-50 text-green-700 border border-green-100"
                            : "bg-gray-100 text-gray-500"
                        }`}>
                          {m.membershipStatus}
                        </span>
                      </td>
                      <td className="px-5 py-3 text-gray-400">{formatShortDate(m.joinedAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

      </div>
    </div>
  )
}
