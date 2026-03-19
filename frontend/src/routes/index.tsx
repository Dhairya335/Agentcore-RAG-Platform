// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Routes, Route } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { useSession } from '@/app/context/SessionContext'
import ChatPage from './ChatPage'
import DocumentLibraryPage from './DocumentLibraryPage'
import PendingAccessPage from './PendingAccessPage'
import InviteAcceptPage from './InviteAcceptPage'
import InviteCompletePage from './InviteCompletePage'
import AdminOrgsPage from './AdminOrgsPage'
import OrgDetailPage from './OrgDetailPage'

/**
 * AppRoutes — Phase 3 Org-Tenancy
 *
 * Routing with membership-aware guards.
 *
 * Public routes (no membership check needed):
 *   /signup                    — invite accept (pre-auth OK)
 *   /signup/complete           — invite complete (post-auth, no membership yet)
 *
 * Guarded routes (require session bootstrap):
 *   /                          — chat (EXTERNAL+ACTIVE or INTERNAL+ACTIVE)
 *   /documents                 — doc library (INTERNAL+ACTIVE only, enforced in page)
 *   /admin/orgs                — org list (INTERNAL+ACTIVE only, enforced in page)
 *   /admin/orgs/:orgId         — org detail (INTERNAL+ACTIVE only, enforced in page)
 *   /pending                   — no membership → redirect here
 */
export default function AppRoutes() {
  return (
    <Routes>
      {/* Invite flow — accessible before or during auth */}
      <Route path="/signup"          element={<InviteAcceptPage />} />
      <Route path="/signup/complete" element={<SessionGuardedRoute><InviteCompletePage /></SessionGuardedRoute>} />

      {/* Pending access — for authenticated users without membership */}
      <Route path="/pending" element={<PendingAccessPage />} />

      {/* Admin routes — INTERNAL only (membership guard + role guard inside page) */}
      <Route path="/admin/orgs"         element={<SessionGuardedRoute><AdminOrgsPage /></SessionGuardedRoute>} />
      <Route path="/admin/orgs/:orgId"  element={<SessionGuardedRoute><OrgDetailPage /></SessionGuardedRoute>} />

      {/* Knowledge base — INTERNAL only (role guard inside page) */}
      <Route path="/documents" element={<SessionGuardedRoute><DocumentLibraryPage /></SessionGuardedRoute>} />

      {/* Chat — main entry point, membership-gated */}
      <Route path="/" element={<MembershipGatedRoute><ChatPage /></MembershipGatedRoute>} />
    </Routes>
  )
}

/**
 * MembershipGatedRoute — redirects EXTERNAL users with no active membership
 * to PendingAccessPage. INTERNAL users always pass.
 *
 * Waits for session bootstrap to complete before rendering children.
 */
function MembershipGatedRoute({ children }: { children: React.ReactNode }) {
  const { loading, session, error } = useSession()

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen gap-3 text-gray-500">
        <Loader2 className="h-6 w-6 animate-spin" />
        <span className="text-sm">Loading session…</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-center space-y-2">
          <p className="text-red-600 text-sm font-medium">Session error</p>
          <p className="text-gray-500 text-sm">{error}</p>
          <button
            className="text-blue-600 text-sm underline"
            onClick={() => window.location.reload()}
          >
            Retry
          </button>
        </div>
      </div>
    )
  }

  // No session yet (not authenticated) — let ChatPage handle auth redirect
  if (!session) return <>{children}</>

  // EXTERNAL user with no active membership → pending
  if (session.roleClass === 'EXTERNAL' && session.membershipStatus !== 'ACTIVE') {
    return <PendingAccessPage />
  }

  return <>{children}</>
}

/**
 * SessionGuardedRoute — only waits for session bootstrap (spinner),
 * but does not enforce membership (pages do their own INTERNAL-only check).
 * Used for routes that need auth but handle their own access control.
 */
function SessionGuardedRoute({ children }: { children: React.ReactNode }) {
  const { loading, error } = useSession()

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen gap-3 text-gray-500">
        <Loader2 className="h-6 w-6 animate-spin" />
        <span className="text-sm">Loading session…</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-center space-y-2">
          <p className="text-red-600 text-sm font-medium">Session error</p>
          <p className="text-gray-500 text-sm">{error}</p>
          <button
            className="text-blue-600 text-sm underline"
            onClick={() => window.location.reload()}
          >
            Retry
          </button>
        </div>
      </div>
    )
  }

  return <>{children}</>
}
