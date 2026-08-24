// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  CONFIRMATION_BUNDLE_RETEST_CONFIRMATION,
  type ConfirmationBundleList,
  type ConfirmationBundleSettingsControl,
  type ConfirmationBundleView,
} from '../lib/admin.schemas'
import { ConfirmationBundleControlPanel } from './ConfirmationBundleControlPanel'

const getConfirmationBundleSettings = vi.fn()
const listConfirmationBundles = vi.fn()
const updateConfirmationBundleSettings = vi.fn()
const authorizeConfirmationBundleRetest = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getConfirmationBundleSettings: () => getConfirmationBundleSettings(),
  listConfirmationBundles: (input: unknown) => listConfirmationBundles(input),
  updateConfirmationBundleSettings: (input: unknown) => updateConfirmationBundleSettings(input),
  authorizeConfirmationBundleRetest: (input: unknown) => authorizeConfirmationBundleRetest(input),
}))

const SHA = {
  artifact: 'a'.repeat(64),
  base: 'b'.repeat(64),
  profile: 'c'.repeat(64),
  settings: 'd'.repeat(64),
  evidence: 'e'.repeat(64),
  dataset: 'f'.repeat(64),
  other: '1'.repeat(64),
}

const SETTINGS = {
  mode: 'shadow' as const,
  eligibility_mode: 'rank' as const,
  top_n: 5,
  min_base_score_micros: 950_000,
  daily_bundle_cap: 10,
  daily_dollar_cap_microusd: 1_000_000,
  per_bundle_request_cap: 100,
  per_bundle_token_cap: 10_000,
  profile_revision: 'confirmation-v9-test-1',
  profile_checksum: SHA.profile,
  challenger_z: 1.64,
}

function control(): ConfirmationBundleSettingsControl {
  const revision = {
    revision: 3,
    parent_revision: 2,
    scope: '*' as const,
    settings: SETTINGS,
    checksum: SHA.settings,
    reason: 'collect bounded shadow evidence before enforcement',
    actor: 'operator@example.com',
    created_at: '2026-08-08T12:00:00Z',
  }
  return {
    current: [revision],
    history: [revision],
    default: {
      ...SETTINGS,
      mode: 'off',
      daily_bundle_cap: 0,
      daily_dollar_cap_microusd: 0,
      per_bundle_request_cap: 0,
      per_bundle_token_cap: 0,
      profile_revision: null,
      profile_checksum: null,
    },
    effective: {
      revision: 3,
      scope: '*',
      settings: SETTINGS,
      checksum: SHA.settings,
      source: 'revision',
      configured: true,
      issuance_active: true,
      max_top_n: 10,
      max_daily_bundle_cap: 1_000,
      max_daily_dollar_microusd: 1_000_000_000,
      max_bundle_request_cap: 100_000,
      max_bundle_token_cap: 100_000_000,
    },
  }
}

function pendingBundle(): ConfirmationBundleView {
  return {
    bundle_id: '11111111-1111-4111-8111-111111111111',
    artifact_sha256: SHA.artifact,
    bench_version: 9,
    profile_revision: SETTINGS.profile_revision,
    profile_checksum: SHA.profile,
    retest_generation: 0,
    generation_reason: 'initial',
    source_bundle_id: null,
    state: 'pending',
    settings_revision: 3,
    settings_checksum: SHA.settings,
    qualification_status: null,
    completion_mode: null,
    completion_ticket_id: null,
    evidence_sha256: null,
    reporter_hotkey: null,
    bundle_signature: null,
    evidence_root: null,
    verified_at: null,
    completed_at: null,
    created_at: '2026-08-08T12:01:00Z',
    updated_at: '2026-08-08T12:01:00Z',
    dimensions: [],
    tickets: [],
    subjects: [
      {
        agent_id: '22222222-2222-4222-8222-222222222222',
        bench_version: 9,
        artifact_sha256: SHA.artifact,
        result_status: 'provisional',
        base_evidence_sha256: SHA.base,
        base_quality_micros: 750_000,
        base_stderr_micros: 20_000,
        base_model_factor_bps: 10_000,
        base_tool_factor_bps: 10_000,
        full_quality_micros: null,
        full_stderr_micros: null,
        semantic_factor_bps: null,
        applied_factor_bps: null,
        full_effective_micros: null,
        bundle_id: '11111111-1111-4111-8111-111111111111',
        created_at: '2026-08-08T12:01:00Z',
        updated_at: '2026-08-08T12:01:00Z',
      },
    ],
  }
}

function providerLane(lane: 'reader' | 'judge', cost: number) {
  return {
    lane,
    cost_source: 'provider_receipt_v1' as const,
    currency: 'USD' as const,
    provider: lane === 'reader' ? 'openai' : 'targon',
    profile_revision: `${lane}-1`,
    model: lane === 'reader' ? 'openai/gpt-oss-20b' : 'Qwen/Qwen3-235B',
    fallback_used: false as const,
    requests: lane === 'reader' ? 2 : 1,
    successes: lane === 'reader' ? 2 : 1,
    receipted_requests: lane === 'reader' ? 2 : 1,
    prompt_tokens: lane === 'reader' ? 100 : 40,
    completion_tokens: lane === 'reader' ? 50 : 10,
    total_tokens: lane === 'reader' ? 150 : 50,
    cost_usd_micros: cost,
    receipt_set_sha256: lane === 'reader' ? '2'.repeat(64) : '3'.repeat(64),
  }
}

const CAPABILITIES = [
  'extraction',
  'multi_session_reasoning',
  'temporal_reasoning',
  'knowledge_update',
  'preference',
  'abstention',
] as const

function ablation(intervention: 'inference' | 'embedding', mode: 'shadow' | 'enforce') {
  return {
    status: 'completed' as const,
    evidence_sha256: intervention === 'inference' ? '4'.repeat(64) : '5'.repeat(64),
    latency_ms: 200,
    request_count: 0 as const,
    input_tokens: 0 as const,
    output_tokens: 0 as const,
    provider_cost_microusd: 0 as const,
    synthetic: true as const,
    evidence: {
      contract_version: 'ablation-v1',
      bench_version: 9 as const,
      artifact_sha256: SHA.artifact,
      intervention,
      mode,
      status: 'passed' as const,
      reason: 'threshold_met',
      profile_revision: `${intervention}-1`,
      profile_checksum: '6'.repeat(64),
      threshold_manifest_sha256: '7'.repeat(64),
      coordinator_sha256: '8'.repeat(64),
      dataset_sha256: '9'.repeat(64),
      case_set_sha256: '0'.repeat(64),
      baseline_scores_sha256: '1'.repeat(64),
      ablated_scores_sha256: '2'.repeat(64),
      baseline_mean_micros: 800_000,
      ablated_mean_micros: 500_000,
      delta_micros: 300_000,
      threshold_micros: 200_000,
      sample_count: 2,
      affected_call_count: 1,
      semantic_factor_bps: 10_000 as const,
      applied_factor_bps: 10_000 as const,
      synthetic_usage: {
        synthetic: true as const,
        intervention,
        budget: {
          max_chat_requests: intervention === 'inference' ? 8 : 0,
          max_chat_input_bytes: intervention === 'inference' ? 4_096 : 0,
          max_embedding_requests: intervention === 'embedding' ? 8 : 0,
          max_embedding_inputs: intervention === 'embedding' ? 16 : 0,
          max_embedding_input_bytes: intervention === 'embedding' ? 4_096 : 0,
        },
        chat_attempts: intervention === 'inference' ? 1 : 0,
        chat_applied: intervention === 'inference' ? 1 : 0,
        chat_input_bytes: intervention === 'inference' ? 64 : 0,
        embedding_attempts: intervention === 'embedding' ? 1 : 0,
        embedding_applied: intervention === 'embedding' ? 1 : 0,
        embedding_inputs: intervention === 'embedding' ? 1 : 0,
        embedding_input_bytes: intervention === 'embedding' ? 64 : 0,
        rejected_requests: 0,
        budget_exhausted: false,
        upstream_requests: 0 as const,
        upstream_input_tokens: 0 as const,
        upstream_output_tokens: 0 as const,
        upstream_provider_cost_microusd: 0 as const,
      },
    },
  }
}

function completedBundle(): ConfirmationBundleView {
  const longmemeval = {
    status: 'completed' as const,
    evidence_sha256: 'a'.repeat(64),
    latency_ms: 1_000,
    request_count: 3,
    input_tokens: 140,
    output_tokens: 60,
    provider_cost_microusd: 15_000,
    synthetic: false as const,
    evidence: {
      schema_version: 2 as const,
      artifact_sha256: SHA.artifact,
      bench_version: 9 as const,
      profile_checksum: SHA.profile,
      case_set_digest: SHA.other,
      dataset_revision: 'longmemeval-s-test-1',
      dataset_sha256: SHA.dataset,
      score: {
        longmem_mean_micros: 500_000,
        longmem_stderr_micros: 30_000,
        case_count: 12,
        per_capability: CAPABILITIES.map((capability) => ({
          capability,
          correct: 1,
          count: 2,
          mean_micros: 500_000,
        })),
      },
      provider_evidence: [providerLane('judge', 5_000), providerLane('reader', 10_000)],
    },
  }
  const inference = ablation('inference', 'enforce')
  const embedding = ablation('embedding', 'enforce')
  return {
    ...pendingBundle(),
    state: 'completed',
    completion_mode: 'enforce',
    qualification_status: 'qualified',
    completion_ticket_id: '33333333-3333-4333-8333-333333333333',
    evidence_sha256: SHA.evidence,
    reporter_hotkey: '5ValidatorAlice',
    bundle_signature: 'ab'.repeat(64),
    verified_at: '2026-08-08T12:06:00Z',
    completed_at: '2026-08-08T12:06:00Z',
    evidence_root: {
      schema_version: 1,
      artifact_sha256: SHA.artifact,
      bench_version: 9,
      confirmation_profile_revision: SETTINGS.profile_revision,
      confirmation_profile_checksum: SHA.profile,
      settings_revision: 3,
      settings_checksum: SHA.settings,
      retest_generation: 0,
      ablation_coordinator_latency_ms: 400,
      composite_policy: {
        schema_version: 1,
        revision: 'composite-v9-test-1',
        formula_revision: 'weighted-quality-gates-v1',
        base_weight_bps: 6_000,
        longmem_weight_bps: 4_000,
        checksum: SHA.other,
      },
      longmemeval,
      inference_ablation: inference,
      embedding_ablation: embedding,
      totals: {
        request_count: 3,
        input_tokens: 140,
        output_tokens: 60,
        provider_cost_microusd: 15_000,
        latency_ms: 1_400,
      },
    },
    dimensions: [
      { dimension: 'longmemeval', ...longmemeval, created_at: '2026-08-08T12:06:00Z' },
      { dimension: 'inference_ablation', ...inference, created_at: '2026-08-08T12:06:00Z' },
      { dimension: 'embedding_ablation', ...embedding, created_at: '2026-08-08T12:06:00Z' },
    ],
    tickets: [
      {
        ticket_id: '33333333-3333-4333-8333-333333333333',
        validator_hotkey: '5ValidatorAlice',
        slot_id: 'slot-0',
        status: 'scored',
        attempt: 1,
        issued_at: '2026-08-08T12:01:00Z',
        deadline: '2026-08-08T13:31:00Z',
        failure_reason: null,
        failure_class: null,
        failure_stage: null,
        failed_at: null,
        prepare_rejection: null,
        prepare_rejected_at: null,
      },
    ],
    subjects: [
      {
        ...pendingBundle().subjects[0]!,
        result_status: 'full_confirmed',
        full_quality_micros: 650_000,
        full_stderr_micros: 18_000,
        semantic_factor_bps: 10_000,
        applied_factor_bps: 10_000,
        full_effective_micros: 650_000,
      },
      {
        ...pendingBundle().subjects[0]!,
        agent_id: '44444444-4444-4444-8444-444444444444',
        base_quality_micros: 900_000,
        base_stderr_micros: 80_000,
        result_status: 'full_confirmed',
        full_quality_micros: 740_000,
        full_stderr_micros: 50_000,
        semantic_factor_bps: 10_000,
        applied_factor_bps: 10_000,
        full_effective_micros: 740_000,
      },
    ],
  }
}

function listing(
  items: ConfirmationBundleView[] = [pendingBundle()],
  count = items.length,
): ConfirmationBundleList {
  return {
    items,
    count,
    budget: {
      utc_day: '2026-08-08',
      revision: 4,
      issued_attempts: 2,
      outstanding_reserved_microusd: 25_000,
      settled_microusd: 15_000,
    },
    shadow_calibration: {
      observed_from_utc_day: '2026-08-01',
      observed_through_utc_day: '2026-08-08',
      observation_days: 8,
      confirmation_profile_revision: SETTINGS.profile_revision,
      confirmation_profile_checksum: SHA.profile,
      base_run_count: 40,
      measured_base_cost_microusd: 130_000,
      confirmation_bundle_count: 10,
      measured_bundle_cost_microusd: 60_000,
      bench_version: 9,
      completed_bundle_count: 8,
      superseded_bundle_count: 0,
      failed_bundle_count: 0,
      qualified_bundle_count: 2,
      promotion_rate_bps: 2_500,
      projected_daily_spend_microusd: 725_000,
      epoch_duration_seconds: null,
      projected_epoch_spend_microusd: null,
      epoch_projection_unavailable_reason:
        'Bench v9 has no configured epoch duration; no projection was guessed.',
    },
  }
}

function unspentSupersededBundle(): ConfirmationBundleView {
  return { ...pendingBundle(), state: 'superseded', updated_at: '2026-08-08T12:03:00Z' }
}

describe('ConfirmationBundleControlPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getConfirmationBundleSettings.mockReset().mockResolvedValue(control())
    listConfirmationBundles.mockReset().mockResolvedValue(listing())
    updateConfirmationBundleSettings.mockReset().mockResolvedValue({})
    authorizeConfirmationBundleRetest.mockReset().mockResolvedValue({})
  })

  it('makes shadow non-authority and committed integer spend explicit', () => {
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing()} readOnly={false} />)

    expect(screen.getByText('40,000')).toBeTruthy()
    expect(screen.getByText('130,000 μUSD')).toBeTruthy()
    expect(screen.getByText('60,000 μUSD')).toBeTruthy()
    expect(screen.getByText('25.0%')).toBeTruthy()
    expect(screen.getByText('725,000 μUSD')).toBeTruthy()
    expect(screen.getByText('Unavailable')).toBeTruthy()
    expect(screen.getByText(/no configured epoch duration/)).toBeTruthy()
    expect(screen.getByText(/Shadow records verified previews but never marks a subject full-confirmed/)).toBeTruthy()
    expect(screen.getByText(/cannot submit evidence or activate rewards/)).toBeTruthy()
  })

  it('keeps every policy mutation disabled for read-only operators', () => {
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing()} readOnly />)

    expect(screen.getByLabelText('Mode')).toHaveProperty('disabled', true)
    expect(screen.queryByRole('button', { name: 'Apply audited revision' })).toBeNull()
  })

  it('sends the complete policy with revision guard and exact confirmation', async () => {
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing()} readOnly={false} />)

    fireEvent.change(screen.getByLabelText('Mode'), { target: { value: 'enforce' } })
    fireEvent.change(screen.getByLabelText('Audit reason'), { target: { value: 'promote only after the shadow qualification audit passed' } })
    fireEvent.change(screen.getByLabelText('Exact confirmation'), { target: { value: 'APPLY V9 CONFIRMATION MODE ENFORCE' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply audited revision' }))

    await waitFor(() => expect(updateConfirmationBundleSettings).toHaveBeenCalledTimes(1))
    expect(updateConfirmationBundleSettings).toHaveBeenCalledWith({
      data: {
        scope: '*',
        expectedRevision: 3,
        settings: { ...SETTINGS, mode: 'enforce' },
        reason: 'promote only after the shadow qualification audit passed',
        confirmation: 'APPLY V9 CONFIRMATION MODE ENFORCE',
      },
    })
  })

  it('shows shared provider lanes and distinct per-subject projections', () => {
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing([completedBundle()])} readOnly={false} />)

    fireEvent.click(screen.getByText(/generation 0/).closest('button')!)
    expect(screen.getAllByText('LongMem 0.5000').length).toBeGreaterThan(0)
    expect(screen.getByText('Mean 0.5000 · 12 cases')).toBeTruthy()
    expect(screen.getByText('openai · openai/gpt-oss-20b')).toBeTruthy()
    expect(screen.getByText('targon · Qwen/Qwen3-235B')).toBeTruthy()
    expect(screen.getAllByText('threshold_met')).toHaveLength(2)
    expect(screen.getByText('composite-v9-test-1 · 6000/4000 bps')).toBeTruthy()
    expect(screen.getByText(/400 ms/)).toBeTruthy()
    const table = screen.getByRole('table')
    expect(within(table).getAllByText('0.6500')).toHaveLength(2)
    expect(within(table).getAllByText('0.7400')).toHaveLength(2)
  })

  it('renders generation lineage in the audit view without truncation overflow', () => {
    const bundle = completedBundle()
    bundle.retest_generation = 1
    bundle.generation_reason = 'operator_retest'
    bundle.source_bundle_id = '66666666-6666-4666-8666-666666666666'
    bundle.evidence_root!.retest_generation = 1
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing([bundle])} readOnly={false} />)

    fireEvent.click(screen.getByText(/generation 1/).closest('button')!)
    expect(screen.getByText('operator retest')).toBeTruthy()
    expect(screen.getByText(/generation 1 · operator retest/)).toBeTruthy()
    expect(screen.getByText('66666666-6666-4666-8666-666666666666')).toBeTruthy()
  })

  it('keeps an unspent superseded audit row visible without offering an illegal retest', () => {
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing([unspentSupersededBundle()])} readOnly={false} />)

    fireEvent.click(screen.getByText(/generation 0/).closest('button')!)
    expect(screen.getByText('not completed')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Authorize retest' })).toBeNull()
    expect(screen.getByText('No verified lane evidence yet.')).toBeTruthy()
  })

  it('requires exact audited confirmation before authorizing a new generation', async () => {
    vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue('55555555-5555-4555-8555-555555555555')
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing([completedBundle()])} readOnly={false} />)

    fireEvent.click(screen.getByText(/generation 0/).closest('button')!)
    const authorize = screen.getByRole('button', { name: 'Authorize retest' })
    expect(authorize).toHaveProperty('disabled', true)
    fireEvent.change(screen.getAllByLabelText('Audit reason').at(-1)!, { target: { value: 'replace the completed evidence with a fresh audited generation' } })
    fireEvent.change(screen.getAllByLabelText('Exact confirmation').at(-1)!, { target: { value: CONFIRMATION_BUNDLE_RETEST_CONFIRMATION } })
    fireEvent.click(authorize)

    await waitFor(() => expect(authorizeConfirmationBundleRetest).toHaveBeenCalledTimes(1))
    expect(authorizeConfirmationBundleRetest).toHaveBeenCalledWith({
      data: {
        bundleId: '11111111-1111-4111-8111-111111111111',
        requestId: '55555555-5555-4555-8555-555555555555',
        expectedGeneration: 0,
        reason: 'replace the completed evidence with a fresh audited generation',
        confirmation: CONFIRMATION_BUNDLE_RETEST_CONFIRMATION,
      },
    })
  })

  it('passes lifecycle filters to the authenticated no-store read', async () => {
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing()} readOnly={false} />)
    fireEvent.click(screen.getByRole('button', { name: 'Completed' }))
    await waitFor(() => expect(listConfirmationBundles).toHaveBeenCalledWith({ data: { state: 'completed', limit: 20, offset: 0 } }))
  })

  it('pages through the Platform-owned total count with explicit offsets', async () => {
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing([pendingBundle()], 21)} readOnly={false} />)

    expect(screen.getByText('1 bundle returned · 21 total · newest first')).toBeTruthy()
    expect(screen.getByText('1–1 of 21')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Previous' })).toHaveProperty('disabled', true)
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))

    await waitFor(() =>
      expect(listConfirmationBundles).toHaveBeenCalledWith({ data: { limit: 20, offset: 20 } }),
    )
  })

  it('teaches the empty state and disables pagination when no rows exist', () => {
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing([], 0)} readOnly={false} />)

    expect(screen.getByText('No bundles match this lifecycle state.')).toBeTruthy()
    expect(screen.getByText('0 bundles returned · 0 total · newest first')).toBeTruthy()
    expect(screen.getByText('No rows')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Previous' })).toHaveProperty('disabled', true)
    expect(screen.getByRole('button', { name: 'Next' })).toHaveProperty('disabled', true)
  })

  it('announces refresh errors without discarding the current audit page', async () => {
    listConfirmationBundles.mockRejectedValueOnce(new Error('Platform timed out'))
    render(<ConfirmationBundleControlPanel initialSettings={control()} initialBundles={listing([pendingBundle()], 21)} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh bundles' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Platform timed out')
    expect(screen.getByText('1–1 of 21')).toBeTruthy()
  })
})
