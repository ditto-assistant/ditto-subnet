// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ScreenerReviewControl } from '../lib/admin.schemas'
import { ScreenerReviewControlPanel } from './ScreenerReviewControlPanel'

const getControl = vi.fn()
const updateSettings = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getScreenerReviewControl: () => getControl(),
  updateScreenerReviewSettings: (input: unknown) => updateSettings(input),
}))

const settings = {
  mode: 'off' as const,
  l2_model: 'openai/gpt-5.6-terra' as const,
  l2_fallback_models: ['z-ai/glm-5.2', 'openai/gpt-5.6-sol'] as const,
  l3_enabled: true,
  l3_model: 'openai/gpt-5.6-sol' as const,
  timeout_seconds: 900,
  max_steps: 18,
  source_review_max_steps: 200,
  source_review_max_read_bytes: 8_000_000,
  source_review_max_completion_tokens: 8_000,
  concern_hold_count: 3,
  clear_min_notes: 3,
  adjudicator_mode: 'off' as const,
  adjudicator_model: 'z-ai/glm-5.3-flash' as const,
  adjudicator_max_steps: 24,
  adjudicator_timeout_seconds: 600,
  fanout_shadow_mode: 'off' as const,
  fanout_shadow_image_source_sha: '0'.repeat(40),
  fanout_shadow_model: 'z-ai/glm-5.3-flash' as const,
  fanout_shadow_concurrency: 2,
  fanout_shadow_max_steps: 4,
  fanout_shadow_max_groups: 4,
  fanout_shadow_max_requests: 40,
  fanout_shadow_max_total_tokens: 1_500_000,
  fanout_shadow_timeout_seconds: 900,
  fanout_shadow_max_cost_usd: 3,
  fanout_shadow_daily_cost_usd: 20,
  fanout_shadow_global_concurrency: 1 as const,
  fanout_shadow_reserved_targon_slots: 1,
  probe_battery_mode: 'off' as const,
  probe_battery_timeout_seconds: 120,
  probe_review_mode: 'off' as const,
  probe_review_timeout_seconds: 420,
  probe_review_max_cost_usd: 1,
  source_review_reasoning_effort: 'high' as const,
  source_review_model: 'openai/gpt-5.6-luna' as const,
  source_review_timeout_seconds: 1_800,
  max_input_tokens: 425_000,
  max_output_tokens: 20_000,
  max_completion_tokens: 2_400,
  adjudicator_max_completion_tokens: null,
  max_cost_usd: 2,
  critic_reasoning_effort: 'medium' as const,
  cache_ttl_seconds: 604_800,
  l2_always_escalate: false,
  audit_retention_days: 30,
  policy_manifest_profile: 'l1' as const,
  policy_manifest_rotation_id: 'v8-luna-source-review-behavioral-oracle',
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
      policy_manifest_profile: 'l1',
      policy_manifest_rotation_id: 'v8-luna-source-review-behavioral-oracle',
      policy_manifest_digest: 'b'.repeat(64),
      expected_policy_manifest_digest: 'b'.repeat(64),
    },
  ],
  shadow_observations: [],
  probe_battery_observations: [],
  policy_manifests: [],
}

describe('ScreenerReviewControlPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getControl.mockReset().mockResolvedValue(state)
    updateSettings.mockReset().mockResolvedValue(undefined)
  })

  it('creates an exact-instance shadow revision without changing the global mode', async () => {
    render(<ScreenerReviewControlPanel initialState={state} readOnly={false} />)

    fireEvent.change(screen.getByLabelText('Scope'), {
      target: { value: 'ditto-screener-prod' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'shadow' }))
    fireEvent.change(screen.getByLabelText('Audit reason'), {
      target: { value: 'bounded one-worker reviewer canary' },
    })
    const confirmation = 'APPLY SCREENER REVIEW ditto-screener-prod SHADOW'
    fireEvent.change(screen.getByLabelText(/^Type to confirm/), {
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
    render(<ScreenerReviewControlPanel initialState={state} readOnly />)

    expect(
      (screen.getByRole('button', { name: 'Append settings revision' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
  })

  it('allows enforce and exposes exact-worker inheritance', () => {
    render(<ScreenerReviewControlPanel initialState={state} readOnly={false} />)

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
    render(<ScreenerReviewControlPanel initialState={state} readOnly={false} />)

    fireEvent.click(screen.getByRole('switch', { name: 'Enable L3 verification' }))
    expect(screen.getByText(/L2 analyst becomes the final paid reviewer/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Audit reason'), {
      target: { value: 'keep L2 review active while disabling costly L3 calls' },
    })
    fireEvent.change(screen.getByLabelText(/^Type to confirm/), {
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

  it('writes an always-escalating posture to the double-check scope before any worker uses it', async () => {
    render(<ScreenerReviewControlPanel initialState={state} readOnly={false} />)

    fireEvent.change(screen.getByLabelText('Scope'), {
      target: { value: 'integrity-double-check' },
    })
    expect(screen.queryByRole('button', { name: 'inherit' })).toBeNull()
    fireEvent.click(
      within(screen.getByLabelText('Agentic review mode')).getByRole('button', { name: 'enforce' }),
    )
    fireEvent.click(screen.getByRole('switch', { name: 'Always escalate to L2/L3' }))
    fireEvent.change(screen.getByLabelText('Audit reason'), {
      target: { value: 'stronger reviewer for top-five double-checks' },
    })
    const confirmation = 'APPLY SCREENER REVIEW integrity-double-check ENFORCE'
    fireEvent.change(screen.getByLabelText(/^Type to confirm/), {
      target: { value: confirmation },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Append settings revision' }))

    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(1))
    expect(updateSettings).toHaveBeenCalledWith({
      data: expect.objectContaining({
        scope: 'integrity-double-check',
        confirmation,
        settings: expect.objectContaining({ mode: 'enforce', l2_always_escalate: true }),
      }),
    })
  })

  it('no longer offers the retired deferred source-review queue policy', () => {
    render(<ScreenerReviewControlPanel initialState={state} readOnly={false} />)

    expect(screen.queryByLabelText('Deferred review mode')).toBeNull()
    expect(screen.queryByLabelText('Integrity double-check mode')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Append queue-policy revision' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'bypass' })).toBeNull()
  })
})
