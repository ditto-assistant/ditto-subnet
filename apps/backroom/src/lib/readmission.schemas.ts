import { z } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'

// SN-485 readmission activation, read-only. Platform resolves it from the
// submission settings revision's readmission_announced_at and the live active
// benchmark: active only when the announcement is set, the active benchmark
// is v19 or later and seven days have passed since the announcement. The
// price is always the upload default fee. Backroom never writes the
// announcement; that is a Platform admin API write.

type PlatformResponseShape<Response extends object> = {
  [Field in keyof Response]-?: z.ZodType<Response[Field]>
}

type GeneratedReadmissionActivationState =
  PlatformComponents['schemas']['ReadmissionActivationState']

export const READMISSION_INACTIVE_REASONS = [
  'announcement_unset',
  'bench_version_below_minimum',
  'notice_period',
] as const

export const readmissionActivationSchema = z.object({
  active: z.boolean(),
  inactive_reasons: z.array(z.enum(READMISSION_INACTIVE_REASONS)).max(8).default([]),
  announced_at: z.string().nullable().default(null),
  effective_at: z.string().nullable().default(null),
  active_bench_version: z.number().int().nonnegative(),
  min_bench_version: z.number().int().nonnegative(),
  notice_period_seconds: z.number().int().nonnegative(),
  fee_amount_rao: z.number().int().positive(),
  fee_source: z.literal('upload_default').default('upload_default'),
  settings_revision: z.number().int().nonnegative(),
} satisfies PlatformResponseShape<GeneratedReadmissionActivationState>)

export type ReadmissionActivation = z.infer<typeof readmissionActivationSchema>

type GeneratedReadmissionCostBasis = PlatformComponents['schemas']['ReadmissionCostBasis']

// The measured per-agent evaluation cost on the active bench (SN-485 section 2):
// median across quorum agents of each agent's mean scored-run cost, times
// runs_per_evaluation. Null costs: too few agents to measure.
export const readmissionCostBasisSchema = z.object({
  bench_version: z.number().int(),
  agents_sampled: z.number().int().nonnegative(),
  runs_per_evaluation: z.number().int().positive(),
  median_run_cost_usd: z.number().nullable().default(null),
  median_agent_cost_usd: z.number().nullable().default(null),
  measured_at: z.string(),
  source: z.string().default(''),
} satisfies PlatformResponseShape<GeneratedReadmissionCostBasis>)

const paidReadmissionRowSchema = z.object({
  block_hash: z.string(),
  extrinsic_index: z.number().int().nonnegative(),
  agent_id: z.string().uuid(),
  agent_name: z.string().nullable().default(null),
  miner_hotkey: z.string(),
  miner_coldkey: z.string(),
  bench_version: z.number().int(),
  rollout_id: z.string().uuid().nullable().default(null),
  outcome: z.enum(['admitted', 'credited']),
  funding: z.enum(['fresh', 'credit']),
  amount_rao: z.number().int(),
  refusal_reason: z.string().nullable().default(null),
  credit_audit: z.record(z.string(), z.unknown()).nullable().default(null),
  credit_spent: z.boolean().nullable().default(null),
  credit_expires_at: z.string().nullable().default(null),
  recorded_at: z.string(),
  updated_at: z.string(),
})

const uploadReadmissionRowSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string().nullable().default(null),
  kind: z.enum(['identical_rescore', 'near_duplicate']),
  lineage_agent_id: z.string().uuid(),
  lineage_evaluation_completed: z.boolean(),
  bench_version: z.number().int(),
  miner_coldkey: z.string(),
  amount_rao: z.number().int().nullable().default(null),
  created_at: z.string(),
})

const readmissionTotalsSchema = z.object({
  admitted: z.number().int().default(0),
  admitted_amount_rao: z.number().int().default(0),
  credit_funded: z.number().int().default(0),
  credited: z.number().int().default(0),
  credited_amount_rao: z.number().int().default(0),
  upload_readmissions: z.number().int().default(0),
  upload_readmission_amount_rao: z.number().int().default(0),
})

export const adminReadmissionsSchema = z.object({
  activation: readmissionActivationSchema,
  cost_basis: readmissionCostBasisSchema.nullable().default(null),
  bench_version: z.number().int().nullable().default(null),
  totals: readmissionTotalsSchema,
  free_admissions_by_category: z.record(z.string(), z.number().int()).default({}),
  paid: z.array(paidReadmissionRowSchema).max(500).default([]),
  uploads: z.array(uploadReadmissionRowSchema).max(500).default([]),
  truncated: z.boolean().default(false),
  generated_at: z.string(),
})

export const listReadmissionsInputSchema = z.object({
  benchVersion: z.number().int().positive().optional(),
  minerColdkey: z.string().trim().min(1).max(128).optional(),
  limit: z.number().int().min(1).max(500).default(100),
  offset: z.number().int().min(0).max(100_000).default(0),
})

export type AdminReadmissions = z.infer<typeof adminReadmissionsSchema>
