import { z } from 'zod'
import { auditReasonSchema } from './admin.schemas'

// int63 seeds stay decimal strings across the JSON/JavaScript boundary.
const seedSchema = z.string().regex(/^(0|[1-9][0-9]*)$/)

export const VOID_INFRA_SCORES_PHRASE = 'VOID INFRA SCORES'

export const voidInfraScoresInputSchema = z.object({
  items: z.array(z.object({
    agentId: z.string().uuid(),
    validatorHotkey: z.string().min(1).max(64),
    seed: seedSchema,
    expectedRunId: z.string().trim().min(1).max(200),
    expectedComposite: z.number().min(0).max(1),
  })).min(1).max(50),
  reason: auditReasonSchema(8),
  dryRun: z.boolean().default(true),
  confirmation: z.literal(VOID_INFRA_SCORES_PHRASE).optional(),
})

export const infraVoidOutcomeSchema = z.object({
  agent_id: z.string().uuid(),
  validator_hotkey: z.string(),
  seed: seedSchema,
  disposition: z.enum([
    'ready', 'applied', 'already_voided', 'not_found', 'stale_guard', 'insufficient_evidence',
  ]),
  detail: z.string().nullable().default(null),
  void_id: z.string().uuid().nullable().default(null),
  void_generation: z.number().int().nonnegative().nullable().default(null),
  evidence: z.record(z.string(), z.unknown()).default({}),
  evidence_digest: z.string().nullable().default(null),
})

export const voidInfraScoresResponseSchema = z.object({
  bench_version: z.number().int().positive(),
  dry_run: z.boolean(),
  applied_count: z.number().int().nonnegative(),
  outcomes: z.array(infraVoidOutcomeSchema),
})

export const listInfraVoidsInputSchema = z.object({
  agentId: z.string().uuid().optional(),
  source: z.enum(['operator', 'outage_sweep']).optional(),
  since: z.string().datetime({ offset: true }).optional(),
  limit: z.number().int().min(1).max(200).default(50),
  offset: z.number().int().nonnegative().default(0),
})

export const infraVoidListSchema = z.object({
  count: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
  items: z.array(z.object({
    void_id: z.string().uuid(),
    agent_id: z.string().uuid(),
    bench_version: z.number().int().positive(),
    validator_hotkey: z.string(),
    seed: seedSchema,
    void_generation: z.number().int().nonnegative(),
    sample_kind: z.string(),
    run_id: z.string(),
    composite: z.number().min(0).max(1),
    reason: z.string(),
    evidence: z.record(z.string(), z.unknown()),
    evidence_digest: z.string(),
    actor: z.string(),
    source: z.enum(['operator', 'outage_sweep']),
    outage_started_at: z.string().nullable().default(null),
    outage_ended_at: z.string().nullable().default(null),
    created_at: z.string(),
    rerun_composite: z.number().min(0).max(1).nullable().default(null),
    rerun_run_id: z.string().nullable().default(null),
  })),
})

export const getProviderOutagesInputSchema = z.object({
  withFindings: z.boolean().default(false),
  limit: z.number().int().min(1).max(100).default(20),
  offset: z.number().int().nonnegative().default(0),
})

const outageWindowFields = {
  outage_started_at: z.string().nullable().default(null),
  outage_ended_at: z.string().nullable().default(null),
}

export const providerOutagesSchema = z.object({
  circuits: z.array(z.object({
    provider: z.string(),
    state: z.enum(['open', 'closed']),
    epoch: z.string().uuid(),
    opened_at: z.string(),
    retry_at: z.string(),
    last_failure_at: z.string(),
    closed_at: z.string().nullable().default(null),
    failure_count: z.number().int().positive(),
    last_status: z.number().int().nullable().default(null),
    last_error_code: z.string(),
    probe_kind: z.string().nullable().default(null),
    probe_expires_at: z.string().nullable().default(null),
    updated_at: z.string(),
    outage_active: z.boolean(),
  })),
  sweep_policy: z.object({
    interval_seconds: z.number().positive(),
    batch_limit: z.number().int().positive(),
    first_lookback_seconds: z.number().positive(),
    settle_lag_seconds: z.number().nonnegative(),
    outage_failure_codes: z.array(z.string()),
    min_outage_failed_calls: z.number().int().nonnegative(),
    outage_failed_calls: z.number().int().nonnegative(),
    outage_failed_share: z.number().min(0).max(1),
  }),
  runs_total: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
  runs: z.array(z.object({
    sweep_id: z.string().uuid(),
    bench_version: z.number().int().positive(),
    started_at: z.string(),
    finished_at: z.string(),
    watermark_from: z.string(),
    watermark_to: z.string(),
    examined_count: z.number().int().nonnegative(),
    voided_count: z.number().int().nonnegative(),
    error_count: z.number().int().nonnegative(),
    voids: z.array(z.object({
      void_id: z.string().uuid(),
      agent_id: z.string().uuid(),
      validator_hotkey: z.string(),
      seed: seedSchema,
      void_generation: z.number().int().nonnegative(),
      run_id: z.string(),
      composite: z.number().min(0).max(1),
      accepted_at: z.string(),
      grant_id: z.string().uuid(),
      chat_requests: z.number().int().nonnegative(),
      outage_failed_requests: z.number().int().nonnegative(),
      ...outageWindowFields,
    })).default([]),
    outage_windows: z.array(z.object({
      grant_id: z.string().uuid(),
      agent_id: z.string().uuid(),
      validator_hotkey: z.string(),
      sample: z.enum(['confirmation', 'canonical']),
      started_at: z.string(),
      ended_at: z.string(),
      outage_failed_requests: z.number().int().nonnegative(),
    })).default([]),
    canonical_flagged: z.array(z.object({
      agent_id: z.string().uuid(),
      validator_hotkey: z.string(),
      run_id: z.string(),
      composite: z.number().min(0).max(1),
      accepted_at: z.string(),
      grant_id: z.string().uuid(),
      chat_requests: z.number().int().nonnegative(),
      failed_requests: z.number().int().nonnegative(),
      outage_failed_requests: z.number().int().nonnegative(),
      failure_codes: z.record(z.string(), z.number().int().nonnegative()).default({}),
      ...outageWindowFields,
    })).default([]),
    errors: z.array(z.object({
      agent_id: z.string().uuid().nullable().default(null),
      validator_hotkey: z.string().nullable().default(null),
      seed: seedSchema.nullable().default(null),
      void_generation: z.number().int().nonnegative().nullable().default(null),
      canonical_scores: z.boolean().default(false),
      error: z.string(),
    })).default([]),
  })),
})
