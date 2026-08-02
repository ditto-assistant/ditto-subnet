export const OMNIAURA_EMAIL_DOMAIN = '@omniaura.ai'

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
