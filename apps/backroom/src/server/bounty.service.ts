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
  bountyPayoutPolicyReadInputSchema,
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

export async function fetchBountyPayoutPolicy(rawInput: unknown = {}) {
  const input = bountyPayoutPolicyReadInputSchema.parse(rawInput)
  const query = input.revision === undefined ? '' : `?revision=${input.revision}`
  try {
    return bountyPayoutPolicySchema.parse(await platformAdminRequest(`${ADMIN}/payout-policy${query}`))
  } catch (error) {
    // No revision yet, or not that one yet, is a state, not a failure.
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

/** The latest audit entry of the given kinds. Ordered here by recorded_at, so
 * a read-back never depends on the order Platform lists them in. */
function latest(view: Operator, kinds: string[], matches: (entry: Operator['audit'][number]) => boolean = () => true) {
  return [...view.audit]
    .sort((a, b) => Date.parse(b.recorded_at) - Date.parse(a.recorded_at))
    .find((entry) => kinds.includes(entry.kind) && matches(entry))
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
      // Controls carry no id, so an entry with this operator's reason may be
      // this write or an earlier identical one.
      const own = latest(view, [kind], (entry) => entry.actor === actor.trim() && entry.reason === input.reason)
      if (control && control === own) {
        return { state: 'applied', value: bountySummarySchema.parse(view.detail.summary) }
      }
      // Later controls hide whether this write was one of them.
      if (own || !control || control.kind !== kind) return { state: 'absent' }
      // No record of this write, and the state is already what it asked for,
      // set by someone else, so Platform refuses it if it is still in flight.
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
      // The decision must be this one, by this operator: the public claim
      // names no reviewer, so the private audit confirms who decided.
      const decided = latest(await fetchBounty({ bountyId: claim.bounty_id }), ['review_decided'], (entry) =>
        entry.claim_id === input.claimId && entry.detail === `${input.decision} submission ${input.submissionId}`)
      const reworkMs = typeof submission.decided_at === 'string' && typeof submission.rework_deadline_at === 'string'
        ? Date.parse(submission.rework_deadline_at) - Date.parse(submission.decided_at)
        : null
      if (
        decided?.actor === actor.trim() && decided.reason === input.reason &&
        submission.decision === input.decision && submission.decision_reason === input.reason &&
        (submission.attribution ?? null) === (input.attribution ?? null) &&
        reworkMs === (input.reworkDays === undefined ? null : input.reworkDays * 86_400_000)
      ) {
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
        // The award's reason and operator are private: confirm them in the audit.
        const recorded = latest(await fetchBounty({ bountyId: claim.bounty_id }), ['award_recorded'], (entry) =>
          entry.obligation_id === award.obligation_id)
        if (recorded?.actor === actor.trim() && recorded.reason === input.reason) return { state: 'applied', value: claim }
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
      // Read this write's own revision, never the latest: a later revision
      // says nothing about whether this one committed.
      const policy = await fetchBountyPayoutPolicy({ revision: target })
      if (!policy) return { state: 'absent' }
      if (
        policy.single_approver_max_alpha_rao === input.singleApproverMaxAlphaRao &&
        policy.dual_approver_max_alpha_rao === input.dualApproverMaxAlphaRao &&
        policy.actor === actor.trim() &&
        policy.reason === input.reason
      ) {
        return { state: 'applied', value: policy }
      }
      return { state: 'superseded', detail: `Payout policy revision ${target} was already written by ${policy.actor}` }
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
      const owed = claim.obligations.find((item) => item.transfers.some((t) => t.transfer_id === input.transferId))
      const transfer = owed?.transfers.find((item) => item.transfer_id === input.transferId)
      if (!owed || !transfer) return { state: 'absent' }
      // Every reconciliation is recorded with its operator, the outcome asked
      // for and the state it left. Only an entry that left the transfer in
      // its current state can be this write: an earlier inconclusive one that
      // someone else settled since is not.
      const mine = latest(await fetchBounty({ bountyId: claim.bounty_id }), ['transfer_reconciled'], (entry) =>
        entry.actor === actor.trim() &&
        entry.detail.startsWith(`transfer ${input.transferId} `) &&
        entry.detail.endsWith(`: ${transfer.state} (asked ${input.rehearsalOutcome})`))
      if (mine) return { state: 'applied', value: owed }
      if (!['dispatched', 'uncertain'].includes(transfer.state)) {
        return { state: 'superseded', detail: `Transfer ${input.transferId} was already reconciled (${transfer.state})` }
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
      if (claim.facts.some((item) => item.kind === input.kind && item.reference === input.reference)) {
        return { state: 'applied', value: claim }
      }
      if (!claim.facts.some((item) => item.kind === input.kind)) return { state: 'absent' }
      return { state: 'superseded', detail: `Claim ${input.claimId} already records a ${input.kind} fact` }
    },
    retryHint: 'retry with the same input.',
  })
  return settled(outcome, 'The delivery fact')
}

