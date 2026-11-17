import { describe, expect, it } from 'vitest'
import { agentScoringReadinessSchema, eraChangeSchema, stuckSubmissionSchema } from './admin.schemas'

// SN-485: Backroom must keep the miner-facing era_changed reason that Platform
// attaches to operator rows, not strip it as an unknown key.
const change = {
  reason: 'not_admitted_to_active_bench',
  bench_version: 18,
  scored_bench_version: null,
  next_step: 'resubmit',
} as const

describe('era change on operator rows', () => {
  it('keeps the reason, next step and serves flag on a stuck row', () => {
    const parsed = stuckSubmissionSchema
      .pick({ era_change: true, serves_active_bench: true, blocking_reason: true })
      .parse({
        era_change: change,
        serves_active_bench: false,
        blocking_reason:
          "historical submission is not admitted to the v18 validator queue (not_admitted_to_active_bench); miners see 'Era changed · resubmit for v18', next step: resubmit to compete on v18",
      })
    expect(parsed.era_change).toEqual(change)
    expect(parsed.serves_active_bench).toBe(false)
    expect(parsed.blocking_reason).toContain('Era changed · resubmit for v18')
  })

  it('reads an older platform as unknown, never as serving', () => {
    const parsed = stuckSubmissionSchema
      .pick({ era_change: true, serves_active_bench: true })
      .parse({})
    expect(parsed).toEqual({ era_change: null, serves_active_bench: null })
  })

  it('keeps the era change on scoring readiness', () => {
    const parsed = agentScoringReadinessSchema.pick({ era_change: true }).parse({
      era_change: { ...change, scored_bench_version: 17 },
    })
    expect(parsed.era_change?.scored_bench_version).toBe(17)
  })

  it('rejects a reason or next step Platform does not publish', () => {
    expect(eraChangeSchema.safeParse({ ...change, next_step: 'free_resubmit' }).success).toBe(
      false,
    )
    expect(eraChangeSchema.safeParse({ ...change, reason: 'other' }).success).toBe(false)
  })
})
