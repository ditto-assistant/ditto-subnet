import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchProviderOutages, voidInfraScores } from './infra-voids.service'

vi.mock('@tanstack/react-start/server-only', () => ({}))

const agentId = 'dba51d92-a0eb-4e97-8a33-9d06016c3fbd'
const validator = '5CqJAjSjv8fjF9uAQpDLyfN1hZEvBjwpFgcGeLbYpcbSaD1C'
const requestId = '11111111-2222-5333-8444-555555555555'

function stubPlatform(body: unknown) {
  vi.stubEnv('DITTO_PLATFORM_API_BASE_URL', 'https://platform.example.test')
  vi.stubEnv('DITTO_ADMIN_API_TOKEN', 'synthetic-infra-void-token')
  const mock = vi.fn().mockResolvedValue(Response.json(body))
  vi.stubGlobal('fetch', mock)
  return mock
}

describe('voidInfraScores', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })

  it('forwards sampleKind and parses a canonical replacement outcome', async () => {
    const mock = stubPlatform({
      bench_version: 18,
      dry_run: true,
      applied_count: 0,
      outcomes: [{
        agent_id: agentId,
        validator_hotkey: validator,
        seed: '5406736405836416917',
        sample_kind: 'canonical',
        disposition: 'ready',
        retest_request_id: requestId,
        evidence: { chat_requests: 1473, outage_failed_requests: 1071 },
        evidence_digest: 'f'.repeat(64),
      }],
    })

    const result = await voidInfraScores({
      items: [{
        agentId,
        validatorHotkey: validator,
        seed: '5406736405836416917',
        expectedRunId: 'ebed9c89-c1d8-4a7a-9470-c5b3251ddd8e',
        expectedComposite: 0.6312,
      }],
      reason: 'Nov 17 openrouter 402 outage measured the provider',
    }, 'operator@example.test')

    const body = JSON.parse(String(mock.mock.calls[0][1].body))
    expect(body.items[0].sample_kind).toBe('auto')
    expect(body.dry_run).toBe(true)
    const [outcome] = result.outcomes
    expect(outcome.sample_kind).toBe('canonical')
    expect(outcome.retest_request_id).toBe(requestId)
    expect(outcome.void_id).toBeNull()
  })
})

describe('fetchProviderOutages', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })

  it('keeps each canonical flag\'s void disposition and request id', async () => {
    stubPlatform({
      circuits: [],
      sweep_policy: {
        interval_seconds: 300, batch_limit: 200, first_lookback_seconds: 21600,
        settle_lag_seconds: 120, outage_failure_codes: ['upstream_http_402'],
        min_outage_failed_calls: 5, outage_failed_calls: 20, outage_failed_share: 0.05,
      },
      runs_total: 1,
      limit: 20,
      offset: 0,
      runs: [{
        sweep_id: '291ff099-de4f-48ce-bc88-1ca229db8ba9',
        bench_version: 18,
        started_at: '2026-11-17T12:17:30Z',
        finished_at: '2026-11-17T12:17:31Z',
        watermark_from: '2026-11-17T12:10:30Z',
        watermark_to: '2026-11-17T12:15:30Z',
        examined_count: 0,
        voided_count: 0,
        error_count: 0,
        canonical_flagged: [{
          agent_id: agentId,
          validator_hotkey: validator,
          run_id: 'ebed9c89-c1d8-4a7a-9470-c5b3251ddd8e',
          composite: 0.6312,
          accepted_at: '2026-11-17T12:13:26Z',
          grant_id: '33333333-4444-4555-8666-777777777777',
          chat_requests: 1473,
          failed_requests: 1071,
          outage_failed_requests: 1071,
          failure_codes: { upstream_http_402: 1071 },
          void_disposition: 'applied',
          void_request_id: requestId,
        }],
      }],
    })

    const result = await fetchProviderOutages({})

    const [flag] = result.runs[0].canonical_flagged
    expect(flag.void_disposition).toBe('applied')
    expect(flag.void_request_id).toBe(requestId)
    expect(flag.void_detail).toBeNull()
  })
})
