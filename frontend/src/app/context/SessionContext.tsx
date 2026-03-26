"use client"

/**
 * SessionContext — Phase 3 Org-Tenancy
 *
 * Membership-aware session bootstrap. Calls GET /auth/session-context once
 * after auth and exposes the result to the entire app tree.
 *
 * State machine:
 *   loading         → spinner (fetching from backend)
 *   INTERNAL+ACTIVE → full access
 *   EXTERNAL+ACTIVE → chat + upload access
 *   EXTERNAL+MISSING → PendingAccessPage
 *   error           → error screen (backend unreachable)
 */

import {
  createContext,
  useContext,
  useEffect,
  useState,
  PropsWithChildren,
} from "react"
import { useAuth as useOidcAuth } from "react-oidc-context"
import { getSessionContext, type SessionContext } from "@/services/sessionService"

// ── Types   ──────

interface SessionState {
  loading:         boolean
  session:         SessionContext | null
  error:           string | null
  /** Manually refresh session from backend (e.g. after complete-registration) */
  refreshSession:  () => Promise<void>
}

// ── Context   ────

const SessionCtx = createContext<SessionState | undefined>(undefined)

export function useSession(): SessionState {
  const ctx = useContext(SessionCtx)
  if (!ctx) throw new Error("useSession must be used within SessionBootstrapProvider")
  return ctx
}

// ── Convenience selectors    ────

/** True only for INTERNAL+ACTIVE users */
export function useIsInternal(): boolean {
  const { session } = useSession()
  return session?.roleClass === "INTERNAL" && session?.membershipStatus === "ACTIVE"
}

/** True for any authenticated user with an active membership */
export function useHasActiveMembership(): boolean {
  const { session } = useSession()
  return session?.membershipStatus === "ACTIVE"
}

/** Direct access to capabilities object */
export function useCapabilities() {
  const { session } = useSession()
  return session?.capabilities ?? {
    canChat:       false,
    canUpload:     false,
    canViewDocs:   false,
    canManageOrgs: false,
  }
}

// ── Provider   ───

export function SessionBootstrapProvider({ children }: PropsWithChildren) {
  const auth = useOidcAuth()
  const [state, setState] = useState<Omit<SessionState, "refreshSession">>({
    loading: true,
    session: null,
    error:   null,
  })

  const fetchSession = async () => {
    const idToken = auth.user?.id_token
    if (!idToken) {
      setState({ loading: false, session: null, error: null })
      return
    }

    setState(prev => ({ ...prev, loading: true, error: null }))

    try {
      const ctx = await getSessionContext(idToken)
      setState({ loading: false, session: ctx, error: null })
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to load session"
      console.error("[SessionBootstrap] error:", err)
      setState({ loading: false, session: null, error: msg })
    }
  }

  useEffect(() => {
    if (auth.isAuthenticated) {
      fetchSession()
    } else if (!auth.isLoading) {
      setState({ loading: false, session: null, error: null })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auth.isAuthenticated, auth.user?.id_token])

  const value: SessionState = {
    ...state,
    refreshSession: fetchSession,
  }

  return (
    <SessionCtx.Provider value={value}>
      {children}
    </SessionCtx.Provider>
  )
}
