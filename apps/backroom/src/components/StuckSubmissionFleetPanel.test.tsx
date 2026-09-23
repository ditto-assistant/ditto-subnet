// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { StuckSubmissionsList } from '../lib/admin.schemas'
import { StuckSubmissionFleetPanel } from './StuckSubmissionFleetPanel'

const listStuckSubmissions = vi.fn()
const batchRetryStuckSubmissions = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  listStuckSubmissions: (input: unknown) => listStuckSubmissions(input),
  batchRetryStuckSubmissions: (input: unknown) => batchRetryStuckSubmissions(input),
}))

const first = {
  agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
  miner_hotkey: '5MinerOne',
  agent_name: 'first-agent',
  agent_version: 2,
  bench_version: 9,
  score_count: 1,
  quorum: 3,
  retry_state: 'exhausted' as const,
  automatic_retry_available: false,
  recovery_allowed: true,
  blocking_reason: 'manual retry evidence required',
  recommended_action: 'retry' as const,
  dominant_failure_code: null,
  provider_outage_slot_count: 0,
  earliest_retry_after: null,
  attempts_used: 12,
  exhausted_validator_count: 2,
  silent_expiry_count: 0,
  snapshot: 'ab'.repeat(32),
  ticket_states: {},
}

const second = {
  ...first,
  agent_id: '8c534973-d27b-4bf6-96bd-280442533fef',
  miner_hotkey: '5MinerTwo',
  agent_name: 'second-agent',
  agent_version: null,
  score_count: 0,
  attempts_used: 18,
  exhausted_validator_count: 3,
  snapshot: 'cd'.repeat(32),
}

const blocked = {
  ...first,
  agent_id: 'c4cfa54b-98ec-49cf-bff4-47ea7154ab03',
  agent_name: 'blocked-agent',
  recovery_allowed: false,
  blocking_reason: 'exhausted on agent-attributable failures; withdraw rather than retry',
  recommended_action: 'withdraw' as const,
  dominant_failure_code: 'inference_request_rejected',
  snapshot: 'ef'.repeat(32),
}

const openCircuit = {
  provider: 'openrouter',
  state: 'open' as const,
  epoch: '5e0e6f1c-5f3c-4a7e-9d1a-1f2e3d4c5b6a',
  opened_at: '2026-09-21T22:49:00Z',
  retry_at: '2026-09-21T22:55:00Z',
  last_failure_at: '2026-09-21T22:53:00Z',
  closed_at: null,
  failure_count: 3,
  last_status: 429,
  last_error_code: 'upstream_http_429',
  probe_kind: null,
  probe_key: null,
  probe_expires_at: null,
}

// ditto-subnet#2087: operator authority remains, but the platform withholds
// `retry` because the circuit that parked this slot is still failing.
const waitingOnProvider = {
  ...first,
  agent_id: '9fa5271e-2977-4501-8bd4-c1bc2fa27e82',
  miner_hotkey: '5MinerArtemis',
  agent_name: 'artemis',
  agent_version: 3,
  score_count: 2,
  blocking_reason: null,
  recommended_action: null,
  provider_outage_slot_count: 1,
  snapshot: '12'.repeat(32),
}

// Withheld purely because the circuit is open: park_scoring_leases expires every
// issued lease, so this row is unsafe to grant even though the outage did not
// park its last attempt (provider_outage_slot_count stays 0).
const waitingWhileCircuitOpen = {
  ...first,
  agent_id: 'a9ae4512-832f-4e00-83ea-7430477f2fba',
  agent_name: 'other-agent',
  recommended_action: null,
  provider_outage_slot_count: 0,
  snapshot: '34'.repeat(32),
}

function response(
  submissions: StuckSubmissionsList['submissions'] = [first, second, blocked],
  outage: Pick<StuckSubmissionsList, 'provider_outage_active' | 'provider_circuit'> = {
    provider_outage_active: false,
    provider_circuit: null,
  },
): StuckSubmissionsList {
  return {
    ...outage,
    generated_at: '2026-08-11T20:00:00Z',
    generation: 'active' as const,
    active_bench_version: 12,
    quorum: 3,
    counts: { exhausted: submissions.length },
    count: submissions.length,
    returned: submissions.length,
    limit: 200,
    offset: 0,
    has_more: false,
    submissions,
  }
}

describe('StuckSubmissionFleetPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    listStuckSubmissions.mockReset().mockResolvedValue(response())
    batchRetryStuckSubmissions.mockReset().mockResolvedValue({
      granted: 2,
      results: [
        { agent_id: first.agent_id, status: 'granted', detail: null, recovery: {} },
        { agent_id: second.agent_id, status: 'granted', detail: null, recovery: {} },
      ],
    })
  })

  it('renders the whole exhausted fleet with retry evidence', () => {
    render(<StuckSubmissionFleetPanel initial={response()} readOnly={false} />)

    expect(screen.getByText('first-agent v2')).toBeTruthy()
    expect(screen.getByText('second-agent')).toBeTruthy()
    expect(screen.getByText('blocked-agent v2')).toBeTruthy()
    expect(screen.getAllByText('12')).toHaveLength(2)
    expect(screen.getByText('18')).toBeTruthy()
    expect(screen.getByText('withdraw · inference_request_rejected')).toBeTruthy()
    expect((screen.getByLabelText('Select blocked-agent') as HTMLInputElement).disabled).toBe(true)
  })

  it('refreshes only the exhausted summary lane', async () => {
    listStuckSubmissions.mockResolvedValue(response([first]))
    render(<StuckSubmissionFleetPanel initial={response()} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(listStuckSubmissions).toHaveBeenCalledWith({
      data: { generation: 'active', state: ['exhausted'], limit: 200, offset: 0 },
    }))
    expect(await screen.findByText('first-agent v2')).toBeTruthy()
    expect(screen.queryByText('second-agent')).toBeNull()
  })

  it('batch retries selected snapshots only after explicit evidence confirmation', async () => {
    listStuckSubmissions.mockResolvedValue(response([]))
    render(<StuckSubmissionFleetPanel initial={response()} readOnly={false} />)

    fireEvent.click(screen.getByText('Select all 2 recoverable'))
    fireEvent.change(screen.getByLabelText('Fleet retry audit reason'), {
      target: { value: 'Validator logs confirm infrastructure-owned failures' },
    })
    expect((screen.getByRole('button', { name: 'Retry 2 selected submissions' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByText(/I confirmed validator-owned failure evidence/))
    fireEvent.click(screen.getByRole('button', { name: 'Retry 2 selected submissions' }))

    await waitFor(() => expect(batchRetryStuckSubmissions).toHaveBeenCalledWith({
      data: {
        reason: 'Validator logs confirm infrastructure-owned failures',
        items: [
          { agentId: first.agent_id, expectedSnapshot: first.snapshot },
          { agentId: second.agent_id, expectedSnapshot: second.snapshot },
        ],
      },
    }))
    expect(await screen.findByText(/Granted 2 validator retries/)).toBeTruthy()
  })

  it('keeps all mutation controls absent for read-only operators', () => {
    render(<StuckSubmissionFleetPanel initial={response()} readOnly />)

    expect(screen.queryByText(/Select all/)).toBeNull()
    expect(screen.queryByLabelText('Fleet retry audit reason')).toBeNull()
    expect((screen.getByLabelText('Select first-agent') as HTMLInputElement).disabled).toBe(true)
  })

  it('waits instead of advertising retry while the parking provider outage persists', () => {
    render(
      <StuckSubmissionFleetPanel
        initial={response([first, waitingOnProvider, waitingWhileCircuitOpen], {
          provider_outage_active: true,
          provider_circuit: openCircuit,
        })}
        readOnly={false}
      />,
    )

    // Both the parked row and the one the open circuit merely endangers.
    expect(screen.getAllByText('wait · provider outage')).toHaveLength(2)
    expect(screen.getByRole('status').textContent).toContain('OpenRouter circuit open')
    // One click must not batch a grant into the live outage...
    expect(screen.getByText('Select all 1 recoverable')).toBeTruthy()
    // ...but an operator can still choose it explicitly.
    expect((screen.getByLabelText('Select artemis') as HTMLInputElement).disabled).toBe(false)
  })

  it('shows an honest empty state when no exhausted work remains', () => {
    render(<StuckSubmissionFleetPanel initial={response([])} readOnly={false} />)

    expect(screen.getByText('No exhausted validator assignments.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Retry .* selected/ })).toBeNull()
  })
})
