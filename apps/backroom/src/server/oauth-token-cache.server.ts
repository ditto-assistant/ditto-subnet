import '@tanstack/react-start/server-only'

import type { BackroomEnv } from './mcp.server'

const TOKEN_KEY_PREFIX = 'token:'
const TOKEN_CACHE_PATH = '/__backroom/oauth-token-cache/'

// Workers KV charges every binding read, including values already hot in KV's
// edge cache. OAuth access-token records are immutable and KV itself has an
// eventually-consistent cache window, so keep the same record in the Cache API
// for at most 60 seconds before asking KV again.
export const OAUTH_TOKEN_CACHE_TTL_SECONDS = 60

type TokenRecord = {
  expiresAt?: number
}

type JsonGetOptions = {
  type: 'json'
}

type OAuthTokenCache = Pick<Cache, 'delete' | 'match' | 'put'>

function isTokenJsonRead(key: string, options: unknown): options is JsonGetOptions {
  return (
    key.startsWith(TOKEN_KEY_PREFIX) &&
    options !== null &&
    options !== undefined &&
    typeof options === 'object' &&
    'type' in options &&
    options.type === 'json'
  )
}

function tokenCacheRequest(request: Request, key: string) {
  const url = new URL(request.url)
  url.pathname = `${TOKEN_CACHE_PATH}${encodeURIComponent(key)}`
  url.search = ''
  return new Request(url, { method: 'GET' })
}

function remainingCacheTtl(record: TokenRecord) {
  const remaining = record.expiresAt
    ? record.expiresAt - Math.floor(Date.now() / 1_000)
    : OAUTH_TOKEN_CACHE_TTL_SECONDS
  return Math.min(OAUTH_TOKEN_CACHE_TTL_SECONDS, Math.max(0, remaining))
}

export function cacheOAuthTokenReads(
  env: BackroomEnv,
  request: Request,
  ctx: ExecutionContext,
  cache?: OAuthTokenCache,
): BackroomEnv {
  const tokenCache =
    cache ?? (caches as unknown as { default: OAuthTokenCache }).default
  const oauthKv = new Proxy(env.OAUTH_KV, {
    get(target, property) {
      if (property === 'get') {
        return async (key: string, options?: unknown) => {
          if (!isTokenJsonRead(key, options)) {
            return Reflect.apply(target.get, target, [key, options])
          }

          const cacheRequest = tokenCacheRequest(request, key)
          const cached = await tokenCache.match(cacheRequest)
          if (cached) return cached.json()

          const record = (await Reflect.apply(target.get, target, [
            key,
            options,
          ])) as TokenRecord | null
          if (!record) return null

          const ttl = remainingCacheTtl(record)
          if (ttl > 0) {
            ctx.waitUntil(
              tokenCache.put(
                cacheRequest,
                new Response(JSON.stringify(record), {
                  headers: {
                    'Cache-Control': `max-age=${ttl}`,
                    'Content-Type': 'application/json',
                  },
                }),
              ),
            )
          }
          return record
        }
      }

      if (property === 'delete') {
        return async (key: string) => {
          await Reflect.apply(target.delete, target, [key])
          if (key.startsWith(TOKEN_KEY_PREFIX)) {
            await tokenCache.delete(tokenCacheRequest(request, key))
          }
        }
      }

      const value = Reflect.get(target, property, target)
      return typeof value === 'function' ? value.bind(target) : value
    },
  }) as KVNamespace

  return { ...env, OAUTH_KV: oauthKv }
}
