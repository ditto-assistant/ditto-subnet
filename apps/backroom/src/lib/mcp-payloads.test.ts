import { describe, expect, it } from 'vitest'
import {
  batchRetryValidationResponseSchema,
  screeningQuarantineListSchema,
  screeningSubmissionListSchema,
  stuckSubmissionsListSchema,
  validatorFleetObservabilitySchema,
} from './admin.schemas'
import {
  compactBatchRetryResponse,
  compactScreeningQuarantines,
  compactScreeningSubmissions,
  compactStuckSubmissions,
  compactValidatorFleet,
} from './mcp-payloads'

// The incident payload this work came from: 26 exhausted submissions recovered
// in one batch, every item carrying the same operator reason paragraph, the
// same actor, the same timestamp, and the same three validator hotkeys.
const REASON =
  'Validator infrastructure failure confirmed across the fleet: all three assigned validators lost their benchmark containers when the shared host ran out of disk during the v7 dataset pull, so every ticket expired without producing a score. No miner artifact, screening verdict, or accepted score is implicated. Restoring the exhausted slots so the same submissions can be scored again on healthy hosts.'
const ACTOR = 'peyton@omniaura.ai'
const CREATED_AT = '2026-07-25T18:04:11.512803Z'
const HOTKEYS = [
  '5FTyAqmQGgeCWvhkYS1BW7ecjZjkTn1YfrfhpQjbwYfPZzD1',
  '5GNJqTPyNqANBkUVMN1LPPrxXnFouWXoe2wNSmmEoLctxiZY',
  '5HGjWAeFDfFCWPsjFQdVV2Msvz2XtMktvgocEZcCj68kUMaw',
]

function agentId(index: number) {
  return `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`
}

function recoveryId(index: number) {
  return `11111111-0000-4000-8000-${String(index).padStart(12, '0')}`
}

function batchRetryPayload(count: number) {
  return batchRetryValidationResponseSchema.parse({
    granted: count,
    results: Array.from({ length: count }, (_, index) => ({
      agent_id: agentId(index),
      status: 'granted',
      detail: null,
      recovery: {
        recovery_id: recoveryId(index),
        agent_id: agentId(index),
        actor: ACTOR,
        reason: REASON,
        score_count: 0,
        bench_version: 7,
        expected_snapshot: index.toString(16).padStart(2, '0').repeat(32),
        granted_validator_hotkeys: HOTKEYS,
        created_at: CREATED_AT,
      },
    })),
  })
}

function stuckSubmissionsPayload(count: number) {
  return stuckSubmissionsListSchema.parse({
    generated_at: CREATED_AT,
    quorum: 3,
    counts: { exhausted: count },
    count,
    returned: count,
    limit: Math.max(1, count),
    offset: 0,
    has_more: false,
    submissions: Array.from({ length: count }, (_, index) => ({
      agent_id: agentId(index),
      miner_hotkey: HOTKEYS[index % HOTKEYS.length],
      agent_name: `miner-agent-${index}`,
      agent_version: 3,
      bench_version: 7,
      score_count: 1,
      quorum: 3,
      retry_state: 'exhausted',
      automatic_retry_available: false,
      recovery_allowed: true,
      blocking_reason: null,
      earliest_retry_after: null,
      attempts_used: 3,
      exhausted_validator_count: 3,
      snapshot: index.toString(16).padStart(2, '0').repeat(32),
      ticket_states: { expired: 3 },
    })),
  })
}

const bytes = (value: unknown) => JSON.stringify(value).length

describe('screening list summaries', () => {
  it('keeps only the newest attempt in the default submission summary', () => {
    const payload = screeningSubmissionListSchema.parse({
      count: 1,
      items: [
        {
          agent_id: agentId(1),
          miner_hotkey: HOTKEYS[0],
          agent_name: 'screened-agent',
          agent_version: 2,
          artifact_sha256: 'ab'.repeat(32),
          agent_status: 'screening_failed',
          screening_policy_version: 9,
          screening_reason: 'latest infrastructure failure',
          screening_reason_code: 'infra',
          submitted_at: '2026-07-25T12:00:00Z',
          attempts: [
            {
              attempt_id: '11111111-1111-1111-1111-111111111111',
              policy_version: 9,
              status: 'failed',
              screener_hotkey: HOTKEYS[1],
              started_at: '2026-07-25T14:00:00Z',
              deadline: '2026-07-25T14:30:00Z',
              finished_at: '2026-07-25T14:10:00Z',
              reason: 'latest infrastructure failure',
            },
            {
              attempt_id: '22222222-2222-2222-2222-222222222222',
              policy_version: 8,
              status: 'expired',
              screener_hotkey: HOTKEYS[2],
              started_at: '2026-07-25T13:00:00Z',
              deadline: '2026-07-25T13:30:00Z',
              finished_at: null,
              reason: 'older timeout'.repeat(80),
            },
          ],
        },
      ],
    })

    const summary = compactScreeningSubmissions(payload, 'summary') as {
      detail: string
      items: Array<Record<string, unknown>>
    }
    expect(summary.detail).toBe('summary')
    expect(summary.items[0]).not.toHaveProperty('attempts')
    expect(summary.items[0]).toMatchObject({
      attempt_count: 2,
      latest_attempt: { status: 'failed', policy_version: 9 },
    })
    expect(bytes(summary)).toBeLessThan(bytes(payload) * 0.5)

    const full = compactScreeningSubmissions(payload, 'full') as {
      items: Array<{ attempts: Array<unknown> }>
    }
    expect(full.items[0].attempts).toHaveLength(2)
  })

  it('replaces quarantine evidence arrays with counts and codes in summary mode', () => {
    const payload = screeningQuarantineListSchema.parse({
      count: 1,
      items: [
        {
          quarantine_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
          agent_id: agentId(2),
          attempt_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
          miner_hotkey: HOTKEYS[0],
          agent_name: 'quarantined-agent',
          agent_version: 3,
          artifact_sha256: 'cd'.repeat(32),
          policy_version: 9,
          manifest_digest: 'ef'.repeat(32),
          finding_digest: '12'.repeat(32),
          reason_code: 'unsafe_source',
          evidence: Array.from({ length: 8 }, (_, index) => ({
            module_id: `module-${index}`,
            code: `CODE_${index}`,
            summary: 'long screener evidence '.repeat(30),
            digest: null,
          })),
          finding: {
            artifact_sha256: 'cd'.repeat(32),
            prompt_revision: 'review-v3',
            risk_level: 'high',
            confidence: 0.98,
            categories: ['credential_access'],
            evidence: Array.from({ length: 8 }, (_, index) => ({
              path: `src/file-${index}.py`,
              line: index + 1,
              category: 'credential_access',
            })),
            summary: 'The authored code reads ambient credentials.',
          },
          finding_verified: true,
          status: 'active',
          created_at: '2026-07-25T12:00:00Z',
          resolved_at: null,
          resolved_by: null,
          resolution: null,
          resolution_reason: null,
        },
      ],
    })

    const summary = compactScreeningQuarantines(payload, 'summary') as {
      items: Array<Record<string, unknown>>
    }
    expect(summary.items[0]).not.toHaveProperty('evidence')
    expect(summary.items[0]).toMatchObject({
      evidence_count: 8,
      finding: { risk_level: 'high', evidence_count: 8 },
    })
    expect(summary.items[0].evidence_codes).toEqual(
      expect.arrayContaining(['CODE_0', 'CODE_1']),
    )
    expect(summary.items[0].finding).not.toHaveProperty('evidence')
    expect(summary.items[0].finding).not.toHaveProperty('artifact_sha256')
    expect(bytes(summary)).toBeLessThan(bytes(payload) * 0.25)

    const full = compactScreeningQuarantines(payload, 'full') as {
      items: Array<{ evidence: Array<unknown>; finding: { evidence: Array<unknown> } }>
    }
    expect(full.items[0].evidence).toHaveLength(8)
    expect(full.items[0].finding.evidence).toHaveLength(8)
  })
})

describe('compactBatchRetryResponse', () => {
  it('does not repeat an invariant field on any item', () => {
    const compact = compactBatchRetryResponse(batchRetryPayload(26)) as unknown as {
      results: { granted: { shared: Record<string, unknown>; items: Array<Record<string, unknown>> } }
    }
    const { shared, items } = compact.results.granted

    // Everything identical across the batch is stated exactly once.
    expect(shared).toMatchObject({
      actor: ACTOR,
      reason: REASON,
      created_at: CREATED_AT,
      bench_version: 7,
      score_count: 0,
      granted_validator_hotkeys: HOTKEYS,
    })
    for (const key of Object.keys(shared)) {
      expect(items.some((item) => key in item)).toBe(false)
    }
    // And the whole payload states each of them exactly once, not 26 times.
    const encoded = JSON.stringify(compact)
    for (const value of [REASON, ACTOR, CREATED_AT, HOTKEYS[0]]) {
      expect(encoded.split(value).length - 1).toBe(1)
    }
  })

  it('keeps every agent and only the fields that differ', () => {
    const compact = compactBatchRetryResponse(batchRetryPayload(26)) as unknown as {
      counts: Record<string, number>
      results: { granted: { items: Array<Record<string, unknown>> } }
    }
    expect(compact.counts).toEqual({
      total: 26,
      granted: 26,
      idempotent: 0,
      skipped: 0,
    })
    expect(compact.results.granted.items).toHaveLength(26)
    expect(compact.results.granted.items[0]).toEqual({
      agent_id: agentId(0),
      recovery_id: recoveryId(0),
      expected_snapshot: '00'.repeat(32),
    })
  })

  it('groups by outcome and reports skips with their reason', () => {
    const payload = batchRetryValidationResponseSchema.parse({
      granted: 1,
      results: [
        {
          agent_id: agentId(0),
          status: 'granted',
          detail: null,
          recovery: {
            recovery_id: recoveryId(0),
            agent_id: agentId(0),
            actor: ACTOR,
            reason: REASON,
            score_count: 0,
            bench_version: 7,
            expected_snapshot: 'ab'.repeat(32),
            granted_validator_hotkeys: HOTKEYS,
            created_at: CREATED_AT,
          },
        },
        {
          agent_id: agentId(1),
          status: 'skipped',
          detail: 'validation state changed',
          recovery: null,
        },
      ],
    })
    expect(compactBatchRetryResponse(payload)).toEqual({
      counts: { total: 2, granted: 1, idempotent: 0, skipped: 1 },
      results: {
        // One granted row: nothing to hoist, so the recovery stays inline.
        granted: {
          items: [
            {
              agent_id: agentId(0),
              recovery_id: recoveryId(0),
              actor: ACTOR,
              reason: REASON,
              score_count: 0,
              bench_version: 7,
              expected_snapshot: 'ab'.repeat(32),
              granted_validator_hotkeys: HOTKEYS,
              created_at: CREATED_AT,
            },
          ],
        },
        skipped: {
          items: [{ agent_id: agentId(1), detail: 'validation state changed' }],
        },
      },
    })
  })

  it('never states agent_id at two nesting levels', () => {
    const compact = compactBatchRetryResponse(batchRetryPayload(3))
    const encoded = JSON.stringify(compact)
    expect(encoded.split(agentId(0)).length - 1).toBe(1)
  })

  it('is a fraction of the platform payload for a real batch', () => {
    const payload = batchRetryPayload(26)
    const before = bytes(payload)
    const after = bytes(compactBatchRetryResponse(payload))
    expect(after).toBeLessThan(before * 0.25)
  })
})

describe('compactStuckSubmissions', () => {
  it('keeps the server summary without dropping a submission', () => {
    const payload = stuckSubmissionsPayload(40)
    const compact = compactStuckSubmissions(payload) as {
      submissions: Array<Record<string, unknown>>
      submissions_shared: Record<string, unknown>
    }
    expect(compact.submissions).toHaveLength(40)
    expect(compact.submissions[0]).toMatchObject({
      agent_id: agentId(0),
      snapshot: '00'.repeat(32),
    })
    expect(compact.submissions[0]).not.toHaveProperty('tickets')
    // Every row carries only compact server-produced counts, which are hoisted
    // when invariant across the returned page.
    expect(compact.submissions_shared.ticket_states).toEqual({ expired: 3 })
    // The snapshot a retry needs survives summarisation.
    expect(compact.submissions.every((row) => 'snapshot' in row)).toBe(true)
  })

  it('states quorum once, in the envelope, not on every row', () => {
    const compact = compactStuckSubmissions(stuckSubmissionsPayload(40)) as {
      quorum: number
      submissions: Array<Record<string, unknown>>
    }
    expect(compact.quorum).toBe(3)
    expect(compact.submissions.some((row) => 'quorum' in row)).toBe(false)
  })

  it('hoists the fields shared by every stuck submission', () => {
    const compact = compactStuckSubmissions(stuckSubmissionsPayload(40)) as {
      submissions_shared: Record<string, unknown>
    }
    expect(compact.submissions_shared).toMatchObject({
      bench_version: 7,
      retry_state: 'exhausted',
      recovery_allowed: true,
      exhausted_validator_count: 3,
    })
  })

  it('is a fraction of the platform payload for a fleet-wide read', () => {
    const payload = stuckSubmissionsPayload(40)
    const before = bytes(payload)
    const summary = bytes(compactStuckSubmissions(payload))
    expect(summary).toBeLessThan(before * 0.5)
  })

  it('keeps a default ten-row triage page under four kilobytes', () => {
    expect(bytes(compactStuckSubmissions(stuckSubmissionsPayload(10)))).toBeLessThan(
      4_000,
    )
  })
})

describe('compactValidatorFleet', () => {
  it('counts serving validators and buckets software versions before paging', () => {
    const payload = validatorFleetObservabilitySchema.parse({
      generated_at: '2026-08-20T13:40:00Z',
      active_bench_version: 11,
      validators: [
        {
          validator_hotkey: '5' + 'A'.repeat(47),
          software_version: '0.64.0',
          protocol_version: 23,
          online: true,
          health: 'healthy',
          bench_serviceability: 'serving',
          healthy_slots: ['slot-0'],
          active_benchmarks: [],
          updater_status: {
            enabled: true,
            state: 'idle',
            current_version: '0.64.0',
            failed_candidate_count: 0,
            suppressed: false,
            observed_at: 1,
          },
        },
        {
          validator_hotkey: '5' + 'B'.repeat(47),
          software_version: '0.63.1',
          protocol_version: 22,
          online: true,
          health: 'healthy',
          bench_serviceability: 'software_obsolete',
          healthy_slots: ['slot-0'],
          active_benchmarks: [],
          updater_status: {
            enabled: true,
            state: 'prefetched',
            current_version: '0.63.1',
            candidate_version: '0.64.0',
            failed_candidate_count: 0,
            suppressed: false,
            observed_at: 2,
          },
        },
      ],
    })

    const compact = compactValidatorFleet(payload)
    expect(compact.serving_count).toBe(1)
    expect(compact.online_serving_count).toBe(1)
    expect(compact.software_obsolete_count).toBe(1)
    expect(compact.rollout.software_versions).toEqual([
      { value: '0.63.1', count: 1, online_count: 1, serving_count: 0 },
      { value: '0.64.0', count: 1, online_count: 1, serving_count: 1 },
    ])
    expect(compact.rollout.updater_current_versions.map((row) => row.value)).toEqual([
      '0.63.1',
      '0.64.0',
    ])
  })
})
