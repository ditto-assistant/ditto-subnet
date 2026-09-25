import { z } from 'zod'

export const conversationAssessmentInputSchema = z.object({
  assessment_id: z.string().uuid().optional(),
  limit: z.number().int().min(1).max(100).default(50),
})

export const conversationObservationsSchema = z.object({
  settings_revision: z.number().int().nonnegative().default(0),
  settings_actor: z.string().nullable().optional(),
  settings_reason: z.string().nullable().optional(),
  settings_updated_at: z.string().nullable().optional(),
  mode: z.enum(['off', 'shadow']),
  instrument: z.string(),
  judge_model: z.string(),
  daily_budget_microusd: z.number().int().nonnegative(),
  next_budget_slot_at: z.string().nullable().optional(),
  reserved_last_day_microusd: z.number().int().nonnegative(),
  proposed_submission_fee_rao: z.literal(200000000),
  current_submission_fee_rao: z.number().int(),
  fee_change_request: z.object({
    expected_revision: z.number().int(),
    cooldown_seconds: z.number().int(),
    fee_amount_rao: z.literal(200000000),
    reason: z.string(),
    actor: z.string(),
    confirmation: z.string(),
  }),
  items: z.array(z.object({
    assessment_id: z.string().uuid(),
    agent_id: z.string().uuid(),
    artifact_sha256: z.string(),
    bench_version: z.number().int(),
    status: z.enum(['leased', 'completed', 'incomplete', 'expired']),
    created_at: z.string(),
    expires_at: z.string(),
    base_quality_micros: z.number().int(),
    conversation_micros: z.number().int().nullable(),
    proposed_quality_micros: z.number().int().nullable(),
    reserved_microusd: z.number().int(),
    spent_microusd: z.number().int().nullable(),
    error_code: z.string().nullable(),
    report_sha256: z.string().nullable().optional(),
    retry_of: z.string().uuid().nullable().optional(),
    retry_assessment_id: z.string().uuid().nullable().optional(),
    retry_authorization: z.object({
      actor: z.string(), reason: z.string(), report_sha256: z.string(),
      authorized_at: z.string(), expires_at: z.string(),
    }).nullable().optional(),
  })).max(100),
})

export const conversationReportSchema = z.object({
  assessment_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  instrument: z.literal('conversational-continuity-v1'),
  status: z.enum(['completed', 'incomplete']),
  model: z.literal('gpt-6-astra'),
  error_code: z.string().nullable(),
}).passthrough().nullable()

export const conversationSettingsInputSchema = z.object({
  expected_revision: z.number().int().nonnegative(),
  mode: z.enum(['off', 'shadow']),
  reason: z.string().min(10),
  confirmation: z.literal('APPLY CONVERSATION SHADOW SETTINGS'),
})

export const conversationRetryInputSchema = z.object({
  expected_revision: z.number().int().nonnegative(),
  assessment_id: z.string().uuid(),
  expected_artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  expected_report_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  reason: z.string().min(10),
  confirmation: z.literal('AUTHORIZE ONE CONVERSATION RETRY'),
})
