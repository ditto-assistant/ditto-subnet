import OAuthProvider, {
  getOAuthApi,
  type OAuthHelpers,
  type OAuthProviderOptions,
} from '@cloudflare/workers-oauth-provider'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
// Exercise the actual Worker OAuth/request-local binding path. SSR routing is
// outside this grant test; its own staff-cookie guards do not accept MCP tokens.
vi.mock('@tanstack/react-start/server-entry', () => ({ default: { fetch: async () => new Response('console', { status: 404 }) } }))
import worker from '../server'

// `cloudflare:workers` is aliased to a test stub in vitest.config.ts; node's
// loader cannot resolve the Workers-runtime module the provider library and the
// MCP handler both import.
import type { BackroomSession } from '../lib/auth.types'
import { sealToken } from './crypto.server'
import { BackroomMcpHandler } from './mcp-handler.server'
import { BACKROOM_TREASURY_OBSERVE_SCOPE as OBSERVE } from './treasury-observer-access.server'
import {
  beginMcpAuthorization,
  completeMcpAuthorization,
  getMcpConsentDetails,
  listMcpGrants,
  mcpTokenExchange,
  revokeMcpGrant,
} from './mcp-oauth.server'
import {
  BACKROOM_ARTIFACT_SCOPE,
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  createBackroomMcpServer,
  type BackroomEnv,
  type McpGrantProps,
} from './mcp.server'

// Issue #2080: end-to-end grant tests against the real OAuth provider library
// (in-memory KV), so the consent cap, same-client grant replacement, token
// issuance, and get_backroom_access are exercised together rather than mocked.

const secret = 'test-backroom-session-secret-0123456789'
const origin = 'https://backroom.dittobench.ai'
const redirectUri = 'http://127.0.0.1:8899/callback'
const FULL = [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE, BACKROOM_WRITE_SCOPE]

const session: BackroomSession = {
  version: 2,
  uid: 'staff-1',
  email: 'peyton@omniaura.ai',
  name: 'Staff User',
  picture: '',
  accessLevel: 'write',
  issuedAt: Date.now(),
  expiresAt: Date.now() + 7 * 24 * 60 * 60_000,
}

beforeEach(() => {
  // Actual Worker entry uses the Workers Cache API; an empty cache exercises
  // the real KV/provider path rather than bypassing its token revocation check.
  vi.stubGlobal('caches', { default: {
    match: async () => undefined, put: async () => {}, delete: async () => true,
  } })
})
afterEach(() => vi.unstubAllGlobals())

function memoryKv() {
  const store = new Map<string, string>()
  return {
    store,
    async get(key: string, options?: unknown) {
      const value = store.get(key)
      if (value === undefined) return null
      const type =
        typeof options === 'string' ? options : (options as { type?: string } | undefined)?.type
      return type === 'json' ? JSON.parse(value) : value
    },
    async put(key: string, value: string) {
      store.set(key, value)
    },
    async delete(key: string) {
      store.delete(key)
    },
    async list(options: { prefix?: string } = {}) {
      const keys = [...store.keys()]
        .filter((name) => name.startsWith(options.prefix ?? ''))
        .sort()
        .map((name) => ({ name }))
      return { keys, list_complete: true, cursor: undefined }
    },
  }
}

const passthrough = { fetch: async () => new Response('not found', { status: 404 }) }

function harness() {
  const kv = memoryKv()
  const env = {
    OAUTH_KV: kv as unknown as KVNamespace,
    SESSION_SECRET: secret,
    BACKROOM_ADMIN_EMAILS: 'peyton@omniaura.ai',
  } as BackroomEnv
  const options: OAuthProviderOptions = {
    apiRoute: '/mcp',
    apiHandler: passthrough,
    defaultHandler: passthrough,
    authorizeEndpoint: '/authorize',
    tokenEndpoint: '/token',
    clientRegistrationEndpoint: '/register',
    scopesSupported: [...FULL, OBSERVE],
    allowImplicitFlow: false,
    allowPlainPKCE: false,
    refreshTokenTTL: 7 * 24 * 60 * 60,
    tokenExchangeCallback: (callback) => mcpTokenExchange(callback, Date.now(), env),
  }
  const provider = new OAuthProvider(options)
  const oauth = getOAuthApi(options, env) as OAuthHelpers
  const ctx = { waitUntil() {}, passThroughOnException() {}, props: {} } as unknown as ExecutionContext
  return { kv, env, provider, oauth, ctx }
}

async function pkcePair() {
  const verifier = 'v'.repeat(20) + crypto.randomUUID() + crypto.randomUUID()
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  const challenge = btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '')
  return { verifier, challenge }
}

async function sessionCookie(overrides: Partial<BackroomSession> = {}) {
  return `__Host-backroom_session=${await sealToken({ ...session, ...overrides }, secret)}`
}

type Harness = ReturnType<typeof harness>

async function authorize(
  h: Harness,
  clientId: string,
  requestedScope: string,
  accessLevel: 'read' | 'artifact' | 'write' | 'full' | 'observe',
  sessionOverrides: Partial<BackroomSession> = {},
) {
  const { verifier, challenge } = await pkcePair()
  const authorize = new URL(`${origin}/authorize`)
  authorize.search = new URLSearchParams({
    response_type: 'code',
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: requestedScope,
    state: 'client-state',
    code_challenge: challenge,
    code_challenge_method: 'S256',
    resource: `${origin}/mcp`,
  }).toString()
  const begin = await beginMcpAuthorization(new Request(authorize), h.oauth, secret)
  const requestToken =
    new URL(begin.headers.get('location') ?? '').searchParams.get('request') ?? ''
  const details = await getMcpConsentDetails(requestToken, origin, secret)
  const complete = await completeMcpAuthorization(
    new Request(`${origin}/oauth/authorize/complete`, {
      method: 'POST',
      headers: {
        Origin: origin,
        'Content-Type': 'application/json',
        Cookie: await sessionCookie(sessionOverrides),
      },
      body: JSON.stringify({ requestToken, csrf: details.csrf, decision: 'allow', accessLevel }),
    }),
    { ...h.env, OAUTH_PROVIDER: h.oauth },
  )
  const { redirectTo } = (await complete.json()) as { redirectTo: string }
  const code = new URL(redirectTo).searchParams.get('code') ?? ''
  return { details, code, verifier }
}

async function exchangeCode(h: Harness, clientId: string, code: string, verifier: string) {
  return h.provider.fetch(
    new Request(`${origin}/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'authorization_code',
        code,
        redirect_uri: redirectUri,
        client_id: clientId,
        code_verifier: verifier,
      }),
    }),
    h.env,
    h.ctx,
  )
}

async function connect(
  h: Harness,
  clientId: string,
  requestedScope: string,
  accessLevel: 'read' | 'artifact' | 'write' | 'full' | 'observe',
  sessionOverrides: Partial<BackroomSession> = {},
) {
  const { details, code, verifier } = await authorize(
    h,
    clientId,
    requestedScope,
    accessLevel,
    sessionOverrides,
  )
  const token = await exchangeCode(h, clientId, code, verifier)
  expect(token.status).toBe(200)
  const body = (await token.json()) as {
    access_token: string
    refresh_token: string
    scope: string
    expires_in: number
  }
  return { details, ...body }
}

async function refresh(h: Harness, clientId: string, refreshToken: string, scope?: string) {
  return h.provider.fetch(
    new Request(`${origin}/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'refresh_token',
        refresh_token: refreshToken,
        client_id: clientId,
        ...(scope ? { scope } : {}),
      }),
    }),
    h.env,
    h.ctx,
  )
}

async function backroomAccess(props: McpGrantProps) {
  const server = createBackroomMcpServer(props)
  const client = new Client({ name: 'backroom-test', version: '1.0.0' })
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)])
  const response = await client.callTool({ name: 'get_backroom_access', arguments: {} })
  await client.close()
  await server.close()
  const content = response.content as Array<{ type: string; text: string }>
  return JSON.parse(content[0].text) as {
    accessLevel: string
    scopes: Array<string>
    grantedScopes: Array<string>
    grant: { id: string; clientId: string } | null
    expires_at: string | null
  }
}

async function tokenProps(h: Harness, accessToken: string) {
  const summary = await h.oauth.unwrapToken<McpGrantProps>(accessToken)
  if (!summary) throw new Error('token did not unwrap')
  return summary
}

async function registerClient(h: Harness) {
  const client = await h.oauth.createClient({
    redirectUris: [redirectUri],
    clientName: 'Codex',
    tokenEndpointAuthMethod: 'none',
  })
  return client.clientId
}

describe('Backroom MCP OAuth grants (issue #2080)', () => {
  it('issues and refreshes a receipt-only grant without inheriting general read or write', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    const connection = await connect(h, clientId, OBSERVE, 'observe')
    expect(connection.scope).toBe(OBSERVE)
    expect(connection.details).toMatchObject({ observerOnly: true, requestedScopes: [OBSERVE], canRequestArtifact: false, canRequestWrite: false })
    const issued = await tokenProps(h, connection.access_token)
    expect(issued.grant.props.scopes).toEqual([OBSERVE])
    const renewed = await refresh(h, clientId, connection.refresh_token)
    expect(renewed.status).toBe(200)
    const body = await renewed.json() as { access_token: string; refresh_token: string; scope: string }
    expect(body.scope).toBe(OBSERVE)
    expect((await tokenProps(h, body.access_token)).grant.props.scopes).toEqual([OBSERVE])
    // Direct broad refresh attempts cannot import read, artifact or write.
    const widened = await refresh(h, clientId, body.refresh_token, [...FULL, OBSERVE].join(' '))
    // The actual provider intersects requested refresh scope before its
    // callback. A broad wire request succeeds only as the existing sole scope.
    expect(widened.status).toBe(200)
    const capped = await widened.json() as { access_token: string; scope: string }
    expect(capped.scope).toBe(OBSERVE)
    expect((await tokenProps(h, capped.access_token)).grant.props.scopes).toEqual([OBSERVE])
    const listing = await listMcpGrants(new Request(`${origin}/oauth/grants`, { headers: { Cookie: await sessionCookie() } }), { ...h.env, OAUTH_PROVIDER: h.oauth })
    expect(await listing.json()).toMatchObject({ grants: [expect.objectContaining({ scopes: [OBSERVE], accessLevel: 'treasury-observer' })] })
  })

  it('refuses mixed observer authorization and implicit generic selections', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    await expect(authorize(h, clientId, `${OBSERVE} ${BACKROOM_READ_SCOPE}`, 'observe')).rejects.toThrow('cannot be mixed')
    for (const choice of ['read', 'artifact', 'write', 'full'] as const) {
      await expect(authorize(h, clientId, OBSERVE, choice)).rejects.toThrow()
    }
    h.env.BACKROOM_ADMIN_EMAILS = ''
    await expect(authorize(h, clientId, OBSERVE, 'observe')).rejects.toThrow()
  })

  it('rechecks dedicated staff authority at both code exchange and refresh', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    const authorized = await authorize(h, clientId, OBSERVE, 'observe')
    h.env.BACKROOM_ADMIN_EMAILS = ''
    const refused = await exchangeCode(h, clientId, authorized.code, authorized.verifier)
    expect(refused.status).toBe(400)
    expect(await refused.json()).toMatchObject({ error: 'invalid_grant' })
    h.env.BACKROOM_ADMIN_EMAILS = session.email
    const connection = await connect(h, clientId, OBSERVE, 'observe')
    h.env.BACKROOM_BLOCKED_EMAILS = session.email
    expect((await refresh(h, clientId, connection.refresh_token)).status).toBe(400)
  })

  it('binds the actual Worker token callback to each request and enforces direct bearer calls', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    const authorized = await authorize(h, clientId, OBSERVE, 'observe')
    const tokenRequest = new Request(`${origin}/token`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'authorization_code', code: authorized.code,
        redirect_uri: redirectUri, client_id: clientId, code_verifier: authorized.verifier }) })
    const issued = await worker.fetch(tokenRequest, h.env, h.ctx)
    expect(issued.status).toBe(200)
    const tokens = await issued.json() as { access_token: string; refresh_token: string; scope: string }
    expect(tokens.scope).toBe(OBSERVE)
    const invoke = (method: string, params: Record<string, unknown> = {}, bindings = h.env) => worker.fetch(new Request(`${origin}/mcp`, {
      method: 'POST', headers: { Authorization: `Bearer ${tokens.access_token}`, 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
    }), bindings, h.ctx)
    const discovery = await invoke('tools/list')
    expect(discovery.status).toBe(200)
    expect(discovery.headers.get('Cache-Control')).toBe('no-store')
    expect(await discovery.json()).toMatchObject({ result: { tools: [expect.objectContaining({ name: 'get_treasury_settings' }), expect.objectContaining({ name: 'record_treasury_receipt' })] } })
    for (const method of ['resources/list', 'resources/read']) expect((await invoke(method)).status).toBe(403)
    expect((await invoke('tools/call', { name: 'record_treasury_settings', arguments: {} })).status).toBe(403)
    expect((await invoke('tools/list', {}, { ...h.env, BACKROOM_ADMIN_EMAILS: '' })).status).toBe(403)
    const refreshRequest = () => new Request(`${origin}/token`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: tokens.refresh_token, client_id: clientId }) })
    // Alternating bindings cannot leave a global provider closure with the
    // previous request's elevated authority. Refusal must use this request.
    const denied = await worker.fetch(refreshRequest(), { ...h.env, BACKROOM_ADMIN_EMAILS: '' }, h.ctx)
    expect(denied.status).toBe(400)
    expect(await denied.json()).toMatchObject({ error: 'invalid_grant' })
    const allowed = await worker.fetch(refreshRequest(), h.env, h.ctx)
    expect(allowed.status).toBe(200)
    expect(await allowed.json()).toMatchObject({ scope: OBSERVE })
    const metadata = await worker.fetch(new Request(`${origin}/.well-known/oauth-authorization-server/mcp`), h.env, h.ctx)
    expect(await metadata.json()).toMatchObject({ scopes_supported: expect.arrayContaining([OBSERVE]) })
  })

  it('revokes dedicated access/refresh and replaces prior full grants for the same client', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    const old = await connect(h, clientId, FULL.join(' '), 'full')
    const dedicated = await connect(h, clientId, OBSERVE, 'observe')
    expect(await h.oauth.unwrapToken(old.access_token)).toBeNull()
    expect((await refresh(h, clientId, old.refresh_token)).status).toBe(400)
    const { grantId } = await tokenProps(h, dedicated.access_token)
    const revoke = await revokeMcpGrant(new Request(`${origin}/oauth/grants/revoke`, {
      method: 'POST', headers: { Origin: origin, 'Content-Type': 'application/json', Cookie: await sessionCookie() }, body: JSON.stringify({ grantId }),
    }), { ...h.env, OAUTH_PROVIDER: h.oauth })
    expect(revoke.status).toBe(200)
    expect(await h.oauth.unwrapToken(dedicated.access_token)).toBeNull()
    expect((await refresh(h, clientId, dedicated.refresh_token)).status).toBe(400)
  })

  it('never returns write to a read-only request, even when consent picks full', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    const connection = await connect(h, clientId, BACKROOM_READ_SCOPE, 'full')

    expect(connection.details.requestedScopes).toEqual([BACKROOM_READ_SCOPE])
    expect(connection.scope).toBe(BACKROOM_READ_SCOPE)
    const summary = await tokenProps(h, connection.access_token)
    expect(summary.scope).toEqual([BACKROOM_READ_SCOPE])
    const access = await backroomAccess(summary.grant.props)
    expect(access).toMatchObject({
      accessLevel: 'read-only',
      scopes: [BACKROOM_READ_SCOPE],
      grant: { id: summary.grantId, clientId },
    })
  })

  it('replaces a previous full grant when the operator reconnects read-only', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    const full = await connect(h, clientId, FULL.join(' '), 'full')
    expect(full.scope.split(' ').sort()).toEqual([...FULL].sort())
    expect((await backroomAccess((await tokenProps(h, full.access_token)).grant.props)).accessLevel).toBe(
      'full',
    )

    const readOnly = await connect(h, clientId, BACKROOM_READ_SCOPE, 'read')
    expect(readOnly.scope).toBe(BACKROOM_READ_SCOPE)
    const summary = await tokenProps(h, readOnly.access_token)
    expect(await backroomAccess(summary.grant.props)).toMatchObject({
      accessLevel: 'read-only',
      scopes: [BACKROOM_READ_SCOPE],
      grantedScopes: [BACKROOM_READ_SCOPE],
    })

    // The earlier full grant, its access token, and its refresh token are gone.
    const grants = await h.oauth.listUserGrants(session.uid)
    expect(grants.items.map((grant) => grant.scope)).toEqual([[BACKROOM_READ_SCOPE]])
    expect(await h.oauth.unwrapToken(full.access_token)).toBeNull()
    expect((await refresh(h, clientId, full.refresh_token)).status).toBe(400)

    // Refreshing the new read-only grant cannot ask its way back to write.
    const refreshed = await refresh(h, clientId, readOnly.refresh_token, FULL.join(' '))
    expect(refreshed.status).toBe(200)
    const refreshedBody = (await refreshed.json()) as { access_token: string; scope: string }
    expect(refreshedBody.scope).toBe(BACKROOM_READ_SCOPE)
    expect((await tokenProps(h, refreshedBody.access_token)).grant.props.scopes).toEqual([
      BACKROOM_READ_SCOPE,
    ])
  })

  it('grants source-read without write when the operator selects source-read', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    // Even if the client asked for everything, the source-read selection wins.
    const connection = await connect(h, clientId, FULL.join(' '), 'artifact')
    expect(connection.scope.split(' ').sort()).toEqual(
      [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE].sort(),
    )
    const summary = await tokenProps(h, connection.access_token)
    expect(await backroomAccess(summary.grant.props)).toMatchObject({
      accessLevel: 'read-artifacts',
      scopes: [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE],
    })
  })

  it('a narrowed token request does not permanently shrink or widen the grant', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    const connection = await connect(
      h,
      clientId,
      `${BACKROOM_READ_SCOPE} ${BACKROOM_ARTIFACT_SCOPE}`,
      'artifact',
    )
    const narrowed = await refresh(h, clientId, connection.refresh_token, BACKROOM_READ_SCOPE)
    const narrowedBody = (await narrowed.json()) as { refresh_token: string; scope: string }
    expect(narrowedBody.scope).toBe(BACKROOM_READ_SCOPE)
    const restored = await refresh(h, clientId, narrowedBody.refresh_token)
    const restoredBody = (await restored.json()) as { scope: string }
    expect(restoredBody.scope.split(' ').sort()).toEqual(
      [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE].sort(),
    )
  })

  it('reports effective scopes capped by the live account level', async () => {
    const access = await backroomAccess({
      session: { ...session, accessLevel: 'read' },
      scopes: FULL,
      clientName: 'Codex',
      grant: { id: 'grant-1', clientId: 'client-1' },
    })
    expect(access).toMatchObject({
      accessLevel: 'read-only',
      scopes: [BACKROOM_READ_SCOPE],
      grantedScopes: FULL,
      grant: { id: 'grant-1', clientId: 'client-1' },
    })
  })

  it('lets an operator inspect and revoke exactly their own MCP client grant', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    const connection = await connect(h, clientId, BACKROOM_READ_SCOPE, 'read')
    const { grantId } = await tokenProps(h, connection.access_token)
    const grantEnv = { ...h.env, OAUTH_PROVIDER: h.oauth }

    const listed = await listMcpGrants(
      new Request(`${origin}/oauth/grants`, { headers: { Cookie: await sessionCookie() } }),
      grantEnv,
    )
    expect(listed.headers.get('Cache-Control')).toBe('no-store')
    const { grants } = (await listed.json()) as { grants: Array<Record<string, unknown>> }
    expect(grants).toEqual([
      expect.objectContaining({
        id: grantId,
        clientId,
        clientName: 'Codex',
        scopes: [BACKROOM_READ_SCOPE],
        requestedScopes: [BACKROOM_READ_SCOPE],
        accessLevel: 'read-only',
      }),
    ])

    const revokeRequest = async (cookie: string, headers: Record<string, string> = {}) =>
      revokeMcpGrant(
        new Request(`${origin}/oauth/grants/revoke`, {
          method: 'POST',
          headers: { Origin: origin, 'Content-Type': 'application/json', Cookie: cookie, ...headers },
          body: JSON.stringify({ grantId }),
        }),
        grantEnv,
      )

    // Cross-origin and other operators cannot revoke this grant.
    expect(
      (await revokeRequest(await sessionCookie(), { Origin: 'https://evil.example' })).status,
    ).toBe(403)
    expect(
      (await revokeRequest(await sessionCookie({ uid: 'staff-2', email: 'alan@omniaura.ai' })))
        .status,
    ).toBe(200)
    expect(await h.oauth.unwrapToken(connection.access_token)).not.toBeNull()

    const revoked = await revokeRequest(await sessionCookie())
    expect(revoked.status).toBe(200)
    expect(await h.oauth.unwrapToken(connection.access_token)).toBeNull()
    expect((await refresh(h, clientId, connection.refresh_token)).status).toBe(400)
    expect((await h.oauth.listUserGrants(session.uid)).items).toEqual([])
  })

  it('clamps the access token to the session and refuses a near-expiry exchange', async () => {
    const h = harness()
    const clientId = await registerClient(h)

    // 90 seconds left: the token must die with the session, not round up.
    const shortLived = await connect(h, clientId, BACKROOM_READ_SCOPE, 'read', {
      expiresAt: Date.now() + 90_000,
    })
    expect(shortLived.expires_in).toBeLessThanOrEqual(90)
    expect(shortLived.expires_in).toBeGreaterThan(80)

    // Under the 60-second Workers KV floor the token cannot be made to expire
    // with the session, so the exchange is refused instead.
    const aboutToExpire = await authorize(h, clientId, BACKROOM_READ_SCOPE, 'read', {
      expiresAt: Date.now() + 1_000,
    })
    const refused = await exchangeCode(h, clientId, aboutToExpire.code, aboutToExpire.verifier)
    expect(refused.status).toBe(400)
    await expect(refused.json()).resolves.toMatchObject({ error: 'invalid_grant' })

    // A full-length session still gets the 24-hour ceiling.
    const normal = await connect(h, clientId, BACKROOM_READ_SCOPE, 'read')
    expect(normal.expires_in).toBe(24 * 60 * 60)
    const issued = await tokenProps(h, normal.access_token)
    const expiresAt = Date.parse(issued.grant.props.accessExpiresAt ?? '')
    expect(expiresAt).toBeGreaterThan(Date.now() + 23 * 60 * 60 * 1000)
    expect(expiresAt).toBeLessThanOrEqual(Date.now() + 24 * 60 * 60 * 1000 + 5_000)
    expect(await backroomAccess(issued.grant.props)).toMatchObject({
      expires_at: issued.grant.props.accessExpiresAt,
    })
  })

  it('refuses every MCP call once the authorizing staff session expired', async () => {
    const h = harness()
    const clientId = await registerClient(h)
    const expiresAt = Date.now() + 120_000
    const connection = await connect(h, clientId, FULL.join(' '), 'full', { expiresAt })
    const props = (await tokenProps(h, connection.access_token)).grant.props
    const handler = new BackroomMcpHandler(
      { props } as unknown as ExecutionContext & { props: McpGrantProps },
      h.env,
    )
    const call = (tool: string) =>
      handler.fetch(
        new Request(`${origin}/mcp`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json, text/event-stream',
          },
          body: JSON.stringify({
            jsonrpc: '2.0',
            id: 1,
            method: 'tools/call',
            params: { name: tool, arguments: {} },
          }),
        }),
      )

    const live = await call('get_backroom_access')
    expect(live.status).toBe(200)

    vi.useFakeTimers({ toFake: ['Date'] })
    try {
      vi.setSystemTime(expiresAt + 1_000)
      for (const tool of [
        'get_backroom_access',
        'get_screening_quarantine_artifact',
        'resolve_screening_quarantine',
      ]) {
        const response = await call(tool)
        expect(response.status).toBe(401)
        expect(response.headers.get('WWW-Authenticate')).toContain('error="invalid_token"')
        await expect(response.json()).resolves.toMatchObject({ error: 'invalid_token' })
      }
    } finally {
      vi.useRealTimers()
    }
  })

  it('refuses grant listing to a blocked operator', async () => {
    const h = harness()
    const listed = await listMcpGrants(
      new Request(`${origin}/oauth/grants`, { headers: { Cookie: await sessionCookie() } }),
      { ...h.env, BACKROOM_BLOCKED_EMAILS: 'peyton@omniaura.ai', OAUTH_PROVIDER: h.oauth },
    )
    expect(listed.status).toBe(401)
  })
})
