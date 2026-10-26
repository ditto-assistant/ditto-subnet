import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  BACKROOM_ARTIFACT_SCOPE,
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  createBackroomMcpServer,
  type McpGrantProps,
} from './mcp.server'
import {
  generatorCompetitionSettingsSchema,
  generatorRunDetailSchema,
} from '../lib/generator-competition.schemas'

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
  champion_bonus: 0.5, seeds_per_run: 2, cases_per_seed: 16,
  window_days: 7, copy_overlap_threshold: 0.5, upload_cooldown_seconds: 3600,
  lease_ttl_seconds: 7200, max_attempts: 6, retry_backoff_seconds: 3600,
  epochs_per_round: 4, counted_columns: 5, challenger_columns: 1, reference_agent_ids: [],
  privacy_window_rounds: 6, onchain_commitment: true, validator_rounds_enabled: false,
  upload_fee_required: false,
}
// What a Platform that predates the generator upload fee returns.
const { upload_fee_required: _absent, ...olderSettings } = settings
const runSummary = {
  run_id: RUN, attempt: 1, status: 'qualified', outcome: 'qualified', score: null,
  case_counts: null,
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
        current: [], history: [], default: { ...olderSettings, mode: 'off' },
        effective: { revision: 0, settings: { ...olderSettings, mode: 'off' }, checksum: '', source: 'default' },
      },
      '/api/v1/admin/generator-competition/submissions?limit=50&offset=0&status=active': {
        submissions: [{ ...submission, source_base64: 'must-not-pass' }], counts_by_status: { active: 1 },
      },
      [`/api/v1/admin/generator-competition/submissions/${SUBMISSION}`]: detail,
      [`/api/v1/admin/generator-competition/runs/${RUN}`]: {
        summary: runSummary, submission_id: SUBMISSION, miner_hotkey: submission.miner_hotkey,
        seeds: [SEED, 'cd'.repeat(32)], probe_epoch: 1, cases_per_seed: 16, settings_revision: 1,
        scorer_version: 'challenge-scorer/3', panel: [],
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
        effective: { settings: { mode: string, upload_fee_required: boolean } }
      }
      expect(control.effective.settings.mode).toBe('off')
      expect(control.effective.settings.upload_fee_required).toBe(false)
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

  it('refuses a malformed hotkey filter before calling Platform', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    try {
      const bad = await client.callTool({ name: 'list_generator_submissions', arguments: { hotkey: 'not-a-hotkey' } })
      expect(bad.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('accepts cases_per_seed up to the pack contract cap of 80 and no further', () => {
    for (const cases of [8, 70, 80]) {
      expect(generatorCompetitionSettingsSchema.safeParse({ ...settings, cases_per_seed: cases }).success).toBe(true)
    }
    for (const cases of [7, 81]) {
      expect(generatorCompetitionSettingsSchema.safeParse({ ...settings, cases_per_seed: cases }).success).toBe(false)
    }
  })

  it('refuses a run detail that serves seeds before the run reported', () => {
    const run = {
      summary: runSummary, submission_id: SUBMISSION, miner_hotkey: '5'.repeat(48),
      seeds: [SEED], probe_epoch: 1, cases_per_seed: 16, settings_revision: 1,
      scorer_version: null, panel: [], evaluation: null, score_summary: null, verdicts: [],
    }
    expect(generatorRunDetailSchema.safeParse(run).success).toBe(true)
    for (const status of ['leased', 'abandoned']) {
      const unreported = { ...run, summary: { ...runSummary, status } }
      expect(generatorRunDetailSchema.safeParse(unreported).success).toBe(false)
      expect(generatorRunDetailSchema.safeParse({ ...unreported, seeds: null }).success).toBe(true)
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
      if (url.endsWith('/settings')) {
        return Response.json({
          revision: 2, parent_revision: 1, scope: '*', settings, reason: 'open shadow lane',
          actor: session.email, created_at: '2026-10-23T00:00:00Z', checksum: 'b'.repeat(64),
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

      // A revision stores the whole policy: omitting the fee flag is refused.
      const partial = await client.callTool({
        name: 'set_generator_competition_settings',
        arguments: { expectedRevision: 1, settings: olderSettings, reason: 'open shadow lane', confirmation: 'APPLY GENERATOR COMPETITION SHADOW' },
      })
      expect(partial.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()

      const applied = json(await client.callTool({
        name: 'set_generator_competition_settings',
        arguments: { expectedRevision: 1, settings: { ...settings, upload_fee_required: true }, reason: 'open shadow lane', confirmation: 'APPLY GENERATOR COMPETITION SHADOW' },
      })) as { revision: number }
      expect(applied.revision).toBe(2)
      expect(fetchMock).toHaveBeenCalledTimes(1)
      const [settingsUrl, settingsInit] = fetchMock.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }]
      expect(settingsUrl).toContain('/api/v1/admin/generator-competition/settings')
      expect(JSON.parse(String(settingsInit.body))).toMatchObject({
        expected_revision: 1, scope: '*', actor: session.email, confirmation: 'APPLY GENERATOR COMPETITION SHADOW',
        settings: { upload_fee_required: true },
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

  it('drives and debugs rounds: reads, artifact-scoped dataset, guarded writes', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const AGENT = '9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d'
    const PUBLISHER = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'
    const summary = {
      round_index: 7, status: 'running', epochs_per_round: 4, commitment: 'aa'.repeat(32),
      commitment_publication: 'published', committed_at: '2026-10-24T00:00:00Z', column_count: 5,
      panel_size: 3, results: 1, secret_hex: null, seed: null,
    }
    const roundDetail = {
      summary, settings_revision: 2, cases_per_column: 16, seed: null, columns: [], panel: [],
      generation: { columns: [] }, column_case_counts: { c0: 16 }, dataset_bytes: 1024,
      lease_id: null, lease_expires_at: null, lease_attempts: 1, commitment_published_at: null,
      commitment_error: null, attempts: [], results: [], pending_agents: [AGENT], score: null,
      scorer_version: null, standings: [], columns_scored: [],
    }
    const schedule = { mode: 'shadow', epochs_per_round: 4, subnet_epoch_index: 29, current_round: 7 }
    let roundSeed: string | null = null
    const calls: Array<[string, string, unknown]> = []
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.replace('https://platform-api.heyditto.ai', '')
      const method = init?.method ?? 'GET'
      calls.push([method, path, init?.body ? JSON.parse(String(init.body)) : null])
      if (path.startsWith('/api/v1/admin/generator-competition/rounds?')) {
        return Response.json({ schedule, rounds: [summary], counts_by_status: { running: 1 } })
      }
      if (path.endsWith('/rounds/preview')) {
        return Response.json({ schedule, columns: [], panel: [], qualified_generators: 0, awaiting_qualification: 1, blockers: ['validator_rounds_enabled is false'] })
      }
      if (path.endsWith('/rounds/loop')) {
        return Response.json({ available: true, status: { last_tick_at: null, last_success_at: null, last_error: null, last_actions: ['round 8: committed'], ticks: 3 }, schedule })
      }
      if (path.endsWith('/rounds/tick')) return Response.json({ actions: ['round 8: committed'], schedule })
      if (path.endsWith('/rounds/7/dataset')) {
        return Response.json({ round_index: 7, dataset_sha256: 'bb'.repeat(32), seed: 'cc'.repeat(32), size_bytes: 3, dataset_base64: 'e30=' })
      }
      if (path.endsWith('/default-generator/upload-url')) {
        return Response.json({ bucket: 'traces', key: 'generator-default/v1/op/k.tar.gz', url: 'https://s3.example/put', method: 'PUT', content_type: 'application/gzip', expires_in: 300, max_bytes: 4194304 })
      }
      if (path.endsWith('/default-generator')) {
        return Response.json({ submission_id: SUBMISSION, source_sha256: 'dd'.repeat(32), tree_sha256: 'ee'.repeat(32), status: 'active', superseded: [] })
      }
      if (path.endsWith('/rounds/9')) {
        return Response.json({
          ...roundDetail,
          summary: { ...summary, round_index: 9, status: 'committed', commitment_publication: 'pending', commitment_confirmations: 1 },
          commitment_publishers_target: 3,
          commitment_publications: [
            {
              validator_hotkey: PUBLISHER, status: 'confirmed', claimed_at: '2026-10-24T00:00:00Z',
              claim_expires_at: '2026-10-24T00:10:00Z', reported_at: '2026-10-24T00:01:00Z', block_number: 4242,
              extrinsic_hash: null, detail: null,
            },
            {
              validator_hotkey: 'quiet-validator', status: 'expired', claimed_at: '2026-10-24T00:00:00Z',
              claim_expires_at: '2026-10-24T00:10:00Z', reported_at: null, block_number: null,
              extrinsic_hash: null, detail: null,
            },
          ],
          commitment_elected: ['next-validator'],
        })
      }
      return Response.json(
        roundSeed ? { ...roundDetail, seed: roundSeed, summary: { ...summary, seed: roundSeed, secret_hex: 'ff'.repeat(32) } } : roundDetail,
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    const reader = await connect([BACKROOM_READ_SCOPE])
    try {
      expect(json(await reader.client.callTool({ name: 'list_generator_rounds', arguments: {} })))
        .toMatchObject({ rounds: [{ round_index: 7, panel_dropped: 0 }], schedule: { current_round: 7 } })
      // An older Platform without panel drops still parses, with empty drops.
      expect(json(await reader.client.callTool({ name: 'get_generator_round', arguments: { roundIndex: 7 } })))
        .toMatchObject({ pending_agents: [AGENT], column_case_counts: { c0: 16 }, skipped_panel_agents: [] })
      // Validator-published commitments are visible per validator; an older
      // Platform without these fields (round 7 above) still parses.
      expect(json(await reader.client.callTool({ name: 'get_generator_round', arguments: { roundIndex: 9 } })))
        .toMatchObject({
          summary: { commitment_confirmations: 1, commitment_publication: 'pending' },
          commitment_publishers_target: 3,
          commitment_publications: [
            { validator_hotkey: PUBLISHER, status: 'confirmed', block_number: 4242 },
            { validator_hotkey: 'quiet-validator', status: 'expired' },
          ],
          commitment_elected: ['next-validator'],
        })
      // Platform shows a closed round's seed to operators; read scope never
      // carries it before the round is revealed.
      roundSeed = 'cc'.repeat(32)
      expect(json(await reader.client.callTool({ name: 'get_generator_round', arguments: { roundIndex: 7 } })))
        .toMatchObject({ seed: null, summary: { seed: null, secret_hex: null } })
      roundSeed = null
      expect(json(await reader.client.callTool({ name: 'preview_generator_round', arguments: {} })))
        .toMatchObject({ blockers: ['validator_rounds_enabled is false'] })
      expect(json(await reader.client.callTool({ name: 'get_generator_round_loop', arguments: {} })))
        .toMatchObject({ available: true, status: { ticks: 3 } })
      const noArtifact = await reader.client.callTool({ name: 'get_generator_round_dataset', arguments: { roundIndex: 7 } })
      expect(noArtifact.isError).toBe(true)
      const noWrite = await reader.client.callTool({ name: 'tick_generator_rounds', arguments: { reason: 'advance the round now' } })
      expect(noWrite.isError).toBe(true)
    } finally {
      await reader.client.close()
      await reader.server.close()
    }

    const artifactReader = await connect([BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE])
    try {
      expect(json(await artifactReader.client.callTool({ name: 'get_generator_round_dataset', arguments: { roundIndex: 7 } })))
        .toMatchObject({ dataset_sha256: 'bb'.repeat(32), size_bytes: 3 })
    } finally {
      await artifactReader.client.close()
      await artifactReader.server.close()
    }

    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    try {
      calls.length = 0
      expect(json(await client.callTool({ name: 'tick_generator_rounds', arguments: { reason: 'advance the round now' } })))
        .toMatchObject({ actions: ['round 8: committed'] })
      expect(calls[0].slice(0, 2)).toEqual(['POST', '/api/v1/admin/generator-competition/rounds/tick'])

      calls.length = 0
      const wrongVoid = await client.callTool({
        name: 'void_generator_round',
        arguments: { roundIndex: 7, expectedStatus: 'running', reason: 'bad dataset found', confirmation: 'VOID GENERATOR ROUND 8' },
      })
      expect(wrongVoid.isError).toBe(true)
      expect(calls).toEqual([])
      await client.callTool({
        name: 'void_generator_round',
        arguments: { roundIndex: 7, expectedStatus: 'running', reason: 'bad dataset found', confirmation: 'VOID GENERATOR ROUND 7' },
      })
      expect(calls[0]).toEqual([
        'POST', '/api/v1/admin/generator-competition/rounds/7/void',
        { expected_status: 'running', reason: 'bad dataset found', actor: session.email, confirmation: 'VOID GENERATOR ROUND 7' },
      ])

      calls.length = 0
      await client.callTool({
        name: 'close_generator_round',
        arguments: { roundIndex: 7, reason: 'agent never runs', confirmation: 'CLOSE GENERATOR ROUND 7' },
      })
      expect(calls[0][2]).toMatchObject({ expected_status: 'running', confirmation: 'CLOSE GENERATOR ROUND 7' })

      calls.length = 0
      await client.callTool({
        name: 'release_generator_round_lease',
        arguments: {
          roundIndex: 7, agentId: AGENT, validatorHotkey: '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
          reason: 'validator went quiet', confirmation: 'RELEASE GENERATOR ROUND LEASE',
        },
      })
      expect(calls[0][2]).toMatchObject({ agent_id: AGENT, confirmation: 'RELEASE GENERATOR ROUND LEASE' })
      const badHotkey = await client.callTool({
        name: 'release_generator_round_lease',
        arguments: { roundIndex: 7, agentId: AGENT, validatorHotkey: 'nope', reason: 'validator went quiet', confirmation: 'RELEASE GENERATOR ROUND LEASE' },
      })
      expect(badHotkey.isError).toBe(true)

      calls.length = 0
      await client.callTool({
        name: 'republish_generator_round_commitment',
        arguments: { roundIndex: 8, reason: 'pylon is back now', confirmation: 'REPUBLISH GENERATOR ROUND COMMITMENT' },
      })
      expect(calls[0][1]).toBe('/api/v1/admin/generator-competition/rounds/8/republish-commitment')

      calls.length = 0
      expect(json(await client.callTool({
        name: 'register_default_generator',
        arguments: {
          minerHotkey: '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY', sourceSha256: 'dd'.repeat(32),
          sourceBase64: 'H4sIAAAAAAAA', reason: 'the public default', confirmation: 'REGISTER DEFAULT GENERATOR',
        },
      }))).toMatchObject({ submission_id: SUBMISSION, status: 'active' })
      expect(calls[0][2]).toMatchObject({ source_sha256: 'dd'.repeat(32), actor: session.email })

      calls.length = 0
      expect(json(await client.callTool({ name: 'create_default_generator_upload', arguments: {} })))
        .toMatchObject({ key: 'generator-default/v1/op/k.tar.gz', method: 'PUT' })
      expect(calls[0].slice(0, 2)).toEqual(['POST', '/api/v1/admin/generator-competition/default-generator/upload-url'])

      calls.length = 0
      expect(json(await client.callTool({
        name: 'register_default_generator',
        arguments: {
          minerHotkey: '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY', sourceSha256: 'dd'.repeat(32),
          uploadKey: 'generator-default/v1/op/k.tar.gz', reason: 'the public default', confirmation: 'REGISTER DEFAULT GENERATOR',
        },
      }))).toMatchObject({ submission_id: SUBMISSION })
      expect(calls[0][2]).toMatchObject({ upload_key: 'generator-default/v1/op/k.tar.gz', source_base64: null })

      calls.length = 0
      const neither = await client.callTool({
        name: 'register_default_generator',
        arguments: {
          minerHotkey: '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY', sourceSha256: 'dd'.repeat(32),
          reason: 'the public default', confirmation: 'REGISTER DEFAULT GENERATOR',
        },
      })
      expect(neither.isError).toBe(true)
      expect(calls).toEqual([])
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('shows dropped and skipped panel agents and the void reason of a fault-only round', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const KEPT = '1a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d'
    const DROPPED = '2a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d'
    const SKIPPED = '3a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d'
    const summary = {
      round_index: 9, status: 'void', epochs_per_round: 4, commitment: 'aa'.repeat(32),
      commitment_publication: 'published', committed_at: '2026-10-24T00:00:00Z', column_count: 5,
      panel_size: 4, panel_dropped: 1, results: 3, answered_results: 0, secret_hex: null, seed: null,
      void_reason: 'insufficient_answered_results: 0 of 3 agent result(s) answered at least one case',
    }
    const slot = { owner_key: 'coldkey:a', champion: false, reference: false }
    const result = { validator_hotkey: '5V', run_id: 'r', answered: 0, faulted: 80, mean_score: null, recorded_at: '2026-10-24T01:00:00Z' }
    const detail = {
      summary, settings_revision: 2, cases_per_column: 16, seed: null, columns: [],
      panel: [
        { ...slot, agent_id: KEPT },
        { ...slot, agent_id: DROPPED, dropped_reason: 'agent status is banned', dropped_at: '2026-10-24T00:30:00Z' },
      ],
      generation: null, column_case_counts: {}, dataset_bytes: 1024, lease_id: null, lease_expires_at: null,
      lease_attempts: 1, commitment_published_at: null, commitment_error: null, attempts: [],
      results: [{ ...result, agent_id: KEPT, excluded_reason: 'faulted every case; counts as not reported' }],
      pending_agents: [], score: null, scorer_version: null, standings: [], columns_scored: [],
      skipped_panel_agents: [{ agent_id: SKIPPED, owner_key: 'coldkey:b', reason: 'miner hotkey is banned' }],
    }
    vi.stubGlobal('fetch', vi.fn(async (url: string) => (
      url.includes('/rounds?')
        ? Response.json({ schedule: { mode: 'shadow', epochs_per_round: 4 }, rounds: [summary], counts_by_status: { void: 1 } })
        : Response.json(detail)
    )))

    const reader = await connect([BACKROOM_READ_SCOPE])
    try {
      expect(json(await reader.client.callTool({ name: 'list_generator_rounds', arguments: {} })))
        .toMatchObject({ rounds: [{ panel_dropped: 1, answered_results: 0, void_reason: summary.void_reason }] })
      expect(json(await reader.client.callTool({ name: 'get_generator_round', arguments: { roundIndex: 9 } })))
        .toMatchObject({
          panel: [{ agent_id: KEPT, dropped_reason: null }, { agent_id: DROPPED, dropped_reason: 'agent status is banned' }],
          results: [{ agent_id: KEPT, excluded_reason: 'faulted every case; counts as not reported' }],
          skipped_panel_agents: [{ agent_id: SKIPPED, reason: 'miner hotkey is banned' }],
        })
    } finally {
      await reader.client.close()
      await reader.server.close()
    }
  })
})
