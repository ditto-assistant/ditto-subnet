// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
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
  earliest_retry_after: null,
  attempts_used: 12,
  exhausted_validator_count: 2,
  silent_expiry_count: 0,
  snapshot: 'ab'.repeat(32),
  tickets: [],
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
  blocking_reason: 'submission left the scoreable queue',
  snapshot: 'ef'.repeat(32),
}

function response(submissions = [first, second, blocked]) {
  return {
    generated_at: '2026-08-11T20:00:00Z',
    quorum: 3,
    counts: { exhausted: submissions.length },
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
    expect((screen.getByLabelText('Select blocked-agent') as HTMLInputElement).disabled).toBe(true)
  })

  it('refreshes only the exhausted summary lane', async () => {
    listStuckSubmissions.mockResolvedValue(response([first]))
    render(<StuckSubmissionFleetPanel initial={response()} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(listStuckSubmissions).toHaveBeenCalledWith({
      data: { state: ['exhausted'], detail: 'summary' },
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

  it('shows an honest empty state when no exhausted work remains', () => {
    render(<StuckSubmissionFleetPanel initial={response([])} readOnly={false} />)

    expect(screen.getByText('No exhausted validator assignments.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Retry .* selected/ })).toBeNull()
  })
})
