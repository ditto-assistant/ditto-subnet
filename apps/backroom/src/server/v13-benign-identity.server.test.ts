import { afterEach, describe, expect, it, vi } from 'vitest'
import type { BackroomSession } from '../lib/auth.types'
import { signV13BenignAttestation } from './v13-benign-identity.server'

const session: BackroomSession = {
  version: 2,
  uid: 'google-sub-one',
  email: 'ONE@omniaura.ai',
  name: 'Operator One',
  picture: '',
  accessLevel: 'write',
  issuedAt: Date.now() - 1000,
  expiresAt: Date.now() + 60_000,
}

afterEach(() => vi.unstubAllEnvs())

describe('V13 known-benign identity assertions', () => {
  it('binds the authenticated subject, approval and evidence under a dedicated key', async () => {
    vi.stubEnv('DITTO_V13_BENIGN_ATTESTATION_SECRET', 'x'.repeat(48))
    const token = await signV13BenignAttestation(
      session, '00000000-0000-0000-0000-000000000001', 'a'.repeat(64), 1_790_000_000,
    )
    const [version, encoded, signature] = token.split('.')
    expect(version).toBe('v1')
    const claims = JSON.parse(Buffer.from(encoded, 'base64url').toString())
    expect(claims).toMatchObject({
      sub: 'google-sub-one', email: 'one@omniaura.ai',
      approval_id: '00000000-0000-0000-0000-000000000001',
      evidence_sha256: 'a'.repeat(64), iat: 1_790_000_000,
    })
    expect(claims.nonce).toMatch(/^[0-9a-f]{32}$/)
    const key = await crypto.subtle.importKey(
      'raw', new TextEncoder().encode(process.env.DITTO_V13_BENIGN_ATTESTATION_SECRET),
      { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
    )
    const expected = new Uint8Array(await crypto.subtle.sign(
      'HMAC', key, new TextEncoder().encode(`${version}.${encoded}`),
    ))
    expect(signature).toBe([...expected].map((byte) => byte.toString(16).padStart(2, '0')).join(''))
  })

  it('fails closed without the key or a real write session', async () => {
    await expect(signV13BenignAttestation(session, 'id', 'a'.repeat(64))).rejects.toThrow(
      'disabled',
    )
    vi.stubEnv('DITTO_V13_BENIGN_ATTESTATION_SECRET', 'x'.repeat(48))
    await expect(signV13BenignAttestation({ ...session, uid: 'sn118-preview' }, 'id', 'a'.repeat(64)))
      .rejects.toThrow('authenticated')
    await expect(signV13BenignAttestation({ ...session, accessLevel: 'read' }, 'id', 'a'.repeat(64)))
      .rejects.toThrow('authenticated')
  })
})
