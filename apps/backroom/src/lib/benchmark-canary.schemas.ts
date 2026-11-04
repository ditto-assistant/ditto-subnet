import { z } from 'zod'

const digest = z.string().regex(/^[0-9a-f]{64}$/)
export const listBenchmarkCanariesInputSchema = z.object({
  limit: z.number().int().min(1).max(50).default(20),
  offset: z.number().int().min(0).default(0),
})
export const issueBenchmarkCanaryInputSchema = z.object({
  canaryId: z.uuid(),
  agentId: z.uuid(),
  benchVersion: z.number().int().positive(),
  validatorHotkey: z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{48}$/),
  slotId: z.string().regex(/^slot-[0-7]$/),
  expectedArtifactSha256: digest,
  expectedScreenedImageSha256: digest,
  expectedActiveVersion: z.number().int().positive(),
  reason: z.string().trim().min(8),
  confirmation: z.string(),
}).superRefine((input, ctx) => {
  if (input.confirmation !== `ISSUE CANARY V${input.benchVersion} ${input.agentId}`) {
    ctx.addIssue({ code: 'custom', path: ['confirmation'], message: 'Exact canary confirmation required' })
  }
})

export const getBenchmarkCanaryInputSchema = z.object({ canaryId: z.uuid() })
export const cancelBenchmarkCanaryInputSchema = getBenchmarkCanaryInputSchema.extend({
  reason: z.string().trim().min(8),
  confirmation: z.string(),
}).superRefine((input, ctx) => {
  if (input.confirmation !== `CANCEL CANARY ${input.canaryId}`) {
    ctx.addIssue({ code: 'custom', path: ['confirmation'], message: 'Exact cancellation confirmation required' })
  }
})

const count = z.number().int().nonnegative()
const bps = z.number().int().min(0).max(10_000)

// Bench v16+ `details.priced_cost`: numbers only (token counts, nano-USD
// costs, ratio and factor), so a canary can be checked against the published
// priced-cost rule without the raw report. Strict picks: no other key passes.
const canaryPricedCostSchema = z.object({
  contract_id: z.string().max(64),
  posture: z.enum(['enforce', 'shadow']),
  applied: z.boolean(),
  result: z.enum(['priced', 'insufficient_evidence']),
  window: z.string().max(32),
  source: z.string().max(32).optional(),
  cases: count,
  prompt_tokens: count,
  completion_tokens: count,
  embedding_tokens: count,
  decision_calls: count,
  decision_input_tokens: count,
  decision_output_tokens: count,
  cost_nano_usd: count,
  normalization_cases: count,
  normalized_cost_nano_usd: count,
  reference_nano_usd: count,
  ratio_bps: z.number().int().min(0).max(100_000),
  floor_bps: bps,
  factor_bps: bps,
  applied_factor_bps: bps,
})

// Bench v16+ `details.dueling` aggregates: the datagen half and gate the fold
// recomputes from, and each column's case statistics. Never case text.
const canaryDuelingSchema = z.object({
  season_id: z.string().max(64),
  datagen_raw: z.number().min(0).max(1),
  datagen_gate: z.number().min(0).max(1),
  column_weight: z.number().min(0).max(1),
  composite_stderr: z.number().nonnegative().optional(),
  columns: z.array(z.object({
    column_id: z.string().max(32),
    n: count,
    sum: z.number().nonnegative(),
    substituted: z.boolean(),
  })).max(16),
})

// Never forward arbitrary scorer details, source, traces or capabilities through
// a read-scoped tool. This is a summary of non-authoritative reported results;
// from bench 16 it adds the priced-cost and dueling aggregates above.
export const benchmarkCanarySchema = z.object({
  canary_id: z.uuid(), agent_id: z.uuid(), bench_version: z.number().int(),
  validator_hotkey: z.string(), slot_id: z.string(), artifact_sha256: digest,
  screened_image_sha256: digest, dataset_sha256: digest,
  seed: z.string().regex(/^\d+$/), run_size: z.string(), actor: z.string(), reason: z.string(),
  issued_at: z.string(), deadline: z.string(), status: z.string(),
  finished_at: z.string().nullable(), failure_detail: z.string().nullable(),
  authoritative: z.literal(false),
  result: z.object({
    run_id: z.string(), bench_version: z.number().int().nullable().optional(),
    composite: z.number(), tool_mean: z.number(), memory_mean: z.number(),
    n: z.number().int(), generated_at: z.string(),
    details: z.object({
      priced_cost: canaryPricedCostSchema.nullish().catch(null),
      dueling: canaryDuelingSchema.nullish().catch(null),
    }).nullish().catch(null).transform((d) =>
      d && (d.priced_cost || d.dueling)
        ? { priced_cost: d.priced_cost ?? null, dueling: d.dueling ?? null }
        : undefined,
    ),
  }).nullable(),
})

export const OPERATOR_DIAGNOSTIC_ISOLATE_CONFIRMATION = 'ISOLATE OPERATOR DIAGNOSTIC AGENT'
export const getOperatorDiagnosticInputSchema = z.object({ agentId: z.uuid() })
export const isolateOperatorDiagnosticInputSchema = getOperatorDiagnosticInputSchema.extend({
  expectedArtifactSha256: digest,
  reason: z.string().trim().min(8),
  confirmation: z.literal(OPERATOR_DIAGNOSTIC_ISOLATE_CONFIRMATION),
})
export const operatorDiagnosticSchema = z.object({
  agent_id: z.uuid(),
  miner_hotkey: z.string(),
  operator_owned: z.boolean(),
  shadow: z.boolean(),
  status: z.string(),
  withdrawals: z.array(z.object({
    bench_version: z.number().int(), created_at: z.string(), in_force: z.boolean(),
  })),
  ordinary_lease_count: z.number().int(),
  diagnostic_lease_count: z.number().int(),
  score_count: z.number().int(),
  isolation_refusal: z.string().nullable(),
  diagnostic_refusal_by_version: z.record(z.string(), z.string().nullable()),
})
