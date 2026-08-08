import { describe, expect, it, vi } from 'vitest'
import type { BackroomEnv } from './mcp.server'
import {
  cacheOAuthTokenReads,
  OAUTH_TOKEN_CACHE_TTL_SECONDS,
} from './oauth-token-cache.server'

function fixture() {
  const kv = {
    get: vi.fn(),
    delete: vi.fn(),
  }
  const stored = new Map<string, Response>()
  const cache = {
    match: vi.fn(async (request: Request) => stored.get(request.url)?.clone()),
    put: vi.fn(async (request: Request, response: Response) => {
      stored.set(request.url, response.clone())
    }),
    delete: vi.fn(async (request: Request) => stored.delete(request.url)),
  }
  const pending: Array<Promise<unknown>> = []
  const ctx = {
    waitUntil(promise: Promise<unknown>) {
      pending.push(promise)
    },
  } as ExecutionContext
  const env = { OAUTH_KV: kv as unknown as KVNamespace } as BackroomEnv
  const request = new Request('https://backroom.heyditto.ai/mcp')

  return { cache, env, kv, pending, request, ctx }
}

describe('cacheOAuthTokenReads', () => {
  it('serves repeated access-token validation from the edge cache', async () => {
    const { cache, env, kv, pending, request, ctx } = fixture()
    const record = {
      expiresAt: Math.floor(Date.now() / 1_000) + 3_000,
      grant: { encryptedProps: 'sealed' },
    }
    kv.get.mockResolvedValue(record)
    const cachedEnv = cacheOAuthTokenReads(env, request, ctx, cache)

    await expect(
      cachedEnv.OAUTH_KV.get('token:user:grant:digest', { type: 'json' }),
    ).resolves.toEqual(record)
    await Promise.all(pending)
    await expect(
      cachedEnv.OAUTH_KV.get('token:user:grant:digest', { type: 'json' }),
    ).resolves.toEqual(record)

    expect(kv.get).toHaveBeenCalledTimes(1)
    expect(cache.put).toHaveBeenCalledTimes(1)
    const cachedResponse = cache.put.mock.calls[0]?.[1]
    expect(cachedResponse?.headers.get('Cache-Control')).toBe(
      `max-age=${OAUTH_TOKEN_CACHE_TTL_SECONDS}`,
    )
  })

  it('does not cache misses or non-token reads', async () => {
    const { cache, env, kv, request, ctx } = fixture()
    kv.get.mockResolvedValue(null)
    const cachedEnv = cacheOAuthTokenReads(env, request, ctx, cache)

    await cachedEnv.OAUTH_KV.get('token:user:grant:missing', { type: 'json' })
    await cachedEnv.OAUTH_KV.get('client:client-id', { type: 'json' })

    expect(kv.get).toHaveBeenCalledTimes(2)
    expect(cache.put).not.toHaveBeenCalled()
  })

  it('evicts a cached token when it is revoked', async () => {
    const { cache, env, kv, request, ctx } = fixture()
    const cachedEnv = cacheOAuthTokenReads(env, request, ctx, cache)

    await cachedEnv.OAUTH_KV.delete('token:user:grant:digest')

    expect(kv.delete).toHaveBeenCalledWith('token:user:grant:digest')
    expect(cache.delete).toHaveBeenCalledTimes(1)
  })
})
