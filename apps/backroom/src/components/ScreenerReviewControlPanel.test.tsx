// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { QueuePolicySettingsControl, ScreenerReviewControl } from '../lib/admin.schemas'
import { ScreenerReviewControlPanel } from './ScreenerReviewControlPanel'

const getControl = vi.fn()
const updateSettings = vi.fn()
const getQueuePolicy = vi.fn()
const updateQueuePolicy = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getScreenerReviewControl: () => getControl(),
  updateScreenerReviewSettings: (input: unknown) => updateSettings(input),
  getQueuePolicyControl: () => getQueuePolicy(),
  updateQueuePolicySettings: (input: unknown) => updateQueuePolicy(input),
}))

const settings = {
  mode: 'off' as const,
  l2_model: 'moonshotai/kimi-k3' as const,
  l2_fallback_models: ['z-ai/glm-5.2', 'openai/gpt-5.6-sol'] as const,
  l3_enabled: true,
  l3_model: 'openai/gpt-5.6-sol' as const,
  timeout_seconds: 900,
  max_steps: 18,
  source_review_max_steps: 24,
  source_review_max_read_bytes: 1_200_000,
  source_review_reasoning_effort: 'high' as const,
  max_input_tokens: 425_000,
  max_output_tokens: 20_000,
  max_completion_tokens: 2_400,
  max_cost_usd: 2,
  critic_reasoning_effort: 'medium' as const,
  cache_ttl_seconds: 604_800,
  audit_retention_days: 30,
}

const state: ScreenerReviewControl = {
  current: [
    {
      revision: 7,
      parent_revision: 6,
      scope: '*',
      settings: { ...settings, l2_fallback_models: [...settings.l2_fallback_models] },
      reason: 'keep global reviewer disabled for canary',
      actor: 'operator@example.com',
      created_at: '2026-07-21T17:00:00Z',
      checksum: 'a'.repeat(64),
    },
  ],
  history: [],
  known_instances: ['ditto-screener-prod'],
  applied_instances: [
    {
      instance_id: 'ditto-screener-prod',
      revision: 7,
      scope: '*',
      mode: 'off',
      checksum: 'a'.repeat(64),
      source: 'platform',
      seen_at: '2026-07-21T17:01:00Z',
      fresh: true,
      matches_effective: true,
      expected_revision: 7,
      expected_scope: '*',
      expected_checksum: 'a'.repeat(64),
    },
  ],
  shadow_observations: [],
}

const queueSettings = {
  rescore_cohort_size: 10,
  priority_cohort_size: 5,
  lane_cycle_size: 4,
  fresh_submission_slots: [0, 1, 3],
  owner_concurrent_submission_limit: 2,
  similarity_budget: {
    enabled: true,
    concurrent_submission_limit: 1,
    jaccard_threshold: 0.9,
    containment_threshold: 0.95,
  },
  deferred_source_review: {
    mode: 'off' as const,
    min_cohort_size: 8,
    composite_mad_multiplier: 6,
    axis_mad_multiplier: 6,
    min_composite_delta: 0.1,
    min_axis_delta: 0.15,
  },
  prev_gen_carryover: {
    enabled: false,
    max_agents: 10,
    min_score_count: 2,
    include_exhausted: false,
    dedupe_scope: 'coldkey' as const,
    require_cohort_complete: true,
    require_desired_era_drained: true,
  },
}

const queuePolicy: QueuePolicySettingsControl = {
  current: [],
  history: [],
  default: queueSettings,
  effective: {
    revision: 0,
    scope: '*',
    settings: queueSettings,
    checksum: '',
    source: 'default',
    open_rollout_desired_version: null,
    open_rollout_rescore_cohort_target: null,
    open_rollout_priority_cohort_target: null,
    open_rollout_overrides_setting: false,
    rollout_locked_fields: [],
    rollout_is_open: false,
    min_cohort_size: 5,
    max_cohort_size: 25,
  },
}

describe('ScreenerReviewControlPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getControl.mockReset().mockResolvedValue(state)
    updateSettings.mockReset().mockResolvedValue(undefined)
    getQueuePolicy.mockReset().mockResolvedValue(queuePolicy)
    updateQueuePolicy.mockReset().mockResolvedValue(queuePolicy)
  })

  it('creates an exact-instance shadow revision without changing the global mode', async () => {
    render(<ScreenerReviewControlPanel initialState={state} initialQueuePolicy={queuePolicy} readOnly={false} />)

    fireEvent.change(screen.getByLabelText('Scope'), {
      target: { value: 'ditto-screener-prod' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'shadow' }))
    fireEvent.change(screen.getAllByLabelText('Audit reason')[1]!, {
      target: { value: 'bounded one-worker reviewer canary' },
    })
    const confirmation = 'APPLY SCREENER REVIEW ditto-screener-prod SHADOW'
    fireEvent.change(screen.getAllByLabelText(/^Type to confirm/)[1]!, {
      target: { value: confirmation },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Append settings revision' }))

    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(1))
    expect(updateSettings).toHaveBeenCalledWith({
      data: expect.objectContaining({
        scope: 'ditto-screener-prod',
        expectedRevision: 0,
        reason: 'bounded one-worker reviewer canary',
        confirmation,
        settings: expect.objectContaining({ mode: 'shadow' }),
      }),
    })
  })

  it('keeps mutation disabled for read-only operators', () => {
    render(<ScreenerReviewControlPanel initialState={state} initialQueuePolicy={queuePolicy} readOnly />)

    expect(
      (screen.getByRole('button', { name: 'Append settings revision' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
  })

  it('allows enforce and exposes exact-worker inheritance', () => {
    render(<ScreenerReviewControlPanel initialState={state} initialQueuePolicy={queuePolicy} readOnly={false} />)

    const reviewerModes = within(screen.getByLabelText('Agentic review mode'))
    expect((reviewerModes.getByRole('button', { name: 'enforce' }) as HTMLButtonElement).disabled)
      .toBe(false)
    expect(screen.queryByRole('button', { name: 'inherit' })).toBeNull()
    fireEvent.change(screen.getByLabelText('Scope'), {
      target: { value: 'ditto-screener-prod' },
    })
    expect(screen.getByRole('button', { name: 'inherit' })).toBeTruthy()
  })

  it('disables L3 independently without turning off L2 review', async () => {
    render(<ScreenerReviewControlPanel initialState={state} initialQueuePolicy={queuePolicy} readOnly={false} />)

    fireEvent.click(screen.getByRole('switch', { name: 'Enable L3 verification' }))
    expect(screen.getByText(/L2 analyst becomes the final paid reviewer/)).toBeTruthy()
    fireEvent.change(screen.getAllByLabelText('Audit reason')[1]!, {
      target: { value: 'keep L2 review active while disabling costly L3 calls' },
    })
    fireEvent.change(screen.getAllByLabelText(/^Type to confirm/)[1]!, {
      target: { value: 'APPLY SCREENER REVIEW * OFF' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Append settings revision' }))

    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(1))
    expect(updateSettings).toHaveBeenCalledWith({
      data: expect.objectContaining({
        settings: expect.objectContaining({ mode: 'off', l3_enabled: false }),
      }),
    })
  })

  it('enforces deferred review without exposing a top-five disable switch', async () => {
    render(<ScreenerReviewControlPanel initialState={state} initialQueuePolicy={queuePolicy} readOnly={false} />)

    const modes = within(screen.getByLabelText('Deferred review mode'))
    fireEvent.click(modes.getByRole('button', { name: 'enforce' }))
    fireEvent.change(screen.getAllByLabelText('Audit reason')[0]!, {
      target: { value: 'prescore first and deep-review only qualified submissions' },
    })
    fireEvent.change(screen.getAllByLabelText(/^Type to confirm/)[0]!, {
      target: { value: 'APPLY QUEUE POLICY SETTINGS' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Append queue-policy revision' }))

    await waitFor(() => expect(updateQueuePolicy).toHaveBeenCalledTimes(1))
    expect(updateQueuePolicy).toHaveBeenCalledWith({
      data: {
        expectedRevision: 0,
        confirmation: 'APPLY QUEUE POLICY SETTINGS',
        reason: 'prescore first and deep-review only qualified submissions',
        settings: {
          ...queueSettings,
          deferred_source_review: {
            ...queueSettings.deferred_source_review,
            mode: 'enforce',
          },
        },
      },
    })
    expect(screen.getByText('Top-five integrity rail')).toBeTruthy()
    expect(screen.queryByLabelText(/disable top-five/i)).toBeNull()
  })

  it('describes observe as legacy pre-score review plus would-trigger audit only', () => {
    render(<ScreenerReviewControlPanel initialState={state} initialQueuePolicy={queuePolicy} readOnly={false} />)

    const modes = within(screen.getByLabelText('Deferred review mode'))
    fireEvent.click(modes.getByRole('button', { name: 'observe' }))

    expect(
      screen.getByText(/Full source review still runs before scoring while the platform records/),
    ).toBeTruthy()
    expect(screen.queryByText(/Build and prescore normally/)).toBeNull()
  })

  it('blocks an out-of-range anomaly policy before the request', () => {
    render(<ScreenerReviewControlPanel initialState={state} initialQueuePolicy={queuePolicy} readOnly={false} />)

    fireEvent.change(screen.getByLabelText('Minimum scored cohort'), {
      target: { value: '4' },
    })
    fireEvent.change(screen.getAllByLabelText('Audit reason')[0]!, {
      target: { value: 'invalid client-side range check' },
    })
    fireEvent.change(screen.getAllByLabelText(/^Type to confirm/)[0]!, {
      target: { value: 'APPLY QUEUE POLICY SETTINGS' },
    })

    expect(screen.getByRole('alert').textContent).toContain('whole-number cohort from 5 to 100')
    expect(
      (screen.getByRole('button', { name: 'Append queue-policy revision' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
    expect(updateQueuePolicy).not.toHaveBeenCalled()
  })
})
