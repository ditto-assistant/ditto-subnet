import { createHmac } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { BackroomSession } from '../lib/auth.types'
import { mintV13BenignAssertion } from './v13-benign-identity.server'

const session: BackroomSession = {
  version: 2,
  uid: 'google-subject-1',
  email: 'reviewer@omniaura.ai',
  name: 'Reviewer',
  picture: '',
  accessLevel: 'write',
  issuedAt: Date.now(),
  expiresAt: Date.now() + 60_000,
}
const approval = {
  approval_id: '11111111-1111-4111-8111-111111111111',
  review_evidence_sha256: 'a'.repeat(64),
}

beforeEach(() => {
  vi.stubEnv('DITTO_V13_BENIGN_ATTESTATION_SECRET', 's'.repeat(48))
  vi.stubEnv('BACKROOM_ADMIN_EMAILS', session.email)
  vi.stubEnv('BACKROOM_BLOCKED_EMAILS', '')
})
afterEach(() => vi.unstubAllEnvs())

describe('V13 authenticated identity issuer', () => {
  it.each(['attest-known-benign', 'authorize-generation'] as const)(
    'issues the Platform assertion contract for %s',
    (action) => {
      const token = mintV13BenignAssertion(session, approval, action)
      const [version, encoded, signature] = token.split('.')
      expect(version).toBe('v1')
      expect(signature).toBe(
        createHmac('sha256', 's'.repeat(48))
          .update(`${version}.${encoded}`)
          .digest('hex'),
      )
      const claims = JSON.parse(Buffer.from(encoded, 'base64url').toString())
      expect(claims).toEqual({
        aud: 'ditto-platform-v13-benign-approval',
        action,
        approval_id: approval.approval_id,
        evidence_sha256: approval.review_evidence_sha256,
        sub: session.uid,
        email: session.email,
        iat: expect.any(Number),
        nonce: expect.stringMatching(/^[0-9a-f]{32}$/),
      })
      expect(
        Math.abs(claims.iat - Math.floor(Date.now() / 1000)),
      ).toBeLessThanOrEqual(1)
      expect(mintV13BenignAssertion(session, approval, action)).not.toBe(token)
    },
  )

  it.each([
    { uid: 'sn118-preview' },
    { accessLevel: 'read' as const },
    { expiresAt: 0 },
    { uid: '' },
    { email: 'outside@example.com' },
  ])('refuses unauthenticated or expired production identities %j', (patch) => {
    expect(() =>
      mintV13BenignAssertion(
        { ...session, ...patch },
        approval,
        'attest-known-benign',
      ),
    ).toThrow()
  })

  it('rechecks live entitlement instead of trusting a stale session', () => {
    vi.stubEnv('BACKROOM_ADMIN_EMAILS', '')
    expect(() =>
      mintV13BenignAssertion(session, approval, 'attest-known-benign'),
    ).toThrow()
    vi.stubEnv('BACKROOM_ADMIN_EMAILS', session.email)
    vi.stubEnv('BACKROOM_BLOCKED_EMAILS', session.email)
    expect(() =>
      mintV13BenignAssertion(session, approval, 'attest-known-benign'),
    ).toThrow()
  })

  it('fails closed when the issuer secret is unavailable', () => {
    vi.stubEnv('DITTO_V13_BENIGN_ATTESTATION_SECRET', '')
    expect(() =>
      mintV13BenignAssertion(session, approval, 'authorize-generation'),
    ).toThrow('not configured')
  })
})
