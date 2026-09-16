import { describe, expect, it } from 'vitest'
import { benchmarkCanarySchema, issueBenchmarkCanaryInputSchema } from './benchmark-canary.schemas'

const id = '11111111-1111-4111-8111-111111111111'
describe('benchmark canary boundary', () => {
  it('requires exact version and agent confirmation', () => {
    const input = { canaryId: id, agentId: id, benchVersion: 13,
      validatorHotkey: '5'.repeat(48), slotId: 'slot-0',
      expectedArtifactSha256: 'a'.repeat(64), expectedScreenedImageSha256: 'b'.repeat(64),
      expectedActiveVersion: 12, reason: 'A non-authoritative diagnostic',
      confirmation: `ISSUE CANARY V13 ${id}` }
    expect(issueBenchmarkCanaryInputSchema.safeParse(input).success).toBe(true)
    expect(issueBenchmarkCanaryInputSchema.safeParse({ ...input, confirmation: 'yes' }).success).toBe(false)
    expect(issueBenchmarkCanaryInputSchema.safeParse({ ...input, benchVersion: 12 }).success).toBe(false)
  })
  it('preserves 63-bit seeds and strips private raw reports', () => {
    const result = benchmarkCanarySchema.parse({ canary_id: id, agent_id: id,
      bench_version: 13, validator_hotkey: '5'.repeat(48), slot_id: 'slot-0',
      artifact_sha256: 'a'.repeat(64), screened_image_sha256: 'b'.repeat(64),
      dataset_sha256: 'c'.repeat(64), seed: '9223372036854775807', run_size: 'full',
      actor: 'staff', reason: 'Diagnostic', issued_at: 'now', deadline: 'later',
      status: 'completed', finished_at: 'now', failure_detail: null, authoritative: false,
      signature: 'private signature', result: { run_id: 'r', bench_version: 13,
        composite: .5, tool_mean: .5, memory_mean: .5, n: 351, generated_at: 'now',
        details: { transcript: 'private trace' }, source: 'private source' },
    })
    expect(result.seed).toBe('9223372036854775807')
    expect(JSON.stringify(result)).not.toContain('private')
  })
})
