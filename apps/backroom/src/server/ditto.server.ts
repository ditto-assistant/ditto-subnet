import '@tanstack/react-start/server-only'

import { setResponseHeader } from '@tanstack/react-start/server'

import { parseJsonPreservingLargeIntegers } from '../lib/json-large-integers'

/**
 * Decode a response body without rounding 64-bit integers.
 *
 * This is the last point where the exact digits of a DittoBench dataset seed
 * still exist: a plain `JSON.parse` here rounds them to the nearest double and
 * no downstream schema can recover them. See `../lib/json-large-integers`.
 */
function decodeResponseBody(text: string): unknown {
  if (!text) return null
  try {
    return parseJsonPreservingLargeIntegers(text)
  } catch {
    return { message: text.slice(0, 240) }
  }
}

function errorMessage(payload: unknown, status: number) {
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    if (typeof record.message === 'string') return record.message
    if (typeof record.detail === 'string') return record.detail
    if (typeof record.error === 'string') return record.error
    if (record.error && typeof record.error === 'object') {
      const nested = record.error as Record<string, unknown>
      if (typeof nested.message === 'string') return nested.message
      if (typeof nested.description === 'string') return nested.description
    }
  }
  return `Ditto API request failed (${status})`
}

/**
 * A non-2xx answer from the platform's public ledger, carrying its status.
 *
 * The status is the whole point. The public ledger splits one submission across
 * a *settled* surface and an *in-progress* surface, and answers the wrong one
 * with 404 — so "404" from `/agent/{id}/scores` means "not settled yet", which
 * is a different fact from "no such agent". Without the status a caller can
 * only re-raise the platform's `detail` string, and an operator reading it
 * cannot tell a provisional submission from one that does not exist.
 */
export class PlatformPublicError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'PlatformPublicError'
    this.status = status
  }
}

/** True when a public read failed because the surface holds no such record. */
export function isPlatformPublicNotFound(error: unknown) {
  return error instanceof PlatformPublicError && error.status === 404
}

// The platform's public score ledger (/api/v1/public/*) is the same
// authoritative database that drives validator weights and the dittobench.ai
// dashboard, served without credentials. Score reads deliberately never
// attach the admin token: these tools are read-only by construction and a
// leaked response can never carry admin authority.
export async function platformPublicRequest(path: string) {
  const baseUrl = (
    process.env.DITTO_PLATFORM_API_BASE_URL ?? 'https://platform-api.heyditto.ai'
  ).replace(/\/$/, '')
  const response = await fetch(`${baseUrl}${path}`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(20_000),
  })

  try {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
  } catch {
    // Shared server helpers can run outside a TanStack response context.
  }

  const payload = decodeResponseBody(await response.text())
  if (!response.ok) {
    throw new PlatformPublicError(
      errorMessage(payload, response.status),
      response.status,
    )
  }
  return payload
}

/** Why a platform admin call failed, so callers can write honest copy. */
export type PlatformAdminFailure = 'timeout' | 'auth' | 'server' | 'request'

export class PlatformAdminError extends Error {
  readonly failure: PlatformAdminFailure
  readonly status: number | null

  constructor(
    failure: PlatformAdminFailure,
    message: string,
    status: number | null = null,
  ) {
    super(message)
    this.name = 'PlatformAdminError'
    this.failure = failure
    this.status = status
  }
}

function isTimeout(error: unknown) {
  return (
    error instanceof DOMException &&
    (error.name === 'TimeoutError' || error.name === 'AbortError')
  )
}

export async function platformAdminRequest(
  path: string,
  options: {
    method?: string
    body?: unknown
    actor?: string
    timeoutMs?: number
    /**
     * Extra attempts after a timeout or a 5xx. GET only, and asserted so: a
     * retried rollout POST would be a second attempt to start, supersede, or
     * activate a benchmark, which is exactly the thing the confirmation
     * phrases exist to make deliberate.
     */
    retries?: number
  } = {},
) {
  const baseUrl = (
    process.env.DITTO_PLATFORM_API_BASE_URL ?? 'https://platform-api.heyditto.ai'
  ).replace(/\/$/, '')
  const token =
    process.env.DITTO_ADMIN_API_TOKEN ??
    process.env.DITTO_PLATFORM_ADMIN_TOKEN
  if (!token) {
    throw new Error('Screening quarantine administration is not configured in Backroom')
  }

  const method = options.method ?? 'GET'
  const timeoutMs = options.timeoutMs ?? 20_000
  const retries = options.retries ?? 0
  if (retries > 0 && method !== 'GET') {
    throw new Error(`refusing to retry a ${method} to ${path}: it is not idempotent`)
  }

  let attempt = 0
  for (;;) {
    let response: Response
    try {
      response = await fetch(`${baseUrl}${path}`, {
        method,
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${token}`,
          ...(options.actor ? { 'X-Admin-Actor': options.actor } : {}),
          ...(options.body === undefined
            ? {}
            : { 'Content-Type': 'application/json' }),
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: AbortSignal.timeout(timeoutMs),
      })
    } catch (error) {
      if (!isTimeout(error)) throw error
      if (attempt++ < retries) continue
      throw new PlatformAdminError(
        'timeout',
        `The platform API did not answer ${path} within ${Math.round(
          timeoutMs / 1000,
        )}s${retries > 0 ? ` (${attempt} attempts)` : ''}.`,
      )
    }

    try {
      setResponseHeader('Cache-Control', 'no-store')
      setResponseHeader('Vary', 'Cookie, Authorization')
    } catch {
      // Shared server helpers can run outside a TanStack response context.
    }

    const payload = decodeResponseBody(await response.text())
    if (response.ok) return payload
    if (response.status >= 500 && attempt++ < retries) continue
    const message = errorMessage(payload, response.status)
    if (response.status === 401 || response.status === 403) {
      throw new PlatformAdminError('auth', message, response.status)
    }
    throw new PlatformAdminError(
      response.status >= 500 ? 'server' : 'request',
      message,
      response.status,
    )
  }
}

/** Read one private binary artifact without exposing the Platform bearer. */
export async function platformAdminBinaryRequest(
  path: string,
  options: { timeoutMs?: number; maxBytes?: number; actor?: string } = {},
) {
  const baseUrl = (
    process.env.DITTO_PLATFORM_API_BASE_URL ?? 'https://platform-api.heyditto.ai'
  ).replace(/\/$/, '')
  const token =
    process.env.DITTO_ADMIN_API_TOKEN ??
    process.env.DITTO_PLATFORM_ADMIN_TOKEN
  if (!token) {
    throw new Error('Platform administration is not configured in Backroom')
  }
  const response = await fetch(`${baseUrl}${path}`, {
    method: 'GET',
    headers: {
      Accept: 'application/octet-stream',
      Authorization: `Bearer ${token}`,
      ...(options.actor ? { 'X-Admin-Actor': options.actor } : {}),
    },
    signal: AbortSignal.timeout(options.timeoutMs ?? 20_000),
  })
  if (!response.ok) {
    const payload = decodeResponseBody(await response.text())
    const message = errorMessage(payload, response.status)
    throw new PlatformAdminError(
      response.status === 401 || response.status === 403
        ? 'auth'
        : response.status >= 500
          ? 'server'
          : 'request',
      message,
      response.status,
    )
  }
  const declaredLength = Number(response.headers.get('content-length') ?? '0')
  const maxBytes = options.maxBytes ?? 2 * 1024 * 1024
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    throw new Error(`Runtime profile is ${declaredLength} bytes; inline download is capped at ${maxBytes}`)
  }
  const bytes = new Uint8Array(await response.arrayBuffer())
  if (bytes.byteLength > maxBytes) {
    throw new Error(`Runtime profile is ${bytes.byteLength} bytes; inline download is capped at ${maxBytes}`)
  }
  return {
    bytes,
    sha256: response.headers.get('x-profile-sha256'),
  }
}
