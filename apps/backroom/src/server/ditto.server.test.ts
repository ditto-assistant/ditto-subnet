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

  it('classifies a connection failure after sending as transport', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed')))

    const error = await platformAdminRequest('/api/v1/admin/cla/settings', {
      method: 'POST',
      body: { mode: 'shadow' },
    }).catch((cause: unknown) => cause)

    expect(error).toBeInstanceOf(PlatformAdminError)
    expect((error as PlatformAdminError).failure).toBe('transport')
    expect((error as PlatformAdminError).status).toBeNull()
    expect((error as Error).message).toContain('fetch failed')
  })

  it.each([
    ['transport', new TypeError('terminated')],
    ['timeout', new DOMException('The operation was aborted due to timeout', 'TimeoutError')],
  ])('classifies a failed response body read as %s', async (failure, cause) => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    const body = new ReadableStream({ start(controller) { controller.error(cause) } })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)))

    const error = await platformAdminRequest('/api/v1/admin/cla/settings', {
      method: 'POST',
      body: { mode: 'shadow' },
    }).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(PlatformAdminError)
    expect((error as PlatformAdminError).failure).toBe(failure)
  })

  it.each([
    [409, 'request', new TypeError('terminated')],
    [403, 'auth', new TypeError('terminated')],
    [502, 'server', new DOMException('The operation was aborted due to timeout', 'TimeoutError')],
  ] as const)('keeps a %i status when its body cannot be read', async (status, failure, cause) => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    const body = new ReadableStream({ start(controller) { controller.error(cause) } })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status })))

    const error = await platformAdminRequest('/api/v1/admin/cla/settings', {
      method: 'POST',
      body: { mode: 'shadow' },
    }).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(PlatformAdminError)
    expect((error as PlatformAdminError).failure).toBe(failure)
    expect((error as PlatformAdminError).status).toBe(status)
  })

  it.each([
    ['an invalid base URL', () => { process.env.DITTO_PLATFORM_API_BASE_URL = 'platform.invalid without scheme' }, {}],
    ['an invalid header value', () => {}, { actor: 'operator@example.com\r\nX-Injected: yes' }],
  ])('fails before sending on %s', async (_name, configure, options) => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    configure()
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const error = await platformAdminRequest('/api/v1/admin/cla/settings', { method: 'POST', body: { mode: 'shadow' }, ...options })
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(Error)
    expect(error).not.toBeInstanceOf(PlatformAdminError)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each(['ECONNREFUSED', 'ENOTFOUND', 'UND_ERR_CONNECT_TIMEOUT', 'CERT_HAS_EXPIRED', 'ERR_TLS_CERT_ALTNAME_INVALID'])('classifies %s as unreachable, not delivered', async (code) => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(Object.assign(new TypeError('fetch failed'), { cause: { code } })))

    const error = await platformAdminRequest('/api/v1/admin/cla/settings', { method: 'POST', body: { mode: 'shadow' } })
      .catch((caught: unknown) => caught)

    expect((error as PlatformAdminError).failure).toBe('unreachable')
    expect((error as PlatformAdminError).status).toBeNull()
  })

  it.each([
    [['ECONNREFUSED', 'EHOSTUNREACH'], 'unreachable'],
    [['ECONNREFUSED', 'ECONNRESET'], 'transport'],
    [['ECONNREFUSED', undefined], 'transport'],
  ])('classifies an aggregate connection failure %o as %s', async (codes, failure) => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    const cause = new AggregateError(codes.map((code) => Object.assign(new Error(code ?? 'unknown'), code ? { code } : {})))
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(Object.assign(new TypeError('fetch failed'), { cause })))

    const error = await platformAdminRequest('/api/v1/admin/cla/settings', { method: 'POST', body: { mode: 'shadow' } })
      .catch((caught: unknown) => caught)

    expect((error as PlatformAdminError).failure).toBe(failure)
  })

  it('classifies an AggregateError thrown directly by its parts', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    const parts = ['ECONNREFUSED', 'EHOSTUNREACH'].map((code) => Object.assign(new Error(code), { code }))
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new AggregateError(parts)))

    const error = await platformAdminRequest('/api/v1/admin/cla/settings', { method: 'POST', body: { mode: 'shadow' } })
      .catch((caught: unknown) => caught)

    expect((error as PlatformAdminError).failure).toBe('unreachable')
  })

  it('reports a GET that ends in a 3xx as a failed read, not a redirect', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(null, { status: 304 })))

    const error = await platformAdminRequest('/api/v1/admin/cla/settings').catch((caught: unknown) => caught)

    expect((error as PlatformAdminError).failure).toBe('request')
    expect((error as PlatformAdminError).status).toBe(304)
    expect((error as Error).message).not.toContain('redirected')
  })

  it('never follows a redirect on a write and leaves its outcome to the read-back', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(null, { status: 301, headers: { Location: 'https://elsewhere.invalid/' } }))
      .mockResolvedValueOnce(Response.json({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    const error = await platformAdminRequest('/api/v1/admin/cla/settings', { method: 'POST', body: { mode: 'shadow' } })
      .catch((caught: unknown) => caught)
    await platformAdminRequest('/api/v1/admin/cla/settings')

    expect(fetchMock.mock.calls[0][1].redirect).toBe('manual')
    // Delivered, so possibly applied: a transport failure the read-back settles.
    expect((error as PlatformAdminError).failure).toBe('transport')
    expect((error as PlatformAdminError).status).toBe(301)
    expect((error as Error).message).toContain('may have been applied')
    expect(fetchMock.mock.calls[1][1].redirect).toBe('follow')
  })

  it('retries a GET whose response body times out', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    const stalled = new ReadableStream({
      start(controller) {
        controller.error(new DOMException('The operation was aborted due to timeout', 'TimeoutError'))
      },
    })
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(stalled))
      .mockResolvedValueOnce(Response.json({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(platformAdminRequest('/api/v1/admin/benchmark-rollout', { retries: 1 }))
      .resolves.toEqual({ ok: true })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('retries a GET whose body read fails with a timeout as its cause', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
    const stalled = new ReadableStream({
      start(controller) {
        const timeout = new DOMException('The operation was aborted due to timeout', 'TimeoutError')
        controller.error(new TypeError('terminated', { cause: timeout }))
      },
    })
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(stalled))
      .mockResolvedValueOnce(Response.json({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(platformAdminRequest('/api/v1/admin/benchmark-rollout', { retries: 1 }))
      .resolves.toEqual({ ok: true })
    expect(fetchMock).toHaveBeenCalledTimes(2)
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
