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

async function connect(scopes: Array<string>) {
  const props: McpGrantProps = { session, scopes, clientName: 'Test client' }
  const server = createBackroomMcpServer(props)
  const client = new Client({ name: 'generator-test', version: '1.0.0' })
  const [a, b] = InMemoryTransport.createLinkedPair()
  await Promise.all([server.connect(b), client.connect(a)])
  return { client, server }
}

function json(response: unknown): unknown {
  const content = (response as { content: Array<{ type: string; text: string }> }).content
  return JSON.parse(content[0].text)
}

const SUBMISSION = '3b2d6a54-0f4c-4b3e-9a6e-2f2d0c1b9e11'
const RUN = '7d0a4c39-4c1f-4a5b-8f3e-9b1d2e3f4a5b'
const SEED = 'ab'.repeat(32)
const settings = {
  mode: 'shadow', panel_size: 5, min_panel: 3, min_solver_owners: 2, solve_threshold: 0.5,
  champion_bonus: 0.5, seeds_per_run: 2, cases_per_seed: 16, rescore_interval_hours: 24,
  window_days: 7, copy_overlap_threshold: 0.5, upload_cooldown_seconds: 3600,
  lease_ttl_seconds: 7200, max_attempts: 6, retry_backoff_seconds: 3600,
}
const runSummary = {
  run_id: RUN, attempt: 1, status: 'scored', outcome: 'scored', score: 0.42,
  case_counts: { discriminating: 30, trivial: 2, unsolved: 0, duplicate: 0 },
  leased_at: '2026-10-23T10:00:00Z', finished_at: '2026-10-23T10:05:00Z',
}
const submission = {
  submission_id: SUBMISSION, miner_hotkey: '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
  source_sha256: 'cd'.repeat(32), tree_sha256: 'ef'.repeat(32), module: 'example.com/gen',
  go_version: '1.27', file_count: 30, size_bytes: 120_000, status: 'active', status_reason: null,
  next_run_at: '2026-10-24T10:05:00Z', submitted_at: '2026-10-23T09:00:00Z', latest_run: runSummary,
}
const detail = {
  summary: submission, owner_key: 'coldkey:abc', attempts: 0, lease_run_id: null,
  lease_expires_at: null, runs: [runSummary],
}

afterEach(() => {
  vi.unstubAllGlobals()
  if (original === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = original
})

describe('generator competition MCP tools', () => {
  it('reads settings, submissions, runs, probe and board from Platform', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const routes: Record<string, unknown> = {
      '/api/v1/admin/generator-competition/settings': {
        current: [], history: [], default: { ...settings, mode: 'off' },
        effective: { revision: 0, settings: { ...settings, mode: 'off' }, checksum: '', source: 'default' },
      },
      '/api/v1/admin/generator-competition/submissions?limit=50&offset=0&status=active': {
        submissions: [{ ...submission, source_base64: 'must-not-pass' }], counts_by_status: { active: 1 },
      },
      [`/api/v1/admin/generator-competition/submissions/${SUBMISSION}`]: detail,
      [`/api/v1/admin/generator-competition/runs/${RUN}`]: {
        summary: runSummary, submission_id: SUBMISSION, miner_hotkey: submission.miner_hotkey,
        seeds: [SEED, 'cd'.repeat(32)], probe_epoch: 1, cases_per_seed: 16, settings_revision: 1,
        scorer_version: 'challenge-scorer/2', panel: [{ agent_id: 'a', owner_key: 'o', champion: true }],
        evaluation: { deterministic: true }, score_summary: { discriminating: 30 }, verdicts: [],
      },
      '/api/v1/admin/generator-competition/probe': {
        current: { epoch: 2, reason: 'leak', actor: 'ops', created_at: 'x', retired_at: null, probed_submissions: 3, seed: 'secret' },
        history: [],
      },
      '/api/v1/generator/board': {
        entries: [], count: 0, window_days: 7, mode: 'shadow', generated_at: '2026-10-23T10:00:00Z',
      },
    }
    const fetchMock = vi.fn(async (url: string) => {
      const path = url.replace('https://platform-api.heyditto.ai', '')
      if (!(path in routes)) return new Response('not found', { status: 404 })
      return Response.json(routes[path])
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    try {
      const control = json(await client.callTool({ name: 'get_generator_competition_settings', arguments: {} })) as {
        effective: { settings: { mode: string } }
      }
      expect(control.effective.settings.mode).toBe('off')
      const listing = json(await client.callTool({ name: 'list_generator_submissions', arguments: { status: 'active' } }))
      expect(JSON.stringify(listing)).not.toContain('must-not-pass')
      expect(json(await client.callTool({ name: 'get_generator_submission', arguments: { submissionId: SUBMISSION } })))
        .toMatchObject({ owner_key: 'coldkey:abc', runs: [{ run_id: RUN }] })
      expect(json(await client.callTool({ name: 'get_generator_run', arguments: { runId: RUN } })))
        .toMatchObject({ seeds: [SEED, 'cd'.repeat(32)], evaluation: { deterministic: true } })
      const probe = json(await client.callTool({ name: 'get_generator_probe', arguments: {} }))
      expect(JSON.stringify(probe)).not.toContain('secret')
      expect(json(await client.callTool({ name: 'get_generator_board', arguments: {} }))).toMatchObject({ mode: 'shadow' })
      const bad = await client.callTool({ name: 'get_generator_run', arguments: { runId: 'not-a-uuid' } })
      expect(bad.isError).toBe(true)
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('guards writes with write scope, confirmations and the operator actor', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/settings') && (init?.method ?? 'GET') === 'GET') {
        return Response.json({
          current: [], history: [], default: settings,
          effective: { revision: 1, settings, checksum: 'a'.repeat(64), source: 'revision' },
        })
      }
      if (url.endsWith('/probe/rotate')) {
        return Response.json({ current: null, history: [] })
      }
      return Response.json(detail)
    })
    vi.stubGlobal('fetch', fetchMock)

    const readOnly = await connect([BACKROOM_READ_SCOPE])
    try {
      const refused = await readOnly.client.callTool({
        name: 'rescore_generator_submission', arguments: { submissionId: SUBMISSION, reason: 'panel changed today' },
      })
      expect(refused.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      await readOnly.client.close()
      await readOnly.server.close()
    }

    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    try {
      const wrongPhrase = await client.callTool({
        name: 'set_generator_competition_settings',
        arguments: { expectedRevision: 1, settings, reason: 'open shadow lane', confirmation: 'APPLY GENERATOR COMPETITION OFF' },
      })
      expect(wrongPhrase.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()

      await client.callTool({
        name: 'set_generator_competition_settings',
        arguments: { expectedRevision: 1, settings, reason: 'open shadow lane', confirmation: 'APPLY GENERATOR COMPETITION SHADOW' },
      })
      const [settingsUrl, settingsInit] = fetchMock.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }]
      expect(settingsUrl).toContain('/api/v1/admin/generator-competition/settings')
      expect(JSON.parse(String(settingsInit.body))).toMatchObject({
        expected_revision: 1, scope: '*', actor: session.email, confirmation: 'APPLY GENERATOR COMPETITION SHADOW',
      })
      expect(settingsInit.headers['X-Admin-Actor']).toBe(session.email)

      fetchMock.mockClear()
      await client.callTool({
        name: 'reject_generator_submission',
        arguments: { submissionId: SUBMISSION, expectedStatus: 'active', reason: 'copy of another miner', confirmation: 'REJECT GENERATOR SUBMISSION' },
      })
      const [rejectUrl, rejectInit] = fetchMock.mock.calls[0] as [string, RequestInit]
      expect(rejectUrl).toContain(`/submissions/${SUBMISSION}/reject`)
      expect(JSON.parse(String(rejectInit.body))).toMatchObject({
        expected_status: 'active', confirmation: 'REJECT GENERATOR SUBMISSION', actor: session.email,
      })

      fetchMock.mockClear()
      const unconfirmed = await client.callTool({
        name: 'rotate_generator_probe_seed', arguments: { reason: 'suspected leak', confirmation: 'rotate' },
      })
      expect(unconfirmed.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
      await client.callTool({
        name: 'rotate_generator_probe_seed', arguments: { reason: 'suspected leak', confirmation: 'ROTATE GENERATOR PROBE SEED' },
      })
      expect(fetchMock.mock.calls[0][0]).toContain('/probe/rotate')
    } finally {
      await client.close()
      await server.close()
    }
  })
})
