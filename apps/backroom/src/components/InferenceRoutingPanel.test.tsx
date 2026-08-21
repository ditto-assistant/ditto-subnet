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
  gateway_provider_order: ['openrouter'] as Array<'instant' | 'openrouter'>,
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
  gateway_providers: [
    { provider: 'instant' as const, configured: true },
    { provider: 'openrouter' as const, configured: true },
  ],
  policies: [policy],
  routes: [route],
  audits: [
    {
      audit_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      actor: 'operator@omniaura.ai',
      action: 'policy_updated' as const,
      model: route.model,
      profile_revision: null,
      payload: {
        gateway_provider_order: ['instant', 'openrouter'],
        speed_weight: 0.5,
        prompt: 'must-not-render',
      },
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
      cost_available: true,
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
      gateway_providers: inventory.gateway_providers,
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
      gateway_providers: inventory.gateway_providers,
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
    expect(screen.getByText('instant → openrouter')).toBeTruthy()
    expect(screen.getByText('Actual upstream providers')).toBeTruthy()
    expect(screen.getByText('Groq')).toBeTruthy()
    expect(screen.getByText('Total input tokens')).toBeTruthy()
    expect(screen.getAllByText('125,000')).toHaveLength(2)
    expect(screen.getByText('Total output tokens')).toBeTruthy()
    expect(screen.getAllByText('8,000')).toHaveLength(2)
    expect(screen.getByText('$0.2500')).toBeTruthy()
    expect(screen.getByText('38.1 tok/s')).toBeTruthy()
    expect(screen.getByText('OpenRouter attempts')).toBeTruthy()
    expect(screen.getByText('Recovered')).toBeTruthy()
    expect(screen.getByText('Benchmark relay abort tickets')).toBeTruthy()
    expect(screen.getByText('Broker recovery exhausted tickets')).toBeTruthy()
  })

  it('totals provider tokens and sorts independently by input or output usage', () => {
    render(
      <InferenceRoutingPanel
        initialInventory={{
          ...inventory,
          provider_telemetry: [
            ...inventory.provider_telemetry,
            {
              provider: 'Fireworks',
              request_count: 4,
              completed_count: 4,
              failed_count: 0,
              inflight_count: 0,
              timeout_count: 0,
              upstream_attempt_count: 4,
              openrouter_attempt_count: 4,
              recovered_after_fallback_count: 0,
              terminal_failure_count: 0,
              prompt_tokens: 25_000,
              completion_tokens: 12_000,
              cost_microusd: 75_000,
              cost_available: true,
              average_latency_ms: 340,
              observed_output_tps: 35.3,
            },
          ],
        }}
        readOnly={false}
      />,
    )

    expect(screen.getByText('150,000')).toBeTruthy()
    expect(screen.getByText('20,000')).toBeTruthy()

    const providers = () =>
      Array.from(screen.getByText('Groq').closest('tbody')!.querySelectorAll('tr')).map(
        (row) => row.firstElementChild?.textContent,
      )

    expect(providers()).toEqual(['Groq', 'Fireworks'])
    fireEvent.click(screen.getByRole('button', { name: 'Sort by output tokens descending' }))
    expect(providers()).toEqual(['Fireworks', 'Groq'])
    fireEvent.click(screen.getByRole('button', { name: 'Sort by output tokens ascending' }))
    expect(providers()).toEqual(['Groq', 'Fireworks'])
  })

  it('does not present an unpriced Instant completion as free', () => {
    render(
      <InferenceRoutingPanel
        initialInventory={{
          ...inventory,
          provider_telemetry: [
            {
              ...inventory.provider_telemetry[0],
              provider: 'instant',
              cost_microusd: 0,
              cost_available: false,
            },
          ],
        }}
        readOnly={false}
      />,
    )

    expect(screen.getByText('Unavailable')).toBeTruthy()
    expect(screen.queryByText('$0.0000')).toBeNull()
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

  it('keeps gateway priority editable while locking adaptive controls in aggregate mode', () => {
    const aggregateRoute: InferenceRoute = {
      ...route,
      provider: 'provider-list',
      profile_revision: 'provider-list-route-newly-discovered-v1',
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
            allow_fallbacks: true,
          },
          routes: [route, aggregateRoute],
        }}
        readOnly={false}
      />,
    )

    expect(screen.getByText('Throughput-first aggregate route')).toBeTruthy()
    expect(
      screen.getByText(
        /Primary: fastest throughput · recovery: DeepInfra → Groq · excluded: CoreWeave · fallback enabled/,
      ),
    ).toBeTruthy()
    expect(screen.getByText('Actual upstream providers')).toBeTruthy()
    expect(screen.getByText('Groq')).toBeTruthy()
    const policyAction = screen.getByRole('button', { name: 'Review policy' })
    expect((policyAction as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(policyAction)
    expect((screen.getByLabelText('Speed weight') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Add Instant fallback' }) as HTMLButtonElement).disabled).toBe(false)
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
        gatewayProviderOrder: ['openrouter'],
        speedWeight: 0.7,
        costWeight: 0.3,
        explorationWeight: 0,
        confirmation: expected,
      }),
    })
  })

  it('reorders configured gateways and submits the exact fallback priority', async () => {
    render(<InferenceRoutingPanel initialInventory={inventory} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Review policy' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add Instant fallback' }))
    fireEvent.click(screen.getByRole('button', { name: 'Move instant earlier' }))

    const expected = 'UPDATE INFERENCE POLICY openai/gpt-oss-20b'
    fireEvent.change(screen.getByLabelText(`Type ${expected} exactly`), {
      target: { value: expected },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Update routing policy' }))

    await waitFor(() => expect(updateInferenceRoutingPolicy).toHaveBeenCalledTimes(1))
    expect(updateInferenceRoutingPolicy).toHaveBeenCalledWith({
      data: expect.objectContaining({
        gatewayProviderOrder: ['instant', 'openrouter'],
      }),
    })
  })

  it('explains why an unconfigured gateway cannot be added', () => {
    render(
      <InferenceRoutingPanel
        initialInventory={{
          ...inventory,
          gateway_providers: inventory.gateway_providers.map((provider) =>
            provider.provider === 'instant' ? { ...provider, configured: false } : provider,
          ),
        }}
        readOnly={false}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Review policy' }))
    expect(
      screen.getByText('Unavailable until its credential is configured: Instant.'),
    ).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Add Instant fallback' })).toBeNull()
  })
})
