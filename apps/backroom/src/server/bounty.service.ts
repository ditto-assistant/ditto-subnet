import type { z } from 'zod'
import {
  bountyApprovalInputSchema,
  bountyAppealDecisionInputSchema,
  bountyAwardInputSchema,
  bountyBoardInputSchema,
  bountyBoardSchema,
  bountyClaimInputSchema,
  bountyClaimSchema,
  bountyDispatchInputSchema,
  bountyFactInputSchema,
  bountyIdInputSchema,
  bountyObligationSchema,
  bountyOperatorSchema,
  bountyPauseInputSchema,
  bountyPayoutPolicyInputSchema,
  bountyPayoutPolicySchema,
  bountyPreviewInputSchema,
  bountyPreviewSchema,
  bountyPublishInputSchema,
  bountyQueueInputSchema,
  bountyQueueSchema,
  bountyReassignInputSchema,
  bountyReconcileInputSchema,
  bountyReviewInputSchema,
  bountyRevisionSchema,
  bountySlotInputSchema,
  bountySummarySchema,
} from '../lib/bounty.schemas'
import { PlatformAdminError, platformAdminRequest } from './ditto.server'
import { platformAdminWrite, type ReadBack, type WriteOutcome } from './platform-write'

// Build-bounty operator tools (#2942). Reads are plain admin GETs. Every write
// goes through platformAdminWrite with a read-back of the exact record it
// would create, so an uncertain outcome is settled honestly: committed,
// nothing applied, or unknown with "retry with the same input".

type Claim = z.infer<typeof bountyClaimSchema>
type Operator = z.infer<typeof bountyOperatorSchema>
type Obligation = z.infer<typeof bountyObligationSchema>

const ADMIN = '/api/v1/admin/bounties'

function settled<T>(outcome: WriteOutcome<T>, what: string): T {
  if (outcome.state === 'committed') return outcome.value
  throw new Error(`${what} may have committed (${outcome.reason}). Retry with the same input: an applied write is returned, not repeated.`)
}

function parsed<T>(schema: z.ZodType<T>) {
  return (payload: unknown) => schema.safeParse(payload).data ?? null
}

export async function fetchBountyBoard(rawInput: unknown = {}) {
  const input = bountyBoardInputSchema.parse(rawInput)
  const query = new URLSearchParams({ limit: String(input.limit), offset: String(input.offset) })
  if (input.repo) query.set('repo', input.repo)
  if (input.issue) query.set('issue', String(input.issue))
  return bountyBoardSchema.parse(await platformAdminRequest(`/api/v1/public/bounties?${query}`))
}

export async function fetchBounty(rawInput: unknown) {
  const input = bountyIdInputSchema.parse(rawInput)
  return bountyOperatorSchema.parse(await platformAdminRequest(`${ADMIN}/${input.bountyId}`))
}

export async function fetchBountyClaim(rawInput: unknown) {
  const input = bountyClaimInputSchema.parse(rawInput)
  return bountyClaimSchema.parse(await platformAdminRequest(`/api/v1/bounties/claims/${input.claimId}`))
}

export async function fetchBountyQueue(rawInput: unknown) {
  const input = bountyQueueInputSchema.parse(rawInput)
  const query = new URLSearchParams()
  if (input.queue === 'reviews' && input.overdueOnly) query.set('overdue_only', 'true')
  if (input.queue === 'settlement' && input.state) query.set('state', input.state)
  const path = `${ADMIN}/queue/${input.queue === 'reviews' ? 'reviews' : 'settlement'}`
  return bountyQueueSchema.parse(await platformAdminRequest(query.size ? `${path}?${query}` : path))
}

export async function fetchBountyPayoutPolicy() {
  try {
    return bountyPayoutPolicySchema.parse(await platformAdminRequest(`${ADMIN}/payout-policy`))
  } catch (error) {
    // No revision yet is a state, not a failure.
    if (error instanceof PlatformAdminError && error.status === 404) return null
    throw error
  }
}

function previewBody(input: z.infer<typeof bountyPreviewInputSchema>) {
  return { repo: input.repo, issue: input.issue, expected_revision: input.expectedRevision, terms: input.terms }
}

/** The exact digest, phrase and refusals for a revision. Writes nothing. */
export async function previewBountyRevision(actor: string, rawInput: unknown) {
  const input = bountyPreviewInputSchema.parse(rawInput)
  return bountyPreviewSchema.parse(
    await platformAdminRequest(`${ADMIN}/preview`, { method: 'POST', actor, body: previewBody(input) }),
  )
}

async function bountyIdFor(repo: string, issue: number) {
  const board = await fetchBountyBoard({ repo, issue, limit: 1 })
  return board.items[0]?.bounty_id
}

export async function publishBountyRevision(actor: string, rawInput: unknown) {
  const input = bountyPublishInputSchema.parse(rawInput)
  const target = input.expectedRevision + 1
  // The confirmation names the digest it commits to: its last word.
  const digest = input.confirmation.trim().split(/\s+/).at(-1) ?? ''
  const outcome = await platformAdminWrite(`${ADMIN}/revisions`, {
    actor,
    body: { ...previewBody(input), reviewer_actor: input.reviewerActor, reason: input.reason, confirmation: input.confirmation },
  }, {
    parse: parsed(bountyRevisionSchema),
    readBack: async (): Promise<ReadBack<z.infer<typeof bountyRevisionSchema>>> => {
      const bountyId = await bountyIdFor(input.repo, input.issue)
      if (!bountyId) return { state: 'absent' }
      const row = (await fetchBounty({ bountyId })).revisions.find((item) => item.revision === target)
      if (!row) return { state: 'absent' }
      if (row.spec_digest === digest && row.actor === actor.trim() && row.reason === input.reason) {
        return { state: 'applied', value: row }
      }
      return { state: 'superseded', detail: `Revision ${target} was already published by ${row.actor}` }
    },
    retryHint: 'preview again and publish with its exact phrase.',
  })
  return settled(outcome, `Bounty revision ${target}`)
}

/** The latest audit entry of the given kinds, newest last in the audit. */
function latest(view: Operator, kinds: string[], matches: (entry: Operator['audit'][number]) => boolean = () => true) {
  return [...view.audit].reverse().find((entry) => kinds.includes(entry.kind) && matches(entry))
}

export async function setBountyClaimsPaused(actor: string, rawInput: unknown) {
  const input = bountyPauseInputSchema.parse(rawInput)
  const kind = input.paused ? 'claims_paused' : 'claims_resumed'
  const outcome = await platformAdminWrite(`${ADMIN}/${input.bountyId}/claims-paused`, {
    actor,
    body: { paused: input.paused, reason: input.reason, confirmation: input.confirmation },
  }, {
    parse: parsed(bountySummarySchema),
    readBack: async () => {
      const view = await fetchBounty({ bountyId: input.bountyId })
      const control = latest(view, ['claims_paused', 'claims_resumed'])
      if (!control || control.kind !== kind) return { state: 'absent' }
      if (control.actor === actor.trim() && control.reason === input.reason) {
        return { state: 'applied', value: bountySummarySchema.parse(view.detail.summary) }
      }
      // The state is already what this write asked for, set by someone else.
      return { state: 'superseded', detail: `New claims were already ${input.paused ? 'paused' : 'resumed'} by ${control.actor}` }
    },
    retryHint: 'read the bounty and retry with the same input.',
  })
  return settled(outcome, 'The claims pause')
}

function submissionOf(claim: Claim, submissionId: string) {
  return claim.submissions.find((item) => item.submission_id === submissionId)
}

export async function reviewBountySubmission(actor: string, rawInput: unknown) {
  const input = bountyReviewInputSchema.parse(rawInput)
  const outcome = await platformAdminWrite(`${ADMIN}/claims/${input.claimId}/review`, {
    actor,
    body: {
      claim_id: input.claimId,
      submission_id: input.submissionId,
      decision: input.decision,
      reason: input.reason,
      rework_days: input.reworkDays ?? null,
      attribution: input.attribution ?? null,
      confirmation: input.confirmation,
    },
  }, {
    parse: parsed(bountyClaimSchema),
    readBack: async () => {
      const claim = await fetchBountyClaim({ claimId: input.claimId })
      const submission = submissionOf(claim, input.submissionId)
      if (!submission?.decision) return { state: 'absent' }
      if (submission.decision === input.decision && submission.decision_reason === input.reason) {
        return { state: 'applied', value: claim }
      }
      return { state: 'superseded', detail: `Submission ${input.submissionId} was already decided: ${submission.decision}` }
    },
    retryHint: 'read the claim and retry with the same input.',
  })
  return settled(outcome, 'The review decision')
}

export async function reassignBountyReviewer(actor: string, rawInput: unknown) {
  const input = bountyReassignInputSchema.parse(rawInput)
  const outcome = await platformAdminWrite(`${ADMIN}/${input.bountyId}/reviewer`, {
    actor,
    body: { reviewer_actor: input.reviewerActor, reason: input.reason, confirmation: input.confirmation },
  }, {
    parse: parsed(bountySummarySchema),
    readBack: async () => {
      const view = await fetchBounty({ bountyId: input.bountyId })
      const mine = latest(view, ['reviewer_assigned'], (entry) =>
        entry.actor === actor.trim() && entry.reason === input.reason && entry.detail === `reviewer ${input.reviewerActor.trim()}`)
      if (mine) return { state: 'applied', value: bountySummarySchema.parse(view.detail.summary) }
      return { state: 'absent' }
    },
    retryHint: 'read the bounty and retry with the same input.',
  })
  return settled(outcome, 'The reviewer reassignment')
}

export async function controlBountySlot(actor: string, rawInput: unknown) {
  const input = bountySlotInputSchema.parse(rawInput)
  if ((input.action === 'cancel') !== (input.kind !== undefined)) {
    throw new Error('kind is required to cancel a slot, and only then')
  }
  const kind = input.action === 'cancel' ? 'slot_cancelled' : 'slot_reopened'
  const outcome = await platformAdminWrite(`${ADMIN}/slots/${input.slotId}/${input.action}`, {
    actor,
    body: input.action === 'cancel'
      ? { kind: input.kind, reason: input.reason, confirmation: input.confirmation }
      : { reason: input.reason, confirmation: input.confirmation },
  }, {
    parse: parsed(bountySummarySchema),
    readBack: async () => {
      const view = await fetchBounty({ bountyId: input.bountyId })
      const mine = latest(view, [kind], (entry) =>
        entry.slot_id === input.slotId && entry.actor === actor.trim() && entry.reason === input.reason)
      if (mine) return { state: 'applied', value: bountySummarySchema.parse(view.detail.summary) }
      return { state: 'absent' }
    },
    retryHint: 'read the bounty and retry with the same input.',
  })
  return settled(outcome, `The slot ${input.action}`)
}

export async function decideBountyAppeal(actor: string, rawInput: unknown) {
  const input = bountyAppealDecisionInputSchema.parse(rawInput)
  const outcome = await platformAdminWrite(`${ADMIN}/appeals/${input.appealId}/decision`, {
    actor,
    body: { upheld: input.upheld, reason: input.reason, restore_days: input.restoreDays ?? null, confirmation: input.confirmation },
  }, {
    parse: parsed(bountyClaimSchema),
    readBack: async () => {
      const view = await fetchBounty({ bountyId: input.bountyId })
      const appeal = view.appeals.find((item) => item.appeal_id === input.appealId)
      if (!appeal || appeal.upheld === null) return { state: 'absent' }
      if (appeal.upheld === input.upheld && appeal.decided_by === actor.trim() && appeal.decision_reason === input.reason) {
        const claim = view.claims.find((item) => item.claim_id === appeal.claim_id)
        if (claim) return { state: 'applied', value: claim }
      }
      return { state: 'superseded', detail: `Appeal ${input.appealId} was already decided by ${appeal.decided_by}` }
    },
    retryHint: 'read the bounty and retry with the same input.',
  })
  return settled(outcome, 'The appeal decision')
}

function awardOf(claim: Claim) {
  return claim.obligations.find((item) => item.kind === 'award')
}

export async function awardBountyClaim(actor: string, rawInput: unknown) {
  const input = bountyAwardInputSchema.parse(rawInput)
  const outcome = await platformAdminWrite(`${ADMIN}/claims/${input.claimId}/award`, {
    actor,
    body: { amount_alpha_rao: input.amountAlphaRao, attribution: input.attribution, reason: input.reason, confirmation: input.confirmation },
  }, {
    parse: parsed(bountyClaimSchema),
    readBack: async () => {
      const claim = await fetchBountyClaim({ claimId: input.claimId })
      const award = awardOf(claim)
      if (!award) return { state: 'absent' }
      if (award.amount_alpha_rao === input.amountAlphaRao && award.attribution === input.attribution) {
        return { state: 'applied', value: claim }
      }
      return { state: 'superseded', detail: `Claim ${input.claimId} already has an award` }
    },
    retryHint: 'read the claim and retry with the same input.',
  })
  return settled(outcome, 'The award')
}

export async function setBountyPayoutPolicy(actor: string, rawInput: unknown) {
  const input = bountyPayoutPolicyInputSchema.parse(rawInput)
  const target = input.expectedRevision + 1
  const outcome = await platformAdminWrite(`${ADMIN}/payout-policy`, {
    actor,
    body: {
      expected_revision: input.expectedRevision,
      single_approver_max_alpha_rao: input.singleApproverMaxAlphaRao,
      dual_approver_max_alpha_rao: input.dualApproverMaxAlphaRao,
      reason: input.reason,
      confirmation: input.confirmation,
    },
  }, {
    parse: parsed(bountyPayoutPolicySchema),
    readBack: async () => {
      const policy = await fetchBountyPayoutPolicy()
      if (!policy || policy.revision < target) return { state: 'absent' }
      if (
        policy.revision === target &&
        policy.single_approver_max_alpha_rao === input.singleApproverMaxAlphaRao &&
        policy.dual_approver_max_alpha_rao === input.dualApproverMaxAlphaRao &&
        policy.actor === actor.trim() &&
        policy.reason === input.reason
      ) {
        return { state: 'applied', value: policy }
      }
      return { state: 'superseded', detail: `Payout policy revision ${target} was already written` }
    },
    retryHint: 'read the payout policy and retry with its revision.',
  })
  return settled(outcome, `Payout policy revision ${target}`)
}

function obligationOf(claim: Claim, obligationId: string): Obligation | undefined {
  return claim.obligations.find((item) => item.obligation_id === obligationId)
}

export async function approveBountyObligation(actor: string, rawInput: unknown) {
  const input = bountyApprovalInputSchema.parse(rawInput)
  const outcome = await platformAdminWrite(`${ADMIN}/obligations/${input.obligationId}/approve`, {
    actor,
    body: {
      round: input.round,
      payee_coldkey: input.payeeColdkey,
      amount_alpha_rao: input.amountAlphaRao,
      policy_revision: input.policyRevision,
      confirmation: input.confirmation,
    },
  }, {
    parse: parsed(bountyObligationSchema),
    readBack: async () => {
      const view = await fetchBounty({ bountyId: input.bountyId })
      const detail = `round ${input.round} ${input.amountAlphaRao} alpha rao to ${input.payeeColdkey} under policy ${input.policyRevision}`
      const mine = latest(view, ['obligation_approved'], (entry) =>
        entry.obligation_id === input.obligationId && entry.actor === actor.trim() && entry.detail === detail)
      if (!mine) return { state: 'absent' }
      for (const claim of view.claims) {
        const owed = obligationOf(claim, input.obligationId)
        if (owed) return { state: 'applied', value: owed }
      }
      return { state: 'absent' }
    },
    retryHint: 'read the settlement queue and retry with the same input.',
  })
  return settled(outcome, 'The approval')
}

export async function dispatchBountyObligation(actor: string, rawInput: unknown) {
  const input = bountyDispatchInputSchema.parse(rawInput)
  const outcome = await platformAdminWrite(`${ADMIN}/obligations/${input.obligationId}/dispatch`, {
    actor,
    body: { round: input.round, rehearsal_outcome: input.rehearsalOutcome, confirmation: input.confirmation },
  }, {
    parse: parsed(bountyObligationSchema),
    readBack: async () => {
      const owed = obligationOf(await fetchBountyClaim({ claimId: input.claimId }), input.obligationId)
      // A transfer for this round is this dispatch: one per round, ever.
      if (owed?.transfers.some((transfer) => transfer.round === input.round)) return { state: 'applied', value: owed }
      return { state: 'absent' }
    },
    retryHint: 'read the claim first: a recorded transfer must be reconciled, never sent again.',
  })
  return settled(outcome, 'The dispatch')
}

export async function reconcileBountyTransfer(actor: string, rawInput: unknown) {
  const input = bountyReconcileInputSchema.parse(rawInput)
  const outcome = await platformAdminWrite(`${ADMIN}/transfers/${input.transferId}/reconcile`, {
    actor,
    body: { rehearsal_outcome: input.rehearsalOutcome, confirmation: input.confirmation },
  }, {
    parse: parsed(bountyObligationSchema),
    readBack: async () => {
      const claim = await fetchBountyClaim({ claimId: input.claimId })
      for (const owed of claim.obligations) {
        const transfer = owed.transfers.find((item) => item.transfer_id === input.transferId)
        if (transfer && !['dispatched', 'uncertain'].includes(transfer.state)) return { state: 'applied', value: owed }
      }
      return { state: 'absent' }
    },
    retryHint: 'retry with the same input.',
  })
  return settled(outcome, 'The reconciliation')
}

export async function recordBountyFact(actor: string, rawInput: unknown) {
  const input = bountyFactInputSchema.parse(rawInput)
  const outcome = await platformAdminWrite(`${ADMIN}/claims/${input.claimId}/facts`, {
    actor,
    body: { kind: input.kind, reference: input.reference, confirmation: input.confirmation },
  }, {
    parse: parsed(bountyClaimSchema),
    readBack: async () => {
      const claim = await fetchBountyClaim({ claimId: input.claimId })
      const fact = claim.facts.find((item) => item.kind === input.kind)
      if (!fact) return { state: 'absent' }
      if (fact.reference === input.reference) return { state: 'applied', value: claim }
      return { state: 'superseded', detail: `Claim ${input.claimId} already records a ${input.kind} fact` }
    },
    retryHint: 'retry with the same input.',
  })
  return settled(outcome, 'The delivery fact')
}

