import { describe, expect, it } from 'vitest'
import { benchmarkRolloutControlSchema } from './admin.schemas'

describe('rollout retry diagnostics', () => {
  const schema = benchmarkRolloutControlSchema.pick({ retry_diagnostics: true })

  it('accepts an older platform without claiming any diagnostics', () => {
    expect(schema.parse({})).toEqual({ retry_diagnostics: [] })
  })

  it('preserves exhausted priority-member evidence for MCP', () => {
    const evidence = {
      agent_id: '842c28de-6b6c-445a-8c3f-cf5492422ef6',
      agent_name: 'fixture',
      bench_version: 13,
      blocks_activation: true,
      state: 'exhausted',
      score_count: 1,
      recovery_allowed: true,
      blocking_reason: null,
      earliest_retry_after: null,
    }
    expect(schema.parse({ retry_diagnostics: [evidence] }).retry_diagnostics)
      .toEqual([evidence])
    expect(schema.safeParse({ retry_diagnostics: [{ ...evidence, score_count: -1 }] }).success)
      .toBe(false)
  })
})
