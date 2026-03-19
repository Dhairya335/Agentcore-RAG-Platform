"use client"

/**
 * InviteCompletePage — Phase 3 Org-Tenancy
 *
 * Shown at /signup/complete after the user authenticates via Cognito.
 *
 * Flow:
 *   1. Read invite token from sessionStorage (set by InviteAcceptPage)
 *   2. Call POST /auth/complete-external-registration with token + idToken
 *   3. Clear sessionStorage token
 *   4. Refresh OIDC session (silentRenew) so new group claims are picked up
 *   5. Refresh session context (SessionBootstrapProvider.refreshSession)
 *   6. Redirect to / (chat page)
 *
 * Error cases:
 *   - No token in sessionStorage → show "link expired" screen
 *   - Backend returns 4xx → show error + link to try again
 *   - Network error → show retry button
 */

import { useEffect, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useAuth as useOidcAuth } from "react-oidc-context"
import { useSession } from "@/app/context/SessionContext"
import { completeExternalRegistration } from "@/services/sessionService"
import { INVITE_TOKEN_KEY } from "./InviteAcceptPage"
import { Button } from "@/components/ui/button"
import { Loader2, CheckCircle2, AlertCircle, AlertTriangle } from "lucide-react"

type Phase = "completing" | "success" | "error"

export default function InviteCompletePage() {
  const auth            = useOidcAuth()
  const { refreshSession } = useSession()
  const navigate        = useNavigate()

  const [phase, setPhase]       = useState<Phase>("completing")
  const [orgName, setOrgName]   = useState<string>("")
  const [errMsg, setErrMsg]     = useState<string>("")
  /** True when registration succeeded but silent token refresh failed.
   *  User should sign out and sign back in to pick up the new group claim. */
  const [renewFailed, setRenewFailed] = useState<boolean>(false)
  const hasRunRef                     = useRef(false)

  useEffect(() => {
    // Only run once
    if (hasRunRef.current) return
    if (auth.isLoading) return
    hasRunRef.current = true

    const complete = async () => {
      const idToken    = auth.user?.id_token ?? ""
      const rawToken   = sessionStorage.getItem(INVITE_TOKEN_KEY) ?? ""

      if (!rawToken) {
        setErrMsg(
          "No invite token found. The invite link may have expired or already been used. " +
          "Please contact your administrator for a new invite."
        )
        setPhase("error")
        return
      }

      if (!idToken) {
        setErrMsg("Not signed in. Please sign in first.")
        setPhase("error")
        return
      }

      try {
        const result = await completeExternalRegistration(rawToken, idToken)
        sessionStorage.removeItem(INVITE_TOKEN_KEY)
        setOrgName(result.orgName)

        // Refresh OIDC tokens so the new 'external' Cognito group claim is included.
        // If this fails the membership record is still written — the user just needs
        // to sign out and back in for the new group to appear in their JWT.
        // Use a local variable (not state) to drive the redirect decision below,
        // since state updates are async and the closure would capture stale values.
        let tokenRenewFailed = false
        try {
          await auth.signinSilent()
        } catch (renewErr) {
          console.warn("[InviteComplete] silent renew failed — user must re-sign-in for group claim:", renewErr)
          tokenRenewFailed = true
          setRenewFailed(true)
        }

        // Refresh session context (picks up ACTIVE membership from backend)
        await refreshSession()

        setPhase("success")
        // Only auto-redirect if token refresh succeeded — if it failed the user
        // must manually sign out and sign back in (shown in the success UI).
        if (!tokenRenewFailed) {
          setTimeout(() => navigate("/", { replace: true }), 1800)
        }
      } catch (err) {
        sessionStorage.removeItem(INVITE_TOKEN_KEY)
        const msg = err instanceof Error ? err.message : "Registration failed"
        setErrMsg(msg)
        setPhase("error")
      }
    }

    complete()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auth.isLoading, auth.user?.id_token])

  // ── Completing ───────────────────────────────────────────────────────────────
  if (phase === "completing" || auth.isLoading) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen gap-4">
        <Loader2 className="h-10 w-10 animate-spin text-blue-500" />
        <p className="text-base font-medium text-gray-700">Activating your account…</p>
        <p className="text-sm text-gray-400">This only takes a moment</p>
      </div>
    )
  }

  // ── Success ──────────────────────────────────────────────────────────────────
  if (phase === "success") {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-gray-50 px-4">
        <div className="max-w-sm w-full bg-white rounded-xl border shadow-sm p-8 text-center space-y-4">
          <div className="flex justify-center">
            <CheckCircle2 className="h-12 w-12 text-green-500" />
          </div>
          <h1 className="text-xl font-semibold text-gray-900">You're all set!</h1>
          {orgName && (
            <p className="text-sm text-gray-500">
              You've been added to <strong>{orgName}</strong>.
            </p>
          )}
          {renewFailed ? (
            // Silent token refresh failed — membership is written but JWT lacks new group claim.
            // User must sign out and back in to get the external group in their token.
            <div className="flex items-start gap-2 rounded-lg bg-amber-50 border border-amber-100 p-3 text-left">
              <AlertTriangle className="h-4 w-4 text-amber-500 mt-0.5 flex-shrink-0" />
              <p className="text-xs text-amber-700">
                Your account is active, but your session couldn't be refreshed automatically.
                Please <strong>sign out and sign back in</strong> to access the app.
              </p>
            </div>
          ) : (
            <p className="text-sm text-gray-400">Redirecting to the app…</p>
          )}
        </div>
      </div>
    )
  }

  // ── Error ────────────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-gray-50 px-4">
      <div className="max-w-sm w-full bg-white rounded-xl border shadow-sm p-8 text-center space-y-4">
        <div className="flex justify-center">
          <AlertCircle className="h-10 w-10 text-red-400" />
        </div>
        <h1 className="text-lg font-semibold text-gray-800">Registration Failed</h1>
        <p className="text-sm text-gray-500">{errMsg}</p>
        <div className="flex flex-col gap-2">
          <Button onClick={() => navigate("/")}>
            Go to Home
          </Button>
        </div>
      </div>
    </div>
  )
}
