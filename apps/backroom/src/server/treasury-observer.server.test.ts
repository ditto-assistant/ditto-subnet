import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BackroomMcpHandler } from './mcp-handler.server'
import { createBackroomMcpServer, type BackroomEnv, type McpGrantProps } from './mcp.server'
import { BACKROOM_TREASURY_OBSERVE_SCOPE as OBSERVE, observerGrant, observerRequestAllowed } from './treasury-observer-access.server'

const props: McpGrantProps = {
  clientName: 'Observer', scopes: [OBSERVE],
  session: { version: 2, uid: 'staff', email: 'peyton@omniaura.ai', name: 'Staff', picture: '',
    accessLevel: 'write', issuedAt: Date.now(), expiresAt: Date.now() + 120_000 },
}
const env = { BACKROOM_ADMIN_EMAILS: props.session.email } as BackroomEnv
const original = process.env.DITTO_ADMIN_API_TOKEN
const selection = {
  stage: 'vendor_payment', epoch_index: 9, bucket_id: 'gamma', source_block: null,
  block: 130, block_hash: '0x' + 'ab'.repeat(32), extrinsic_index: 0,
  extrinsic_hash: '0x' + 'cd'.repeat(32), amount_atomic: 25,
  payee_rule_id: 'vendor', parent_receipt_id: null, reason: 'Observe finalized vendor payment',
  confirmation: 'INGEST VERIFIED TREASURY RECEIPT',
}

function request(message: unknown, method = 'POST') {
  return new Request('https://backroom.dittobench.ai/mcp', { method,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
    ...(method === 'POST' ? { body: JSON.stringify(message) } : {}),
  })
}
function call(name: string, args: Record<string, unknown> = {}) {
  return { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }
}
function handler(grant = props, bindings = env) {
  return new BackroomMcpHandler({ props: grant } as unknown as ExecutionContext & { props: McpGrantProps }, bindings)
}
async function connect() {
  const server = createBackroomMcpServer(props)
  const client = new Client({ name: 'observer-test', version: '1' })
  const [a, b] = InMemoryTransport.createLinkedPair()
  await Promise.all([client.connect(a), server.connect(b)])
  return { server, client }
}
afterEach(() => {
  vi.unstubAllGlobals()
  if (original === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = original
})

describe('Dedicated receipt OAuth protocol authority', () => {
  it('exposes exactly two SDK tools and strips unrelated history/audit response fields', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'test-token'
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      revision: 1, checksum: 'a'.repeat(64), settings: { future: 'checksum-preserved' },
      actor: 'PRIVATE', reason: 'PRIVATE', effective: { secret: 'PRIVATE' },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect()
    try {
      expect((await client.listTools()).tools.map(t => t.name).sort()).toEqual(['get_treasury_settings', 'record_treasury_receipt'])
      const missing = await client.callTool({ name: 'get_treasury_settings', arguments: {} })
      expect(missing.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
      const result = await client.callTool({ name: 'get_treasury_settings', arguments: { revision: 1 } })
      expect(result.structuredContent).toEqual({ history: [{ revision: 1, checksum: 'a'.repeat(64), settings: { future: 'checksum-preserved' } }] })
      expect(JSON.stringify(result)).not.toContain('PRIVATE')
      expect(fetchMock.mock.calls[0][0]).toContain('/treasury-settings/revisions/1')
      for (const name of ['get_backroom_access', 'get_treasury_receipts', 'record_treasury_settings', 'resolve_screening_quarantine', 'get_screening_quarantine_artifact']) {
        const refused = await client.callTool({ name, arguments: {} })
        expect(refused.isError).toBe(true)
      }
      expect(fetchMock).toHaveBeenCalledTimes(1)
    } finally { await client.close(); await server.close() }
  })

  it('permits only exact confirmed safe-integer ingestion with the signed actor', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'test-token'
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ ...selection,
      receipt_id: 'a'.repeat(64), policy_digest: 'b'.repeat(64), amount_atomic: '25',
      status: 'chain_finalized', public_event_id: null, published: false, replayed: false,
      provider_credit_status: 'not_proven',
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect()
    try {
      for (const bad of [{ ...selection, confirmation: 'WRONG' }, { ...selection, amount_atomic: Number.MAX_SAFE_INTEGER + 1 }, { ...selection, stage: 'provider_credit' }]) {
        expect((await client.callTool({ name: 'record_treasury_receipt', arguments: bad })).isError).toBe(true)
      }
      expect(fetchMock).not.toHaveBeenCalled()
      const result = await client.callTool({ name: 'record_treasury_receipt', arguments: { ...selection, actor: 'FORGED' } })
      expect(result.isError).not.toBe(true)
      const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
      expect(init.headers).toMatchObject({ 'X-Admin-Actor': props.session.email })
      expect(String(init.body)).not.toContain('FORGED')
      expect(String(init.body)).not.toContain('confirmation')
    } finally { await client.close(); await server.close() }
  })

  it('refuses a substituted historical revision instead of following latest policy', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'test-token'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({
      revision: 2, checksum: 'a'.repeat(64), settings: {},
    })))
    const { client, server } = await connect()
    try {
      const result = await client.callTool({ name: 'get_treasury_settings', arguments: { revision: 1 } })
      expect(result.isError).toBe(true)
      expect(result.structuredContent).toBeUndefined()
    } finally { await client.close(); await server.close() }
  })

  it('refuses direct protocol escapes, batches, downloads and oversized bodies before Platform', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const messages = [
      call('record_treasury_settings'), call('get_backroom_access'), call('get_screening_quarantine_artifact'),
      { jsonrpc: '2.0', id: 1, method: 'resources/list' },
      { jsonrpc: '2.0', id: 1, method: 'resources/read', params: { uri: 'file:///secret' } },
      [call('get_treasury_settings', { revision: 1 })],
      { ...call('get_treasury_settings'), padding: 'x'.repeat(65_536) },
    ]
    for (const message of messages) {
      const response = await handler().fetch(request(message))
      expect(response.status).toBe(403)
      expect(response.headers.get('Cache-Control')).toBe('no-store')
    }
    for (const method of ['GET', 'DELETE']) expect((await handler().fetch(request({}, method))).status).toBe(403)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rechecks live entitlement/session and refuses mixed grants even for direct SDK creation', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    for (const bindings of [{}, { BACKROOM_BLOCKED_EMAILS: props.session.email, ...env }]) {
      expect((await handler(props, bindings as BackroomEnv).fetch(request(call('get_treasury_settings', { revision: 1 })))).status).toBe(403)
    }
    expect((await handler({ ...props, session: { ...props.session, expiresAt: 0 } }).fetch(request(call('get_treasury_settings')))).status).toBe(401)
    const mixed = { ...props, scopes: [OBSERVE, 'backroom:read'] }
    expect((await handler(mixed).fetch(request(call('get_treasury_settings')))).status).toBe(403)
    expect(() => createBackroomMcpServer(mixed)).toThrow('mixed')
    expect(() => createBackroomMcpServer({ ...props, session: { ...props.session, accessLevel: 'read' } })).toThrow('live write-level')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('accepts only the bounded stateless protocol surface', async () => {
    expect(observerGrant(['backroom:read'])).toBe(false)
    expect(() => observerGrant([OBSERVE, 'backroom:write'])).toThrow()
    for (const method of ['initialize', 'notifications/initialized', 'ping', 'tools/list']) {
      expect(await observerRequestAllowed(request({ jsonrpc: '2.0', id: 1, method }))).toBe(true)
    }
    expect(await observerRequestAllowed(request(call('record_treasury_receipt', selection)))).toBe(true)
    expect(await observerRequestAllowed(new Request('https://backroom.test/mcp', { method: 'POST', body: 'not json' }))).toBe(false)
  })
})
