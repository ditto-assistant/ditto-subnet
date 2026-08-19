import '@tanstack/react-start/server-only'

import { getRequest } from '@tanstack/react-start/server'
import { createRemoteJWKSet, jwtVerify } from 'jose'
import type { BackroomSession, OAuthAttempt } from '../lib/auth.types'
import {
  accessLevelForEmail,
  isVerifiedOmniauraEmail,
  safeReturnTo,
  SESSION_LIFETIME_MS,
} from '../lib/auth.policy'
import { constantTimeEqual, randomToken } from './crypto.server'
import {
  clearOAuthAttemptCookie,
  readOAuthAttempt,
  setOAuthAttemptCookie,
  setSessionCookie,
} from './session.server'

const GOOGLE_AUTHORIZE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
const GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
const GOOGLE_JWKS = createRemoteJWKSet(new URL('https://www.googleapis.com/oauth2/v3/certs'))

function bytesToBase64Url(bytes: Uint8Array) {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_')
}

async function pkceChallenge(verifier: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return bytesToBase64Url(new Uint8Array(digest))
}

function googleClientId() {
  const value = process.env.GOOGLE_CLIENT_ID
  if (!value) throw new Error('GOOGLE_CLIENT_ID is not configured')
  return value
}

function googleClientSecret() {
  const value = process.env.GOOGLE_CLIENT_SECRET
  if (!value) throw new Error('GOOGLE_CLIENT_SECRET is not configured')
  return value
}

function redirectUri() {
  return process.env.GOOGLE_REDIRECT_URI ?? new URL('/auth/callback', getRequest().url).toString()
}

function oauthError(payload: unknown, fallback: string) {
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    if (typeof record.error_description === 'string') {
      return record.error_description
    }
    if (typeof record.error === 'string') return record.error
    if (record.error && typeof record.error === 'object') {
      const nested = record.error as Record<string, unknown>
      if (typeof nested.message === 'string') return nested.message
    }
  }
  return fallback
}

export async function createGoogleAuthorizationUrl(returnTo?: string) {
  const attempt: OAuthAttempt = {
    version: 2,
    state: randomToken(),
    verifier: randomToken(48),
    nonce: randomToken(),
    createdAt: Date.now(),
    returnTo: safeReturnTo(returnTo),
  }
  await setOAuthAttemptCookie(attempt)

  const query = new URLSearchParams({
    client_id: googleClientId(),
    redirect_uri: redirectUri(),
    response_type: 'code',
    scope: 'openid email profile',
    state: attempt.state,
    nonce: attempt.nonce,
    code_challenge: await pkceChallenge(attempt.verifier),
    code_challenge_method: 'S256',
    hd: 'omniaura.ai',
    prompt: 'select_account',
  })
  return `${GOOGLE_AUTHORIZE_URL}?${query.toString()}`
}

async function exchangeGoogleCode(code: string, attempt: OAuthAttempt) {
  const response = await fetch(GOOGLE_TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      code,
      client_id: googleClientId(),
      client_secret: googleClientSecret(),
      redirect_uri: redirectUri(),
      grant_type: 'authorization_code',
      code_verifier: attempt.verifier,
    }),
  })
  const payload = (await response.json()) as {
    id_token?: string
    error?: string
    error_description?: string
  }
  if (!response.ok || !payload.id_token) {
    throw new Error(oauthError(payload, 'Google OAuth exchange failed'))
  }
  return payload.id_token
}

async function verifyGoogleIdentity(idToken: string, attempt: OAuthAttempt) {
  let payload
  try {
    ;({ payload } = await jwtVerify(idToken, GOOGLE_JWKS, {
      algorithms: ['RS256'],
      audience: googleClientId(),
      issuer: ['accounts.google.com', 'https://accounts.google.com'],
      clockTolerance: 5,
      maxTokenAge: '1h',
    }))
  } catch {
    throw new Error('Google identity verification failed')
  }
  if (payload.nonce !== attempt.nonce || !payload.sub || !payload.iat) {
    throw new Error('Google identity verification failed')
  }
  const email = payload.email?.trim().toLowerCase() ?? ''
  if (
    payload.hd !== 'omniaura.ai' ||
    !isVerifiedOmniauraEmail(email, payload.email_verified === true)
  ) {
    throw new Error('This account is not authorized to enter Backroom')
  }
  return {
    uid: payload.sub,
    email,
    name: payload.name?.trim() ?? '',
    picture: payload.picture ?? '',
  }
}

async function createSession(identity: {
  uid: string
  email: string
  name: string
  picture: string
}) {
  const now = Date.now()
  const session: BackroomSession = {
    version: 2,
    ...identity,
    name: identity.name || identity.email,
    accessLevel: accessLevelForEmail(identity.email, process.env.BACKROOM_ADMIN_EMAILS),
    issuedAt: now,
    expiresAt: now + SESSION_LIFETIME_MS,
  }
  await setSessionCookie(session)
}

export async function completeGoogleOAuth(code: string, state: string) {
  try {
    const attempt = await readOAuthAttempt()
    if (!attempt || !constantTimeEqual(attempt.state, state)) {
      throw new Error('This sign-in attempt expired. Please start again.')
    }
    const googleIdToken = await exchangeGoogleCode(code, attempt)
    const identity = await verifyGoogleIdentity(googleIdToken, attempt)
    await createSession(identity)
    return attempt.returnTo
  } finally {
    clearOAuthAttemptCookie()
  }
}
