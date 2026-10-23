import { z } from 'zod'

export const sourceReleaseEvidenceInputSchema = z.object({
  agentId: z.string().uuid(),
  candidateAgentId: z.string().uuid().optional(),
})

export const sourceReleaseEvidenceSchema = z.object({
  agent_id: z.string().uuid(), artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  candidate_agent_id: z.string().uuid().nullable(), reference_time: z.string(),
  policy_revision: z.number().int().nonnegative(),
  disclosure: z.enum(['public', 'never']), embargo_hours: z.number().int().min(6).max(8760),
  emission_confirmed_at: z.string().nullable(),
  derived_eligibility_at: z.string().nullable(),
  publication_evidence_at: z.string().nullable(),
  publication_proven_by_reference_time: z.boolean(),
  exact_sha_public_handoff_count: z.number().int().nonnegative(),
  earliest_exact_sha_public_handoff_at: z.string().nullable(),
  absence_proves_nonpublication: z.literal(false), limitations: z.string(),
})
