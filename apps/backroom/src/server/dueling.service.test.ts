import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  createBackroomMcpServer,
  type McpGrantProps,
} from './mcp.server'

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
const SEASON = '5f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b'
const PREP = '6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'

async function connect(scopes: Array<string>) {
  const props: McpGrantProps = { session, scopes, clientName: 'Test client' }
  const server = createBackroomMcpServer(props)
  const client = new Client({ name: 'dueling-test', version: '1.0.0' })
  const [a, b] = InMemoryTransport.createLinkedPair()
  await Promise.all([server.connect(b), client.connect(a)])
  return { client, server }
}

function json(response: unknown): unknown {
  const content = (response as { content: Array<{ type: string; text: string }> }).content
  return JSON.parse(content[0].text)
}

const season = {
  id: SEASON, bench_version: 16, season_index: 26119, shadow: true, status: 'open',
  commitment: 'ab'.repeat(32), committed_at: '2026-11-03T00:00:00Z', rounds_per_season: 1,
  settings_revision: 12, anchor_block: 100, anchor_block_hash: '0x' + 'cd'.repeat(32),
  opened_at: '2026-11-03T01:00:00Z', closes_at_round: 26120, closed_at: null,
  reveal_at_epoch: null, revealed_at: null, void_reason: null, rotation_requested_at: null,
  rotation_immediate: null, rotation_actor: null, rotation_reason: null,
  columns: [{ column_id: 'c0', owner_key: '' }], fallback: null, detail: null,
  secret_hex: null, season_seed: null, batteries: [], column_means: [], slices: { ready: 2 },
}

describe('dueling season tools', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    if (original === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
    else process.env.DITTO_ADMIN_API_TOKEN = original
  })

  it('reads seasons and slices and guards the rotation', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes('/dueling/slices/') && url.endsWith('/retry') && init?.method === 'POST') {
        return Response.json({
          id: PREP, season_id: SEASON, season_index: 26119, bench_version: 16, run_seed: '7',
          status: 'pending', attempts: 3, slice_sha256: null, size_bytes: null, bytes_pruned: false,
          columns: null, error: null, scorer_version: null, created_at: '2026-11-03T00:00:00Z',
          first_leased_at: null, ready_at: null, failed_at: null, latency_seconds: null,
          retries: 1, last_retry_at: '2026-11-03T03:00:00Z', last_retry_actor: session.email,
          last_retry_reason: 'builder host replaced', retryable: false, retry_refusal: null,
        })
      }
      if (url.includes('/dueling/seasons')) {
        return Response.json(url.endsWith('/seasons') ? {
          target: { bench_version: 16, shadow: true, blocked: null, live_versions: [16] },
          current: [season], next: [], recent: [],
        } : season)
      }
      if (url.includes('/dueling/source-releases')) {
        return Response.json({ items: [], counts: { released: 1, embargoed: 2 } })
      }
      if (url.includes('/dueling/slices')) {
        return Response.json({ slices: [], counts: { ready: 2 }, latency_p50_seconds: 12, latency_p95_seconds: 40 })
      }
      if (url.endsWith('/dueling/rotate') && init?.method === 'POST') {
        return Response.json({ ...season, rotation_requested_at: '2026-11-03T02:00:00Z', rotation_actor: session.email, rotation_reason: 'column under the floor', rotation_immediate: false })
      }
      return new Response('not found', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    try {
      const board = json(await client.callTool({ name: 'get_dueling_season', arguments: {} })) as {
        target: { bench_version: number }, current: Array<{ secret_hex: string | null }>
      }
      expect(board.target.bench_version).toBe(16)
      expect(board.current[0].secret_hex).toBeNull()
      expect(json(await client.callTool({ name: 'get_dueling_season', arguments: { seasonId: SEASON } })))
        .toMatchObject({ id: SEASON, slices: { ready: 2 } })
      expect(json(await client.callTool({ name: 'list_dueling_slices', arguments: { seasonId: SEASON, status: 'ready' } })))
        .toMatchObject({ counts: { ready: 2 } })
      expect(json(await client.callTool({ name: 'list_generator_source_releases', arguments: { status: 'embargoed' } })))
        .toMatchObject({ counts: { released: 1, embargoed: 2 } })
      const [releasesUrl] = fetchMock.mock.calls.find(([url]) => String(url).includes('/source-releases')) as [string]
      expect(releasesUrl).toContain('status=embargoed')
      const [slicesUrl] = fetchMock.mock.calls.find(([url]) => String(url).includes('/slices')) as [string]
      expect(slicesUrl).toContain(`season_id=${SEASON}`)
      expect(slicesUrl).toContain('status=ready')

      fetchMock.mockClear()
      const wrong = await client.callTool({
        name: 'rotate_dueling_season',
        arguments: { reason: 'column under the floor', confirmation: 'ROTATE' },
      })
      expect(wrong.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
      const rotated = json(await client.callTool({
        name: 'rotate_dueling_season',
        arguments: { reason: 'column under the floor', confirmation: 'ROTATE DUELING SEASON' },
      })) as { rotation_actor: string }
      expect(rotated.rotation_actor).toBe(session.email)
      const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
      expect(JSON.parse(String(init.body))).toEqual({
        immediate: false, reason: 'column under the floor', actor: session.email,
        confirmation: 'ROTATE DUELING SEASON',
      })

      fetchMock.mockClear()
      const badRetry = await client.callTool({
        name: 'retry_dueling_slice_preparation',
        arguments: {
          preparationId: PREP, expectedStatus: 'failed', expectedError: 'c0: build failed',
          reason: 'builder host replaced', confirmation: 'RETRY DUELING SLICE',
        },
      })
      expect(badRetry.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
      const retried = json(await client.callTool({
        name: 'retry_dueling_slice_preparation',
        arguments: {
          preparationId: PREP, expectedStatus: 'failed', expectedError: 'c0: build failed',
          reason: 'builder host replaced', confirmation: `RETRY DUELING SLICE ${PREP}`,
        },
      })) as { status: string, retries: number }
      expect(retried).toMatchObject({ status: 'pending', retries: 1 })
      const [retryUrl, retryInit] = fetchMock.mock.calls[0] as [string, RequestInit]
      expect(retryUrl).toContain(`/dueling/slices/${PREP}/retry`)
      expect(JSON.parse(String(retryInit.body))).toEqual({
        expected_status: 'failed', expected_error: 'c0: build failed',
        reason: 'builder host replaced', actor: session.email,
        confirmation: `RETRY DUELING SLICE ${PREP}`, force: false,
      })
    } finally {
      await client.close()
      await server.close()
    }

    const readOnly = await connect([BACKROOM_READ_SCOPE])
    try {
      const refused = await readOnly.client.callTool({
        name: 'rotate_dueling_season',
        arguments: { reason: 'column under the floor', confirmation: 'ROTATE DUELING SEASON' },
      })
      expect(refused.isError).toBe(true)
    } finally {
      await readOnly.client.close()
      await readOnly.server.close()
    }
  })

  it('shows the on-chain commitment and guards its republish', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const committed = {
      ...season, status: 'committed', opened_at: null, anchor_block: null, anchor_block_hash: null,
      commitment_publication: 'failed', commitment_block: null, commitment_extrinsic_hash: null,
      commitment_error: 'pylon outage', commitment_publications: [{
        validator_hotkey: '5Validator', status: 'failed', claimed_at: '2026-11-03T00:00:00Z',
        claim_expires_at: '2026-11-03T00:10:00Z', reported_at: '2026-11-03T00:01:00Z',
        block_number: null, extrinsic_hash: null, detail: 'rate limited',
      }],
      commitment_publishers_elected: [],
    }
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith(`/seasons/${SEASON}/republish-commitment`) && init?.method === 'POST') {
        return Response.json({
          ...committed, commitment_publication: 'pending', commitment_error: null,
          commitment_publications: [], commitment_publishers_elected: ['5Other'],
        })
      }
      if (url.endsWith(`/seasons/${SEASON}`)) return Response.json(committed)
      return new Response('not found', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    try {
      expect(json(await client.callTool({ name: 'get_dueling_season', arguments: { seasonId: SEASON } })))
        .toMatchObject({
          commitment_publication: 'failed', commitment_error: 'pylon outage',
          commitment_publications: [{ status: 'failed', detail: 'rate limited' }],
        })

      fetchMock.mockClear()
      const wrong = await client.callTool({
        name: 'republish_dueling_season_commitment',
        arguments: { seasonId: SEASON, reason: 'pylon outage recovered', confirmation: 'REPUBLISH' },
      })
      expect(wrong.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
      const reopened = json(await client.callTool({
        name: 'republish_dueling_season_commitment',
        arguments: {
          seasonId: SEASON, reason: 'pylon outage recovered',
          confirmation: 'REPUBLISH DUELING SEASON COMMITMENT',
        },
      })) as { commitment_publication: string, commitment_publishers_elected: Array<string> }
      expect(reopened).toMatchObject({
        commitment_publication: 'pending', commitment_publishers_elected: ['5Other'],
      })
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
      expect(url).toContain(`/dueling/seasons/${SEASON}/republish-commitment`)
      expect(JSON.parse(String(init.body))).toEqual({
        reason: 'pylon outage recovered', actor: session.email,
        confirmation: 'REPUBLISH DUELING SEASON COMMITMENT',
      })
    } finally {
      await client.close()
      await server.close()
    }
  })
})
