import { z } from 'zod'
import type { components } from '../generated/platform-api'

export const attemptLookupInputSchema = z.object({
  agent_id: z.string().uuid(),
  reference_agent_id: z.string().uuid().optional(),
})

export const attemptPolicySchema = z.object({
  report_only: z.literal(true),
  admission_effect: z.literal('none'),
  source_clearance: z.literal(false),
  integrity_clearance: z.literal(false),
  classifier_version: z.number().int(),
  source_build: z.string(),
  settings_digest: z.string(),
  reference_corpus: z.record(z.string(), z.string()),
  max_archive_bytes: z.number().int(),
  max_unpacked_bytes: z.number().int(),
  max_members: z.number().int(),
  max_owner_links: z.number().int(),
  small_delta_jaccard: z.number(),
}) satisfies z.ZodType<components['schemas']['AttemptObservationPolicy']>

export const attemptRecordSchema = z.object({
  policy: attemptPolicySchema,
  agent_id: z.string().uuid(),
  reference_agent_id: z.string().uuid().nullish(),
  as_of: z.string(),
  classification: z.enum([
    'first_submission', 'infrastructure_retry', 'packaging_only_repair',
    'small_source_delta', 'material_new_work', 'inconclusive',
  ]),
  reason: z.string(),
  sha256: z.string(),
  reference_sha256: z.string().nullish(),
  feedback_status: z.enum(['infrastructure', 'repairable', 'completed', 'pending']),
  feedback_reason: z.string().nullish(),
  feedback_at: z.string().nullish(),
}) satisfies z.ZodType<components['schemas']['AttemptComparison']>
