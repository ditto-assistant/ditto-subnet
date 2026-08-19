export const OMNIAURA_EMAIL_DOMAIN = '@omniaura.ai'

// Staff sessions are fixed-lifetime: no silent renew. Write access is still
// re-derived from BACKROOM_ADMIN_EMAILS on every request. This bound is the
// remaining read-access window after a Workspace account is disabled, and it
// matches the MCP refresh-token TTL so console and agent grants die together.
export const SESSION_LIFETIME_MS = 7 * 24 * 60 * 60 * 1000
export const SESSION_MAX_AGE_SECONDS = SESSION_LIFETIME_MS / 1000

export function isVerifiedOmniauraEmail(email: string, verified: boolean) {
  return verified && email.trim().toLowerCase().endsWith(OMNIAURA_EMAIL_DOMAIN)
}

export function parseAdminEmails(value: string | undefined) {
  return new Set(
    (value ?? '')
      .split(',')
      .map((email) => email.trim().toLowerCase())
      .filter((email) => isVerifiedOmniauraEmail(email, true)),
  )
}

export function accessLevelForEmail(email: string, configuredAdmins: string | undefined) {
  const normalized = email.trim().toLowerCase()
  if (!isVerifiedOmniauraEmail(normalized, true)) {
    throw new Error('This account is not authorized to enter Backroom')
  }
  return parseAdminEmails(configuredAdmins).has(normalized) ? 'write' : 'read'
}

export function isSameOriginRequest(request: Request) {
  const origin = request.headers.get('origin')
  if (!origin) return false
  try {
    return new URL(origin).origin === new URL(request.url).origin
  } catch {
    return false
  }
}

export function safeReturnTo(value: string | undefined) {
  if (!value || !value.startsWith('/') || value.startsWith('//')) {
    return '/screener-capacity'
  }
  try {
    const url = new URL(value, 'https://backroom.invalid')
    if (url.origin !== 'https://backroom.invalid') return '/screener-capacity'
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return '/screener-capacity'
  }
}
