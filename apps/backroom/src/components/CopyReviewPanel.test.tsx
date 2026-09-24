// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ATH_HOLD_WITHDRAWAL_CONFIRMATION, type CopyReviewConsoleItem } from '../lib/admin.schemas'
import { CopyReviewPanel } from './CopyReviewPanel'

vi.mock('@tanstack/react-start', () => ({ useServerFn: (serverFn: unknown) => serverFn }))
vi.mock('../server/admin.functions', () => ({
  decideCopyReview: vi.fn(),
  listCopyReviews: vi.fn(),
  openAthReview: vi.fn(),
  getAthReview: vi.fn(),
  previewAthHoldWithdrawalFn: vi.fn(),
  executeAthHoldWithdrawalFn: vi.fn(),
  getCopyReviewSourceDiff: vi.fn(),
  getCopyReviewSourceDiffFile: vi.fn(),
}))

import { decideCopyReview, executeAthHoldWithdrawalFn, getAthReview, listCopyReviews, openAthReview, previewAthHoldWithdrawalFn } from '../server/admin.functions'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function item(overrides: Partial<CopyReviewConsoleItem> = {}): CopyReviewConsoleItem {
  return {
    review_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    agent_id: '11111111-1111-4111-8111-111111111111',
    miner_hotkey: '5MinerHotkeyExample',
    miner_coldkey: null,
    agent_name: 'held-agent',
    agent_version: 2,
    submitted_at: '2026-07-15T12:00:00Z',
    status: 'pending',
    agent_status: 'ath_pending_review',
    opened_at: '2026-07-15T13:00:00Z',
    resolved_at: null,
    resolved_by: null,
    resolution: null,
    resolution_reason: null,
    original: {
      review_kind: 'copy',
      duplicate_of: '22222222-2222-4222-8222-222222222222',
      reason: 'legacy baseline-dominated hold',
      reason_source: 'original_hold',
      superseded_reason: null,
      superseded_resolution: null,
      superseded_resolution_reason: null,
      superseded_at: null,
      policy_version: 1,
      fingerprint_versions: { lexical: null, structural: null, prompt: null },
      reference_provenance: 'legacy',
      backfilled: true,
      duplicate_of_coldkey: null,
      duplicate_of_name: 'jackie',
      duplicate_of_version: 3,
      duplicate_of_hotkey: '5G9QoBvJLtAsE9WRnz8cPknYu6D6WfHmfbfGxiWpyvs5JSP4',
      duplicate_of_submitted_at: '2026-07-15T04:52:56Z',
      deferred_review: null,
    },
    current_comparison: {
      availability: 'available',
      bulk_eligible: true,
      algorithm_version: 'reference-aware-v2',
      lexical_fingerprint_version: 2,
      normalized_source_fingerprint_version: 'v2',
      prompt_fingerprint_version: 'p2',
      canonical_reference_revision: '959cd69a1a8d3b0defbfb8296518adb7d4f17c14',
      reference_corpus_id: '21dc06cd72aafefb56d0e89e8b3127280dda249ae26cb649ee855185121e9ce6',
      reference_exclusion_mode: 'starter-kit-mainline-history',
      miner_exclusion_mode: 'cross-miner-only',
      same_miner_excluded: false,
      chronology_direction: 'reference-before-candidate',
      chronology_eligible: true,
      exact_byte_match: false,
      normalized_source_match: false,
      lexical: {
        candidate_version: 2, reference_version: 2, compatible: true, applicable: true,
        candidate_cardinality: 100, reference_cardinality: 90, jaccard: 0.12, containment: 0.28,
        above_threshold: false, decision_role: 'trigger',
      },
      structural: {
        candidate_version: 'v2', reference_version: 'v2', compatible: true, applicable: true,
        candidate_cardinality: 100, reference_cardinality: 90, jaccard: 0.1, containment: 0.2,
        above_threshold: false, decision_role: 'advisory',
      },
      prompt: {
        candidate_version: 'p2', reference_version: 'p2', compatible: true, applicable: true,
        candidate_cardinality: 20, reference_cardinality: 18, jaccard: 0.03, containment: 0.06,
        above_threshold: false, decision_role: 'advisory',
      },
      triggered: false,
      triggered_signal: null,
      current_decision: 'clear',
    },
    ...overrides,
  }
}

const eligible = item()
const unavailable = item({
  review_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
  agent_id: '33333333-3333-4333-8333-333333333333',
  agent_name: 'legacy-agent',
  current_comparison: { availability: 'unavailable', bulk_eligible: false, reason: 'current comparison unavailable' },
})

const panelProps = {
  initialGeneration: 'active' as const,
  initialActiveBenchVersion: 8,
  initialRolloutBenchVersion: 9,
}

function listResult(
  items: Array<CopyReviewConsoleItem>,
  bulkEligibleCount: number,
  generation: 'active' | 'rollout' | 'history' = 'active',
) {
  return {
    items,
    count: items.length,
    limit: 200,
    offset: 0,
    bulk_eligible_count: bulkEligibleCount,
    generation,
    active_bench_version: 8,
    rollout_bench_version: 9,
  }
}

describe('CopyReviewPanel', () => {
  it('opens an exact guarded benchmark review hold', async () => {
    const detailedReason = `Deterministic benchmark-family routing: ${'e'.repeat(1_000)}`
    vi.mocked(openAthReview).mockResolvedValue({
      review: {
        ...eligible,
        original: {
          ...eligible.original,
          review_kind: 'benchmark_overfit',
          duplicate_of: null,
        },
      },
      agent_status: 'ath_pending_review',
      idempotent: false,
      reopened: false,
    })
    vi.mocked(listCopyReviews).mockResolvedValue(listResult([], 0))
    render(<CopyReviewPanel {...panelProps} initialItems={[]} initialBulkEligibleCount={0} readOnly={false} />)
    expect(screen.getByText('Public hold reason')).toBeDefined()
    fireEvent.change(screen.getByLabelText('Agent ID'), { target: { value: eligible.agent_id } })
    fireEvent.change(screen.getByLabelText('Artifact SHA-256'), { target: { value: 'ab'.repeat(32) } })
    fireEvent.change(screen.getByLabelText('Score count'), { target: { value: '3' } })
    fireEvent.change(screen.getByLabelText('Hold reason'), { target: { value: detailedReason } })
    fireEvent.click(screen.getByText('Preview hold or reopen'))
    expect(openAthReview).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Confirm and execute'))

    await waitFor(() => expect(openAthReview).toHaveBeenCalledWith({
      data: {
        agentId: eligible.agent_id,
        expectedSha256: 'ab'.repeat(32),
        expectedScoreCount: 3,
        reason: detailedReason,
      },
    }))
    expect(await screen.findByText(/excluded from emissions/)).toBeDefined()
  })

  it('confirms when an existing review was reopened', async () => {
    vi.mocked(openAthReview).mockResolvedValue({
      review: eligible,
      agent_status: 'ath_pending_review',
      idempotent: false,
      reopened: true,
    })
    vi.mocked(listCopyReviews).mockResolvedValue(listResult([eligible], 1))
    render(<CopyReviewPanel {...panelProps} initialItems={[]} initialBulkEligibleCount={0} readOnly={false} />)
    fireEvent.change(screen.getByLabelText('Agent ID'), { target: { value: eligible.agent_id } })
    fireEvent.change(screen.getByLabelText('Artifact SHA-256'), { target: { value: 'ab'.repeat(32) } })
    fireEvent.change(screen.getByLabelText('Score count'), { target: { value: '3' } })
    fireEvent.change(screen.getByLabelText('Hold reason'), { target: { value: 'New evidence warrants another review' } })
    fireEvent.click(screen.getByText('Preview hold or reopen'))
    fireEvent.click(screen.getByText('Confirm and execute'))

    expect(await screen.findByText(/was reopened for review/)).toBeDefined()
  })

  it('separates immutable original evidence from current calibrated evidence', () => {
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible, unavailable]} initialBulkEligibleCount={1} readOnly={false} />)
    expect(screen.getByText('2 pending')).toBeDefined()
    expect(screen.getByText('1 safe to clear')).toBeDefined()
    expect(screen.getByText('Cleared')).toBeDefined()
    expect(screen.getByText('Unavailable')).toBeDefined()
    fireEvent.click(screen.getByText(/held-agent/))
    expect(screen.getByRole('heading', { name: 'Review evidence' })).toBeDefined()
    expect(screen.getByText('Current calibrated comparison')).toBeDefined()
  })

  it('labels a rotated-hotkey match as same-owner lineage', () => {
    const comparison = eligible.current_comparison
    if (comparison.availability !== 'available') throw new Error('fixture unavailable')
    const sameOwner = item({
      current_comparison: {
        ...comparison,
        bulk_eligible: false,
        miner_exclusion_mode: 'same-payment-coldkey-excluded-hotkey-fallback',
        same_miner_excluded: true,
        chronology_eligible: false,
        current_decision: 'excluded',
      },
    })

    render(<CopyReviewPanel {...panelProps} initialItems={[sameOwner]} initialBulkEligibleCount={0} readOnly />)

    expect(screen.getByText('Same-owner lineage')).toBeDefined()
    fireEvent.click(screen.getByText(/held-agent/))
    expect(screen.getByText('Excluded — same payment owner lineage')).toBeDefined()
  })

  it('marks a withdrawn rejection as history and shows the reconsideration reason', () => {
    // lets_635 v1: the queue kept showing the withdrawn I5 REJECT prose as the
    // live hold reason on a pending appeal. The current reason is now the
    // reconsideration, and the prior decision is labelled, not deleted.
    const rejectProse = 'Reject under policy v13 for I5: benchmark-shaped answer assembly'
    const reconsidered = item({
      original: {
        ...eligible.original,
        reason: 'I5 rejection withdrawn as unsupported; reconsidering under v13',
        reason_source: 'reconsideration',
        superseded_reason: 'Deferred source review qualified this submission for I5',
        superseded_resolution: 'reject',
        superseded_resolution_reason: rejectProse,
        superseded_at: '2026-09-23T05:36:00Z',
      },
    })
    render(<CopyReviewPanel {...panelProps} initialItems={[reconsidered]} initialBulkEligibleCount={1} readOnly />)

    expect(screen.getByText('Prior rejection withdrawn')).toBeDefined()
    expect(
      screen.getAllByText(/I5 rejection withdrawn as unsupported/).length,
    ).toBeGreaterThan(0)
    fireEvent.click(screen.getByText(/held-agent/))
    expect(screen.getByText('Current reason (reconsideration)')).toBeDefined()
    expect(screen.getByText(/Superseded — history, not an active finding/)).toBeDefined()
    expect(screen.getByText(new RegExp(`Rejected: ${rejectProse}`))).toBeDefined()
    expect(
      screen.getByText(/Originally held for: Deferred source review qualified/),
    ).toBeDefined()
  })

  it('leaves an ordinary hold reason unlabelled', () => {
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly />)
    expect(screen.queryByText('Prior rejection withdrawn')).toBeNull()
    fireEvent.click(screen.getByText(/held-agent/))
    expect(screen.getByText('Reason')).toBeDefined()
    expect(screen.queryByText(/Superseded — history/)).toBeNull()
  })

  it('names the matched submission that triggered the hold', () => {
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly />)
    // Table row shows the matched name; the evidence pane repeats it with identity.
    expect(screen.getByText('copy of jackie v3')).toBeDefined()
    fireEvent.click(screen.getByText(/held-agent/))
    expect(screen.getByText('jackie v3')).toBeDefined()
    expect(screen.getByText(/5G9QoBvJLtAs…/)).toBeDefined()
  })

  it('names a same-hotkey match as a rename of the previously rejected agent', () => {
    const renamed = item({
      miner_hotkey: '5G9QoBvJLtAsE9WRnz8cPknYu6D6WfHmfbfGxiWpyvs5JSP4',
      original: {
        ...eligible.original,
        duplicate_of_name: 'Zeus_v11',
        duplicate_of_version: 6,
        duplicate_of_hotkey: '5G9QoBvJLtAsE9WRnz8cPknYu6D6WfHmfbfGxiWpyvs5JSP4',
      },
    })
    render(<CopyReviewPanel {...panelProps} initialItems={[renamed]} initialBulkEligibleCount={1} readOnly />)
    expect(screen.getByText('same-miner rename of Zeus_v11 v6')).toBeDefined()
    fireEvent.click(screen.getByText(/held-agent/))
    expect(screen.getByText('Previously rejected as')).toBeDefined()
    expect(screen.getByText('Zeus_v11 v6')).toBeDefined()
  })

  it('renders public-safe deferred trigger and inconclusive evidence without copy language', () => {
    const deferred = item({
      original: {
        ...eligible.original,
        review_kind: 'deferred_source_review',
        duplicate_of: null,
        reason: 'Score qualified this submission for deferred source review',
        deferred_review: {
          mode: 'enforce',
          triggers: ['top_five', 'memory_anomaly'],
          rank: 2,
          cohort_size: 20,
          peer_count: 19,
          candidate: { composite: 0.77, memory: 0.91 },
          thresholds: { memory: { median: 0.42, mad: 0.06, cutoff: 0.78 } },
          screening_attempt_id: '44444444-4444-4444-8444-444444444444',
          screening_reason_code: 'review-budget-exhausted',
          review_audit: {
            stage: 'l2',
            reason_code: 'max-input-tokens-exhausted',
            prompt_revision: 'source-review-v9',
            harness_revision: 'agent-tools-v2',
            max_steps: 18,
            steps_used: 18,
            max_read_bytes: 1048576,
            read_bytes_used: 900000,
            max_input_tokens: 425000,
            input_tokens_used: 425000,
            max_output_tokens: 20000,
            output_tokens_used: 18000,
            max_cost_usd: 2,
            cost_usd_used: 1.98,
          },
          review_audit_digest: 'd'.repeat(64),
        },
      },
      current_comparison: {
        availability: 'unavailable',
        bulk_eligible: false,
        reason: 'copy comparison does not apply',
      },
    })

    render(<CopyReviewPanel {...panelProps} initialItems={[deferred]} initialBulkEligibleCount={0} readOnly />)
    expect(screen.getByText('score-qualified source review')).toBeDefined()
    expect(screen.getByText('Deferred review')).toBeDefined()
    fireEvent.click(screen.getByText(/held-agent/))
    expect(screen.getByText('Score-qualified source review')).toBeDefined()
    expect(screen.getByText('top five, memory anomaly')).toBeDefined()
    expect(screen.getByText('memory cutoff 0.7800')).toBeDefined()
    expect(screen.getByText(/review budget exhausted/)).toBeDefined()
    expect(screen.getByText(/max input tokens exhausted/)).toBeDefined()
    expect(screen.getByText(/steps 18\/18/)).toBeDefined()
    expect(screen.getByText(/input 425,000\/425,000/)).toBeDefined()
    expect(screen.getByText(/prompt source-review-v9/)).toBeDefined()
    expect(screen.getByText(/not a copy comparison/)).toBeDefined()
  })

  it('read-only access hides all mutation controls', () => {
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly />)
    fireEvent.click(screen.getByText(/held-agent/))
    expect(screen.getByText(/Read-only access/)).toBeDefined()
    expect(screen.queryByText(/Clear 1 eligible submission/)).toBeNull()
  })

  it('records an individual canonical clear decision', async () => {
    const detailedReason = `Reference-aware comparison clears this pair: ${'e'.repeat(1_000)}`
    vi.mocked(decideCopyReview).mockResolvedValue({
      review: { ...eligible, current_comparison: undefined, status: 'resolved', resolution: 'clear', resolved_at: '2026-07-16T12:00:00Z', resolved_by: 'operator', resolution_reason: 'safe comparison' },
      agent_status: 'scored',
      idempotent: false,
    } as never)
    vi.mocked(listCopyReviews).mockResolvedValue(listResult([], 0))
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly={false} />)
    fireEvent.click(screen.getByText(/held-agent/))
    fireEvent.change(screen.getByPlaceholderText(/Miner-visible reason/), { target: { value: detailedReason } })
    fireEvent.click(screen.getByText('Preview clear'))
    expect(decideCopyReview).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Confirm and execute'))
    await waitFor(() => expect(decideCopyReview).toHaveBeenCalledWith({ data: { agentId: eligible.agent_id, resolution: 'clear', reason: detailedReason } }))
  })

  it('labels a deferred-review rejection without calling it benchmark overfit', async () => {
    const deferred = item({
      original: {
        ...eligible.original,
        review_kind: 'deferred_source_review',
        duplicate_of: null,
        deferred_review: null,
      },
      current_comparison: {
        availability: 'unavailable',
        bulk_eligible: false,
        reason: 'copy comparison does not apply',
      },
    })
    vi.mocked(decideCopyReview).mockResolvedValue({
      review: {
        ...deferred,
        current_comparison: undefined,
        status: 'resolved',
        resolution: 'reject',
        resolved_at: '2026-08-02T01:00:00Z',
        resolved_by: 'operator',
        resolution_reason: 'manual source review found a current-policy violation',
      },
      agent_status: 'rejected',
      idempotent: false,
    } as never)
    vi.mocked(listCopyReviews).mockResolvedValue(listResult([], 0))

    render(<CopyReviewPanel {...panelProps} initialItems={[deferred]} initialBulkEligibleCount={0} readOnly={false} />)
    fireEvent.click(screen.getByText(/held-agent/))
    fireEvent.click(screen.getByLabelText('Reject submission'))
    fireEvent.change(screen.getByPlaceholderText(/Miner-visible reason/), {
      target: { value: 'manual source review found a current-policy violation' },
    })
    fireEvent.click(screen.getByText('Preview reject'))
    fireEvent.click(screen.getByText('Confirm and execute'))

    expect(await screen.findByText(/rejected after score-qualified source review/)).toBeDefined()
    expect(screen.queryByText(/rejected after benchmark-overfit review/)).toBeNull()
  })

  it('bulk-clears only eligible rows and reports partial failure', async () => {
    const second = item({ review_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', agent_id: '44444444-4444-4444-8444-444444444444', agent_name: 'second-clear' })
    vi.mocked(decideCopyReview)
      .mockResolvedValueOnce({} as never)
      .mockRejectedValueOnce(new Error('conflicting operator decision'))
    vi.mocked(listCopyReviews).mockResolvedValue(listResult([unavailable], 0))
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible, second, unavailable]} initialBulkEligibleCount={2} readOnly={false} />)
    fireEvent.change(screen.getByPlaceholderText(/Shared miner-visible reason/), { target: { value: 'current calibrated evidence is clear' } })
    fireEvent.click(screen.getByText('Preview clearing 2 eligible submissions'))
    fireEvent.click(screen.getByText('Confirm and execute'))
    await waitFor(() => expect(decideCopyReview).toHaveBeenCalledTimes(2))
    expect(vi.mocked(decideCopyReview).mock.calls.map(([call]) => call.data.agentId)).toEqual([eligible.agent_id, second.agent_id])
    await waitFor(() => expect(screen.getByText(/1 failed and remain pending/)).toBeDefined())
    expect(screen.getByText(/conflicting operator decision/)).toBeDefined()
  })

  it('loads historical reviews only through an explicit generation switch', async () => {
    vi.mocked(listCopyReviews).mockResolvedValue(
      listResult([unavailable], 0, 'history'),
    )
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly />)

    expect(screen.getByText('Active benchmark v8')).toBeDefined()
    fireEvent.click(screen.getByText('Historical reviews'))

    await waitFor(() => expect(listCopyReviews).toHaveBeenCalledWith({
      data: { generation: 'history' },
    }))
    expect(await screen.findByText(/legacy-agent/)).toBeDefined()
    expect(screen.getByText(/Older benchmark generations/)).toBeDefined()
  })

  it('surfaces rollout-target reviews in their own operator lane', async () => {
    vi.mocked(listCopyReviews).mockResolvedValue(
      listResult([eligible], 1, 'rollout'),
    )
    render(<CopyReviewPanel {...panelProps} initialItems={[]} initialBulkEligibleCount={0} readOnly />)

    fireEvent.click(screen.getByText('Rollout target v9'))

    await waitFor(() => expect(listCopyReviews).toHaveBeenCalledWith({
      data: { generation: 'rollout' },
    }))
    expect(await screen.findByText(/held-agent/)).toBeDefined()
    expect(screen.getByText(/require review before activation can converge/)).toBeDefined()
  })

  it('previews a withdrawal before recording it, and does not clear the hold', async () => {
    const correction = 'Precautionary hold withdrawn. No misconduct finding and no completed certification.'
    vi.mocked(getAthReview).mockResolvedValue({
      review: eligible,
      agent_status: 'ath_pending_review',
      held_artifact_sha256: 'ab'.repeat(32),
      held_score_count: 3,
      previous_status: 'scored',
      opened_by: 'operator',
      action_history: [],
    })
    vi.mocked(previewAthHoldWithdrawalFn).mockResolvedValue({
      agent_id: eligible.agent_id,
      review_id: eligible.review_id,
      artifact_sha256: 'ab'.repeat(32),
      score_count: 3,
      agent_status: 'ath_pending_review',
      restored_status: 'scored',
      board_before: {
        bench_version: 7,
        read_at: '2026-09-23T00:00:00Z',
        ranked_count: 0,
        champion_agent_id: null,
        champion_hotkey: null,
        champion_score: null,
        raw_leader_agent_id: null,
        raw_leader_score: null,
        fingerprint: 'before',
      },
      board_after: {
        bench_version: 7,
        read_at: '2026-09-23T00:00:00Z',
        ranked_count: 1,
        champion_agent_id: eligible.agent_id,
        champion_hotkey: eligible.miner_hotkey,
        champion_score: 0.97,
        raw_leader_agent_id: eligible.agent_id,
        raw_leader_score: 0.97,
        fingerprint: 'after',
      },
      would_change_crown: true,
      emission_reward_eligible: false,
      emission_gate: 'unavailable',
      would_change_emission_crown: false,
      emission_reason: 'Emission eligibility stays closed.',
      preview_token: 'preview-token-value',
      expires_at: '2026-09-23T00:10:00Z',
    })
    vi.mocked(executeAthHoldWithdrawalFn).mockResolvedValue({
      review: { ...eligible, status: 'resolved', resolution: 'withdraw', resolution_reason: correction },
      agent_status: 'scored',
      restored_status: 'scored',
      emission_reward_eligible: false,
      emission_gate: 'unavailable',
      emission_reason: 'Emission eligibility stays closed.',
    })
    vi.mocked(listCopyReviews).mockResolvedValue(listResult([], 0))
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly={false} />)
    fireEvent.click(screen.getByText(/held-agent/))
    fireEvent.click(screen.getByText('Withdraw hold'))
    fireEvent.change(screen.getByPlaceholderText(/Miner-visible reason/), { target: { value: correction } })
    fireEvent.click(screen.getByText('Preview withdrawal'))
    expect(executeAthHoldWithdrawalFn).not.toHaveBeenCalled()
    expect(await screen.findByText(/Public crown would change/)).toBeDefined()
    expect(screen.getByText(/Emission eligibility stays closed/)).toBeDefined()
    fireEvent.click(screen.getByText('Confirm and execute'))
    await waitFor(() => expect(executeAthHoldWithdrawalFn).toHaveBeenCalledWith({
      data: {
        agentId: eligible.agent_id,
        reviewId: eligible.review_id,
        expectedSha256: 'ab'.repeat(32),
        expectedScoreCount: 3,
        expectedAgentStatus: 'ath_pending_review',
        reason: correction,
        previewToken: 'preview-token-value',
        confirmation: ATH_HOLD_WITHDRAWAL_CONFIRMATION,
      },
    }))
    expect(decideCopyReview).not.toHaveBeenCalled()
  })
})
