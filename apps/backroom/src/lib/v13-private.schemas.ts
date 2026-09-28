import { z } from 'zod'

const id = z.string().uuid()
const sha = z.string().regex(/^[0-9a-f]{64}$/)
const role = z.enum(['target', 'known_benign'])

export const listV13BenignApprovalsInputSchema = z.object({
  limit: z.number().int().min(1).max(100).default(20),
  offset: z.number().int().min(0).default(0),
})

export const v13BenignApprovalLookupInputSchema = z.object({ approvalId: id })

export const v13BenignApprovalWriteInputSchema = z.object({
  agentId: id,
  attemptId: id,
  artifactSha256: sha,
  imageSha256: sha,
  profileSha256: sha,
  reviewEvidenceSha256: sha,
  reason: z.string().min(8),
  confirmation: z.literal('RECORD V13 BENIGN CONTROL'),
})

export const v13BenignApprovalSchema = z.object({
  approval_id: id,
  agent_id: id,
  attempt_id: id,
  artifact_sha256: sha,
  image_sha256: sha,
  profile_sha256: sha,
  review_evidence_sha256: sha,
  reason: z.string(),
  approval_receipt_sha256: sha,
  actor: z.string(),
  approved_at: z.string(),
  status: z.literal('recorded_unverified'),
})

export const v13ReplayPrivateLookupInputSchema = z.object({
  replayId: id,
  role: role.optional(),
})

export const v13ReplayGroupWriteInputSchema = z.object({
  replayId: id,
  targetAgentId: id,
  targetAttemptId: id,
  targetArtifactSha256: sha,
  targetImageSha256: sha,
  approvalId: id,
  profileSha256: sha,
  confirmation: z.literal('RECORD V13 REPLAY GENERATION'),
})

export const v13ReplayPackageWriteInputSchema = z.object({
  replayId: id,
  role,
  generationReceiptSha256: sha,
  manifestSha256: sha,
  pairInventorySha256: sha,
  confirmation: z.literal('REGISTER V13 REPLAY PACKAGE'),
})

export const v13ReplayGroupSchema = z.object({
  group_id: id,
  replay_id: id,
  target_agent_id: id,
  target_attempt_id: id,
  target_artifact_sha256: sha,
  target_image_sha256: sha,
  control_agent_id: id,
  control_attempt_id: id,
  control_artifact_sha256: sha,
  control_image_sha256: sha,
  approval_id: id,
  approval_receipt_sha256: sha,
  profile_sha256: sha,
  target_receipt_sha256: sha,
  control_receipt_sha256: sha,
  actor: z.string(),
  started_at: z.string(),
  status: z.literal('recorded_unverified'),
})

export const v13ReplayPackageSchema = z.object({
  replay_id: id,
  group_id: id,
  role,
  agent_id: id,
  attempt_id: id,
  artifact_sha256: sha,
  image_sha256: sha,
  profile_sha256: sha,
  generation_receipt_sha256: sha,
  manifest_sha256: sha,
  pair_inventory_sha256: sha,
  registrar_actor: z.string(),
  registered_at: z.string(),
  status: z.literal('recorded_unverified'),
})

export const v13ReplayPrivateReceiptSchema = z.object({
  replay_id: id,
  group_id: id,
  receipt_sha256: sha,
  runner_hotkey: z.string(),
  observed_at: z.string(),
  created_at: z.string(),
  status: z.literal('recorded_unverified'),
  policy_verification_complete: z.literal(false),
})

export const v13PrivateStatisticsSchema = z.object({
  replay_id: id,
  receipt_sha256: sha,
  source_binding_current: z.boolean(),
  report: z.object({
    revision: z.literal('v13-private-paired-hoeffding-holm-report-v1'),
    status: z.enum(['signal', 'inconclusive']),
    classes: z.array(z.object({
      transformation_class: z.string(),
      pairs: z.number().int().min(20),
      target_degradation_bps: z.number(),
      clean_degradation_bps: z.number(),
      lower_confidence_bound_bps: z.number(),
      holm_alpha: z.number(),
      hoeffding_p_upper_bound: z.number(),
      replicated_direction: z.boolean(),
      clean_seed_criterion_met: z.boolean(),
      criterion_met: z.boolean(),
    })),
    policy_verification_complete: z.literal(false),
    terminal_eligible: z.literal(false),
  }),
  policy_verification_complete: z.literal(false),
  terminal_eligible: z.literal(false),
})
