import { z } from 'zod'

const kind = z.enum(['first_submission', 'infrastructure_retry', 'packaging_only_repair',
  'small_source_delta', 'material_new_work', 'inconclusive'])
const reason = z.string().trim().min(8)
const id = z.string().uuid()
const settings = z.object({
  mode: z.enum(['off', 'shadow', 'enforce']),
  small_delta_jaccard: z.number().min(0.90).max(1),
  lineage_jaccard: z.number().min(0.70).max(1),
  low_information_limit: z.number().int().min(1).max(100),
  fast_repair_limit: z.number().int().min(1).max(10),
  window_seconds: z.number().int().min(3600).max(604800),
  cooldown_seconds: z.number().int().min(60).max(21600),
})

export const attemptPolicyInputSchema = z.object({
  expected_revision: z.number().int().nonnegative(), settings,
  calibration_id: id.nullable().optional(), reason, confirmation: z.string(),
})
export const attemptReplayInputSchema = z.object({
  settings, reason,
  cases: z.array(z.object({ agent_id: id, expected_classification: kind,
    expected_throttled: z.boolean() })).min(1).max(500),
})
export const attemptAppealInputSchema = z.object({
  agent_id: id, expected_policy_revision: z.number().int().nonnegative(),
  reason, confirmation: z.string(),
})
export const attemptLookupInputSchema = z.object({ agent_id: id })
export const attemptCalibrationLookupInputSchema = z.object({ calibration_id: id })
const guidance = z.object({
  policy_revision: z.number().int(), settings_digest: z.string().default(''),
  evaluated_at: z.string().nullable().default(null),
  mode: z.enum(['off', 'shadow', 'enforce']), classification: kind,
  reference_agent_id: id.nullable().default(null), lineage_agent_id: id.nullable().default(null),
  completed_low_information_attempts: z.number().int(),
  reserved_low_information_attempts: z.number().int(), fast_repairs_remaining: z.number().int(),
  fast_repair: z.boolean(), appeal_id: id.nullable().default(null),
  retry_at: z.string().nullable().default(null), reason: z.string(),
})
const revision = z.object({
  revision: z.number().int(), parent_revision: z.number().int(), settings,
  calibration_id: id.nullable().default(null), actor: z.string(), reason: z.string(),
  created_at: z.string().nullable().default(null),
})
export const attemptPolicySchema = z.object({
  current: revision, effective_settings: settings,
  enforcement_blocked_reason: z.string().nullable(), history: z.array(revision),
})
export const attemptRevisionSchema = revision
export const attemptAppealSchema = z.object({
  appeal_id: id, policy_revision: z.number().int(), actor: z.string(),
  reason: z.string(), created_at: z.string(),
})
export const attemptRecordSchema = z.object({ guidance, appeals: z.array(attemptAppealSchema) })
export const attemptReplaySchema = z.object({
  calibration_id: id, settings_digest: z.string(), case_count: z.number().int(),
  false_throttles: z.number().int(), false_allows: z.number().int(),
  classification_mismatches: z.number().int(), inconclusive_count: z.number().int(),
  proposed_delays: z.number().int(), immediate_admissions_deferred: z.number().int(),
  immediate_admission_deferral_ratio: z.number(), eligible_for_enforcement: z.boolean(),
  coverage: z.record(z.string(), z.number().int()),
  rows: z.array(z.object({ agent_id: id, guidance, expected_classification: kind,
    expected_throttled: z.boolean(), would_throttle: z.boolean() })),
})
export const attemptCalibrationSchema = z.object({
  report: attemptReplaySchema, actor: z.string(), reason: z.string(), created_at: z.string(),
})
