import '@tanstack/react-start/server-only'

import type { BackroomSession } from '../lib/auth.types'

const encoder = new TextEncoder()

function base64url(bytes: Uint8Array): string {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_')
}

function hex(bytes: Uint8Array): string {
  return [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

/** Bound one short-lived assertion to the live, Google-authenticated MCP session. */
export async function signV13BenignAttestation(
  session: BackroomSession,
  approvalId: string,
  evidenceSha256: string,
  now = Math.floor(Date.now() / 1000),
): Promise<string> {
  const secret = process.env.DITTO_V13_BENIGN_ATTESTATION_SECRET
  if (!secret || secret.length < 32) throw new Error('V13 human attestations are disabled')
  if (
    session.accessLevel !== 'write' || session.uid === 'sn118-preview' ||
    !session.uid || !session.email || session.expiresAt <= Date.now()
  ) throw new Error('An authenticated Backroom write session is required')
  const nonce = hex(crypto.getRandomValues(new Uint8Array(16)))
  const claims = {
    aud: 'ditto-platform-v13-benign-approval',
    action: 'attest-known-benign',
    approval_id: approvalId,
    evidence_sha256: evidenceSha256,
    sub: session.uid,
    email: session.email.toLowerCase(),
    iat: now,
    nonce,
  }
  const unsigned = `v1.${base64url(encoder.encode(JSON.stringify(claims)))}`
  const key = await crypto.subtle.importKey(
    'raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  )
  const signature = new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(unsigned)))
  return `${unsigned}.${hex(signature)}`
}
