// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SubnetLiveness, SubnetLivenessSignal } from '../lib/admin.schemas'
import { SubnetLivenessPanel, formatLivenessValue } from './SubnetLivenessPanel'

const getSubnetLiveness = vi.fn()

vi.mock('@tanstack/react-start', () => ({
  useServerFn: (serverFn: unknown) => serverFn,
}))

vi.mock('../server/admin.functions', () => ({
  getSubnetLiveness: (...args: unknown[]) => getSubnetLiveness(...args),
}))

afterEach(() => {
  cleanup()
  getSubnetLiveness.mockReset()
})

function signal(overrides: Partial<SubnetLivenessSignal> = {}): SubnetLivenessSignal {
  return {
    name: 'screening_admission',
    status: 'ok',
    value: 0,
    unit: 'seconds',
    warn_threshold: 300,
    threshold: 900,
    since: null,
    hint: 'Check node_controls in get_screener_capacity.',
    detail: {},
    ...overrides,
  }
}

function view(overrides: Partial<SubnetLiveness> = {}): SubnetLiveness {
  return {
    generated_at: '2026-10-01T00:00:00Z',
    environment: 'prod',
    status: 'breach',
    signals: [
      signal({ status: 'breach', value: 7200, since: '2026-09-30T22:00:00Z' }),
      signal({
        name: 'v13_scorer_cohort_pin',
        unit: 'members',
        value: null,
        warn_threshold: null,
        threshold: 1,
        hint: 'No v13 scorer pin governs dispatch right now.',
      }),
    ],
    unavailable: [{ name: 'disk_and_db_headroom', reason: 'host metrics' }],
    ...overrides,
  }
}

describe('SubnetLivenessPanel', () => {
  it('shows each signal with its status, value, threshold and breach hint', () => {
    render(<SubnetLivenessPanel initialState={{ ok: true, view: view() }} />)

    expect(screen.getByText('Screening admission at 0')).toBeTruthy()
    expect(screen.getByText('2.0 h')).toBeTruthy()
    expect(screen.getAllByText('breach').length).toBeGreaterThanOrEqual(2)
    // A breached signal shows its operator hint inline; an ok one does not.
    expect(screen.getByText('Check node_controls in get_screener_capacity.')).toBeTruthy()
    expect(screen.queryByText('No v13 scorer pin governs dispatch right now.')).toBeNull()
    expect(screen.getByText('n/a')).toBeTruthy()
    expect(screen.getByText(/disk_and_db_headroom/)).toBeTruthy()
  })

  it('reports a failed read instead of hiding the panel, then refreshes', async () => {
    getSubnetLiveness.mockResolvedValueOnce({ ok: true, view: view({ status: 'ok', signals: [signal()] }) })
    render(<SubnetLivenessPanel initialState={{ ok: false, status: 404, message: 'Not Found' }} />)

    expect(screen.getByRole('alert').textContent).toContain('/api/v1/admin/subnet-liveness')
    fireEvent.click(screen.getByRole('button', { name: /refresh liveness/i }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(getSubnetLiveness).toHaveBeenCalledTimes(1)
    expect(screen.getAllByText('ok').length).toBeGreaterThanOrEqual(1)
  })

  it('formats durations and member counts', () => {
    expect(formatLivenessValue(null, 'seconds')).toBe('n/a')
    expect(formatLivenessValue(45, 'seconds')).toBe('45 s')
    expect(formatLivenessValue(900, 'seconds')).toBe('15 min')
    expect(formatLivenessValue(259_200, 'seconds')).toBe('3.0 d')
    // Never round up onto a threshold: 899.6 s is under 15 min, 14,399 s under 4 h.
    expect(formatLivenessValue(899.6, 'seconds')).toBe('14 min')
    expect(formatLivenessValue(14_399, 'seconds')).toBe('3.9 h')
    expect(formatLivenessValue(59.6, 'seconds')).toBe('59 s')
    expect(formatLivenessValue(1, 'members')).toBe('1 member')
    expect(formatLivenessValue(3, 'members')).toBe('3 members')
  })
})
