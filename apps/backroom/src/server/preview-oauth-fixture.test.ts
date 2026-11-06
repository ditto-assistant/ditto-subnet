import { createRequire } from 'node:module'
import { expect, it } from 'vitest'
import { accessLevelForEmail } from '../lib/auth.policy'
import { readSessionFromRequest } from './session.server'

const fixture = createRequire(import.meta.url)('../../../../preview/cloud/oauth-fixture.cjs') as {
  EMAIL: string
  response: (request: unknown, env: unknown) => { status: number; cookie?: string }
}

it('unseals the isolated preview fixture using the real Backroom session reader', async () => {
  const secret = 'a'.repeat(64)
  const cookie = fixture.response({
    method: 'POST', url: '/__preview/oauth-session',
    headers: { origin: 'https://backroom.192.0.2.1.sslip.io', host: 'backroom.192.0.2.1.sslip.io' },
  }, {
    PREVIEW_PR: '3359', PREVIEW_PROFILE: 'stack', PREVIEW_BASE_DOMAIN: '192.0.2.1.sslip.io', PREVIEW_SESSION_SECRET: secret,
  }).cookie
  expect(cookie).toBeTruthy()
  const session = await readSessionFromRequest(new Request('https://backroom.192.0.2.1.sslip.io/', {
    headers: { cookie: cookie!.split(';')[0]! },
  }), secret)
  expect(session).toMatchObject({ version: 2, uid: 'synthetic-preview-only', email: fixture.EMAIL })
  expect(accessLevelForEmail(session!.email, fixture.EMAIL)).toBe('write')
  expect(await readSessionFromRequest(new Request('https://backroom.192.0.2.1.sslip.io/', {
    headers: { cookie: cookie!.split(';')[0]! },
  }), 'b'.repeat(64))).toBeNull()
})
