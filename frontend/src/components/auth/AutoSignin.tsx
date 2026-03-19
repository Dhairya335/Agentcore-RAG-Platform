"use client"

import { ReactNode, useEffect, useState, PropsWithChildren } from "react"
import { useAuth } from "react-oidc-context"
import { useLocation } from "react-router-dom"
import { Button } from "@/components/ui/button"

/**
 * Public paths that should render without requiring authentication.
 * The invite acceptance page must be accessible before Cognito sign-in
 * because it reads the token from the URL, stores it, then redirects.
 */
const PUBLIC_PATHS = ["/signup", "/signup/complete"]

function AutoSigninContent({ children }: PropsWithChildren) {
  const auth     = useAuth()
  const location = useLocation()

  const isPublicPath = PUBLIC_PATHS.some(
    p => location.pathname === p || location.pathname.startsWith(p + "/")
  )

  if (auth.isLoading) {
    return <div className="flex items-center justify-center min-h-screen text-xl">Loading...</div>
  }

  // Invite / registration flow — pass through without forcing auth
  if (isPublicPath) {
    return <>{children}</>
  }

  if (!auth.isAuthenticated) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen gap-4">
        <p className="text-4xl">Please sign in</p>
        <Button onClick={() => auth.signinRedirect()}>Sign In</Button>
      </div>
    )
  }

  return <>{children}</>
}

export function AutoSignin({ children }: { children: ReactNode }) {
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    setMounted(true)
  }, [])

  if (!mounted) {
    return null
  }

  return <AutoSigninContent>{children}</AutoSigninContent>
}
