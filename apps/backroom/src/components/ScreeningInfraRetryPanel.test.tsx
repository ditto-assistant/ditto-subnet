// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ScreeningInfraRetryView } from '../lib/admin.schemas'
import { ScreeningInfraRetryPanel } from './ScreeningInfraRetryPanel'

const getScreeningInfraRetries = vi.fn()

vi.mock('@tanstack/react-start', () => ({
  useServerFn: (serverFn: unknown) => serverFn,
}))

vi.mock('../server/admin.functions', () => ({
  getScreeningInfraRetries: (...args: unknown[]) => getScreeningInfraRetries(...args),
}))

afterEach(() => {
  cleanup()
  getScreeningInfraRetries.mockReset()
})

function view(overrides: Partial<ScreeningInfraRetryView> = {}): ScreeningInfraRetryView {
  return {
    generated_at: '2026-09-21T12:00:00Z',
    basis: 'Derived from screening attempt history at read time.',
    policy: {
      auto_retry_reason_codes: ['docker-build-infrastructure'],
      base_backoff_seconds: 600, max_backoff_seconds: 3600, jitter_fraction: 0.2,
      auto_retry_max_age_seconds: 86400, auto_retry_max_streak: 8, plan_max_claimable: 500,
      breaker_distinct_agents: 3, breaker_window_seconds: 300, breaker_open_seconds: 600,
      breaker_probe_interval_seconds: 300, breaker_history_lookback_seconds: 172800,
    },
    summary: {
      parked_agents: 3,
      by_state: { backoff: 0, breaker_held: 1, probe_due: 0, due: 0, capped: 1 },
      not_admitted: 1, aged_out_agents: 4, open_breakers: 1, half_open_breakers: 1,
      breakers_total: 2,
    },
    agents: [
      {
        agent_id: '11111111-1111-4111-8111-111111111111',
        attempt_id: '22222222-2222-4222-8222-222222222222',
        reason_code: 'docker-build-infrastructure', provider: 'gcp', lane: 'buildkit',
        consecutive_failures: 2, failed_at: '2026-09-21T11:50:00Z',
        backoff_until: '2026-09-21T12:05:00Z', next_retry_at: '2026-09-21T12:10:00Z',
        state: 'breaker_held', breaker_phase: 'open', admitted: true, claim_outlook: 'waiting_breaker',
      },
      {
        agent_id: '33333333-3333-4333-8333-333333333333',
        attempt_id: '44444444-4444-4444-8444-444444444444',
        reason_code: 'docker-build-infrastructure', provider: null, lane: null,
        consecutive_failures: 8, failed_at: '2026-09-21T09:00:00Z',
        backoff_until: '2026-09-21T10:00:00Z', next_retry_at: '2026-09-21T10:00:00Z',
        state: 'capped', breaker_phase: null, admitted: true, claim_outlook: 'needs_operator',
      },
    ],
    agents_limit: 200,
    agents_truncated: true,
    breakers: [{
      reason_code: 'docker-build-infrastructure', provider: 'gcp', lane: 'buildkit',
      phase: 'open', opened_at: '2026-09-21T11:55:00Z', open_until: '2026-09-21T12:05:00Z',
      last_probe_at: null, next_probe_at: '2026-09-21T12:10:00Z', parked_agents: 1,
    }, {
      reason_code: 'docker-build-infrastructure', provider: 'gcp', lane: 'old-lane',
      phase: 'half_open', opened_at: '2026-09-21T08:00:00Z', open_until: '2026-09-21T08:10:00Z',
      last_probe_at: null, next_probe_at: '2026-09-21T08:10:00Z', parked_agents: 0,
    }],
    breakers_limit: 50,
    breakers_truncated: false,
    ...overrides,
  }
}

const ok = (overrides: Partial<ScreeningInfraRetryView> = {}) => ({
  ok: true as const,
  view: view(overrides),
})

describe('ScreeningInfraRetryPanel', () => {
  it('shows the policy, breaker, parked agents, and the truncation notice', () => {
    render(<ScreeningInfraRetryPanel initialState={ok()} />)

    expect(screen.getByText(/nothing is stored/i)).toBeTruthy()
    expect(screen.getByText(/wait for an operator retry/i)).toBeTruthy()
    expect(screen.getByText(/10 min doubling to 1 h, ±20% jitter/)).toBeTruthy()
    expect(screen.getByText(/3 distinct agents within 5 min/)).toBeTruthy()
    expect(screen.getByText('Open: holding retries')).toBeTruthy()
    expect(screen.getByText('Half-open: probing')).toBeTruthy()
    expect(screen.getByText(/4 aged out/)).toBeTruthy()
    expect(screen.getByText(/1 open and\s+1 half-open of 2 breakers/)).toBeTruthy()
    expect(screen.getByText('Held by breaker', { selector: 'span' })).toBeTruthy()
    expect(screen.getByText('Needs an operator retry')).toBeTruthy()
    expect(screen.getByText('Waiting for breaker')).toBeTruthy()
    expect(screen.queryByText(/longest/)).toBeNull()
    expect(screen.getByText('unknown provider / unknown lane')).toBeTruthy()
    expect(screen.getByText(/Showing the 2 agents with the earliest next retry, of\s+3 parked/)).toBeTruthy()
  })

  it('shows the real failure and its HTTP status, not a generic message', () => {
    render(<ScreeningInfraRetryPanel initialState={{ ok: false, status: 500, message: 'boom from platform' }} />)
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toContain('boom from platform')
    expect(text).toContain('HTTP 500')
  })

  it('names a missing endpoint distinctly (older Platform build)', () => {
    render(<ScreeningInfraRetryPanel initialState={{ ok: false, status: 404, message: 'Not Found' }} />)
    expect(screen.getByRole('alert').textContent).toContain('does not expose')
  })

  it('shows a refresh failure without dropping the last good view', async () => {
    getScreeningInfraRetries.mockResolvedValue({ ok: false, status: 502, message: 'bad gateway' })
    render(<ScreeningInfraRetryPanel initialState={ok()} />)
    fireEvent.click(screen.getByRole('button', { name: /refresh retries/i }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('bad gateway'))
    expect(screen.getByText('Needs an operator retry')).toBeTruthy()
  })

  it('refreshes from the server function', async () => {
    getScreeningInfraRetries.mockResolvedValue(
      ok({ agents: [], agents_truncated: false, summary: { ...view().summary, parked_agents: 0 } }),
    )
    render(<ScreeningInfraRetryPanel initialState={ok()} />)

    fireEvent.click(screen.getByRole('button', { name: /refresh retries/i }))

    await waitFor(() =>
      expect(screen.getByText(/No agent is parked on an infrastructure failure/)).toBeTruthy(),
    )
  })
})
