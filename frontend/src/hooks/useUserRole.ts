/**
 * useUserRole — RBAC role hook (Phase 3)
 *
 * Reads the user's Cognito group membership from the decoded ID token
 * and returns a normalised role string.
 *
 * Role model:
 *   INTERNAL — Company A (vendor/system owner). Sees document library,
 *              collections, chunk previews, source viewer, debug panels.
 *   EXTERNAL — Company B (client). Chat-only interface.
 *
 * Source of truth: Cognito Groups encoded in the ID token claim
 *   "cognito:groups": ["internal"]  → INTERNAL
 *   "cognito:groups": ["external"]  → EXTERNAL
 *   missing / empty                 → EXTERNAL (fail-safe default)
 *
 * Usage:
 *   const role = useUserRole()
 *   if (role === 'INTERNAL') { ... show document library ... }
 *
 * NOTE: This hook is for conditional UI rendering only.
 * Backend enforcement happens at the Lambda and Aurora SQL layers.
 * Never use this hook as the sole access gate for sensitive operations.
 */

import { useMemo } from 'react'
import { useAuth as useOidcAuth } from 'react-oidc-context'

export type UserRole = 'INTERNAL' | 'EXTERNAL'

const INTERNAL_GROUP = 'internal'

/**
 * Decode a JWT string and return its payload as a plain object.
 * Does NOT verify the signature — verification is done server-side by
 * Cognito (ID token) and AgentCore Runtime / API Gateway Authorizer.
 */
function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const parts = token.split('.')
    if (parts.length !== 3) return {}
    // Base64url → Base64 → decode
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const json = atob(base64)
    return JSON.parse(json) as Record<string, unknown>
  } catch {
    return {}
  }
}

export function useUserRole(): UserRole {
  const auth = useOidcAuth()

  const role = useMemo<UserRole>(() => {
    // Not authenticated → treat as EXTERNAL (safest default)
    if (!auth?.isAuthenticated || !auth.user) {
      return 'EXTERNAL'
    }

    // Prefer the profile claim if react-oidc-context has already parsed it.
    // The 'cognito:groups' claim may be in the ID token payload.
    const profile = auth.user.profile as Record<string, unknown> | undefined

    // Try profile first (parsed by oidc-client-ts from ID token)
    let groups: string[] = []
    if (profile) {
      const rawGroups = profile['cognito:groups']
      if (Array.isArray(rawGroups)) {
        groups = rawGroups as string[]
      }
    }

    // If not found in profile, decode ID token directly.
    // This handles cases where oidc-client-ts strips non-standard claims.
    if (groups.length === 0 && auth.user.id_token) {
      const claims = decodeJwtPayload(auth.user.id_token)
      const rawGroups = claims['cognito:groups']
      if (Array.isArray(rawGroups)) {
        groups = rawGroups as string[]
      }
    }

    return groups.includes(INTERNAL_GROUP) ? 'INTERNAL' : 'EXTERNAL'
  }, [auth?.isAuthenticated, auth?.user])

  return role
}

/**
 * Convenience guard — returns true only for INTERNAL users.
 * Use to conditionally render internal-only UI elements.
 *
 * Example:
 *   const isInternal = useIsInternal()
 *   {isInternal && <KnowledgeBaseSidebar />}
 */
export function useIsInternal(): boolean {
  return useUserRole() === 'INTERNAL'
}
