import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  approveBountyObligation,
  awardBountyClaim,
  controlBountySlot,
  decideBountyAppeal,
  dispatchBountyObligation,
  fetchBountyPayoutPolicy,
  fetchBountyQueue,
  publishBountyRevision,
  reassignBountyReviewer,
  reconcileBountyTransfer,
  recordBountyFact,
  reviewBountySubmission,
  setBountyClaimsPaused,
  setBountyPayoutPolicy,
} from './bounty.service'
import { NothingAppliedError } from './platform-write'

const ACTOR = 'operator@example.com'
const BOUNTY = '00000000-0000-4000-8000-0000000000b1'
const CLAIM = '00000000-0000-4000-8000-0000000000c1'
const SLOT = '00000000-0000-4000-8000-0000000000d1'
const SUBMISSION = '00000000-0000-4000-8000-0000000000e1'
const OBLIGATION = '00000000-0000-4000-8000-0000000000f1'
const TRANSFER = '00000000-0000-4000-8000-0000000000a1'
const APPEAL = '00000000-0000-4000-8000-0000000000a2'
const PAYEE = '5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty'
const DIGEST = 'ab'.repeat(32)
const REPO = 'ditto-assistant/ditto-subnet'
const TERMS = {
  title: 'TEST FIXTURE bounty', scope: 'Fixture scope.', public_interfaces: 'Fixture interface.',
  acceptance_evidence: 'Fixture tests pass.', reviewer_login: 'reviewer-fixture',
  entry_fee_alpha_rao: 2_000_000_000, reward_pool_alpha_rao: 100_000_000_000, funding_mode: 'rehearsal',
  fee_escrow_coldkey: PAYEE, fee_escrow_hotkey: '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
}

const transfer = (state: string, round = 1) => ({ transfer_id: TRANSFER, round, state })
const obligation = (over: Record<string, unknown> = {}) => ({
  obligation_id: OBLIGATION, kind: 'award', payee_coldkey: PAYEE, amount_alpha_rao: 40, state: 'approved',
  round: 1, attribution: 'Fixture credit', transfers: [], ...over,
})
const claim = (over: Record<string, unknown> = {}) => ({
  claim_id: CLAIM, bounty_id: BOUNTY, slot_id: SLOT, slot_status: 'accepted',
  submissions: [{ submission_id: SUBMISSION, decision: null, decision_reason: null }],
  obligations: [], facts: [], ...over,
})
const audit = (kind: string, over: Record<string, unknown> = {}) => ({
  kind, actor: ACTOR, reason: 'fixture operator reason', detail: '', recorded_at: '2026-10-16T00:00:00Z',
  slot_id: null, obligation_id: null, ...over,
})
const operator = (over: Record<string, unknown> = {}) => ({
  detail: { summary: { bounty_id: BOUNTY, repo: REPO, issue: 2942, current_revision: 1, claims_paused: false } },
  reviewer_actor: 'reviewer@example.com',
  revisions: [], claims: [claim()], appeals: [], audit: [], ...over,
})
const envelope = (message: string, status: number) => Response.json({ error_code: 3000, message, request_id: null }, { status })
const outage = () => envelope('upstream exploded', 502)

beforeEach(() => {
  process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
})
afterEach(() => {
  vi.unstubAllGlobals()
})

function sent(fetchMock: ReturnType<typeof vi.fn>, call = 0) {
  const [url, init] = fetchMock.mock.calls[call] as [string, RequestInit]
  return { url, body: init.body ? JSON.parse(String(init.body)) : undefined }
}

type Case = {
  name: string
  run: () => Promise<unknown>
  path: string
  body: Record<string, unknown>
  committed: unknown
  // Read-back responses after an outage: applied, absent, superseded.
  applied: Response[]
  absent: Response[]
  superseded?: Response[]
}

const reason = 'fixture operator reason'
const cases: Case[] = [
  {
    name: 'publish_bounty_revision',
    run: () => publishBountyRevision(ACTOR, {
      repo: REPO, issue: 2942, expectedRevision: 1, terms: TERMS, reviewerActor: 'reviewer@example.com', reason,
      confirmation: `PUBLISH BOUNTY ${REPO}#2942 REVISION 2 ${DIGEST}`,
    }),
    path: '/api/v1/admin/bounties/revisions',
    body: { repo: REPO, issue: 2942, expected_revision: 1, reviewer_actor: 'reviewer@example.com', reason },
    committed: { revision: 2, spec_digest: DIGEST },
    applied: [
      Response.json({ items: [{ bounty_id: BOUNTY }], total: 1 }),
      Response.json(operator({ revisions: [{ revision: 2, spec_digest: DIGEST, reviewer_actor: 'reviewer@example.com', actor: ACTOR, reason }] })),
    ],
    absent: [Response.json({ items: [], total: 0 })],
    superseded: [
      Response.json({ items: [{ bounty_id: BOUNTY }], total: 1 }),
      Response.json(operator({ revisions: [{ revision: 2, spec_digest: 'cd'.repeat(32), reviewer_actor: 'r', actor: 'other@example.com', reason: 'other publication' }] })),
    ],
  },
  {
    name: 'set_bounty_claims_paused',
    run: () => setBountyClaimsPaused(ACTOR, { bountyId: BOUNTY, paused: true, reason, confirmation: `PAUSE BOUNTY CLAIMS ${REPO}#2942` }),
    path: `/api/v1/admin/bounties/${BOUNTY}/claims-paused`,
    body: { paused: true, reason },
    committed: { bounty_id: BOUNTY, claims_paused: true },
    applied: [Response.json(operator({ audit: [audit('claims_paused')] }))],
    absent: [Response.json(operator({ audit: [audit('claims_resumed')] }))],
    superseded: [Response.json(operator({ audit: [audit('claims_paused', { actor: 'other@example.com' })] }))],
  },
  {
    name: 'review_bounty_submission',
    run: () => reviewBountySubmission(ACTOR, {
      claimId: CLAIM, submissionId: SUBMISSION, decision: 'reject', reason, confirmation: `REJECT BOUNTY CLAIM ${REPO}#2942 ${CLAIM}`,
    }),
    path: `/api/v1/admin/bounties/claims/${CLAIM}/review`,
    body: { claim_id: CLAIM, submission_id: SUBMISSION, decision: 'reject', reason, rework_days: null, attribution: null },
    committed: claim(),
    applied: [Response.json(claim({ submissions: [{ submission_id: SUBMISSION, decision: 'reject', decision_reason: reason }] }))],
    absent: [Response.json(claim())],
    superseded: [Response.json(claim({ submissions: [{ submission_id: SUBMISSION, decision: 'accept', decision_reason: 'other' }] }))],
  },
  {
    name: 'reassign_bounty_reviewer',
    run: () => reassignBountyReviewer(ACTOR, { bountyId: BOUNTY, reviewerActor: 'next@example.com', reason, confirmation: `REASSIGN BOUNTY REVIEWER ${REPO}#2942` }),
    path: `/api/v1/admin/bounties/${BOUNTY}/reviewer`,
    body: { reviewer_actor: 'next@example.com', reason },
    committed: { bounty_id: BOUNTY, claims_paused: false },
    applied: [Response.json(operator({ audit: [audit('reviewer_assigned', { detail: 'reviewer next@example.com' })] }))],
    absent: [Response.json(operator())],
  },
  {
    name: 'control_bounty_slot',
    run: () => controlBountySlot(ACTOR, {
      bountyId: BOUNTY, slotId: SLOT, action: 'cancel', kind: 'team', reason, confirmation: `CANCEL BOUNTY SLOT ${REPO}#2942 ROUND 1 SLOT 1`,
    }),
    path: `/api/v1/admin/bounties/slots/${SLOT}/cancel`,
    body: { kind: 'team', reason },
    committed: { bounty_id: BOUNTY, claims_paused: false },
    applied: [Response.json(operator({ audit: [audit('slot_cancelled', { slot_id: SLOT })] }))],
    absent: [Response.json(operator({ audit: [audit('slot_reopened', { slot_id: SLOT })] }))],
  },
  {
    name: 'decide_bounty_appeal',
    run: () => decideBountyAppeal(ACTOR, { bountyId: BOUNTY, appealId: APPEAL, upheld: false, reason, confirmation: `DENY BOUNTY APPEAL ${APPEAL}` }),
    path: `/api/v1/admin/bounties/appeals/${APPEAL}/decision`,
    body: { upheld: false, reason, restore_days: null },
    committed: claim(),
    applied: [Response.json(operator({ appeals: [{ appeal_id: APPEAL, claim_id: CLAIM, upheld: false, decided_by: ACTOR, decision_reason: reason }] }))],
    absent: [Response.json(operator({ appeals: [{ appeal_id: APPEAL, claim_id: CLAIM, upheld: null, decided_by: null, decision_reason: null }] }))],
    superseded: [Response.json(operator({ appeals: [{ appeal_id: APPEAL, claim_id: CLAIM, upheld: true, decided_by: 'other@example.com', decision_reason: 'other' }] }))],
  },
  {
    name: 'award_bounty_claim',
    run: () => awardBountyClaim(ACTOR, { claimId: CLAIM, amountAlphaRao: 40, attribution: 'Fixture credit', reason, confirmation: `AWARD BOUNTY CLAIM ${CLAIM} 40 ALPHA RAO` }),
    path: `/api/v1/admin/bounties/claims/${CLAIM}/award`,
    body: { amount_alpha_rao: 40, attribution: 'Fixture credit', reason },
    committed: claim(),
    applied: [Response.json(claim({ obligations: [obligation()] }))],
    absent: [Response.json(claim())],
    superseded: [Response.json(claim({ obligations: [obligation({ amount_alpha_rao: 41 })] }))],
  },
  {
    name: 'set_bounty_payout_policy',
    run: () => setBountyPayoutPolicy(ACTOR, {
      expectedRevision: 0, singleApproverMaxAlphaRao: 5, dualApproverMaxAlphaRao: 50, reason, confirmation: 'SET BOUNTY PAYOUT POLICY 1',
    }),
    path: '/api/v1/admin/bounties/payout-policy',
    body: { expected_revision: 0, single_approver_max_alpha_rao: 5, dual_approver_max_alpha_rao: 50, reason },
    committed: { revision: 1, single_approver_max_alpha_rao: 5, dual_approver_max_alpha_rao: 50, actor: ACTOR, reason },
    applied: [Response.json({ revision: 1, single_approver_max_alpha_rao: 5, dual_approver_max_alpha_rao: 50, actor: ACTOR, reason })],
    absent: [envelope('no bounty payout policy is configured', 404)],
    superseded: [Response.json({ revision: 1, single_approver_max_alpha_rao: 9, dual_approver_max_alpha_rao: 90, actor: 'other@example.com', reason: 'other' })],
  },
  {
    name: 'approve_bounty_obligation',
    run: () => approveBountyObligation(ACTOR, {
      bountyId: BOUNTY, obligationId: OBLIGATION, round: 1, payeeColdkey: PAYEE, amountAlphaRao: 40, policyRevision: 1,
      confirmation: `APPROVE BOUNTY OBLIGATION ${OBLIGATION} ROUND 1 40 ALPHA RAO TO ${PAYEE}`,
    }),
    path: `/api/v1/admin/bounties/obligations/${OBLIGATION}/approve`,
    body: { round: 1, payee_coldkey: PAYEE, amount_alpha_rao: 40, policy_revision: 1 },
    committed: obligation(),
    applied: [Response.json(operator({
      claims: [claim({ obligations: [obligation()] })],
      audit: [audit('obligation_approved', { obligation_id: OBLIGATION, reason: null, detail: `round 1 40 alpha rao to ${PAYEE} under policy 1` })],
    }))],
    absent: [Response.json(operator({ claims: [claim({ obligations: [obligation()] })] }))],
  },
  {
    name: 'dispatch_bounty_obligation',
    run: () => dispatchBountyObligation(ACTOR, { claimId: CLAIM, obligationId: OBLIGATION, round: 1, confirmation: `DISPATCH BOUNTY OBLIGATION ${OBLIGATION} ROUND 1` }),
    path: `/api/v1/admin/bounties/obligations/${OBLIGATION}/dispatch`,
    body: { round: 1, rehearsal_outcome: 'confirmed' },
    committed: obligation({ transfers: [transfer('confirmed')] }),
    applied: [Response.json(claim({ obligations: [obligation({ transfers: [transfer('uncertain')] })] }))],
    absent: [Response.json(claim({ obligations: [obligation()] }))],
  },
  {
    name: 'reconcile_bounty_transfer',
    run: () => reconcileBountyTransfer(ACTOR, { claimId: CLAIM, transferId: TRANSFER, confirmation: `RECONCILE BOUNTY TRANSFER ${TRANSFER}` }),
    path: `/api/v1/admin/bounties/transfers/${TRANSFER}/reconcile`,
    body: { rehearsal_outcome: 'confirmed' },
    committed: obligation({ transfers: [transfer('confirmed')] }),
    applied: [Response.json(claim({ obligations: [obligation({ transfers: [transfer('confirmed')] })] }))],
    absent: [Response.json(claim({ obligations: [obligation({ transfers: [transfer('uncertain')] })] }))],
  },
  {
    name: 'record_bounty_fact',
    run: () => recordBountyFact(ACTOR, { claimId: CLAIM, kind: 'merged', reference: 'a'.repeat(40), confirmation: `RECORD BOUNTY MERGED ${CLAIM}` }),
    path: `/api/v1/admin/bounties/claims/${CLAIM}/facts`,
    body: { kind: 'merged', reference: 'a'.repeat(40) },
    committed: claim(),
    applied: [Response.json(claim({ facts: [{ kind: 'merged', reference: 'a'.repeat(40) }] }))],
    absent: [Response.json(claim())],
    superseded: [Response.json(claim({ facts: [{ kind: 'merged', reference: 'b'.repeat(40) }] }))],
  },
]

describe('bounty writes decide every outcome', () => {
  it.each(cases)('$name sends the exact body and returns a readable commit', async (c) => {
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(c.committed))
    vi.stubGlobal('fetch', fetchMock)
    await expect(c.run()).resolves.toMatchObject(c.committed as object)
    const request = sent(fetchMock)
    expect(request.url).toContain(c.path)
    expect(request.body).toMatchObject(c.body)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each(cases)('$name settles an outage whose read-back shows this write', async (c) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage()).mockImplementation(
      (() => { const queue = [...c.applied]; return async () => queue.shift() ?? Response.json({}) })(),
    ))
    await expect(c.run()).resolves.toBeTruthy()
  })

  it.each(cases)('$name reports an outage without its write as unknown, never failed', async (c) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage()).mockImplementation(
      (() => { const queue = [...c.absent]; return async () => queue.shift() ?? Response.json({}) })(),
    ))
    await expect(c.run()).rejects.toThrow('may have committed')
  })

  it.each(cases.filter((c) => c.superseded))('$name reports nothing applied when another write holds the target', async (c) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage()).mockImplementation(
      (() => { const queue = [...(c.superseded ?? [])]; return async () => queue.shift() ?? Response.json({}) })(),
    ))
    await expect(c.run()).rejects.toBeInstanceOf(NothingAppliedError)
  })
})

describe('bounty reads', () => {
  it('reads an unset payout policy as null, not a failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(envelope('no bounty payout policy is configured', 404)))
    await expect(fetchBountyPayoutPolicy()).resolves.toBeNull()
  })

  it.each([
    [{ queue: 'reviews', overdueOnly: true }, '/api/v1/admin/bounties/queue/reviews?overdue_only=true'],
    [{ queue: 'settlement', state: 'uncertain' }, '/api/v1/admin/bounties/queue/settlement?state=uncertain'],
    [{ queue: 'settlement' }, '/api/v1/admin/bounties/queue/settlement'],
  ])('routes queue %o', async (input, path) => {
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json({ items: [], total: 0 }))
    vi.stubGlobal('fetch', fetchMock)
    await fetchBountyQueue(input)
    expect(sent(fetchMock).url.endsWith(path)).toBe(true)
  })

  it('refuses a cancel without a kind and a reopen with one before any request', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    await expect(controlBountySlot(ACTOR, { bountyId: BOUNTY, slotId: SLOT, action: 'cancel', reason, confirmation: 'x' }))
      .rejects.toThrow('kind is required')
    await expect(controlBountySlot(ACTOR, { bountyId: BOUNTY, slotId: SLOT, action: 'reopen', kind: 'team', reason, confirmation: 'x' }))
      .rejects.toThrow('kind is required')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
