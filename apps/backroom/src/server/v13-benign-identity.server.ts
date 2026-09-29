import '@tanstack/react-start/server-only'
import { createHmac, randomBytes } from 'node:crypto'
import type { BackroomSession } from '../lib/auth.types'
import { accessLevelForEmail } from '../lib/auth.policy'

/** Issued only from an authenticated staff session, never from tool input. */
export function mintV13BenignAssertion(
  session: BackroomSession,
  approval: { approval_id: string; review_evidence_sha256: string },
  action: 'attest-known-benign' | 'authorize-generation',
) {
  if (
    session.accessLevel !== 'write' ||
    session.expiresAt <= Date.now() ||
    session.uid === 'sn118-preview' ||
    !/^[A-Za-z0-9:_-]{1,120}$/.test(session.uid) ||
    accessLevelForEmail(
      session.email,
      process.env.BACKROOM_ADMIN_EMAILS,
      process.env.BACKROOM_BLOCKED_EMAILS,
    ) !== 'write'
  )
    throw new Error('Authenticated production write access is required')
  const secret = process.env.DITTO_V13_BENIGN_ATTESTATION_SECRET
  if (!secret || secret.length < 32)
    throw new Error('V13 identity issuer is not configured')
  const claims = {
    aud: 'ditto-platform-v13-benign-approval',
    action,
    approval_id: approval.approval_id,
    evidence_sha256: approval.review_evidence_sha256,
    sub: session.uid,
    email: session.email.trim().toLowerCase(),
    iat: Math.floor(Date.now() / 1000),
    nonce: randomBytes(16).toString('hex'),
  }
  const signed = `v1.${Buffer.from(JSON.stringify(claims)).toString('base64url')}`
  return `${signed}.${createHmac('sha256', secret).update(signed).digest('hex')}`
}
