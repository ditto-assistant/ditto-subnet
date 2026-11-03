import { z } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'

// Stranded previous-era submissions
// (GET /api/v1/admin/benchmark-rollout/stranded-previous-era): read-only.
// Scored/live submissions with previous-era scores, no active-version score and
// no admission path into the active era, so no active-version dataset or
// ticket exists for them and they are absent from the active leaderboard.

type PlatformResponseShape<Response extends object> = {
  [Field in keyof Response]-?: z.ZodType<Response[Field]>
}

type Schemas = PlatformComponents['schemas']

const timestamp = () => z.string().datetime({ offset: true })

const strandedEraAgentRowSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  agent_version: z.number().int().nullable(),
  miner_hotkey: z.string(),
  owner_key: z.string(),
  status: z.string(),
  submitted_at: timestamp(),
  first_screening_pass_at: timestamp().nullable(),
  previous_score_count: z.number().int(),
  previous_median_composite: z.number().nullable(),
  previous_finalized: z.boolean(),
  has_active_dataset: z.boolean(),
  queue_withdrawn: z.boolean(),
  cohort_sibling_agent_id: z.string().uuid().nullable(),
  owner_best_stranded: z.boolean(),
  reason: z.enum(['owner_in_cohort', 'owner_outside_cohort']),
} satisfies PlatformResponseShape<Schemas['StrandedEraAgentRow']>)

const strandedEraRolloutSummarySchema = z.object({
  rollout_id: z.string().uuid(),
  from_version: z.number().int(),
  desired_version: z.number().int(),
  status: z.string(),
  created_at: timestamp(),
  activated_at: timestamp().nullable(),
  cohort_size: z.number().int(),
} satisfies PlatformResponseShape<Schemas['StrandedEraRolloutSummary']>)

export const strandedPreviousEraSchema = z.object({
  active_bench_version: z.number().int(),
  previous_bench_version: z.number().int().nullable(),
  rollout: strandedEraRolloutSummarySchema.nullable(),
  prev_gen_carryover_enabled: z.boolean(),
  total: z.number().int(),
  owner_count: z.number().int(),
  finalized_count: z.number().int(),
  owner_in_cohort_count: z.number().int(),
  owner_outside_cohort_count: z.number().int(),
  truncated: z.boolean(),
  limit: z.number().int(),
  offset: z.number().int(),
  items: z.array(strandedEraAgentRowSchema),
} satisfies PlatformResponseShape<Schemas['StrandedEraResponse']>)

export const strandedPreviousEraInputSchema = z.object({
  limit: z.number().int().min(1).max(500).default(200),
  offset: z.number().int().min(0).default(0),
  ownerBestOnly: z.boolean().default(false),
})

export type StrandedPreviousEra = z.infer<typeof strandedPreviousEraSchema>
