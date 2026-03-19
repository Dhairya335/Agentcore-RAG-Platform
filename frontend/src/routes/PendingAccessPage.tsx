"use client"

/**
 * PendingAccessPage — Phase 3 Org-Tenancy
 *
 * Shown to authenticated EXTERNAL users who have no active org membership.
 * This happens when:
 *   - A user signed up via Cognito but hasn't consumed an invite yet
 *   - An invite was never sent to them
 *
 * From here the user can:
 *   1. Sign out and try a different account
 *   2. Contact their administrator to receive an invite link
 *
 * No upload/chat entry points are rendered here.
 */

import { useAuth } from "@/hooks/useAuth"
import { Button } from "@/components/ui/button"
import { Mail, LogOut, Clock } from "lucide-react"

export default function PendingAccessPage() {
  const { signOut, user } = useAuth()
  const email = (user?.profile?.email as string | undefined) ?? ""

  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-gray-50 px-4">
      <div className="max-w-md w-full bg-white rounded-xl border shadow-sm p-8 text-center space-y-6">

        {/* Icon */}
        <div className="flex justify-center">
          <div className="p-4 rounded-full bg-amber-50 border border-amber-100">
            <Clock className="h-10 w-10 text-amber-500" />
          </div>
        </div>

        {/* Heading */}
        <div className="space-y-2">
          <h1 className="text-2xl font-semibold text-gray-900">Access Pending</h1>
          <p className="text-sm text-gray-500">
            Your account{email ? ` (${email})` : ""} has been created but isn't linked
            to an organisation yet.
          </p>
        </div>

        {/* Instructions */}
        <div className="bg-gray-50 rounded-lg p-4 text-left space-y-3">
          <p className="text-sm font-medium text-gray-700">To gain access:</p>
          <ol className="text-sm text-gray-600 space-y-2 list-decimal list-inside">
            <li>Contact your administrator and ask them to send you an invite link.</li>
            <li>Open the invite link — it will automatically link your account.</li>
            <li>Return here and sign in again.</li>
          </ol>
        </div>

        {/* Contact info hint */}
        <div className="flex items-center gap-2 text-sm text-gray-500 justify-center">
          <Mail className="h-4 w-4 flex-shrink-0" />
          <span>Your administrator generates invite links from the admin panel.</span>
        </div>

        {/* Sign out */}
        <Button
          variant="outline"
          className="w-full gap-2"
          onClick={() => signOut()}
        >
          <LogOut className="h-4 w-4" />
          Sign Out
        </Button>

      </div>
    </div>
  )
}
