import '@tanstack/react-start/server-only'

import { getRequest, getRequestHeader, getResponseHeaders } from '@tanstack/react-start/server'
import type { BackroomSession, BackroomUser, OAuthAttempt } from '../lib/auth.types'
import { accessLevelForEmail } from '../lib/auth.policy'
import { sealToken, unsealToken } from './crypto.server'

const SESSION_COOKIE_SECURE = '__Host-backroom_session'
const SESSION_COOKIE_LOCAL = 'backroom_session'
const OAUTH_COOKIE_SECURE = '__Host-backroom_oauth'
const OAUTH_COOKIE_LOCAL = 'backroom_oauth'
const SESSION_MAX_AGE_SECONDS = 60 * 60 * 12
const OAUTH_MAX_AGE_SECONDS = 60 * 10

function isSecureRequest() {
  return new URL(getRequest().url).protocol === 'https:'
}

function activeCookieName(kind: 'session' | 'oauth') {
  if (kind === 'session') {
    return isSecureRequest() ? SESSION_COOKIE_SECURE : SESSION_COOKIE_LOCAL
  }
  return isSecureRequest() ? OAUTH_COOKIE_SECURE : OAUTH_COOKIE_LOCAL
}

function readCookieHeader(header: string | null | undefined, ...names: Array<string>) {
  if (!header) return null

  for (const part of header.split(/;\s*/)) {
    const separator = part.indexOf('=')
    if (separator < 0) continue
    const name = part.slice(0, separator)
    if (names.includes(name)) return part.slice(separator + 1)
  }
  return null
}

function readCookie(...names: Array<string>) {
  return readCookieHeader(getRequestHeader('cookie'), ...names)
}

function writeCookie(name: string, value: string, maxAge: number) {
  const attributes = [`${name}=${value}`, 'HttpOnly', 'SameSite=Lax', 'Path=/', `Max-Age=${maxAge}`]
  if (isSecureRequest()) attributes.push('Secure')
  getResponseHeaders().append('Set-Cookie', attributes.join('; '))
}

function validSession(value: BackroomSession | null): value is BackroomSession {
  return Boolean(
    value &&
      value.version === 2 &&
      value.uid &&
      value.email &&
      value.issuedAt > 0 &&
      value.expiresAt > Date.now() &&
      (value.accessLevel === 'read' || value.accessLevel === 'write'),
  )
}

export async function setSessionCookie(session: BackroomSession) {
  writeCookie(activeCookieName('session'), await sealToken(session), SESSION_MAX_AGE_SECONDS)
}

export function clearSessionCookie() {
  writeCookie(activeCookieName('session'), '', 0)
}

export async function readSession() {
  const token = readCookie(SESSION_COOKIE_SECURE, SESSION_COOKIE_LOCAL)
  if (!token) return null
  const session = await unsealToken<BackroomSession>(token)
  if (!validSession(session)) return null

  let accessLevel: BackroomSession['accessLevel']
  try {
    accessLevel = accessLevelForEmail(session.email, process.env.BACKROOM_ADMIN_EMAILS)
  } catch {
    clearSessionCookie()
    return null
  }
  if (accessLevel === session.accessLevel) return session
  const current = { ...session, accessLevel }
  await setSessionCookie(current)
  return current
}

export function publicUser(session: BackroomSession): BackroomUser {
  return {
    uid: session.uid,
    email: session.email,
    name: session.name,
    picture: session.picture,
    accessLevel: session.accessLevel,
  }
}

export async function setOAuthAttemptCookie(attempt: OAuthAttempt) {
  writeCookie(activeCookieName('oauth'), await sealToken(attempt), OAUTH_MAX_AGE_SECONDS)
}

export function clearOAuthAttemptCookie() {
  writeCookie(activeCookieName('oauth'), '', 0)
}

export async function readOAuthAttempt() {
  const token = readCookie(OAUTH_COOKIE_SECURE, OAUTH_COOKIE_LOCAL)
  if (!token) return null
  const attempt = await unsealToken<OAuthAttempt>(token)
  if (
    !attempt ||
    attempt.version !== 2 ||
    !attempt.state ||
    !attempt.verifier ||
    !attempt.nonce ||
    !attempt.returnTo ||
    Date.now() - attempt.createdAt > OAUTH_MAX_AGE_SECONDS * 1000
  ) {
    return null
  }
  return attempt
}

export async function readSessionFromRequest(request: Request, secret?: string) {
  const token = readCookieHeader(
    request.headers.get('cookie'),
    SESSION_COOKIE_SECURE,
    SESSION_COOKIE_LOCAL,
  )
  if (!token) return null
  const session = await unsealToken<BackroomSession>(token, secret)
  return validSession(session) ? session : null
}
