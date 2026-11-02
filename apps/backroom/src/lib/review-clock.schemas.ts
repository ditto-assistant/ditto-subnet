import { z } from 'zod'

const sha256 = z.string().regex(/^[0-9a-f]{64}$/)

export const scheduleV13ReviewClockInputSchema = z.object({
  expectedRevision: z.number().int().nonnegative(),
  policyDocumentDigest: sha256,
  policyManifestDigest: sha256,
  activateAt: z.string().datetime({ offset: true }),
  windowSeconds: z.number().int().min(3600).max(604800),
  reason: z.string().trim().min(8),
  confirmation: z.literal('SCHEDULE V13 REVIEW CLOCK'),
})

const reviewClockRevisionSchema = z.object({
  revision: z.number().int().positive(),
  policy_version: z.literal(13),
  policy_document_digest: sha256.nullable(),
  policy_manifest_digest: sha256,
  activate_at: z.string().datetime({ offset: true }),
  window_seconds: z.number().int().min(3600).max(604800),
  start_event: z.literal('first-v13-screening-claim'),
  reason: z.string(),
  actor: z.string(),
  created_at: z.string().datetime({ offset: true }),
  state: z.enum(['pending', 'due']),
})

export const v13ReviewClockScheduleSchema = z.object({
  current_policy_document_digest: sha256,
  latest: reviewClockRevisionSchema.nullable(),
  revisions: z.array(reviewClockRevisionSchema),
  finalizer_state: z.literal('not_configured'),
})
