/**
 * sessionService.ts — Phase 3 Org-Tenancy
 *
 * API client for:
 *   GET  /auth/session-context              — membership-aware session bootstrap
 *   POST /admin/orgs                        — create org (INTERNAL only)
 *   GET  /admin/orgs                        — list orgs  (INTERNAL only)
 *   GET  /admin/orgs/{orgId}                — org detail (INTERNAL only)
 *   POST /admin/orgs/{orgId}/invites        — create invite (INTERNAL only)
 *
 * The org API URL is loaded from /aws-exports.json (orgApiUrl field).
 */

let ORG_API_BASE = ""

async function loadOrgApiBase(): Promise<string> {
  if (ORG_API_BASE) return ORG_API_BASE

  const response = await fetch("/aws-exports.json")
  const config   = await response.json()
  if (!config.orgApiUrl) throw new Error("orgApiUrl not found in aws-exports.json")

  ORG_API_BASE = config.orgApiUrl.endsWith("/")
    ? config.orgApiUrl
    : `${config.orgApiUrl}/`
  return ORG_API_BASE
}

// ── Session Context ────────────────────────────────────────────────────────────

export type RoleClass        = "INTERNAL" | "EXTERNAL"
export type MembershipStatus = "ACTIVE" | "MISSING"

export interface SessionCapabilities {
  canChat:       boolean
  canUpload:     boolean
  canViewDocs:   boolean
  canManageOrgs: boolean
}

export interface SessionContext {
  userId:           string
  email:            string
  roleClass:        RoleClass
  membershipStatus: MembershipStatus
  orgId:            string | null
  orgName:          string | null
  capabilities:     SessionCapabilities
}

/**
 * Fetch authoritative session context from the backend.
 * Called once after auth, before any route rendering.
 * Returns null if the call fails (caller should treat as MISSING).
 */
export async function getSessionContext(idToken: string): Promise<SessionContext> {
  const base = await loadOrgApiBase()
  const resp = await fetch(`${base}auth/session-context`, {
    headers: { Authorization: `Bearer ${idToken}` },
  })

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err.error || `Session context failed: HTTP ${resp.status}`)
  }

  return resp.json() as Promise<SessionContext>
}

// ── Org Management ─────────────────────────────────────────────────────────────

export type OrgType = "CLIENT" | "INTERNAL_VENDOR" | "PERSONAL_FUTURE"

export interface OrgSummary {
  orgId:     string
  orgName:   string
  orgType:   OrgType
  status:    string
  createdAt: string
}

export interface OrgMember {
  userId:           string
  email:            string
  membershipStatus: string
  joinedAt:         string
}

export interface OrgInvite {
  inviteId:  string
  createdAt: string
  expiresAt: string
  consumed:  boolean
  createdBy: string
}

export interface OrgDetail extends OrgSummary {
  members: OrgMember[]
  invites: OrgInvite[]
}

export interface OrgListResponse {
  orgs:  OrgSummary[]
  count: number
}

/**
 * List all orgs. INTERNAL only.
 */
export async function listOrgs(idToken: string): Promise<OrgListResponse> {
  const base = await loadOrgApiBase()
  const resp = await fetch(`${base}admin/orgs`, {
    headers: { Authorization: `Bearer ${idToken}` },
  })

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err.error || `List orgs failed: HTTP ${resp.status}`)
  }

  return resp.json() as Promise<OrgListResponse>
}

/**
 * Get full org detail including members and invites. INTERNAL only.
 */
export async function getOrgDetail(orgId: string, idToken: string): Promise<OrgDetail> {
  const base = await loadOrgApiBase()
  const resp = await fetch(`${base}admin/orgs/${encodeURIComponent(orgId)}`, {
    headers: { Authorization: `Bearer ${idToken}` },
  })

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err.error || `Get org detail failed: HTTP ${resp.status}`)
  }

  return resp.json() as Promise<OrgDetail>
}

/**
 * Create a new client org. INTERNAL only.
 */
export async function createOrg(
  orgName: string,
  orgType: OrgType,
  idToken: string,
): Promise<OrgSummary> {
  const base = await loadOrgApiBase()
  const resp = await fetch(`${base}admin/orgs`, {
    method:  "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${idToken}` },
    body:    JSON.stringify({ orgName, orgType }),
  })

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err.error || `Create org failed: HTTP ${resp.status}`)
  }

  return resp.json() as Promise<OrgSummary>
}

// ── Invite Management ──────────────────────────────────────────────────────────

export interface CreateInviteRequest {
  invitedEmail: string
}

export interface CreateInviteResponse {
  inviteId:     string
  inviteUrl:    string
  inviteToken:  string   // returned ONCE — admin must copy/email
  expiresAt:    string
  orgId:        string
  invitedEmail: string
}

/**
 * Create an invite for an external user to join an org.
 * INTERNAL only. Returns raw token once.
 */
export async function createInvite(
  orgId:        string,
  invitedEmail: string,
  idToken:      string,
): Promise<CreateInviteResponse> {
  const base = await loadOrgApiBase()
  const resp = await fetch(`${base}admin/orgs/${encodeURIComponent(orgId)}/invites`, {
    method:  "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${idToken}` },
    body:    JSON.stringify({ invitedEmail }),
  })

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err.error || `Create invite failed: HTTP ${resp.status}`)
  }

  return resp.json() as Promise<CreateInviteResponse>
}

// ── Registration ───────────────────────────────────────────────────────────────

export interface CompleteRegistrationResponse {
  orgId:   string
  orgName: string
  userId:  string
  email:   string
}

/**
 * Complete external user registration by consuming an invite token.
 * Called after the user authenticates via Cognito.
 */
export async function completeExternalRegistration(
  inviteToken: string,
  idToken:     string,
): Promise<CompleteRegistrationResponse> {
  const base = await loadOrgApiBase()
  const resp = await fetch(`${base}auth/complete-external-registration`, {
    method:  "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${idToken}` },
    body:    JSON.stringify({ inviteToken }),
  })

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err.error || `Registration failed: HTTP ${resp.status}`)
  }

  return resp.json() as Promise<CompleteRegistrationResponse>
}
