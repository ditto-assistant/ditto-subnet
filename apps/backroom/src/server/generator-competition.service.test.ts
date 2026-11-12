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
import { PlatformAdminError } from './ditto.server'
import { frontierRefusal } from './generator-competition.service'
import {
  generatorFrontierAuditListSchema,
  generatorCompetitionSettingsSchema,
  generatorRoundPreviewSchema,
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
  privacy_window_rounds: 6, onchain_commitment: true, validator_rounds_enabled: false, min_round_validators: 3,
  upload_fee_required: false, dueling_enabled: false, rounds_per_season: 1,
  dueling_shadow_bench_version: null,
}
// What a Platform that predates the generator upload fee (and the round
// switch quorum, and dueling seasons) returns.
const {
  upload_fee_required: _absent, min_round_validators: _noQuorum,
  dueling_enabled: _noDueling, rounds_per_season: _noSeasons,
  dueling_shadow_bench_version: _noShadow, ...olderSettings
} = settings
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
        effective: {
          settings: {
            mode: string, upload_fee_required: boolean, min_round_validators: number,
            dueling_enabled: boolean, rounds_per_season: number,
          }
        }
      }
      expect(control.effective.settings.mode).toBe('off')
      expect(control.effective.settings.upload_fee_required).toBe(false)
      expect(control.effective.settings.min_round_validators).toBe(3)
      expect(control.effective.settings.dueling_enabled).toBe(false)
      expect(control.effective.settings.rounds_per_season).toBe(1)
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

  it('reads generator source only with the artifact scope, attributed to the operator', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const base = `/api/v1/admin/generator-competition/submissions/${SUBMISSION}`
    const identity = { submission_id: SUBMISSION, source_sha256: 'cd'.repeat(32) }
    const routes: Record<string, unknown> = {
      [`${base}/source-files`]: {
        ...identity, file_count: 2,
        files: [{ path: 'data/world1.json', bytes: 58 }, { path: 'main.go', bytes: 40 }],
        opaque_blobs: [], opaque_total: 0, truncated: false,
      },
      [`${base}/source-file?path=data%2Fworld1.json&start_line=1&end_line=400`]: {
        ...identity, path: 'data/world1.json', total_lines: 1, start_line: 1, end_line: 1,
        lines: [{ line: 1, text: '{"id": "v3h1-q1"}' }],
      },
      [`${base}/source-search?pattern=v3h1-q&mode=literal&ignore_case=false&context=0&limit=50&offset=0&path_glob=data%2F*.json`]: {
        ...identity, pattern: 'v3h1-q', mode: 'literal', path_glob: 'data/*.json',
        matches: [{ path: 'data/world1.json', line: 1, text: '{"id": "v3h1-q1"}', context_before: [], context_after: [] }],
        match_count: 1, returned: 1, limit: 50, offset: 0, has_more: false,
        files_searched: 1, files_matched: 1, opaque_skipped: 0, truncated: false,
      },
    }
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      const path = url.replace('https://platform-api.heyditto.ai', '')
      if (!(path in routes)) return new Response('not found', { status: 404 })
      return Response.json(routes[path])
    })
    vi.stubGlobal('fetch', fetchMock)

    const readOnly = await connect([BACKROOM_READ_SCOPE])
    try {
      const refused = await readOnly.client.callTool({
        name: 'list_generator_source_files', arguments: { submissionId: SUBMISSION },
      })
      expect(refused.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      await readOnly.client.close()
      await readOnly.server.close()
    }

    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE])
    try {
      expect(json(await client.callTool({
        name: 'list_generator_source_files', arguments: { submissionId: SUBMISSION },
      }))).toMatchObject({ ...identity, count: 2, has_more: false })
      expect(json(await client.callTool({
        name: 'read_generator_source_file', arguments: { submissionId: SUBMISSION, path: 'data/world1.json' },
      }))).toMatchObject({ ...identity, path: 'data/world1.json', total_lines: 1 })
      expect(json(await client.callTool({
        name: 'search_generator_source',
        arguments: { submissionId: SUBMISSION, pattern: 'v3h1-q', mode: 'literal', pathGlob: 'data/*.json' },
      }))).toMatchObject({ ...identity, match_count: 1, has_more: false })
      expect(fetchMock).toHaveBeenCalledTimes(3)
      // Platform audits every source read under this actor.
      for (const [, init] of fetchMock.mock.calls) {
        expect(new Headers(init?.headers).get('X-Admin-Actor')).toBe(session.email)
      }
      const bad = await client.callTool({
        name: 'read_generator_source_file', arguments: { submissionId: 'not-a-uuid', path: 'main.go' },
      })
      expect(bad.isError).toBe(true)
      expect(fetchMock).toHaveBeenCalledTimes(3)
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('lists pre-screen batches from Platform', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const batch = {
      prescreen_index: 1099511627776, status: 'generating',
      opened_at: '2026-11-12T06:00:00Z', expires_at: '2026-11-12T09:00:00Z',
      settings_revision: 14, cases_per_column: 16, bench_version: 18,
      columns: [{
        column_id: 'c0', submission_id: SUBMISSION, miner_hotkey: submission.miner_hotkey,
        owner_key: 'coldkey:abc', source_sha256: 'cd'.repeat(32), counted: false, public_default: false,
      }],
      panel: [{ agent_id: RUN, owner_key: 'coldkey:panel', champion: true, reference: false }],
      lease_attempts: 0,
    }
    const fetchMock = vi.fn(async (url: string) => {
      const path = url.replace('https://platform-api.heyditto.ai', '')
      if (path !== '/api/v1/admin/generator-competition/prescreens?limit=20&offset=0') {
        return new Response('not found', { status: 404 })
      }
      return Response.json({ prescreens: [batch], counts_by_status: { generating: 1 } })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    try {
      expect(json(await client.callTool({ name: 'list_generator_prescreens', arguments: {} })))
        .toMatchObject({
          counts_by_status: { generating: 1 },
          prescreens: [{ prescreen_index: 1099511627776, status: 'generating', void_reason: null, columns: [{ counted: false }] }],
        })
      expect(fetchMock).toHaveBeenCalledTimes(1)
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

  it('reads a round preview from a Platform that predates the challenger queue', () => {
    const preview = generatorRoundPreviewSchema.parse({
      schedule: { mode: 'shadow', epochs_per_round: 4 }, columns: [], panel: [],
      qualified_generators: 0, awaiting_qualification: 0, blockers: [],
    })
    expect(preview.challenger_queue).toEqual([])
  })

  it('admits challenger columns up to the round column capacity', () => {
    // Platform bounds counted plus challenger columns at 8 (MAX_ROUND_COLUMNS),
    // so a revision with three challengers beside five counted columns must
    // parse on read and write. The field alone allows 7 (one counted column);
    // the write refuses a pair above 8 (see the write-path test).
    for (const challengers of [0, 2, 3]) {
      expect(generatorCompetitionSettingsSchema.safeParse({ ...settings, counted_columns: 5, challenger_columns: challengers }).success).toBe(true)
    }
    expect(generatorCompetitionSettingsSchema.safeParse({ ...settings, counted_columns: 1, challenger_columns: 7 }).success).toBe(true)
    for (const challengers of [-1, 8]) {
      expect(generatorCompetitionSettingsSchema.safeParse({ ...settings, counted_columns: 1, challenger_columns: challengers }).success).toBe(false)
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

      // A dueling season spans at least two epochs.
      const shortSeason = await client.callTool({
        name: 'set_generator_competition_settings',
        arguments: {
          expectedRevision: 1,
          settings: { ...settings, epochs_per_round: 1, dueling_enabled: true },
          reason: 'turn dueling seasons on',
          confirmation: 'APPLY GENERATOR COMPETITION SHADOW',
        },
      })
      expect(shortSeason.isError).toBe(true)
      expect(JSON.stringify(shortSeason.content)).toContain('at least 2 epochs')
      expect(fetchMock).not.toHaveBeenCalled()

      // A round holds at most eight columns; Backroom refuses more before Platform does.
      const nineColumns = await client.callTool({
        name: 'set_generator_competition_settings',
        arguments: {
          expectedRevision: 1,
          settings: { ...settings, counted_columns: 5, challenger_columns: 4 },
          reason: 'add a fourth challenger column',
          confirmation: 'APPLY GENERATOR COMPETITION SHADOW',
        },
      })
      expect(nineColumns.isError).toBe(true)
      expect(JSON.stringify(nineColumns.content)).toContain('at most 8 columns')
      expect(fetchMock).not.toHaveBeenCalled()

      // A canary shadow version needs dueling on.
      const shadowOff = await client.callTool({
        name: 'set_generator_competition_settings',
        arguments: {
          expectedRevision: 1,
          settings: { ...settings, dueling_shadow_bench_version: 16 },
          reason: 'shadow canary',
          confirmation: 'APPLY GENERATOR COMPETITION SHADOW',
        },
      })
      expect(shadowOff.isError).toBe(true)
      expect(JSON.stringify(shadowOff.content)).toContain('needs dueling_enabled')
      expect(fetchMock).not.toHaveBeenCalled()

      const applied = json(await client.callTool({
        name: 'set_generator_competition_settings',
        arguments: {
          expectedRevision: 1,
          settings: { ...settings, upload_fee_required: true, counted_columns: 5, challenger_columns: 3 },
          reason: 'open shadow lane', confirmation: 'APPLY GENERATOR COMPETITION SHADOW',
        },
      })) as { revision: number }
      expect(applied.revision).toBe(2)
      expect(fetchMock).toHaveBeenCalledTimes(1)
      const [settingsUrl, settingsInit] = fetchMock.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }]
      expect(settingsUrl).toContain('/api/v1/admin/generator-competition/settings')
      expect(JSON.parse(String(settingsInit.body))).toMatchObject({
        expected_revision: 1, scope: '*', actor: session.email, confirmation: 'APPLY GENERATOR COMPETITION SHADOW',
        settings: { upload_fee_required: true, counted_columns: 5, challenger_columns: 3 },
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
      const unphrased = await client.callTool({
        name: 'reinstate_generator_submission',
        arguments: {
          submissionId: SUBMISSION, expectedStatus: 'rejected', expectedStatusReason: 'generation_failed: x',
          reason: 'stale scorer refused the case count', confirmation: 'REINSTATE',
        },
      })
      expect(unphrased.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
      await client.callTool({
        name: 'reinstate_generator_submission',
        arguments: {
          submissionId: SUBMISSION, expectedStatus: 'rejected', expectedStatusReason: 'generation_failed: x',
          reason: 'stale scorer refused the case count', confirmation: 'REINSTATE GENERATOR SUBMISSION',
        },
      })
      const [reinstateUrl, reinstateInit] = fetchMock.mock.calls[0] as [string, RequestInit]
      expect(reinstateUrl).toContain(`/submissions/${SUBMISSION}/reinstate`)
      expect(JSON.parse(String(reinstateInit.body))).toEqual({
        expected_status: 'rejected', expected_status_reason: 'generation_failed: x',
        reason: 'stale scorer refused the case count', actor: session.email,
        confirmation: 'REINSTATE GENERATOR SUBMISSION', override_operator_ruling: false,
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

  it('shows the live scorer and passes the capability override through', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const refusalText = 'generator scorer prod (challenge-scorer/4) supports 8..64 cases per pack; cases_per_seed is 70'
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/scorer')) {
        return Response.json({
          scorers: [{
            scorer_id: 'prod', scorer_version: 'challenge-scorer/4', min_cases: 8, max_cases: 64,
            sandbox: 'docker', build_revision: null, last_poll: 'round',
            first_seen_at: '2026-10-27T00:00:00Z', last_seen_at: '2026-10-27T01:00:00Z', live: true,
            grading_bench_versions: null, effective_grading_bench_versions: [13],
          }, {
            // A Platform that predates explicit grading versions omits them.
            scorer_id: 'old', scorer_version: 'challenge-scorer/3', min_cases: null, max_cases: null,
            sandbox: null, build_revision: null, last_poll: 'qualification',
            first_seen_at: '2026-10-26T00:00:00Z', last_seen_at: '2026-10-26T01:00:00Z', live: false,
          }],
          live_scorer_id: 'prod', capability_ttl_seconds: 21600, cases_per_seed: 70,
          supports_cases_per_seed: false, refusal: refusalText, round_contract: 14, supports_round_contract: false,
        })
      }
      const body = JSON.parse(String(init?.body ?? '{}')) as { override_scorer_capability?: boolean }
      if (!body.override_scorer_capability) {
        return Response.json({ detail: `${refusalText}: redeploy the scorer first` }, { status: 409 })
      }
      return Response.json({
        revision: 2, parent_revision: 1, scope: '*', settings, reason: 'raise the case count',
        actor: session.email, created_at: '2026-10-27T00:00:00Z', checksum: 'b'.repeat(64),
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    try {
      expect(json(await client.callTool({ name: 'get_generator_scorer', arguments: {} })))
        .toMatchObject({
          live_scorer_id: 'prod', supports_cases_per_seed: false, round_contract: 14, supports_round_contract: false,
          scorers: [{ max_cases: 64, grading_bench_versions: null, effective_grading_bench_versions: [13] }, { grading_bench_versions: null }],
        })
      const write = {
        expectedRevision: 1, settings, reason: 'raise the case count', confirmation: 'APPLY GENERATOR COMPETITION SHADOW',
      }
      const refused = await client.callTool({ name: 'set_generator_competition_settings', arguments: write })
      expect(refused.isError).toBe(true)
      expect((refused.content as Array<{ text: string }>)[0].text).toContain('overrideScorerCapability=true')
      const sent = JSON.parse(String((fetchMock.mock.calls.at(-1) as [string, RequestInit])[1].body))
      expect(sent).not.toHaveProperty('override_scorer_capability')
      const applied = await client.callTool({
        name: 'set_generator_competition_settings', arguments: { ...write, overrideScorerCapability: true },
      })
      expect(applied.isError).toBeFalsy()
      expect(JSON.parse(String((fetchMock.mock.calls.at(-1) as [string, RequestInit])[1].body)))
        .toMatchObject({ override_scorer_capability: true })
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('refuses to report a settings write an older Platform silently dropped', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    // An older Platform ignores the unknown field and stores the revision without it.
    const fetchMock = vi.fn(async () => Response.json({
      revision: 2, parent_revision: 1, scope: '*', settings: olderSettings, reason: 'charge generator uploads',
      actor: session.email, created_at: '2026-10-23T00:00:00Z', checksum: 'b'.repeat(64),
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    try {
      const dropped = await client.callTool({
        name: 'set_generator_competition_settings',
        arguments: {
          expectedRevision: 1, settings: { ...settings, upload_fee_required: true },
          reason: 'charge generator uploads', confirmation: 'APPLY GENERATOR COMPETITION SHADOW',
        },
      })
      expect(dropped.isError).toBe(true)
      const text = (dropped.content as Array<{ text: string }>)[0].text
      expect(text).toContain('revision 2 without upload_fee_required')
      expect(fetchMock).toHaveBeenCalledTimes(1)

      // Sending the default value (fee off) changes nothing on an older
      // Platform, so that write succeeds.
      const unchanged = await client.callTool({
        name: 'set_generator_competition_settings',
        arguments: {
          expectedRevision: 1, settings: { ...settings, upload_fee_required: false },
          reason: 'raise the cooldown only', confirmation: 'APPLY GENERATOR COMPETITION SHADOW',
        },
      })
      expect(unchanged.isError).toBeFalsy()
      expect(json(unchanged)).toMatchObject({ revision: 2, settings: { upload_fee_required: false } })
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
        return Response.json({
          schedule, columns: [], panel: [], qualified_generators: 0, awaiting_qualification: 1, blockers: ['validator_rounds_enabled is false'],
          bench_contract: {
            active_version: 14, bench_version: 13, reason: '2 validator(s) can run a 70-case round column at bench v14, below min_round_validators 3: rounds stay on v13 until a quorum can run v14',
            round_contracts: [13, 14], capable_validators: { 13: 3, 14: 2 }, previous_version: 13, min_round_validators: 3, scorer_grading_versions: [13, 14],
          },
          challenger_queue: [
            {
              queue_position: 1, submission_id: SUBMISSION, miner_hotkey: '5Kin', owner_key: 'coldkey:kin',
              module: 'kinbind', file_count: 46, size_bytes: 200_032, qualified_at: '2026-11-11T14:59:38Z',
              family: SUBMISSION, family_size: 12, family_measured: 2, family_position: 2, novelty: 0, status: 'waiting',
              prescreen_index: 1099511627776, prescreen_score: 0.25,
            },
            {
              queue_position: null, submission_id: RUN, miner_hotkey: '5Dr', owner_key: 'coldkey:dr',
              module: 'gen-v3h', file_count: 49, size_bytes: 486_590, qualified_at: '2026-11-11T23:35:47Z',
              family: RUN, family_size: 1, family_measured: 1, family_position: 1, status: 'in_flight',
              column_id: null, in_flight_round: 26114,
            },
          ],
        })
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
        .toMatchObject({
          blockers: ['validator_rounds_enabled is false'],
          bench_contract: {
            active_version: 14, bench_version: 13, round_contracts: [13, 14], capable_validators: { 13: 3, 14: 2 },
            previous_version: 13, min_round_validators: 3, scorer_grading_versions: [13, 14],
          },
          challenger_queue: [
            { submission_id: SUBMISSION, family_position: 2, novelty: 0, status: 'waiting', column_id: null, in_flight_round: null, prescreen_index: 1099511627776, prescreen_score: 0.25 },
            { queue_position: null, status: 'in_flight', in_flight_round: 26114, novelty: null },
          ],
        })
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

  it('shows a round skipped as unchanged and the loop that skipped it', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const summary = {
      round_index: 12, status: 'skipped', epochs_per_round: 4, commitment: 'aa'.repeat(32),
      commitment_publication: 'disabled', committed_at: '2026-11-04T00:00:00Z', column_count: 5,
      panel_size: 5, panel_dropped: 0, results: 0, answered_results: 0, secret_hex: null, seed: null,
      void_reason: null, skip_reason: 'skipped_unchanged', carried_from_round: 11,
    }
    const detail = {
      summary, settings_revision: 2, cases_per_column: 70, seed: null, columns: [], panel: [],
      generation: { skipped_unchanged: { reason: 'skipped_unchanged', carried_from_round: 11 } },
      column_case_counts: {}, dataset_bytes: null, lease_id: null, lease_expires_at: null,
      lease_attempts: 0, commitment_published_at: null, commitment_error: null, attempts: [],
      results: [], pending_agents: [], score: null, scorer_version: null, standings: [],
      columns_scored: [], skipped_panel_agents: [],
    }
    const loop = {
      available: true,
      status: { last_tick_at: null, last_success_at: null, last_error: null, last_actions: ['round 12: skipped'], ticks: 1 },
      schedule: { mode: 'enforce', epochs_per_round: 4 },
      skip_unchanged_rounds: true,
      skipped_rounds: 3,
      last_skipped_round: { round_index: 12, reason: 'skipped_unchanged', carried_from_round: 11, skipped_at: '2026-11-04T05:00:00Z' },
    }
    vi.stubGlobal('fetch', vi.fn(async (url: string) => (
      url.includes('/rounds/loop')
        ? Response.json(loop)
        : url.includes('/rounds?')
          ? Response.json({ schedule: { mode: 'enforce', epochs_per_round: 4 }, rounds: [summary], counts_by_status: { skipped: 3 } })
          : Response.json(detail)
    )))

    const reader = await connect([BACKROOM_READ_SCOPE])
    try {
      expect(json(await reader.client.callTool({ name: 'list_generator_rounds', arguments: {} })))
        .toMatchObject({ rounds: [{ status: 'skipped', skip_reason: 'skipped_unchanged', carried_from_round: 11 }] })
      expect(json(await reader.client.callTool({ name: 'get_generator_round', arguments: { roundIndex: 12 } })))
        .toMatchObject({ summary: { status: 'skipped', skip_reason: 'skipped_unchanged', carried_from_round: 11 } })
      expect(json(await reader.client.callTool({ name: 'get_generator_round_loop', arguments: {} })))
        .toMatchObject({
          skip_unchanged_rounds: true,
          skipped_rounds: 3,
          last_skipped_round: { round_index: 12, reason: 'skipped_unchanged', carried_from_round: 11 },
        })
    } finally {
      await reader.client.close()
      await reader.server.close()
    }
  })

  it('reads and rules frontier audits with artifact-scoped cases and guarded writes', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const AGENT = '9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d'
    const audit = {
      round_index: 7, column_id: 'c1', submission_id: SUBMISSION, miner_hotkey: '5GenMiner',
      status: 'pending', sample: ['c1/k2', 'c1/k5'], attested_cases: 6, invalid_case_ids: [],
      reason: null, actor: null, ruled_at: null, audit_deadline: '2026-11-10T10:00:00Z',
    }
    const frontier = {
      round_index: 7, state: 'audit_pending', oracle_agent_id: AGENT, frontier_weight: 1, audit_cases: 2,
      candidates: ['c1/k0', 'c1/k2', 'c1/k5'], oracle_attempts: 1, oracle_deadline: null,
      oracle: {
        validator_hotkey: '5Validator', run_id: 'run-o', recorded_at: '2026-11-09T10:00:00Z',
        passed: 2, failed: 1, faulted: 0,
        results: [{ case_id: 'c1/k0', score: 0, fault: false }, { case_id: 'c1/k2', score: 1, fault: false }, { case_id: 'c1/k5', score: 1, fault: false }],
      },
      attested: { c1: ['c1/k2', 'c1/k5'] }, audits: [audit], applied: null,
      column_scores: { c1: 0 }, base_column_scores: null, created_at: '2026-11-09T09:00:00Z',
      settled_at: null, settle_reason: null,
    }
    const pair = { pair_id: 'new2', timestamp: '2026-02-01T09:00:00Z', wave: 1, prompt: 'The code is now 5002.', response: 'Updated.' }
    const calls: Array<[string, string, unknown]> = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.replace('https://platform-api.heyditto.ai', '')
      const method = init?.method ?? 'GET'
      calls.push([method, path, init?.body ? JSON.parse(String(init.body)) : null])
      if (path.startsWith('/api/v1/admin/generator-competition/frontier/audits')) {
        return Response.json({ audits: [audit], count: 3, returned: 1, limit: 1, offset: 0, has_more: true })
      }
      if (path.startsWith('/api/v1/admin/generator-competition/rounds/7/frontier/audits/c1') && method === 'GET') {
        return Response.json({
          audit,
          cases: [{
            case_id: 'c1/k2', question: 'What is the code for locker 2 now?', expected_answer: '5002',
            answer_kind: 'number', answer_items: [], accept_any: [], distractor_answers: ['1002'],
            forbidden_answer: null, dump_guard: [], user_id: '', run_after_wave: 1, evidence: [pair],
            visible_pairs: 16, memory_matches: [pair], memory_truncated: false,
            panel_mean_score: 0, panel_solvers: 0, panel_size: 3, oracle_score: 1, oracle_fault: false,
          }],
        })
      }
      if (path.endsWith('/rounds/7/frontier/settle')) return Response.json({ ...frontier, state: 'settled', applied: { c1: ['c1/k2', 'c1/k5'] } })
      if (path.endsWith('/rounds/7/frontier/audits/c1')) return Response.json({ ...frontier, audits: [{ ...audit, status: 'passed' }] })
      if (path.endsWith('/rounds/7/frontier')) return Response.json(frontier)
      return Response.json({ detail: 'unexpected' }, { status: 404 })
    }))

    const reader = await connect([BACKROOM_READ_SCOPE])
    try {
      expect(json(await reader.client.callTool({ name: 'list_generator_frontier_audits', arguments: {} })))
        // The paging contract survives parsing: count is the queue total.
        .toMatchObject({
          count: 3, returned: 1, limit: 1, offset: 0, has_more: true,
          audits: [{ column_id: 'c1', sample: ['c1/k2', 'c1/k5'] }],
        })
      expect(calls[0][1]).toBe('/api/v1/admin/generator-competition/frontier/audits?include_ruled=false&limit=50&offset=0')
      expect(json(await reader.client.callTool({ name: 'get_generator_round_frontier', arguments: { roundIndex: 7 } })))
        .toMatchObject({ state: 'audit_pending', attested: { c1: ['c1/k2', 'c1/k5'] }, oracle: { passed: 2 } })
      // Case content is private benchmark content: artifact scope only.
      const noArtifact = await reader.client.callTool({ name: 'get_generator_frontier_audit', arguments: { roundIndex: 7, columnId: 'c1' } })
      expect(noArtifact.isError).toBe(true)
      const noWrite = await reader.client.callTool({
        name: 'settle_generator_round_frontier',
        arguments: { roundIndex: 7, reason: 'oracle agent misconfigured', confirmation: 'SETTLE GENERATOR FRONTIER 7' },
      })
      expect(noWrite.isError).toBe(true)
    } finally {
      await reader.client.close()
      await reader.server.close()
    }

    const artifactReader = await connect([BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE])
    try {
      calls.length = 0
      expect(json(await artifactReader.client.callTool({
        name: 'get_generator_frontier_audit', arguments: { roundIndex: 7, columnId: 'c1', q: 'locker 2' },
      }))).toMatchObject({ cases: [{ expected_answer: '5002', distractor_answers: ['1002'], oracle_score: 1 }] })
      expect(calls[0][1]).toBe('/api/v1/admin/generator-competition/rounds/7/frontier/audits/c1?q=locker%202')
    } finally {
      await artifactReader.client.close()
      await artifactReader.server.close()
    }

    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    try {
      calls.length = 0
      // Confirmation names the verdict; a valid ruling names no invalid case.
      for (const args of [
        { valid: true, invalidCaseIds: [], confirmation: 'RULE FRONTIER AUDIT 7 c1 INVALID' },
        { valid: true, invalidCaseIds: ['c1/k2'], confirmation: 'RULE FRONTIER AUDIT 7 c1 VALID' },
        { valid: false, invalidCaseIds: [], confirmation: 'RULE FRONTIER AUDIT 7 c1 INVALID' },
      ]) {
        const refused = await client.callTool({
          name: 'rule_generator_frontier_audit',
          arguments: { roundIndex: 7, columnId: 'c1', reason: 'both cases are fair supersession', ...args },
        })
        expect(refused.isError).toBe(true)
      }
      expect(calls).toEqual([])
      expect(json(await client.callTool({
        name: 'rule_generator_frontier_audit',
        arguments: {
          roundIndex: 7, columnId: 'c1', valid: true, reason: 'both cases are fair supersession',
          confirmation: 'RULE FRONTIER AUDIT 7 c1 VALID',
        },
      }))).toMatchObject({ audits: [{ status: 'passed' }] })
      expect(calls[0]).toEqual([
        'POST', '/api/v1/admin/generator-competition/rounds/7/frontier/audits/c1',
        { valid: true, invalid_case_ids: [], reason: 'both cases are fair supersession', actor: session.email, confirmation: 'RULE FRONTIER AUDIT 7 c1 VALID' },
      ])
      calls.length = 0
      expect(json(await client.callTool({
        name: 'settle_generator_round_frontier',
        arguments: { roundIndex: 7, reason: 'oracle agent misconfigured', confirmation: 'SETTLE GENERATOR FRONTIER 7' },
      }))).toMatchObject({ state: 'settled' })
      expect(calls[0][2]).toMatchObject({ confirmation: 'SETTLE GENERATOR FRONTIER 7', actor: session.email })
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('describes the frontier audit memory fields without overstating them', async () => {
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE])
    try {
      const tool = (await client.listTools()).tools.find((t) => t.name === 'get_generator_frontier_audit')
      const help = (await client.callTool({ name: 'get_backroom_tool_help', arguments: { tool: 'get_generator_frontier_audit' } }))
      const text = [tool?.description ?? '', JSON.stringify(help)].join(' ')
      // visible_pairs is a count; memory_matches is a capped, possibly
      // truncated list: neither may read as the user's whole memory.
      expect(text).toContain('the count of the user')
      expect(text).toContain('memory_truncated is true when the list was cut')
      expect(text).not.toContain("visible_pairs (the user's memory at that point)")
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('rejects impossible frontier audit paging from Platform', () => {
    const page = { audits: [], count: 0, returned: 0, limit: 50, offset: 0, has_more: false }
    expect(generatorFrontierAuditListSchema.parse(page)).toMatchObject(page)
    for (const bad of [{ count: -1 }, { limit: 0 }, { limit: -5 }, { returned: -1 }, { offset: -1 }]) {
      expect(() => generatorFrontierAuditListSchema.parse({ ...page, ...bad })).toThrow()
    }
  })

  it('defaults the frontier settings a write omits', () => {
    const parsed = generatorCompetitionSettingsSchema.parse(settings)
    expect(parsed).toMatchObject({
      frontier_weight: 1, frontier_oracle_agent_id: null, frontier_audit_cases: 3,
      frontier_audit_grace_hours: 24, frontier_oracle_timeout_hours: 12,
    })
    expect(() => generatorCompetitionSettingsSchema.parse({ ...settings, frontier_weight: 1.5 })).toThrow()
  })

  it('defaults the season catch-up settings a write omits and bounds the cap', () => {
    const parsed = generatorCompetitionSettingsSchema.parse(settings)
    expect(parsed).toMatchObject({ season_catchup_enabled: true, season_catchup_max_agents: 5 })
    expect(generatorCompetitionSettingsSchema.parse({ ...settings, season_catchup_enabled: false }))
      .toMatchObject({ season_catchup_enabled: false })
    expect(() => generatorCompetitionSettingsSchema.parse({ ...settings, season_catchup_max_agents: 11 })).toThrow()
  })

  it('names the frontier write that was refused and the recovery its status implies', () => {
    const conflict = (message: string) => new PlatformAdminError('request', message, 409)
    const invalid = (message: string) => new PlatformAdminError('request', message, 422)

    const ruled = frontierRefusal('rule', conflict('column c1 audit is already passed'))!.message
    expect(ruled).toContain('Nothing was ruled')
    expect(ruled).toContain('re-read get_generator_round_frontier')

    // A settle refusal says nothing about ruling or an audit sample.
    const settled = frontierRefusal('settle', conflict('frontier is already settled'))!.message
    expect(settled).toContain('Nothing was settled')
    expect(settled).not.toMatch(/ruled|sample/)

    // A 422 is the request itself: correct it; re-reading fixes nothing.
    for (const kind of ['rule', 'settle'] as const) {
      const message = frontierRefusal(kind, invalid('request validation failed'))!.message
      expect(message).toContain('correct the refused fields')
      expect(message).not.toContain('re-read')
    }
    expect(frontierRefusal('settle', invalid('x'))!.message).not.toContain('invalidCaseIds')

    // Anything else passes through untouched.
    expect(frontierRefusal('rule', new PlatformAdminError('server', 'boom', 503))).toBeNull()
    expect(frontierRefusal('rule', new Error('plain'))).toBeNull()
  })
})
