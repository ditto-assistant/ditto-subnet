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
  it('exposes the bench 16 priced-cost and dueling aggregates, never other details', () => {
    const priced = { contract_id: 'priced-cost-v1', posture: 'enforce', applied: true,
      result: 'priced', window: 'whole_run', source: 'relay', cases: 350,
      prompt_tokens: 22_318_268, completion_tokens: 1_690_233, embedding_tokens: 294_000,
      decision_calls: 0, decision_input_tokens: 0, decision_output_tokens: 0,
      cost_nano_usd: 1_816_989_710, normalization_cases: 350,
      normalized_cost_nano_usd: 1_816_989_710, reference_nano_usd: 1_000_000_000,
      ratio_bps: 18_169, floor_bps: 6000, factor_bps: 6733, applied_factor_bps: 6733,
      prices: { chat_model: 'private-ish' } }
    const dueling = { season_id: 'season', season_index: 3, slice_sha256: 'd'.repeat(64),
      owner_key: 'coldkey:private', datagen_raw: 0.8, datagen_gate: 0.6733,
      column_weight: 0.1, composite_stderr: 0.01,
      columns: [{ column_id: 'c0', owner_key: 'coldkey:private', submission_id: 'x',
        n: 35, sum: 30, sum_sq: 29, substituted: false, grading_bench_version: 15 }] }
    const result = benchmarkCanarySchema.parse({ canary_id: id, agent_id: id,
      bench_version: 16, validator_hotkey: '5'.repeat(48), slot_id: 'slot-0',
      artifact_sha256: 'a'.repeat(64), screened_image_sha256: 'b'.repeat(64),
      dataset_sha256: 'c'.repeat(64), seed: '1', run_size: 'full',
      actor: 'staff', reason: 'Diagnostic', issued_at: 'now', deadline: 'later',
      status: 'completed', finished_at: 'now', failure_detail: null, authoritative: false,
      result: { run_id: 'r', bench_version: 16, composite: .5, tool_mean: .5,
        memory_mean: .5, n: 350, generated_at: 'now',
        details: { priced_cost: priced, dueling, transcript: 'private trace',
          tool_efficiency: 0.95, memory_over_call: 0.94, metamorphic_consistency: 1,
          metamorphic: { groups: 5, split_groups: 0, reference_groups: 13, factor: 1 },
          conversational_sanity: 1, transform_robustness: 0.9,
          audit_pairs: { both_correct: 9, base_only: 1, transform_only: 0, both_wrong: 0 },
          token_efficiency: { multiplier: 1, formula_version: 'private-formula' },
          v9_base: { run_id: 'private-run', ordinary_composite_micros: 633747,
            effective_composite_micros: 633747, semantic_gate_factor_bps: 10000,
            applied_gate_factor_bps: 10000, score_gates_sha256: 'private-digest',
            score_gates: { rollout_mode: 'shadow', threshold_profile: { id: 'private-profile' },
              model_use: { result: 'passed', factor_bps: 10000, observed_requests: 9 },
              answer_stuffing: { result: 'passed', factor_bps: 10000 } } } } },
    })
    expect(result.result?.details?.priced_cost).toMatchObject({ factor_bps: 6733, window: 'whole_run' })
    expect(result.result?.details?.dueling?.datagen_gate).toBe(0.6733)
    expect(result.result?.details?.dueling?.columns[0]).toEqual({ column_id: 'c0', n: 35, sum: 30, substituted: false })
    expect(result.result?.details?.gate_factors).toMatchObject({
      tool_efficiency: 0.95, memory_over_call: 0.94, transform_robustness: 0.9,
      metamorphic: { groups: 5, split_groups: 0, reference_groups: 13, factor: 1 },
      token_efficiency: { multiplier: 1 },
      v9_base: { applied_gate_factor_bps: 10000, ordinary_composite_micros: 633747,
        score_gates: { rollout_mode: 'shadow', model_use: { result: 'passed', factor_bps: 10000 } } },
    })
    expect(JSON.stringify(result)).not.toContain('private')
    // A malformed block is dropped, not a failed read.
    const broken = benchmarkCanarySchema.parse({ ...result, result: { ...result.result,
      details: { priced_cost: { factor_bps: 'x' } } } })
    expect(broken.result?.details).toBeUndefined()
  })
  it('drops out-of-contract gate verdicts, modes and multipliers', () => {
    const base = { canary_id: id, agent_id: id, bench_version: 16, validator_hotkey: '5'.repeat(48),
      slot_id: 'slot-0', artifact_sha256: 'a'.repeat(64), screened_image_sha256: 'b'.repeat(64),
      dataset_sha256: 'c'.repeat(64), seed: '1', run_size: 'full', actor: 'staff', reason: 'Diagnostic',
      issued_at: 'now', deadline: 'later', status: 'completed', finished_at: 'now',
      failure_detail: null, authoritative: false }
    const dueling = { season_id: 's', datagen_raw: 0.9, datagen_gate: 0.9, column_weight: 0.1, columns: [] }
    const parse = (details: object) => benchmarkCanarySchema.parse({ ...base, result: { run_id: 'r',
      bench_version: 16, composite: .5, tool_mean: .5, memory_mean: .5, n: 350, generated_at: 'now', details } })
    const gates = (v9: object, extra: object = {}) => parse({ dueling, ...extra, v9_base: {
      ordinary_composite_micros: 1, effective_composite_micros: 1, semantic_gate_factor_bps: 10000,
      applied_gate_factor_bps: 10000, score_gates: v9 } }).result?.details?.gate_factors
    expect(gates({ rollout_mode: 'disabled' })?.v9_base?.score_gates).toBeNull()
    expect(gates({ rollout_mode: 'enforce', model_use: { result: 'free text', factor_bps: 0 } })
      ?.v9_base?.score_gates?.model_use).toBeNull()
    expect(gates({ rollout_mode: 'enforce' }, { token_efficiency: { multiplier: 1.5 } })?.token_efficiency).toBeNull()
  })
  it('exposes aggregate run budget exhaustion without exposing raw case details', () => {
    const base = { canary_id: id, agent_id: id, bench_version: 18, validator_hotkey: '5'.repeat(48),
      slot_id: 'slot-0', artifact_sha256: 'a'.repeat(64), screened_image_sha256: 'b'.repeat(64),
      dataset_sha256: 'c'.repeat(64), seed: '1', run_size: 'full', actor: 'staff', reason: 'Diagnostic',
      issued_at: 'now', deadline: 'later', status: 'completed', finished_at: 'now',
      failure_detail: null, authoritative: false }
    const budget = { contract_id: 'run-budget-v1', budget_nano_usd: 1_000_000_000, budget_usd: 1,
      cases: 350, exhausted: true, admitted_cases: 46, unrun_cases: 304, unrun_tool_cases: 4,
      unrun_memory_cases: 125, unrun_slice_cases: 175, spent_nano_usd_at_cutoff: 1_015_309_800,
      reason: 'run_budget_exhausted', private_detail: 'private budget detail' }
    const wrap = (run_budget: unknown) => benchmarkCanarySchema.parse({ ...base, result: {
      run_id: 'r', bench_version: 18, composite: .039, tool_mean: .52, memory_mean: 0,
      n: 350, generated_at: 'now', details: { run_budget, transcript: 'private transcript' },
      per_case: [{ notes: ['private case content'] }],
    } })
    const result = wrap(budget)
    expect(result.result?.details?.run_budget).toMatchObject({ exhausted: true,
      admitted_cases: 46, unrun_memory_cases: 125, unrun_slice_cases: 175,
      reason: 'run_budget_exhausted' })
    expect(JSON.stringify(result)).not.toContain('private')
    expect(wrap({ ...budget, reason: 'private free text' }).result?.details).toBeUndefined()
    expect(wrap({ ...budget, exhausted: 'yes' }).result?.details).toBeUndefined()
    expect(wrap({ ...budget, exhausted: false, reason: undefined }).result?.details?.run_budget?.exhausted).toBe(false)
    expect(wrap({ ...budget, reason: null, budget_usd: null }).result?.details?.run_budget)
      .toMatchObject({ reason: null, budget_usd: null, unrun_cases: 304 })
    expect(wrap({ ...budget, budget_usd: undefined }).result?.details?.run_budget?.unrun_cases).toBe(304)
  })
})
