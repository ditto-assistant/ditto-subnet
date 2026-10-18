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
import { PlatformAdminError } from './ditto.server'
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
const decided = (over: Record<string, unknown>) => ({
  appeal_id: APPEAL, claim_id: CLAIM, decided_at: '2026-10-16T00:00:00Z', restored_deadline_at: null, ...over,
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
      Response.json(operator({ revisions: [{ revision: 2, spec_digest: DIGEST, reviewer_actor: 'reviewer@example.com', actor: ACTOR, reason }] })),
    ],
    absent: [envelope('bounty not found', 404)],
    // The same terms, operator and reason, binding another reviewer.
    superseded: [
      Response.json(operator({ revisions: [{ revision: 2, spec_digest: DIGEST, reviewer_actor: 'other-reviewer@example.com', actor: ACTOR, reason }] })),
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
    applied: [
      Response.json(claim({ submissions: [{ submission_id: SUBMISSION, decision: 'reject', decision_reason: reason }] })),
      Response.json(operator({ audit: [audit('review_decided', { claim_id: CLAIM, detail: `reject submission ${SUBMISSION}` })] })),
    ],
    absent: [Response.json(claim())],
    // The same decision and reason, recorded by another operator.
    superseded: [
      Response.json(claim({ submissions: [{ submission_id: SUBMISSION, decision: 'reject', decision_reason: reason }] })),
      Response.json(operator({ audit: [audit('review_decided', { claim_id: CLAIM, actor: 'other@example.com', detail: `reject submission ${SUBMISSION}` })] })),
    ],
  },
  {
    name: 'reassign_bounty_reviewer',
    run: () => reassignBountyReviewer(ACTOR, { bountyId: BOUNTY, reviewerActor: 'next@example.com', reason, confirmation: `REASSIGN BOUNTY REVIEWER ${REPO}#2942` }),
    path: `/api/v1/admin/bounties/${BOUNTY}/reviewer`,
    body: { reviewer_actor: 'next@example.com', reason },
    committed: { bounty_id: BOUNTY, claims_paused: false },
    applied: [Response.json(operator({
      reviewer_actor: 'next@example.com', audit: [audit('reviewer_assigned', { detail: 'reviewer next@example.com' })],
    }))],
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
    applied: [Response.json(operator({ appeals: [decided({ upheld: false, decided_by: ACTOR, decision_reason: reason })] }))],
    absent: [Response.json(operator({ appeals: [decided({ upheld: null, decided_by: null, decision_reason: null, decided_at: null })] }))],
    superseded: [Response.json(operator({ appeals: [decided({ upheld: true, decided_by: 'other@example.com', decision_reason: 'other', restored_deadline_at: '2026-10-18T00:00:00Z' })] }))],
  },
  {
    name: 'award_bounty_claim',
    run: () => awardBountyClaim(ACTOR, { claimId: CLAIM, amountAlphaRao: 40, attribution: 'Fixture credit', reason, confirmation: `AWARD BOUNTY CLAIM ${CLAIM} 40 ALPHA RAO` }),
    path: `/api/v1/admin/bounties/claims/${CLAIM}/award`,
    body: { amount_alpha_rao: 40, attribution: 'Fixture credit', reason },
    committed: claim(),
    applied: [
      Response.json(claim({ obligations: [obligation()] })),
      Response.json(operator({ audit: [audit('award_recorded', { obligation_id: OBLIGATION })] })),
    ],
    absent: [Response.json(claim())],
    // The same amount and credit, but another reason: another award.
    superseded: [
      Response.json(claim({ obligations: [obligation()] })),
      Response.json(operator({ audit: [audit('award_recorded', { obligation_id: OBLIGATION, reason: 'another award reason' })] })),
    ],
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
    run: () => dispatchBountyObligation(ACTOR, {
      claimId: CLAIM, obligationId: OBLIGATION, round: 1, rehearsalOutcome: 'confirmed', confirmation: `DISPATCH BOUNTY OBLIGATION ${OBLIGATION} ROUND 1`,
    }),
    path: `/api/v1/admin/bounties/obligations/${OBLIGATION}/dispatch`,
    body: { round: 1, rehearsal_outcome: 'confirmed' },
    committed: obligation({ transfers: [transfer('confirmed')] }),
    applied: [
      Response.json(claim({ obligations: [obligation({ transfers: [transfer('uncertain')] })] })),
      Response.json(operator({ audit: [audit('transfer_dispatched', { obligation_id: OBLIGATION, reason: null, detail: 'round 1 via simulated: uncertain' })] })),
    ],
    absent: [Response.json(claim({ obligations: [obligation()] }))],
    // The round's one transfer was sent by another operator.
    superseded: [
      Response.json(claim({ obligations: [obligation({ transfers: [transfer('uncertain')] })] })),
      Response.json(operator({ audit: [audit('transfer_dispatched', { actor: 'other@example.com', obligation_id: OBLIGATION, reason: null, detail: 'round 1 via simulated: uncertain' })] })),
    ],
  },
  {
    name: 'reconcile_bounty_transfer',
    run: () => reconcileBountyTransfer(ACTOR, {
      claimId: CLAIM, transferId: TRANSFER, rehearsalOutcome: 'confirmed', confirmation: `RECONCILE BOUNTY TRANSFER ${TRANSFER}`,
    }),
    path: `/api/v1/admin/bounties/transfers/${TRANSFER}/reconcile`,
    body: { rehearsal_outcome: 'confirmed' },
    committed: obligation({ transfers: [transfer('confirmed')] }),
    applied: [
      Response.json(claim({ obligations: [obligation({ transfers: [transfer('confirmed')] })] })),
      Response.json(operator({ audit: [audit('transfer_reconciled', { reason: null, detail: `transfer ${TRANSFER} round 1: confirmed (asked confirmed)` })] })),
    ],
    absent: [
      Response.json(claim({ obligations: [obligation({ transfers: [transfer('uncertain')] })] })),
      Response.json(operator()),
    ],
    // Already reconciled as failed by someone else: not this write.
    superseded: [
      Response.json(claim({ obligations: [obligation({ transfers: [transfer('failed')] })] })),
      Response.json(operator({ audit: [audit('transfer_reconciled', { actor: 'other@example.com', reason: null, detail: `transfer ${TRANSFER} round 1: failed (asked failed)` })] })),
    ],
  },
  {
    name: 'record_bounty_fact',
    run: () => recordBountyFact(ACTOR, { claimId: CLAIM, kind: 'merged', reference: 'a'.repeat(40), confirmation: `RECORD BOUNTY MERGED ${CLAIM}` }),
    path: `/api/v1/admin/bounties/claims/${CLAIM}/facts`,
    body: { kind: 'merged', reference: 'a'.repeat(40) },
    committed: claim(),
    // The exact fact is found whatever else the claim records.
    applied: [Response.json(claim({ facts: [{ kind: 'deployed', reference: 'v1' }, { kind: 'merged', reference: 'a'.repeat(40) }] }))],
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
    const error = await c.run().catch((caught: unknown) => caught)
    expect((error as Error).message).toContain('may have committed')
    // The Platform failure stays readable as the cause.
    expect(((error as Error).cause as PlatformAdminError).failure).toBe('server')
  })

  it.each(cases.filter((c) => c.superseded))('$name reports nothing applied when another write holds the target', async (c) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage()).mockImplementation(
      (() => { const queue = [...(c.superseded ?? [])]; return async () => queue.shift() ?? Response.json({}) })(),
    ))
    await expect(c.run()).rejects.toBeInstanceOf(NothingAppliedError)
  })
})

describe('bounty write inputs', () => {
  it.each([
    ['dispatch', () => dispatchBountyObligation(ACTOR, { claimId: CLAIM, obligationId: OBLIGATION, round: 1, confirmation: 'x' })],
    ['reconcile', () => reconcileBountyTransfer(ACTOR, { claimId: CLAIM, transferId: TRANSFER, confirmation: 'x' })],
  ])('%s refuses an omitted rehearsal outcome before any request', async (_name, run) => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    await expect(run()).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('reads the audit newest first whatever order Platform lists it in', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(outage())
      .mockResolvedValueOnce(Response.json(operator({
        audit: [
          audit('claims_paused', { recorded_at: '2026-10-16T02:00:00Z' }),
          audit('claims_resumed', { recorded_at: '2026-10-16T01:00:00Z' }),
        ],
      })))
    vi.stubGlobal('fetch', fetchMock)
    const value = await setBountyClaimsPaused(ACTOR, { bountyId: BOUNTY, paused: true, reason, confirmation: `PAUSE BOUNTY CLAIMS ${REPO}#2942` })
    expect(value).toMatchObject({ bounty_id: BOUNTY })
  })
})

describe('bounty read-backs find this write, not the latest state', () => {
  const pause = () => setBountyClaimsPaused(ACTOR, { bountyId: BOUNTY, paused: true, reason, confirmation: `PAUSE BOUNTY CLAIMS ${REPO}#2942` })

  it('reports a pause hidden by later controls as unknown, never as not applied', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage()).mockResolvedValueOnce(Response.json(operator({
      audit: [
        audit('claims_paused', { recorded_at: '2026-10-16T01:00:00Z' }),
        audit('claims_resumed', { actor: 'other@example.com', recorded_at: '2026-10-16T02:00:00Z' }),
        audit('claims_paused', { actor: 'other@example.com', recorded_at: '2026-10-16T03:00:00Z' }),
      ],
    }))))
    await expect(pause()).rejects.toThrow('may have committed')
  })

  const reconcile = (rehearsalOutcome: string) => reconcileBountyTransfer(ACTOR, {
    claimId: CLAIM, transferId: TRANSFER, rehearsalOutcome, confirmation: `RECONCILE BOUNTY TRANSFER ${TRANSFER}`,
  })
  const reconciled = (state: string, audits: ReturnType<typeof audit>[]) => vi.fn().mockResolvedValueOnce(outage())
    .mockResolvedValueOnce(Response.json(claim({ obligations: [obligation({ transfers: [transfer(state)] })] })))
    .mockResolvedValueOnce(Response.json(operator({ audit: audits })))

  it.each([
    [2, 'applied'],
    [3, 'superseded'],
  ])('compares the deadline an upheld appeal restored (restoreDays %i: %s)', async (restoreDays, expected) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage()).mockResolvedValueOnce(Response.json(operator({
      appeals: [decided({ upheld: true, decided_by: ACTOR, decision_reason: reason, restored_deadline_at: '2026-10-18T00:00:00Z' })],
    }))))
    const run = decideBountyAppeal(ACTOR, { bountyId: BOUNTY, appealId: APPEAL, upheld: true, restoreDays, reason, confirmation: `UPHOLD BOUNTY APPEAL ${APPEAL}` })
    if (expected === 'applied') await expect(run).resolves.toMatchObject({ claim_id: CLAIM })
    else await expect(run).rejects.toBeInstanceOf(NothingAppliedError)
  })

  it('reports an inconclusive reconciliation settled since by someone else as unknown, never applied or refused', async () => {
    vi.stubGlobal('fetch', reconciled('confirmed', [
      audit('transfer_reconciled', { reason: null, detail: `transfer ${TRANSFER} round 1: uncertain (asked unknown)`, recorded_at: '2026-10-16T01:00:00Z' }),
      audit('transfer_reconciled', { actor: 'other@example.com', reason: null, detail: `transfer ${TRANSFER} round 1: confirmed (asked confirmed)`, recorded_at: '2026-10-16T02:00:00Z' }),
    ]))
    await expect(reconcile('unknown')).rejects.toThrow('may have committed')
  })

  it('recognises a live reconciliation, which records no requested outcome', async () => {
    vi.stubGlobal('fetch', reconciled('confirmed', [
      audit('transfer_reconciled', { reason: null, detail: `transfer ${TRANSFER} round 1: confirmed` }),
    ]))
    await expect(reconcile('confirmed')).resolves.toMatchObject({ obligation_id: OBLIGATION })
  })

  it('reports a rework decision as unknown when its times do not parse', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage())
      .mockResolvedValueOnce(Response.json(claim({
        submissions: [{
          submission_id: SUBMISSION, decision: 'changes_requested', decision_reason: reason,
          decided_at: 'not-a-time', rework_deadline_at: 'not-a-time',
        }],
      })))
      .mockResolvedValueOnce(Response.json(operator({
        audit: [audit('review_decided', { claim_id: CLAIM, detail: `changes_requested submission ${SUBMISSION}` })],
      }))))
    await expect(reviewBountySubmission(ACTOR, {
      claimId: CLAIM, submissionId: SUBMISSION, decision: 'changes_requested', reworkDays: 3, reason,
      confirmation: `REQUEST CHANGES BOUNTY CLAIM ${REPO}#2942 ${CLAIM}`,
    })).rejects.toThrow('may have committed')
  })

  it('reports a review as unknown when its decision has no audit entry yet', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage())
      .mockResolvedValueOnce(Response.json(claim({ submissions: [{ submission_id: SUBMISSION, decision: 'reject', decision_reason: reason }] })))
      .mockResolvedValueOnce(Response.json(operator())))
    await expect(reviewBountySubmission(ACTOR, {
      claimId: CLAIM, submissionId: SUBMISSION, decision: 'reject', reason, confirmation: `REJECT BOUNTY CLAIM ${REPO}#2942 ${CLAIM}`,
    })).rejects.toThrow('may have committed')
  })

  it('reports a dispatch as unknown when its transfer has no audit entry yet', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage())
      .mockResolvedValueOnce(Response.json(claim({ obligations: [obligation({ transfers: [transfer('uncertain')] })] })))
      .mockResolvedValueOnce(Response.json(operator())))
    await expect(dispatchBountyObligation(ACTOR, {
      claimId: CLAIM, obligationId: OBLIGATION, round: 1, rehearsalOutcome: 'confirmed', confirmation: `DISPATCH BOUNTY OBLIGATION ${OBLIGATION} ROUND 1`,
    })).rejects.toThrow('may have committed')
  })

  it('never takes an audit entry without a readable time as the latest', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage()).mockResolvedValueOnce(Response.json(operator({
      audit: [
        audit('claims_resumed', { actor: 'other@example.com', recorded_at: 'not-a-time' }),
        audit('claims_paused', { recorded_at: '2026-10-16T01:00:00Z' }),
      ],
    }))))
    await expect(pause()).resolves.toMatchObject({ bounty_id: BOUNTY })
  })

  const reassign = () => reassignBountyReviewer(ACTOR, {
    bountyId: BOUNTY, reviewerActor: 'next@example.com', reason, confirmation: `REASSIGN BOUNTY REVIEWER ${REPO}#2942`,
  })
  const assigned = (at: string) => audit('reviewer_assigned', { detail: 'reviewer next@example.com', recorded_at: at })

  it.each([
    ['another reassignment', 'other@example.com', audit('reviewer_assigned', { actor: 'other@example.com', detail: 'reviewer other@example.com', recorded_at: '2026-10-16T02:00:00Z' })],
    ['a publication binding the same reviewer', 'next@example.com', audit('revision_published', { recorded_at: '2026-10-16T02:00:00Z' })],
    ['a control at the same instant', 'next@example.com', audit('reviewer_assigned', { actor: 'other@example.com', detail: 'reviewer next@example.com', recorded_at: '2026-10-16T01:00:00Z' })],
  ])('never reads an earlier identical reassignment followed by %s as a refused write applied', async (_name, inForce, later) => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(envelope('explicit confirmation is required', 409))
      .mockResolvedValueOnce(Response.json(operator({ reviewer_actor: inForce, audit: [assigned('2026-10-16T01:00:00Z'), later] }))))
    await expect(reassign()).rejects.toThrow('explicit confirmation is required')
  })

  const cancel = () => controlBountySlot(ACTOR, {
    bountyId: BOUNTY, slotId: SLOT, action: 'cancel', kind: 'team', reason, confirmation: `CANCEL BOUNTY SLOT ${REPO}#2942 ROUND 1 SLOT 1`,
  })

  it.each([
    ['reopened since', '2026-10-16T02:00:00Z'],
    ['reopened at the same instant', '2026-10-16T01:00:00Z'],
  ])('reports a slot cancel whose earlier identical cancel was %s as unknown, never applied', async (_name, at) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage()).mockResolvedValueOnce(Response.json(operator({
      audit: [
        audit('slot_cancelled', { slot_id: SLOT, recorded_at: '2026-10-16T01:00:00Z' }),
        audit('slot_reopened', { slot_id: SLOT, actor: 'other@example.com', recorded_at: at }),
      ],
    }))))
    await expect(cancel()).rejects.toThrow('may have committed')
  })

  it('reads a slot cancel by its own slot, whatever other slots record', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage()).mockResolvedValueOnce(Response.json(operator({
      audit: [
        audit('slot_cancelled', { slot_id: SLOT, recorded_at: '2026-10-16T01:00:00Z' }),
        audit('slot_reopened', { slot_id: '00000000-0000-4000-8000-0000000000d2', actor: 'other@example.com', recorded_at: '2026-10-16T02:00:00Z' }),
      ],
    }))))
    await expect(cancel()).resolves.toMatchObject({ bounty_id: BOUNTY })
  })

  it('reports a pause tied in time with another control as unknown', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(outage()).mockResolvedValueOnce(Response.json(operator({
      audit: [
        audit('claims_paused', { recorded_at: '2026-10-16T01:00:00Z' }),
        audit('claims_resumed', { actor: 'other@example.com', recorded_at: '2026-10-16T01:00:00Z' }),
      ],
    }))))
    await expect(pause()).rejects.toThrow('may have committed')
  })

  it('reads the payout policy revision this write targets, whatever followed it', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(outage())
      .mockResolvedValueOnce(Response.json({ revision: 1, single_approver_max_alpha_rao: 5, dual_approver_max_alpha_rao: 50, actor: ACTOR, reason }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(setBountyPayoutPolicy(ACTOR, {
      expectedRevision: 0, singleApproverMaxAlphaRao: 5, dualApproverMaxAlphaRao: 50, reason, confirmation: 'SET BOUNTY PAYOUT POLICY 1',
    })).resolves.toMatchObject({ revision: 1, actor: ACTOR })
    expect(sent(fetchMock, 1).url.endsWith('/api/v1/admin/bounties/payout-policy?revision=1')).toBe(true)
  })
})

describe('bounty inputs are canonical before any request', () => {
  it.each([
    [{ upheld: false, restoreDays: 2 }],
    [{ upheld: true }],
  ])('refuses an appeal decision %o Platform would refuse, before any request', async (decision) => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    await expect(decideBountyAppeal(ACTOR, { bountyId: BOUNTY, appealId: APPEAL, reason, confirmation: 'x', ...decision }))
      .rejects.toThrow('restoreDays is required to uphold an appeal')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('reads back a review sent with uppercase ids against the lowercase record', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(outage())
      .mockResolvedValueOnce(Response.json(claim({ submissions: [{ submission_id: SUBMISSION, decision: 'reject', decision_reason: reason }] })))
      .mockResolvedValueOnce(Response.json(operator({ audit: [audit('review_decided', { claim_id: CLAIM, detail: `reject submission ${SUBMISSION}` })] })))
    vi.stubGlobal('fetch', fetchMock)
    await expect(reviewBountySubmission(ACTOR, {
      claimId: CLAIM.toUpperCase(), submissionId: SUBMISSION.toUpperCase(), decision: 'reject', reason,
      confirmation: `REJECT BOUNTY CLAIM ${REPO}#2942 ${CLAIM}`,
    })).resolves.toMatchObject({ claim_id: CLAIM })
    expect(sent(fetchMock).url).toContain(`/claims/${CLAIM}/review`)
    expect(sent(fetchMock).body).toMatchObject({ claim_id: CLAIM, submission_id: SUBMISSION })
  })

  it.each([
    [{ queue: 'reviews', state: 'uncertain' }],
    [{ queue: 'settlement', overdueOnly: true }],
  ])('refuses a filter queue %o cannot apply', async (input) => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    await expect(fetchBountyQueue(input)).rejects.toThrow('filters only the')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('bounty reads', () => {
  it('reads an unset payout policy as null, not a failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(envelope('no bounty payout policy is configured', 404)))
    await expect(fetchBountyPayoutPolicy()).resolves.toBeNull()
  })

  it('reads one exact payout policy revision on request', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(envelope('bounty payout policy revision 7 not found', 404))
    vi.stubGlobal('fetch', fetchMock)
    await expect(fetchBountyPayoutPolicy({ revision: 7 })).resolves.toBeNull()
    expect(sent(fetchMock).url.endsWith('/api/v1/admin/bounties/payout-policy?revision=7')).toBe(true)
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

  it('keeps queue filters on a runtime without URLSearchParams.size', async () => {
    const size = vi.spyOn(URLSearchParams.prototype, 'size', 'get').mockReturnValue(undefined as never)
    try {
      const fetchMock = vi.fn().mockResolvedValueOnce(Response.json({ items: [], total: 0 }))
      vi.stubGlobal('fetch', fetchMock)
      await fetchBountyQueue({ queue: 'reviews', overdueOnly: true })
      expect(sent(fetchMock).url.endsWith('/queue/reviews?overdue_only=true')).toBe(true)
    } finally {
      size.mockRestore()
    }
  })

  it('reads a publication back through the uncached admin route, by issue', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(outage())
      .mockResolvedValueOnce(Response.json(operator({ revisions: [{ revision: 2, spec_digest: DIGEST, reviewer_actor: 'reviewer@example.com', actor: ACTOR, reason }] })))
    vi.stubGlobal('fetch', fetchMock)
    await publishBountyRevision(ACTOR, {
      repo: REPO, issue: 2942, expectedRevision: 1, terms: TERMS, reviewerActor: 'reviewer@example.com', reason,
      confirmation: `PUBLISH BOUNTY ${REPO}#2942 REVISION 2 ${DIGEST}`,
    })
    const readBack = sent(fetchMock, 1).url
    expect(readBack).toContain('/api/v1/admin/bounties?repo=')
    expect(readBack).not.toContain('/public/')
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
