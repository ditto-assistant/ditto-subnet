// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { InferenceRoute } from '../lib/admin.schemas'
import { InferenceRoutingPanel } from './InferenceRoutingPanel'

const listInferenceRoutes = vi.fn()
const calibrateInferenceRoute = vi.fn()
const updateInferenceRoutingPolicy = vi.fn()

vi.mock('@tanstack/react-start', () => ({
  useServerFn: (value: unknown) => value,
}))
vi.mock('../server/admin.functions', () => ({
  listInferenceRoutes: () => listInferenceRoutes(),
  calibrateInferenceRoute: (input: unknown) => calibrateInferenceRoute(input),
  updateInferenceRoutingPolicy: (input: unknown) => updateInferenceRoutingPolicy(input),
}))

const route: InferenceRoute = {
  model: 'openai/gpt-oss-20b',
  provider: 'Weights & Biases',
  profile_revision: 'oss-wandb-fp8-v1',
  quantization: 'fp8',
  status: 'healthy',
  calibration_status: 'shadow',
  calibration_revision: 2,
  calibration_manifest_sha256: null,
  calibration_sample_count: 0,
  calibration_tool_accuracy: null,
  calibration_composite: null,
  sample_count: 31,
  selected_ticket_count: 4,
  exploration_ticket_count: 1,
  last_selected_at: null,
  ewma_tokens_per_second: 161.4,
  ewma_latency_ms: 260,
  ewma_error_rate: 0.01,
  ewma_timeout_rate: 0.02,
  prompt_price_per_token: 0.00000003,
  completion_price_per_token: 0.00000013,
  updated_at: '2026-07-22T00:00:00Z',
}

const policy = {
  model: route.model,
  revision: 3,
  enabled: false,
  speed_weight: 0.5,
  cost_weight: 0.4,
  exploration_weight: 0.1,
  exploration_ticket_budget: 5,
  min_tool_accuracy: 0.8,
  min_composite: 0.7,
  min_calibration_samples: 60,
  max_error_rate: 0.05,
  max_timeout_rate: 0.03,
  cooldown_seconds: 300,
  ewma_alpha: 0.2,
  updated_at: '2026-07-22T00:00:00Z',
}

const inventory = {
  routing_mode: 'adaptive' as const,
  aggregate_route: null,
  policies: [policy],
  routes: [route],
  audits: [
    {
      audit_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      actor: 'operator@omniaura.ai',
      action: 'policy_updated' as const,
      model: route.model,
      profile_revision: null,
      payload: { speed_weight: 0.5, prompt: 'must-not-render' },
      recorded_at: '2026-07-22T00:05:00Z',
    },
  ],
  provider_telemetry: [
    {
      provider: 'Groq',
      request_count: 12,
      completed_count: 11,
      failed_count: 1,
      inflight_count: 0,
      timeout_count: 1,
      upstream_attempt_count: 14,
      openrouter_attempt_count: 13,
      recovered_after_fallback_count: 2,
      terminal_failure_count: 1,
      prompt_tokens: 125_000,
      completion_tokens: 8_000,
      cost_microusd: 250_000,
      average_latency_ms: 210,
      observed_output_tps: 38.1,
    },
  ],
  relay_recovery_telemetry: {
    benchmark_relay_abort_ticket_count: 3,
    broker_recovery_exhausted_ticket_count: 1,
  },
}

describe('InferenceRoutingPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    listInferenceRoutes.mockReset().mockResolvedValue(inventory)
    calibrateInferenceRoute.mockReset().mockResolvedValue({
      routing_mode: 'adaptive',
      aggregate_route: null,
      policies: [policy],
      routes: [
        {
          ...route,
          calibration_status: 'eligible',
          calibration_revision: 3,
          calibration_manifest_sha256: 'ab'.repeat(32),
          calibration_sample_count: 60,
          calibration_tool_accuracy: 0.91,
          calibration_composite: 0.84,
        },
      ],
      audits: inventory.audits,
      provider_telemetry: inventory.provider_telemetry,
      relay_recovery_telemetry: inventory.relay_recovery_telemetry,
    })
    updateInferenceRoutingPolicy.mockReset().mockResolvedValue({
      routing_mode: 'adaptive',
      aggregate_route: null,
      policies: [{ ...policy, revision: 4, enabled: true, speed_weight: 0.7, cost_weight: 0.3 }],
      routes: [route],
      audits: inventory.audits,
      provider_telemetry: inventory.provider_telemetry,
      relay_recovery_telemetry: inventory.relay_recovery_telemetry,
    })
  })

  it('shows identity, aggregate quality, performance, reliability, cost, and calibration state', () => {
    render(<InferenceRoutingPanel initialInventory={inventory} readOnly={false} />)

    expect(screen.getByText('Weights & Biases')).toBeTruthy()
    expect(screen.getAllByText('openai/gpt-oss-20b')).toHaveLength(2)
    expect(screen.getByText('161.4 tok/s')).toBeTruthy()
    expect(screen.getByText('Error 1.0%')).toBeTruthy()
    expect(screen.getByText('Timeout 2.0%')).toBeTruthy()
    expect(screen.getByText('Input $0.030')).toBeTruthy()
    expect(screen.getByText('No calibration manifest')).toBeTruthy()
    expect(screen.getByText(/operator@omniaura.ai/).textContent).toContain('UTC')
    expect(screen.queryByText('must-not-render')).toBeNull()
    expect(screen.queryByText('Actual upstream providers')).toBeNull()
    expect(screen.queryByText('Benchmark relay abort tickets')).toBeNull()
    expect(screen.queryByText('Broker recovery exhausted tickets')).toBeNull()
  })

  it('hides ledger telemetry even when the inventory still carries those fields', () => {
    render(<InferenceRoutingPanel initialInventory={inventory} readOnly={false} />)

    expect(screen.queryByText('Actual upstream providers')).toBeNull()
    expect(screen.queryByText('Total input tokens')).toBeNull()
    expect(screen.queryByText('Total output tokens')).toBeNull()
    expect(screen.queryByText('125,000')).toBeNull()
    expect(screen.queryByText('Benchmark relay abort tickets')).toBeNull()
  })

  it('requires reviewed metrics and exact profile confirmation before admission', async () => {
    render(<InferenceRoutingPanel initialInventory={inventory} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'eligible' }))
    const submit = screen.getByRole('button', { name: 'Set eligible' })
    expect((submit as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('Calibration manifest SHA-256'), {
      target: { value: 'ab'.repeat(32) },
    })
    fireEvent.change(screen.getByLabelText('Reviewed samples'), {
      target: { value: '60' },
    })
    fireEvent.change(screen.getByLabelText('Tool accuracy'), {
      target: { value: '0.91' },
    })
    fireEvent.change(screen.getByLabelText('Composite'), {
      target: { value: '0.84' },
    })
    const expected = 'ELIGIBLE INFERENCE ROUTE oss-wandb-fp8-v1'
    fireEvent.change(screen.getByLabelText(`Type ${expected} exactly`), {
      target: { value: expected },
    })

    expect((submit as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(submit)

    await waitFor(() => expect(calibrateInferenceRoute).toHaveBeenCalledTimes(1))
    expect(calibrateInferenceRoute).toHaveBeenCalledWith({
      data: {
        profileRevision: route.profile_revision,
        model: route.model,
        provider: route.provider,
        expectedRevision: route.calibration_revision,
        action: 'eligible',
        manifestSha256: 'ab'.repeat(32),
        toolAccuracy: 0.91,
        composite: 0.84,
        sampleCount: 60,
        confirmation: expected,
      },
    })
    expect(await screen.findByText('60 reviewed samples')).toBeTruthy()
  })

  it('keeps every admission action disabled for read-only operators', () => {
    render(<InferenceRoutingPanel initialInventory={inventory} readOnly />)

    expect(screen.getByText(/read-only access/)).toBeTruthy()
    for (const action of ['eligible', 'shadow', 'disabled']) {
      expect((screen.getByRole('button', { name: action }) as HTMLButtonElement).disabled).toBe(
        true,
      )
    }
  })

  it('locks adaptive controls in aggregate mode except for the logical aggregate route', () => {
    const aggregateRoute: InferenceRoute = {
      ...route,
      provider: 'openrouter',
      profile_revision: 'openrouter-route-newly-discovered-v2',
    }
    render(
      <InferenceRoutingPanel
        initialInventory={{
          ...inventory,
          routing_mode: 'aggregate_throughput',
          aggregate_route: {
            model: aggregateRoute.model,
            provider: aggregateRoute.provider,
            profile_revision: aggregateRoute.profile_revision,
            provider_sort: 'throughput',
            provider_order: [],
            reliability_provider_order: ['DeepInfra', 'Groq'],
            ignored_providers: ['CoreWeave'],
            allow_fallbacks: false,
          },
          routes: [route, aggregateRoute],
        }}
        readOnly={false}
      />,
    )

    expect(screen.getByText('Throughput-first aggregate route')).toBeTruthy()
    expect(
      screen.getByText(
        /Primary: fastest throughput · recovery: disabled · excluded: CoreWeave · fallback disabled/,
      ),
    ).toBeTruthy()
    expect(screen.queryByText('Actual upstream providers')).toBeNull()
    const policyAction = screen.getByRole('button', { name: 'Locked in aggregate mode' })
    expect((policyAction as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('Individual admission locked')).toBeTruthy()

    for (const action of ['eligible', 'shadow', 'disabled']) {
      const buttons = screen.getAllByRole('button', { name: action }) as HTMLButtonElement[]
      expect(buttons).toHaveLength(2)
      expect(buttons[0].disabled).toBe(true)
      expect(buttons[1].disabled).toBe(false)
    }
  })

  it('fails closed when aggregate identity is absent or only the provider matches', () => {
    const providerMatch: InferenceRoute = {
      ...route,
      provider: 'openrouter',
      profile_revision: 'unreviewed-profile',
    }
    render(
      <InferenceRoutingPanel
        initialInventory={{
          ...inventory,
          routing_mode: 'aggregate_throughput',
          routes: [providerMatch],
        }}
        readOnly={false}
      />,
    )

    for (const action of ['eligible', 'shadow', 'disabled']) {
      expect((screen.getByRole('button', { name: action }) as HTMLButtonElement).disabled).toBe(
        true,
      )
    }
  })

  it('guards the complete per-model routing policy with exact confirmation', async () => {
    render(<InferenceRoutingPanel initialInventory={inventory} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Review policy' }))
    fireEvent.change(screen.getByLabelText('Speed weight'), {
      target: { value: '0.7' },
    })
    fireEvent.change(screen.getByLabelText('Cost weight'), {
      target: { value: '0.3' },
    })
    fireEvent.change(screen.getByLabelText('Exploration weight'), {
      target: { value: '0' },
    })
    const submit = screen.getByRole('button', {
      name: 'Update routing policy',
    })
    expect((submit as HTMLButtonElement).disabled).toBe(true)

    const expected = 'UPDATE INFERENCE POLICY openai/gpt-oss-20b'
    fireEvent.change(screen.getByLabelText(`Type ${expected} exactly`), {
      target: { value: expected },
    })
    fireEvent.click(submit)

    await waitFor(() => expect(updateInferenceRoutingPolicy).toHaveBeenCalledTimes(1))
    expect(updateInferenceRoutingPolicy).toHaveBeenCalledWith({
      data: expect.objectContaining({
        model: route.model,
        expectedRevision: policy.revision,
        speedWeight: 0.7,
        costWeight: 0.3,
        explorationWeight: 0,
        confirmation: expected,
      }),
    })
  })
})
