import { afterEach, describe, expect, it, vi } from 'vitest'
import { PlatformAdminError, platformAdminRequest } from './ditto.server'

const originalToken = process.env.DITTO_ADMIN_API_TOKEN
const originalLegacyToken = process.env.DITTO_PLATFORM_ADMIN_TOKEN
const originalBaseUrl = process.env.DITTO_PLATFORM_API_BASE_URL

afterEach(() => {
  vi.unstubAllGlobals()
  if (originalToken === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = originalToken
  if (originalLegacyToken === undefined) delete process.env.DITTO_PLATFORM_ADMIN_TOKEN
  else process.env.DITTO_PLATFORM_ADMIN_TOKEN = originalLegacyToken
  if (originalBaseUrl === undefined) delete process.env.DITTO_PLATFORM_API_BASE_URL
  else process.env.DITTO_PLATFORM_API_BASE_URL = originalBaseUrl
})

describe('platformAdminRequest', () => {
  it('fails closed before making a request when the admin token is missing', async () => {
    delete process.env.DITTO_ADMIN_API_TOKEN
    delete process.env.DITTO_PLATFORM_ADMIN_TOKEN
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(platformAdminRequest('/api/v1/admin/screening-quarantines')).rejects.toThrow(
      'Screening quarantine administration is not configured',
    )
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('keeps the admin token server-side and attributes write requests', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    process.env.DITTO_PLATFORM_ADMIN_TOKEN = 'legacy-token-must-not-win'
    process.env.DITTO_PLATFORM_API_BASE_URL = 'https://platform.example.test/'
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await platformAdminRequest('/api/v1/admin/screening-quarantines/id/resolve', {
      method: 'POST',
      actor: 'operator@omniaura.ai',
      body: { resolution: 'rescreen', reason: 'Review again' },
    })

    const [url, request] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      'https://platform.example.test/api/v1/admin/screening-quarantines/id/resolve',
    )
    expect(request.headers).toMatchObject({
      Authorization: 'Bearer secret-admin-token',
      'X-Admin-Actor': 'operator@omniaura.ai',
    })
  })

  it('surfaces FastAPI error detail without exposing the token', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json({ detail: 'quarantine is not active' }, { status: 409 }),
      ),
    )

    await expect(platformAdminRequest('/api/v1/admin/screening-quarantines/id')).rejects.toThrow(
      'quarantine is not active',
    )
  })

  it('refuses to retry a write, however transient the failure looks', async () => {
    // A retried rollout POST is a second attempt to start, supersede, or
    // activate a benchmark. The confirmation phrases exist to make exactly
    // that deliberate, so no transport-level convenience may replay one.
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      platformAdminRequest('/api/v1/admin/benchmark-rollout/7', {
        method: 'POST',
        retries: 2,
        body: { confirmation: 'START BENCHMARK V7' },
      }),
    ).rejects.toThrow('refusing to retry a POST')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('classifies a timeout as a timeout rather than a bare abort', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(
        new DOMException('The operation was aborted due to timeout', 'TimeoutError'),
      ),
    )

    const error = await platformAdminRequest('/api/v1/admin/benchmark-rollout', {
      timeoutMs: 25_000,
    }).catch((cause: unknown) => cause)

    expect(error).toBeInstanceOf(PlatformAdminError)
    expect((error as PlatformAdminError).failure).toBe('timeout')
    expect((error as Error).message).toContain('25s')
  })

  it('supports the legacy Worker binding during the token-name rollout', async () => {
    delete process.env.DITTO_ADMIN_API_TOKEN
    process.env.DITTO_PLATFORM_ADMIN_TOKEN = 'legacy-admin-token'
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await platformAdminRequest('/api/v1/admin/screening-quarantines')

    expect(fetchMock.mock.calls[0]?.[1]?.headers).toMatchObject({
      Authorization: 'Bearer legacy-admin-token',
    })
  })
})
