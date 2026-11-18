import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  createBackroomMcpServer,
  type McpGrantProps,
} from './mcp.server'
import { WRITE_TOOL_NAMES } from './mcp-contract.server'

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
  const client = new Client({ name: 'model-context-test', version: '1.0.0' })
  const [a, b] = InMemoryTransport.createLinkedPair()
  await Promise.all([server.connect(b), client.connect(a)])
  return { client, server }
}

function json(response: unknown): unknown {
  const content = (response as { content: Array<{ type: string; text: string }> }).content
  return JSON.parse(content[0].text)
}

const HOTKEY = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'
const SUBMISSION = '3b2d6a54-0f4c-4b3e-9a6e-2f2d0c1b9e11'
const RUN = '7d0a4c39-4c1f-4a5b-8f3e-9b1d2e3f4a5b'
const BIG_SEED = '18446744073709551557'
const base = {
  base_id: 'qwen3.5-0.8b',
  hf_repo: 'Qwen/Qwen3.5-0.8B',
  revision: '2fc06364715b967f1860aea9cf38778875588b17',
  parameters: 873_441_376,
  architectures: ['Qwen3_5ForConditionalGeneration'],
  lora_target_modules: ['q_proj', 'v_proj'],
}
const settings = {
  mode: 'shadow', base_allowlist: [base], max_lora_rank: 64, max_total_bytes: 6 * 1024 ** 3,
  upload_cooldown_seconds: 21_600, upload_fee_required: false, season_days: 7,
  floor_margin: 0.01, budget_tokens: 2048, tool_k: 8, rank_shares: [0.5, 0.2, 0.13, 0.1, 0.07],
  bench_version: 18, lease_ttl_seconds: 14_400, max_attempts: 3,
}
const latestRun = {
  run_id: RUN, season: 1, cpm_score_version: 1, status: 'scored', score: 0.61,
  fault: null, started_at: '2026-11-18T10:00:00Z', finished_at: '2026-11-18T10:30:00Z',
}
const summary = {
  submission_id: SUBMISSION, hotkey: HOTKEY, hf_repo: 'miner/cpm',
  revision: 'a'.repeat(40), manifest_sha256: 'b'.repeat(64), weights_sha256: 'c'.repeat(64),
  base_id: 'qwen3.5-0.8b', kind: 'lora', commitment_block: 1000, status: 'scored',
  reject_reason: null, mirror_repo: null,
  model_url: `https://huggingface.co/miner/cpm/tree/${'a'.repeat(40)}`,
  created_at: '2026-11-18T09:00:00Z', latest_run: latestRun,
}
const runView = {
  run_id: RUN, kind: 'submission', submission_id: SUBMISSION, base_id: 'qwen3.5-0.8b',
  season: 1, cpm_score_version: 1, status: 'scored', score: 0.61,
  components: { memory_recall: 0.8, distractor_rate: 0.2, tool_recall: 0.4 },
  per_seed: [{ seed: BIG_SEED, score: 0.61 }], scorer_id: 'cpm-1', scorer_version: '0.1.0',
  gpu_sku: 'L40S', attempts: 1, lease_expires_at: null, created_at: '2026-11-18T09:30:00Z',
  started_at: '2026-11-18T10:00:00Z', finished_at: '2026-11-18T10:30:00Z', fault: null,
}
const detail = {
  summary, manifest: { schema: 'ditto-cpm-manifest-v1' }, total_bytes: 4096,
  screening_detail: { rank: 4 }, screening_attempts: 1, screening_lease_until: null,
  updated_at: '2026-11-18T10:30:00Z', runs: [runView],
  actions: [{
    id: 1, action: 'rescore', subject: SUBMISSION, actor: session.email,
    reason: 'scorer host fault', detail: { season: 1 }, created_at: '2026-11-18T11:00:00Z',
  }],
}
// Platform serves seeds as bare uint64 JSON numbers; the body is raw text so
// the lossless parser sees the exact digits.
const seasonJson = `{"season":1,"open_block":5000000,"open_block_hash":"0x${'cd'.repeat(32)}","seeds":[${BIG_SEED},42,7],"bench_version":18,"cpm_score_version":1,"base_floors":{"qwen3.5-0.8b":0.3},"gpu_sku":"L40S","opened_at":"2026-11-18T00:00:00Z","closes_at":"2026-11-25T00:00:00Z","closed_at":null}`
const board = {
  entries: [{
    rank: 1, hotkey: HOTKEY, submission_id: SUBMISSION, score: 0.61, eligible: true,
    commitment_block: 1000, season: 1, base_id: 'qwen3.5-0.8b', kind: 'lora',
    hf_repo: 'miner/cpm', revision: 'a'.repeat(40), model_url: summary.model_url,
    components: { memory_recall: 0.8 }, floor: 0.3,
  }],
  count: 1, season: 1, mode: 'shadow', floor_margin: 0.01,
  base_floors: { 'qwen3.5-0.8b': 0.3 }, generated_at: '2026-11-18T12:00:00Z',
}

afterEach(() => {
  vi.unstubAllGlobals()
  if (original === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = original
})

describe('model_context MCP tools', () => {
  it('registers exactly four write tools', () => {
    expect([...WRITE_TOOL_NAMES].filter((name) => name.includes('model_context')).sort()).toEqual([
      'reject_model_context_submission',
      'rescore_model_context_submission',
      'rotate_model_context_season',
      'set_model_context_settings',
    ])
  })

  it('reads settings, submissions, runs, board with its season, and scorer', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const routes: Record<string, string> = {
      '/api/v1/admin/model-context/settings': JSON.stringify({
        current: [], history: [], default: { ...settings, mode: 'off' },
        effective: { revision: 0, settings: { ...settings, mode: 'off' }, checksum: '', source: 'default' },
      }),
      '/api/v1/admin/model-context/submissions?limit=50&offset=0&status=scored': JSON.stringify({
        submissions: [summary], counts_by_status: { scored: 1, rejected: 2 },
      }),
      [`/api/v1/admin/model-context/submissions/${SUBMISSION}`]: JSON.stringify({
        ...detail, runs: [{ ...runView, lease_id: 'must-not-pass' }],
      }),
      [`/api/v1/admin/model-context/runs/${RUN}`]: `{"run":${JSON.stringify(runView)},"submission":${JSON.stringify(summary)},"season":${seasonJson},"floor":0.3}`,
      '/api/v1/model-context/board': JSON.stringify(board),
      '/api/v1/model-context/seasons?limit=1&offset=0': `{"current":1,"seasons":[${seasonJson}]}`,
      '/api/v1/admin/model-context/scorer': JSON.stringify({
        scorers: [], queued_runs: 2, leased_runs: 1, screening_submissions: 0, current_season: 1,
      }),
    }
    const fetchMock = vi.fn(async (url: string) => {
      const path = url.replace('https://platform-api.heyditto.ai', '')
      if (!(path in routes)) return new Response('not found', { status: 404 })
      return new Response(routes[path], { headers: { 'Content-Type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    try {
      const control = json(await client.callTool({ name: 'get_model_context_settings', arguments: {} })) as {
        effective: { settings: { mode: string } }
      }
      expect(control.effective.settings.mode).toBe('off')

      const listing = json(await client.callTool({
        name: 'list_model_context_submissions', arguments: { status: 'scored' },
      })) as { has_more: boolean, returned: number }
      expect(listing).toMatchObject({ returned: 1, has_more: false })

      const one = json(await client.callTool({
        name: 'get_model_context_submission', arguments: { submissionId: SUBMISSION },
      }))
      expect(JSON.stringify(one)).not.toContain('must-not-pass')
      expect(one).toMatchObject({ actions: [{ action: 'rescore' }], runs: [{ run_id: RUN }] })

      const run = json(await client.callTool({ name: 'get_model_context_run', arguments: { runId: RUN } }))
      // The uint64 seed survives exactly, never rounded through a double.
      expect(run).toMatchObject({ floor: 0.3, season: { seeds: [BIG_SEED, '42', '7'] } })

      const boardRead = json(await client.callTool({ name: 'get_model_context_board', arguments: {} }))
      expect(boardRead).toMatchObject({
        board: { count: 1, entries: [{ rank: 1, eligible: true }] },
        season: { season: 1, seeds: [BIG_SEED, '42', '7'] },
      })

      expect(json(await client.callTool({ name: 'get_model_context_scorer', arguments: {} })))
        .toMatchObject({ queued_runs: 2, loop_running: null })

      const bad = await client.callTool({ name: 'get_model_context_run', arguments: { runId: 'nope' } })
      expect(bad.isError).toBe(true)
      const badHotkey = await client.callTool({
        name: 'list_model_context_submissions', arguments: { hotkey: 'not-a-hotkey' },
      })
      expect(badHotkey.isError).toBe(true)
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('guards writes with write scope, confirmations and the operator actor', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith('/settings')) {
        return Response.json({
          revision: 2, parent_revision: 1, scope: '*', settings, reason: 'open shadow lane',
          actor: session.email, created_at: '2026-11-18T00:00:00Z', checksum: 'b'.repeat(64),
        })
      }
      if (url.endsWith('/seasons/rotate')) {
        return new Response(seasonJson, { headers: { 'Content-Type': 'application/json' } })
      }
      return Response.json(detail)
    })
    vi.stubGlobal('fetch', fetchMock)

    const readOnly = await connect([BACKROOM_READ_SCOPE])
    try {
      for (const [name, args] of [
        ['rescore_model_context_submission', {
          submissionId: SUBMISSION, expectedStatus: 'scored', reason: 'scorer host fault',
          confirmation: 'RESCORE MODEL CONTEXT SUBMISSION',
        }],
        ['rotate_model_context_season', {
          reason: 'retire the seeds now', confirmation: 'ROTATE MODEL CONTEXT SEASON',
        }],
      ] as const) {
        const refused = await readOnly.client.callTool({ name, arguments: args })
        expect(refused.isError).toBe(true)
      }
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      await readOnly.client.close()
      await readOnly.server.close()
    }

    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    try {
      const refusals: Array<[string, Record<string, unknown>, string | null]> = [
        ['set_model_context_settings', {
          expectedRevision: 1, settings, reason: 'open shadow lane',
          confirmation: 'APPLY MODEL CONTEXT COMPETITION OFF',
        }, 'APPLY MODEL CONTEXT COMPETITION SHADOW'],
        ['set_model_context_settings', {
          expectedRevision: 1, settings: { ...settings, upload_fee_required: true },
          reason: 'charge uploads', confirmation: 'APPLY MODEL CONTEXT COMPETITION SHADOW',
        }, 'fee path is not built'],
        ['set_model_context_settings', {
          expectedRevision: 1, settings: { ...settings, rank_shares: [0.2, 0.5] },
          reason: 'reshape shares', confirmation: 'APPLY MODEL CONTEXT COMPETITION SHADOW',
        }, 'must not increase'],
        ['set_model_context_settings', {
          expectedRevision: 1, settings: { mode: 'shadow' }, reason: 'partial write',
          confirmation: 'APPLY MODEL CONTEXT COMPETITION SHADOW',
        }, null],
        ['reject_model_context_submission', {
          submissionId: SUBMISSION, expectedStatus: 'scored', reason: 'copied weights',
          confirmation: 'REJECT',
        }, null],
        ['reject_model_context_submission', {
          submissionId: SUBMISSION, expectedStatus: 'scored', reason: 'short',
          confirmation: 'REJECT MODEL CONTEXT SUBMISSION',
        }, null],
        ['rescore_model_context_submission', {
          submissionId: SUBMISSION, expectedStatus: 'rejected', reason: 'scorer host fault',
          confirmation: 'RESCORE MODEL CONTEXT SUBMISSION',
        }, null],
        ['rotate_model_context_season', {
          reason: 'retire the seeds now', confirmation: 'rotate',
        }, null],
      ]
      for (const [name, args, message] of refusals) {
        const refused = await client.callTool({ name, arguments: args })
        expect(refused.isError, name).toBe(true)
        if (message) expect(JSON.stringify(refused.content)).toContain(message)
      }
      expect(fetchMock).not.toHaveBeenCalled()

      const applied = json(await client.callTool({
        name: 'set_model_context_settings',
        arguments: {
          expectedRevision: 1, settings, reason: 'open shadow lane',
          confirmation: 'APPLY MODEL CONTEXT COMPETITION SHADOW',
        },
      })) as { revision: number }
      expect(applied.revision).toBe(2)
      const [settingsUrl, settingsInit] = fetchMock.mock.calls[0] as unknown as [
        string, RequestInit & { headers: Record<string, string> },
      ]
      expect(settingsUrl).toContain('/api/v1/admin/model-context/settings')
      expect(JSON.parse(String(settingsInit.body))).toEqual({
        scope: '*', expected_revision: 1, settings, reason: 'open shadow lane',
        actor: session.email, confirmation: 'APPLY MODEL CONTEXT COMPETITION SHADOW',
      })
      expect(settingsInit.headers['X-Admin-Actor']).toBe(session.email)

      const writes: Array<[string, Record<string, unknown>, string, Record<string, unknown>]> = [
        ['rescore_model_context_submission', {
          submissionId: SUBMISSION, expectedStatus: 'scored', reason: 'scorer host fault',
          confirmation: 'RESCORE MODEL CONTEXT SUBMISSION',
        }, `/api/v1/admin/model-context/submissions/${SUBMISSION}/rescore`, {
          expected_status: 'scored', reason: 'scorer host fault', actor: session.email,
          confirmation: 'RESCORE MODEL CONTEXT SUBMISSION',
        }],
        ['reject_model_context_submission', {
          submissionId: SUBMISSION, expectedStatus: 'scored', reason: 'copied weights',
          confirmation: 'REJECT MODEL CONTEXT SUBMISSION',
        }, `/api/v1/admin/model-context/submissions/${SUBMISSION}/reject`, {
          expected_status: 'scored', reason: 'copied weights', actor: session.email,
          confirmation: 'REJECT MODEL CONTEXT SUBMISSION',
        }],
        ['rotate_model_context_season', {
          reason: 'retire the seeds now', confirmation: 'ROTATE MODEL CONTEXT SEASON',
        }, '/api/v1/admin/model-context/seasons/rotate', {
          reason: 'retire the seeds now', actor: session.email,
          confirmation: 'ROTATE MODEL CONTEXT SEASON',
        }],
      ]
      for (const [name, args, path, body] of writes) {
        fetchMock.mockClear()
        const response = await client.callTool({ name, arguments: args })
        expect(response.isError, name).toBeFalsy()
        const [url, init] = fetchMock.mock.calls[0] as unknown as [
          string, RequestInit & { headers: Record<string, string> },
        ]
        expect(url).toContain(path)
        expect(init.method).toBe('POST')
        expect(JSON.parse(String(init.body))).toEqual(body)
        expect(init.headers['X-Admin-Actor']).toBe(session.email)
      }
    } finally {
      await client.close()
      await server.close()
    }
  })

  it('keeps a Platform refusal verbatim and says nothing was applied', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-token'
    vi.stubGlobal('fetch', vi.fn(async () => Response.json(
      { detail: 'a run of this submission is already queued or leased in season 1' },
      { status: 409 },
    )))
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    try {
      const refused = await client.callTool({
        name: 'rescore_model_context_submission',
        arguments: {
          submissionId: SUBMISSION, expectedStatus: 'scored', reason: 'scorer host fault',
          confirmation: 'RESCORE MODEL CONTEXT SUBMISSION',
        },
      })
      expect(refused.isError).toBe(true)
      const text = JSON.stringify(refused.content)
      expect(text).toContain('already queued or leased in season 1')
      expect(text).toContain('Nothing was applied')
      expect(text).toContain('get_model_context_submission')
    } finally {
      await client.close()
      await server.close()
    }
  })
})
