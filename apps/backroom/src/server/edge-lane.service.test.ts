import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BACKROOM_READ_SCOPE, createBackroomMcpServer, type McpGrantProps } from './mcp.server'

const session = {
  version: 2 as const,
  uid: 'staff-1',
  email: 'staff@omniaura.ai',
  name: 'Staff User',
  picture: '',
  accessLevel: 'write' as const,
  issuedAt: Date.now(),
  expiresAt: Date.now() + 7 * 24 * 60 * 60_000,
}

const original = process.env.DITTO_ADMIN_API_TOKEN
const NODE = `edn_${'a'.repeat(32)}`

async function connect(scopes: Array<string>) {
  const props: McpGrantProps = { session, scopes, clientName: 'Test client' }
  const server = createBackroomMcpServer(props)
  const client = new Client({ name: 'edge-lane-test', version: '1.0.0' })
  const [a, b] = InMemoryTransport.createLinkedPair()
  await Promise.all([server.connect(b), client.connect(a)])
  return { client, server }
}

function json(response: unknown): Record<string, unknown> {
  const content = (response as { content: Array<{ type: string; text: string }> }).content
  return JSON.parse(content[0].text) as Record<string, unknown>
}

const settings = {
  mode: 'shadow',
  max_nodes: 100,
  replicas: 3,
  max_object_bytes: 8 * 1024 * 1024,
  max_total_object_bytes: 1024 ** 3,
  node_quota_cap_bytes: 2 * 1024 ** 3,
  lease_seconds: 900,
  poll_interval_seconds: 30,
  challenge_deadline_seconds: 60,
  window_seconds: 3600,
  challenges_per_window: 6,
  retrieval_share: 0.2,
  revoked_share: 0.15,
  min_challenges: 3,
  availability_floor: 0.8,
  max_new_assignments_per_poll: 16,
  revoked_lookback_seconds: 86_400,
}

const revision = (number: number, createdAt: string) => ({
  revision: number,
  parent_revision: number - 1,
  scope: '*',
  settings,
  reason: 'open the SN-481 shadow lane for team Macs',
  actor: 'operator@example.com',
  created_at: createdAt,
  checksum: 'c'.repeat(64),
})

const ledgerRow = {
  window_index: 490_000,
  node_id: NODE,
  operator: NODE,
  scheduled: 6,
  passed: 6,
  failed: 0,
  missed: 0,
  refilled: 0,
  refused: 0,
  revocation_checks: 1,
  revocation_violations: 0,
  held_gib_hours: 0.02,
  availability: 1,
  eligible: true,
  verified_units: 0.02,
  hypothetical_share: 1,
  reasons: [],
  settings_revision: 2,
  weight_eligible: false,
  computed_at: '2026-11-13T02:05:00Z',
}

const responses: Record<string, unknown> = {
  '/api/v1/admin/edge-lane-settings': {
    current: [revision(2, '2026-11-13T01:00:00Z')],
    history: [revision(1, '2026-11-12T01:00:00Z'), revision(2, '2026-11-13T01:00:00Z')],
    default: { ...settings, mode: 'off' },
    effective: {
      revision: 2,
      scope: '*',
      settings,
      checksum: 'c'.repeat(64),
      source: 'revision',
      max_age_seconds: 5,
      weight_eligible: false,
    },
  },
  '/api/v1/admin/edge/overview': {
    mode: 'shadow',
    settings_revision: 2,
    weight_eligible: false,
    payouts: 'none',
    nodes_active: 3,
    nodes_live: 2,
    nodes_suspended: 0,
    nodes_retired: 1,
    objects_active: 24,
    objects_revoked: 4,
    active_object_bytes: 4_603_088,
    open_assignments: 72,
    held_assignments: 70,
    current_window_index: 490_001,
    recent_windows: [
      {
        window_index: 490_000,
        settings_revision: 2,
        node_count: 3,
        eligible_nodes: 3,
        verified_units: 0.06,
        closed_at: '2026-11-13T02:05:00Z',
      },
    ],
  },
  '/api/v1/admin/edge/nodes': {
    nodes: [
      {
        node_id: NODE,
        operator: NODE,
        hotkey_ss58: null,
        client_name: 'ditto-desktop',
        client_version: '1.0.0',
        platform: { os: 'Darwin', arch: 'arm64', memory_gb: 16 },
        status: 'active',
        quota_bytes: 2 * 1024 ** 3,
        enrolled_at: '2026-11-13T00:00:00Z',
        last_poll_at: '2026-11-13T02:10:00Z',
        held_objects: 24,
        held_bytes: 4_603_088,
        latest_window: ledgerRow,
      },
    ],
  },
  '/api/v1/admin/edge/ledger': { rows: [ledgerRow], closed_now: [490_000] },
}

function stubPlatform(overrides: Record<string, unknown> = {}) {
  process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    expect(init?.body).toBeUndefined()
    const path = new URL(url).pathname
    return Response.json({ ...responses, ...overrides }[path] ?? { detail: 'not found' }, {
      status: path in responses ? 200 : 404,
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('Ditto Edge shadow lane tools', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    if (original === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
    else process.env.DITTO_ADMIN_API_TOKEN = original
  })

  it('reads the lane with read scope only and annotates every tool read-only', async () => {
    const fetchMock = stubPlatform()
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    try {
      const tools = (await client.listTools()).tools.filter((tool) =>
        tool.name.includes('edge_lane'),
      )
      expect(tools.map((tool) => tool.name).sort()).toEqual([
        'get_edge_lane_ledger',
        'get_edge_lane_overview',
        'get_edge_lane_settings',
        'list_edge_lane_nodes',
      ])
      expect(tools.every((tool) => tool.annotations?.readOnlyHint === true)).toBe(true)

      const policy = json(await client.callTool({ name: 'get_edge_lane_settings', arguments: {} }))
      expect(policy.effective).toMatchObject({ revision: 2, weight_eligible: false })
      // History stays opt-in, like every other settings read.
      expect(policy.history).toEqual([])
      expect(policy.history_count).toBe(2)
      const paged = json(
        await client.callTool({ name: 'get_edge_lane_settings', arguments: { historyLimit: 1 } }),
      )
      expect((paged.history as Array<{ revision: number }>)[0].revision).toBe(2)

      const overview = json(await client.callTool({ name: 'get_edge_lane_overview', arguments: {} }))
      expect(overview).toMatchObject({ weight_eligible: false, payouts: 'none', nodes_live: 2 })

      const nodes = json(
        await client.callTool({ name: 'list_edge_lane_nodes', arguments: { limit: 1 } }),
      )
      expect((nodes.nodes as Array<{ node_id: string }>)[0].node_id).toBe(NODE)
      expect(nodes).toMatchObject({
        count: 1,
        returned: 1,
        limit: 1,
        offset: 0,
        has_more: false,
        may_be_truncated: false,
      })

      const ledger = json(
        await client.callTool({
          name: 'get_edge_lane_ledger',
          arguments: { windowIndex: 490_000, nodeId: NODE },
        }),
      )
      expect(ledger.closed_now).toEqual([490_000])

      const urls = fetchMock.mock.calls.map(([url]) => String(url))
      expect(urls).toContain('https://platform-api.heyditto.ai/api/v1/admin/edge-lane-settings')
      expect(urls).toContain('https://platform-api.heyditto.ai/api/v1/admin/edge/overview')
      expect(urls).toContain('https://platform-api.heyditto.ai/api/v1/admin/edge/nodes?limit=1000')
      expect(urls).toContain(
        `https://platform-api.heyditto.ai/api/v1/admin/edge/ledger?limit=200&window_index=490000&node_id=${NODE}`,
      )
      // Reads only: Backroom never writes to the lane.
      expect(fetchMock.mock.calls.every(([, init]) => (init?.method ?? 'GET') === 'GET')).toBe(true)
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('says a node listing at the platform cap may be cut short', async () => {
    const node = (responses['/api/v1/admin/edge/nodes'] as { nodes: Array<Record<string, unknown>> })
      .nodes[0]
    stubPlatform({
      '/api/v1/admin/edge/nodes': {
        nodes: Array.from({ length: 1000 }, (_, index) => ({
          ...node,
          node_id: `edn_${index.toString(16).padStart(32, '0')}`,
        })),
      },
    })
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    try {
      const last = json(
        await client.callTool({ name: 'list_edge_lane_nodes', arguments: { limit: 200, offset: 800 } }),
      )
      // The last local page ends the window, but not necessarily the lane.
      expect(last).toMatchObject({
        count: 1000,
        has_more: false,
        platform_cap: 1000,
        may_be_truncated: true,
      })
      // Fields every row shares move to nodes_shared; each row keeps its id.
      expect(last.nodes_shared).toMatchObject({ status: 'active', client_name: 'ditto-desktop' })
      const rows = last.nodes as Array<Record<string, unknown>>
      expect(rows).toHaveLength(200)
      expect(rows[0]).toEqual({ node_id: `edn_${(800).toString(16).padStart(32, '0')}` })
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('refuses a ledger row that claims weight eligibility', async () => {
    stubPlatform({
      '/api/v1/admin/edge/ledger': {
        rows: [{ ...ledgerRow, weight_eligible: true }],
        closed_now: [],
      },
    })
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    try {
      const response = await client.callTool({ name: 'get_edge_lane_ledger', arguments: {} })
      // The lane is shadow-only; a row saying otherwise is a bug to surface, not show.
      expect(response.isError).toBe(true)
    } finally {
      await client.close()
      await server.close()
    }
  })
})
