"use client"

/**
 * InviteAcceptPage — Phase 3 Org-Tenancy
 *
 * Entry point for the invite URL: /signup?invite=<raw_token>
 *
 * Flow:
 *   1. Parse `invite` token from URL query param
 *   2. Store token in sessionStorage (survives Cognito redirect)
 *   3. If user is NOT authenticated → redirect to Cognito sign-in
 *   4. If user IS authenticated → proceed to InviteCompletionHandler
 *
 * The AuthProvider's onSigninCallback replaces the URL (removing ?code=),
 * which is fine because by then the token is in sessionStorage.
 *
 * Session storage key: "fast_invite_token"
 */

import { useEffect, useState } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import { useAuth as useOidcAuth } from "react-oidc-context"
import { Button } from "@/components/ui/button"
import { AlertCircle, Loader2, KeyRound } from "lucide-react"

export const INVITE_TOKEN_KEY = "fast_invite_token"

export default function InviteAcceptPage() {
  const [searchParams]  = useSearchParams()
  const auth            = useOidcAuth()
  const navigate        = useNavigate()
  const [error, setError] = useState<string | null>(null)

  const rawToken = searchParams.get("invite")

  useEffect(() => {
    if (!rawToken) {
      setError("No invite token found in the URL. Please use the link sent to you.")
      return
    }

    // Always persist token before any redirect — works even if auth callback
    // replaces the URL and wipes the query string.
    sessionStorage.setItem(INVITE_TOKEN_KEY, rawToken)

    if (auth.isLoading) return

    if (!auth.isAuthenticated) {
      // Trigger Cognito sign-in. After auth, the app lands on whatever URL
      // onSigninCallback sets (we set it to /signup in redirect_uri, or /),
      // and InviteCompletionHandler detects the token in sessionStorage.
      auth.signinRedirect({
        // Pass a state hint so we know to run completion after auth
        state: { postAuthAction: "complete-invite" },
      })
      return
    }

    // Already authenticated — go directly to completion
    navigate("/signup/complete", { replace: true })
  }, [rawToken, auth.isLoading, auth.isAuthenticated, navigate])

  if (auth.isLoading) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen gap-4">
        <Loader2 className="h-8 w-8 animate-spin text-blue-500" />
        <p className="text-sm text-gray-500">Loading…</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-gray-50 px-4">
        <div className="max-w-sm w-full bg-white rounded-xl border shadow-sm p-8 text-center space-y-4">
          <div className="flex justify-center">
            <AlertCircle className="h-10 w-10 text-red-400" />
          </div>
          <h1 className="text-lg font-semibold text-gray-800">Invalid Invite Link</h1>
          <p className="text-sm text-gray-500">{error}</p>
          <Button variant="outline" onClick={() => navigate("/")}>
            Go to Home
          </Button>
        </div>
      </div>
    )
  }

  // Signing in — brief interstitial
  return (
    <div className="flex flex-col items-center justify-center min-h-screen gap-4">
      <div className="flex justify-center">
        <div className="p-4 rounded-full bg-blue-50 border border-blue-100">
          <KeyRound className="h-10 w-10 text-blue-500" />
        </div>
      </div>
      <p className="text-lg font-medium text-gray-700">Accepting your invite…</p>
      <p className="text-sm text-gray-400">Redirecting to sign in</p>
    </div>
  )
}
