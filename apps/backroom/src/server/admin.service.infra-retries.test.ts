import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readScreeningInfraRetries } from './admin.service'

beforeEach(() => {
  process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
})

afterEach(() => {
  vi.unstubAllGlobals()
  delete process.env.DITTO_ADMIN_API_TOKEN
})

describe('readScreeningInfraRetries', () => {
  it('keeps the HTTP status and message when Platform refuses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ detail: 'Not Found' }, { status: 404 })))
    expect(await readScreeningInfraRetries()).toEqual({ ok: false, status: 404, message: 'Not Found' })
  })

  it('reports a schema failure as a failure, never as an empty view', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ unexpected: true })))
    const outcome = await readScreeningInfraRetries()
    expect(outcome.ok).toBe(false)
    if (!outcome.ok) {
      expect(outcome.status).toBeNull()
      expect(outcome.message.length).toBeGreaterThan(0)
      expect(outcome.message.length).toBeLessThanOrEqual(400)
    }
  })
})
