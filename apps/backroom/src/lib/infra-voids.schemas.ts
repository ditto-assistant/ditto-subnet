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
