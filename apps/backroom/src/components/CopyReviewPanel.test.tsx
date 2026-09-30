// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

  it('labels a re-hold of a withdrawn manual hold as superseding the withdrawal', () => {
    const withdrawalProse = 'Precautionary hold withdrawn. No misconduct finding.'
    const reheld = item({
      original: {
        ...eligible.original,
        review_kind: 'benchmark_overfit',
        reason: 'Re-held on new evidence',
        reason_source: 'reconsideration',
        superseded_reason: 'Manual precautionary hold',
        superseded_resolution: 'withdraw',
        superseded_resolution_reason: withdrawalProse,
        superseded_at: '2026-09-23T05:36:00Z',
      },
    })
    render(<CopyReviewPanel {...panelProps} initialItems={[reheld]} initialBulkEligibleCount={0} readOnly />)

    expect(screen.getByText('Re-held after a withdrawn hold')).toBeDefined()
    fireEvent.click(screen.getByText(/held-agent/))
    expect(screen.getByText(new RegExp(`Hold withdrawn: ${withdrawalProse}`))).toBeDefined()
    expect(screen.queryByText(/Cleared: /)).toBeNull()
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
            model_disposition: null,
            resolution_basis: null,
            dossier_complete: null,
            model_categories: null,
            model_inconclusive_invariants: null,
            model_evidence_count: null,
            model_causal_role_count: null,
            model_steps_observed: null,
            tool_calls_observed: null,
            budget_stop_reason: null,
            requested_model: null,
            response_provider: null,
            final_stage: null,
            cause_detail: null,
            model_tool_failure_subcode: null,
            max_elapsed_ms: null,
            elapsed_ms: null,
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

  function audit(overrides: Record<string, unknown> = {}) {
    return {
      review: eligible,
      agent_status: 'ath_pending_review',
      held_artifact_sha256: 'ab'.repeat(32),
      held_score_count: 3,
      current_artifact_sha256: 'ab'.repeat(32),
      current_score_count: 3,
      withdrawable: true,
      withdrawal_refusal: null,
      previous_status: 'scored',
      opened_by: 'operator',
      action_history: [],
      ...overrides,
    }
  }

  const board = {
    bench_version: 7,
    read_at: '2026-09-23T00:00:00Z',
    ranked_count: 0,
    champion_agent_id: null,
    champion_hotkey: null,
    champion_score: null,
    raw_leader_agent_id: null,
    raw_leader_score: null,
    fingerprint: 'before',
  }

  it('previews a withdrawal with the current guards and a typed confirmation', async () => {
    const correction = 'Precautionary hold withdrawn. No misconduct finding and no completed certification.'
    // A score arrived during the hold: the opening evidence says 3, Platform
    // compares 4. Sending the held count is what made every UI preview 409.
    vi.mocked(getAthReview).mockResolvedValue(audit({ current_score_count: 4 }) as never)
    vi.mocked(previewAthHoldWithdrawalFn).mockResolvedValue({
      agent_id: eligible.agent_id,
      review_id: eligible.review_id,
      artifact_sha256: 'ab'.repeat(32),
      score_count: 4,
      agent_status: 'ath_pending_review',
      restored_status: 'scored',
      board_before: board,
      board_after: {
        ...board,
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
      emission_gate: 'enforce',
      would_change_emission_crown: false,
      emission_reason: 'Emissions stay withheld as unresolved_review.',
      preview_token: 'preview-token-value',
      expires_at: '2026-09-23T00:10:00Z',
    })
    vi.mocked(executeAthHoldWithdrawalFn).mockResolvedValue({
      review: { ...eligible, status: 'resolved', resolution: 'withdraw', resolution_reason: correction },
      agent_status: 'scored',
      restored_status: 'scored',
      emission_reward_eligible: false,
      emission_gate: 'enforce',
      emission_reason: 'Emissions stay withheld as unresolved_review.',
    })
    vi.mocked(listCopyReviews).mockResolvedValue(listResult([], 0))
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly={false} />)
    fireEvent.click(screen.getByText(/held-agent/))
    fireEvent.click(await screen.findByText('Withdraw hold'))
    fireEvent.change(screen.getByPlaceholderText(/Miner-visible reason/), { target: { value: correction } })
    fireEvent.click(screen.getByText('Preview withdrawal'))

    await waitFor(() => expect(previewAthHoldWithdrawalFn).toHaveBeenCalledWith({
      data: {
        agentId: eligible.agent_id,
        reviewId: eligible.review_id,
        expectedSha256: 'ab'.repeat(32),
        expectedScoreCount: 4,
        expectedAgentStatus: 'ath_pending_review',
        reason: correction,
      },
    }))
    expect(executeAthHoldWithdrawalFn).not.toHaveBeenCalled()
    expect(await screen.findByText(/4 current scores/)).toBeDefined()
    expect(screen.getByText(/3 when held/)).toBeDefined()
    expect(screen.getByText(/Public crown would change/)).toBeDefined()
    expect(screen.getByText(/Emission gate enforce; emission crown would not change; rewards withheld/)).toBeDefined()
    expect(screen.getByText(/Emissions stay withheld as unresolved_review/)).toBeDefined()

    const confirm = screen.getByText('Confirm and execute') as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Withdrawal confirmation'), { target: { value: 'withdraw ath hold' } })
    expect(confirm.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Withdrawal confirmation'), { target: { value: ATH_HOLD_WITHDRAWAL_CONFIRMATION } })
    expect(confirm.disabled).toBe(false)
    fireEvent.click(confirm)
    await waitFor(() => expect(executeAthHoldWithdrawalFn).toHaveBeenCalledWith({
      data: {
        agentId: eligible.agent_id,
        reviewId: eligible.review_id,
        expectedSha256: 'ab'.repeat(32),
        expectedScoreCount: 4,
        expectedAgentStatus: 'ath_pending_review',
        reason: correction,
        previewToken: 'preview-token-value',
        confirmation: ATH_HOLD_WITHDRAWAL_CONFIRMATION,
      },
    }))
    expect(decideCopyReview).not.toHaveBeenCalled()
  })

  it('drops a withdrawal preview that returns after the operator switched rows', async () => {
    const other = item({
      review_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
      agent_id: '44444444-4444-4444-8444-444444444444',
      agent_name: 'other-agent',
    })
    vi.mocked(getAthReview).mockResolvedValue(audit() as never)
    let resolvePreview: (value: unknown) => void = () => {}
    vi.mocked(previewAthHoldWithdrawalFn).mockReturnValue(
      new Promise((resolve) => {
        resolvePreview = resolve
      }) as never,
    )
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible, other]} initialBulkEligibleCount={1} readOnly={false} />)
    fireEvent.click(screen.getByText(/held-agent/))
    fireEvent.click(await screen.findByText('Withdraw hold'))
    fireEvent.change(screen.getByPlaceholderText(/Miner-visible reason/), {
      target: { value: 'Precautionary hold withdrawn. No misconduct finding.' },
    })
    fireEvent.click(screen.getByText('Preview withdrawal'))
    await waitFor(() => expect(previewAthHoldWithdrawalFn).toHaveBeenCalledTimes(1))

    // The operator moves to another row while A's preview is still in flight.
    fireEvent.click(screen.getByText(/other-agent/))
    await waitFor(() => expect(getAthReview).toHaveBeenCalledWith({ data: { agentId: other.agent_id } }))
    await act(async () => {
      resolvePreview({
      agent_id: eligible.agent_id,
      review_id: eligible.review_id,
      artifact_sha256: 'ab'.repeat(32),
      score_count: 3,
      agent_status: 'ath_pending_review',
      restored_status: 'scored',
      board_before: board,
      board_after: board,
      would_change_crown: false,
      emission_reward_eligible: true,
      emission_gate: 'off',
      would_change_emission_crown: false,
      emission_reason: 'Emissions resume.',
      preview_token: 'stale-preview-token',
      expires_at: '2026-09-23T00:10:00Z',
      })
    })

    expect(screen.queryByLabelText('Withdrawal confirmation')).toBeNull()
    expect(screen.queryByText(/Withdraw hold for/)).toBeNull()
    expect(executeAthHoldWithdrawalFn).not.toHaveBeenCalled()
  })

  it('confirms the previewed reason and retires the preview when the reason is edited', async () => {
    const previewed = 'Precautionary hold withdrawn. No misconduct finding.'
    const edited = 'A different public reason typed after the preview.'
    vi.mocked(getAthReview).mockResolvedValue(audit() as never)
    vi.mocked(previewAthHoldWithdrawalFn).mockResolvedValue({
      agent_id: eligible.agent_id,
      review_id: eligible.review_id,
      artifact_sha256: 'ab'.repeat(32),
      score_count: 3,
      agent_status: 'ath_pending_review',
      restored_status: 'scored',
      board_before: board,
      board_after: board,
      would_change_crown: false,
      emission_reward_eligible: true,
      emission_gate: 'off',
      would_change_emission_crown: false,
      emission_reason: 'Reward eligibility is off.',
      preview_token: 'signed-preview-token-value',
      expires_at: '2026-09-23T00:10:00Z',
    })
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly={false} />)
    fireEvent.click(screen.getByText(/held-agent/))
    fireEvent.click(await screen.findByText('Withdraw hold'))
    const reasonField = screen.getByPlaceholderText(/Miner-visible reason/) as HTMLTextAreaElement
    fireEvent.change(reasonField, { target: { value: previewed } })
    fireEvent.click(screen.getByText('Preview withdrawal'))

    const dialog = await screen.findByRole('dialog')
    // The confirmation shows the reason execute will send, and the field it
    // came from is locked while the confirmation is open.
    expect(within(dialog).getByText(previewed)).toBeDefined()
    expect(reasonField.disabled).toBe(true)

    // Any edit that does reach the field retires the preview instead of
    // letting the dialog show one reason while execute sends another.
    fireEvent.change(reasonField, { target: { value: edited } })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(executeAthHoldWithdrawalFn).not.toHaveBeenCalled()
    expect(reasonField.disabled).toBe(false)

    fireEvent.click(screen.getByText('Preview withdrawal'))
    await waitFor(() => expect(previewAthHoldWithdrawalFn).toHaveBeenLastCalledWith({
      data: expect.objectContaining({ reason: edited }),
    }))
    const reopened = await screen.findByRole('dialog')
    expect(within(reopened).getByText(edited)).toBeDefined()
    expect(within(reopened).queryByText(previewed)).toBeNull()
  })

  async function openWithdrawalConfirmation() {
    vi.mocked(getAthReview).mockResolvedValue(audit() as never)
    vi.mocked(previewAthHoldWithdrawalFn).mockResolvedValue({
      agent_id: eligible.agent_id,
      review_id: eligible.review_id,
      artifact_sha256: 'ab'.repeat(32),
      score_count: 3,
      agent_status: 'ath_pending_review',
      restored_status: 'scored',
      board_before: board,
      board_after: board,
      would_change_crown: false,
      emission_reward_eligible: true,
      emission_gate: 'off',
      would_change_emission_crown: false,
      emission_reason: 'Reward eligibility is off.',
      preview_token: 'signed-preview-token-value',
      expires_at: '2026-09-23T00:10:00Z',
    })
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly={false} />)
    fireEvent.click(screen.getByText(/held-agent/))
    fireEvent.click(await screen.findByText('Withdraw hold'))
    fireEvent.change(screen.getByPlaceholderText(/Miner-visible reason/), {
      target: { value: 'Precautionary hold withdrawn. No misconduct finding.' },
    })
    fireEvent.click(screen.getByText('Preview withdrawal'))
    await screen.findByRole('dialog')
    fireEvent.change(screen.getByLabelText('Withdrawal confirmation'), {
      target: { value: ATH_HOLD_WITHDRAWAL_CONFIRMATION },
    })
  }

  it('dismisses the withdrawal confirmation when a refresh drops its hold', async () => {
    await openWithdrawalConfirmation()
    // Another operator resolved the hold; the queue no longer lists it.
    vi.mocked(listCopyReviews).mockResolvedValue(listResult([], 0))
    fireEvent.click(screen.getByText('Refresh'))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByText(/held-agent left the review queue after the preview; nothing was withdrawn/)).toBeDefined()
    expect(screen.queryByText('Confirm and execute')).toBeNull()
    expect(executeAthHoldWithdrawalFn).not.toHaveBeenCalled()
  })

  it('keeps the withdrawal confirmation when a refresh still lists its hold', async () => {
    await openWithdrawalConfirmation()
    vi.mocked(listCopyReviews).mockResolvedValue(listResult([eligible], 1))
    fireEvent.click(screen.getByText('Refresh'))
    await waitFor(() => expect(listCopyReviews).toHaveBeenCalled())

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/Withdraw hold for held-agent/)).toBeDefined()
    const confirm = screen.getByText('Confirm and execute') as HTMLButtonElement
    expect(confirm.disabled).toBe(false)
  })

  it('does not offer a withdrawal Platform would refuse', async () => {
    vi.mocked(getAthReview).mockResolvedValue(
      audit({
        withdrawable: false,
        withdrawal_refusal: 'only a manual precautionary hold can be withdrawn',
      }) as never,
    )
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly={false} />)
    fireEvent.click(screen.getByText(/held-agent/))
    await waitFor(() => expect(getAthReview).toHaveBeenCalledWith({ data: { agentId: eligible.agent_id } }))
    expect(screen.getByText('Clear hold')).toBeDefined()
    expect(screen.getByText('Reject submission')).toBeDefined()
    expect(screen.queryByText('Withdraw hold')).toBeNull()
    expect(previewAthHoldWithdrawalFn).not.toHaveBeenCalled()
  })

  it('never reads the audit for a read-only operator', () => {
    render(<CopyReviewPanel {...panelProps} initialItems={[eligible]} initialBulkEligibleCount={1} readOnly />)
    fireEvent.click(screen.getByText(/held-agent/))
    expect(getAthReview).not.toHaveBeenCalled()
    expect(screen.queryByText('Withdraw hold')).toBeNull()
  })
})
