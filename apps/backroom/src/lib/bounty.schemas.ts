import { z } from 'zod'

// Build bounties (#2942). Responses validate the fields Backroom's logic reads
// and pass every other Platform field through unchanged.
// Platform writes ids in lowercase; canonical input lets every request and
// read-back compare ids exactly, whatever case an operator typed.
const uuid = z.string().uuid().transform((value) => value.toLowerCase())
const digest = z.string().regex(/^[0-9a-f]{64}$/)
const address = z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{47,48}$/)
const repo = z.string().regex(/^[a-z0-9][a-z0-9-]{0,38}\/[a-z0-9._-]{1,100}$/)
const alphaRao = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER)
const reason = z.string().min(8)
const confirmation = z.string().min(1).max(400)

export const bountyTermsSchema = z.object({
  title: z.string().min(1).max(200),
  scope: z.string().min(1).max(20000),
  public_interfaces: z.string().min(1).max(20000),
  acceptance_evidence: z.string().min(1).max(20000),
  dependencies: z.array(z.string()).max(20).default([]),
  reviewer_login: z.string().min(1).max(39),
  entry_fee_alpha_rao: alphaRao,
  reward_pool_alpha_rao: alphaRao,
  attempts_per_round: z.number().int().min(1).max(10).default(3),
  delivery_days: z.number().int().min(1).max(60).default(5),
  review_days: z.number().int().min(1).max(60).default(7),
  hold_minutes: z.number().int().min(1).max(1440).default(30),
  funding_mode: z.enum(['rehearsal', 'live']),
  fee_escrow_coldkey: address,
  fee_escrow_hotkey: address,
})

// Inputs (camelCase, as every Backroom tool).
export const bountyBoardInputSchema = z.object({
  repo: repo.optional(),
  issue: z.number().int().min(1).optional(),
  limit: z.number().int().min(1).max(100).default(50),
  offset: z.number().int().min(0).default(0),
})
export const bountyIdInputSchema = z.object({ bountyId: uuid })
export const bountyClaimInputSchema = z.object({ claimId: uuid })
export const bountyQueueInputSchema = z.object({
  queue: z.enum(['reviews', 'settlement']),
  overdueOnly: z.boolean().default(false),
  state: z.enum(['awaiting_approval', 'over_policy_limit', 'approved', 'dispatched', 'uncertain', 'confirmed', 'failed']).optional(),
})
export const bountyPreviewInputSchema = z.object({
  repo,
  issue: z.number().int().min(1),
  expectedRevision: z.number().int().min(0),
  terms: bountyTermsSchema,
})
export const bountyPublishInputSchema = bountyPreviewInputSchema.extend({
  reviewerActor: z.string().min(1).max(120),
  reason,
  confirmation,
})
export const bountyPauseInputSchema = z.object({ bountyId: uuid, paused: z.boolean(), reason, confirmation })
export const bountyReviewInputSchema = z.object({
  claimId: uuid,
  submissionId: uuid,
  decision: z.enum(['accept', 'changes_requested', 'reject']),
  reason,
  reworkDays: z.number().int().min(1).max(60).optional(),
  attribution: z.string().min(1).max(2000).optional(),
  confirmation,
})
export const bountyReassignInputSchema = z.object({
  bountyId: uuid,
  reviewerActor: z.string().min(1).max(120),
  reason,
  confirmation,
})
export const bountySlotInputSchema = z.object({
  bountyId: uuid,
  slotId: uuid,
  action: z.enum(['cancel', 'reopen']),
  kind: z.enum(['team', 'system_fault']).optional(),
  reason,
  confirmation,
})
export const bountyAppealDecisionInputSchema = z.object({
  bountyId: uuid,
  appealId: uuid,
  upheld: z.boolean(),
  reason,
  restoreDays: z.number().int().min(1).max(60).optional(),
  confirmation,
})
export const bountyAwardInputSchema = z.object({
  claimId: uuid,
  amountAlphaRao: alphaRao.min(1),
  attribution: z.string().min(1).max(2000),
  reason,
  confirmation,
})
export const bountyPayoutPolicyReadInputSchema = z.object({
  revision: z.number().int().min(1).max(2_147_483_647).optional(),
})
export const bountyPayoutPolicyInputSchema = z.object({
  expectedRevision: z.number().int().min(0),
  singleApproverMaxAlphaRao: alphaRao,
  dualApproverMaxAlphaRao: alphaRao,
  reason,
  confirmation,
})
export const bountyApprovalInputSchema = z.object({
  bountyId: uuid,
  obligationId: uuid,
  round: z.number().int().min(1).max(1000),
  payeeColdkey: address,
  amountAlphaRao: alphaRao.min(1),
  policyRevision: z.number().int().min(1),
  confirmation,
})
export const bountyDispatchInputSchema = z.object({
  claimId: uuid,
  obligationId: uuid,
  round: z.number().int().min(1).max(1000),
  // Required: an omitted outcome must never become a confirmed transfer.
  rehearsalOutcome: z.enum(['confirmed', 'failed', 'unknown']),
  confirmation,
})
export const bountyReconcileInputSchema = z.object({
  claimId: uuid,
  transferId: uuid,
  rehearsalOutcome: z.enum(['confirmed', 'failed', 'expired', 'unknown']),
  confirmation,
})
export const bountyFactInputSchema = z.object({
  claimId: uuid,
  kind: z.enum(['merged', 'deployed', 'adopted']),
  reference: z.string().min(1).max(500),
  confirmation,
})

// Responses.
const transferSchema = z.object({ transfer_id: uuid, round: z.number().int(), state: z.string() }).passthrough()
export const bountyObligationSchema = z.object({
  obligation_id: uuid,
  kind: z.enum(['fee_refund', 'fee_retained', 'award']),
  payee_coldkey: z.string(),
  amount_alpha_rao: alphaRao,
  state: z.string(),
  round: z.number().int(),
  attribution: z.string().nullable(),
  transfers: z.array(transferSchema),
}).passthrough()
export const bountyClaimSchema = z.object({
  claim_id: uuid,
  bounty_id: uuid,
  slot_id: uuid,
  slot_status: z.string(),
  submissions: z.array(z.object({
    submission_id: uuid,
    decision: z.string().nullable(),
    decision_reason: z.string().nullable(),
  }).passthrough()),
  obligations: z.array(bountyObligationSchema),
  facts: z.array(z.object({ kind: z.string(), reference: z.string() }).passthrough()),
}).passthrough()
export const bountyDetailSchema = z.object({
  summary: z.object({ bounty_id: uuid, repo: z.string(), issue: z.number().int(), current_revision: z.number().int() }).passthrough(),
}).passthrough()
export const bountyBoardSchema = z.object({
  items: z.array(z.object({ bounty_id: uuid }).passthrough()),
  total: z.number().int(),
}).passthrough()
export const bountySummarySchema = z.object({ bounty_id: uuid, claims_paused: z.boolean() }).passthrough()
const auditEntrySchema = z.object({
  kind: z.string(),
  actor: z.string(),
  reason: z.string().nullable(),
  detail: z.string(),
  recorded_at: z.string(),
  slot_id: uuid.nullable(),
  obligation_id: uuid.nullable(),
}).passthrough()
export const bountyOperatorSchema = z.object({
  detail: bountyDetailSchema,
  reviewer_actor: z.string(),
  revisions: z.array(z.object({
    revision: z.number().int(),
    spec_digest: digest,
    reviewer_actor: z.string(),
    actor: z.string(),
    reason: z.string(),
  }).passthrough()),
  claims: z.array(bountyClaimSchema),
  appeals: z.array(z.object({
    appeal_id: uuid,
    claim_id: uuid,
    upheld: z.boolean().nullable(),
    decided_by: z.string().nullable(),
    decision_reason: z.string().nullable(),
    decided_at: z.string().nullable(),
    restored_deadline_at: z.string().nullable(),
  }).passthrough()),
  audit: z.array(auditEntrySchema),
}).passthrough()
export const bountyQueueSchema = z.object({ items: z.array(z.object({}).passthrough()), total: z.number().int() }).passthrough()
export const bountyPreviewSchema = z.object({
  revision: z.number().int(),
  spec_digest: digest,
  confirmation: z.string(),
  refusals: z.array(z.string()),
})
export const bountyRevisionSchema = z.object({ revision: z.number().int(), spec_digest: digest }).passthrough()
export const bountyPayoutPolicySchema = z.object({
  revision: z.number().int(),
  single_approver_max_alpha_rao: alphaRao,
  dual_approver_max_alpha_rao: alphaRao,
  actor: z.string(),
  reason: z.string(),
}).passthrough()

// Catalog forms: the MCP tool catalog is loaded into every session, so it
// publishes compact shapes (ids as format uuid, terms as an object) and each
// service function still parses its input with the strict schema above.
const catalogId = z.string().meta({ format: 'uuid' })
const catalogTerms = z.record(z.string(), z.unknown()).meta({ description: 'BountyTerms; see help' })
export const bountyCatalogInputs = {
  bounty: z.object({ bountyId: catalogId }),
  claim: z.object({ claimId: catalogId }),
  preview: z.object({ repo: z.string(), issue: z.number().int(), expectedRevision: z.number().int(), terms: catalogTerms }),
  publish: z.object({
    repo: z.string(), issue: z.number().int(), expectedRevision: z.number().int(), terms: catalogTerms,
    reviewerActor: z.string(), reason: z.string(), confirmation: z.string(),
  }),
  pause: z.object({ bountyId: catalogId, paused: z.boolean(), reason: z.string(), confirmation: z.string() }),
  review: z.object({
    claimId: catalogId, submissionId: catalogId, decision: z.enum(['accept', 'changes_requested', 'reject']),
    reason: z.string(), reworkDays: z.number().int().optional(), attribution: z.string().optional(), confirmation: z.string(),
  }),
  reassign: z.object({ bountyId: catalogId, reviewerActor: z.string(), reason: z.string(), confirmation: z.string() }),
  slot: z.object({
    bountyId: catalogId, slotId: catalogId, action: z.enum(['cancel', 'reopen']),
    kind: z.enum(['team', 'system_fault']).optional(), reason: z.string(), confirmation: z.string(),
  }),
  appeal: z.object({
    bountyId: catalogId, appealId: catalogId, upheld: z.boolean(), reason: z.string(),
    restoreDays: z.number().int().optional(), confirmation: z.string(),
  }),
  award: z.object({ claimId: catalogId, amountAlphaRao: z.number().int(), attribution: z.string(), reason: z.string(), confirmation: z.string() }),
  policyRead: z.object({ revision: z.number().int().optional() }),
  policy: z.object({
    expectedRevision: z.number().int(), singleApproverMaxAlphaRao: z.number().int(), dualApproverMaxAlphaRao: z.number().int(),
    reason: z.string(), confirmation: z.string(),
  }),
  approve: z.object({
    bountyId: catalogId, obligationId: catalogId, round: z.number().int(), payeeColdkey: z.string(),
    amountAlphaRao: z.number().int(), policyRevision: z.number().int(), confirmation: z.string(),
  }),
  dispatch: z.object({
    claimId: catalogId, obligationId: catalogId, round: z.number().int(),
    rehearsalOutcome: z.enum(['confirmed', 'failed', 'unknown']), confirmation: z.string(),
  }),
  reconcile: z.object({
    claimId: catalogId, transferId: catalogId,
    rehearsalOutcome: z.enum(['confirmed', 'failed', 'expired', 'unknown']), confirmation: z.string(),
  }),
  fact: z.object({ claimId: catalogId, kind: z.enum(['merged', 'deployed', 'adopted']), reference: z.string(), confirmation: z.string() }),
}
