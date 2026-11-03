import { z } from 'zod'

const sha = z.string().regex(/^[0-9a-f]{64}$/)
const imageId = z.string().regex(/^sha256:[0-9a-f]{64}$/)

export const officialControlsInputSchema = z.object({
  limit: z.number().int().min(1).max(100).default(20),
  offset: z.number().int().min(0).default(0),
})
export const officialControlLookupSchema = z.object({ controlId: z.string().uuid() })
export const officialControlImageSchema = z.object({
  source_kind: z.literal('canonical_starter_fixture'),
  control_id: z.string().uuid(), request_id: z.string().uuid(), fixture_id: z.string().uuid(),
  artifact_sha256: sha, image_sha256: sha, image_id: imageId,
  size_bytes: z.number().int().positive().max(8 * 1024 ** 3),
  producer: z.string(), created_at: z.string(), verified_at: z.string().nullable(),
  image_identity_execution_verified: z.literal(false),
  policy_verification_complete: z.literal(false),
})
export const officialControlArtifactSchema = z.object({
  control: officialControlImageSchema, download_url: z.string().url(), expires_at: z.string(),
})
export const officialControlApprovalViewSchema = z.object({
  source_kind: z.literal('canonical_starter_fixture'),
  approval_id: z.string().uuid(), control_id: z.string().uuid(), fixture_id: z.string().uuid(),
  artifact_sha256: sha, image_sha256: sha, image_id: imageId,
  profile_sha256: sha, review_evidence_sha256: sha, approval_receipt_sha256: sha,
  actor: z.string(), reason: z.string(), approved_at: z.string(),
  status: z.literal('recorded_unverified'), policy_verification_complete: z.literal(false),
})
