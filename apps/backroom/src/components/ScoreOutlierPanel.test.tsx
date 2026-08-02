// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ScoreOutlierPanel } from './ScoreOutlierPanel'

const listScoreOutliers = vi.fn()
const requestScoreRetest = vi.fn()
const queueValidatorScoreRetests = vi.fn()
const releaseScoreRetestTicket = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('@tanstack/react-router', () => ({
  Link: ({
    search,
    children,
    ...props
  }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { search?: { page: number } }) => (
    <a {...props} data-page={search?.page}>
      {children}
    </a>
  ),
}))
vi.mock('../server/admin.functions', () => ({
  listScoreOutliers: (input: unknown) => listScoreOutliers(input),
  requestScoreRetest: (input: unknown) => requestScoreRetest(input),
  queueValidatorScoreRetests: (input: unknown) => queueValidatorScoreRetests(input),
  releaseScoreRetestTicket: (input: unknown) => releaseScoreRetestTicket(input),
}))

const item = {
  agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
  agent_name: 'outlying-agent',
  miner_hotkey: '5Miner',
  agent_status: 'scored',
  bench_version: 4,
  snapshot: 'ab'.repeat(32),
  median_composite: 0.82,
  direction: 'low' as const,
  outlier: { validator_hotkey: '5Outlier', run_id: 'run-low', composite: 0.11 },
  peers: [
    { validator_hotkey: '5PeerA', run_id: 'run-a', composite: 0.82 },
    { validator_hotkey: '5PeerB', run_id: 'run-b', composite: 0.84 },
  ],
  deviation: 0.71,
  peer_spread: 0.02,
  ticket_status: 'scored' as const,
  replacement_pending: false,
  replacement_queued: false,
  queue_position: null,
  replacement_deadline: null,
  replacement_allowed: true,
  blocking_reason: null,
  queue_allowed: true,
  queue_blocking_reason: null,
}

describe('ScoreOutlierPanel', () => {
  afterEach(cleanup)
  beforeEach(() => {
    listScoreOutliers.mockReset().mockResolvedValue({ items: [item], count: 1 })
    requestScoreRetest.mockReset().mockResolvedValue({})
    queueValidatorScoreRetests.mockReset().mockResolvedValue({
      validator_hotkey: item.outlier.validator_hotkey,
      activated: 0,
      queued: 2,
      idempotent: 0,
      skipped: 0,
      results: [],
    })
    releaseScoreRetestTicket.mockReset().mockResolvedValue({})
  })

  it('requires a reason and re-tests the exact outlying validator', async () => {
    render(<ScoreOutlierPanel initialItems={[item]} initialCount={1} readOnly={false} />)
    const action = screen.getByRole('button', { name: 'Re-test same validator' })
    expect((action as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByPlaceholderText('Evidence that this validator score should be re-tested'), {
      target: { value: 'Runtime logs show this validator run was unhealthy' },
    })
    expect((action as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(action)
    await waitFor(() => expect(requestScoreRetest).toHaveBeenCalledTimes(1))
    expect(requestScoreRetest.mock.calls[0]?.[0]).toMatchObject({
      data: {
        agentId: item.agent_id,
        validatorHotkey: item.outlier.validator_hotkey,
        expectedSnapshot: item.snapshot,
        expectedRunId: item.outlier.run_id,
      },
    })
  })

  it('releases a pending replacement ticket without changing the score', async () => {
    const pending = {
      ...item,
      ticket_status: 'issued' as const,
      replacement_pending: true,
      replacement_deadline: '2026-07-20T20:00:00Z',
      replacement_allowed: false,
      blocking_reason: 'replacement score is already pending',
    }
    listScoreOutliers.mockResolvedValue({ items: [pending], count: 1 })
    render(<ScoreOutlierPanel initialItems={[pending]} initialCount={1} readOnly={false} />)
    fireEvent.change(screen.getByPlaceholderText('Why this replacement ticket should be released'), {
      target: { value: 'Validator evidence cleared and no re-test is needed' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Release ticket' }))
    await waitFor(() => expect(releaseScoreRetestTicket).toHaveBeenCalledTimes(1))
    expect(releaseScoreRetestTicket.mock.calls[0]?.[0]).toMatchObject({
      data: {
        agentId: item.agent_id,
        validatorHotkey: item.outlier.validator_hotkey,
        expectedDeadline: pending.replacement_deadline,
      },
    })
  })

  it('queues every eligible outlier for one validator with one shared reason', async () => {
    const second = {
      ...item,
      agent_id: '11111111-1111-4111-8111-111111111111',
      agent_name: 'second-outlier',
      snapshot: 'cd'.repeat(32),
      outlier: { ...item.outlier, run_id: 'run-low-2', composite: 0.14 },
    }
    listScoreOutliers.mockResolvedValue({ items: [item, second], count: 2 })
    render(<ScoreOutlierPanel initialItems={[item, second]} initialCount={2} readOnly={false} />)
    const action = screen.getByRole('button', { name: 'Queue 2 re-tests' })
    expect((action as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByPlaceholderText("Evidence shared by this validator's outlier runs"), {
      target: { value: 'The shared validator relay failed across this run window' },
    })
    fireEvent.click(action)
    await waitFor(() => expect(queueValidatorScoreRetests).toHaveBeenCalledTimes(1))
    expect(queueValidatorScoreRetests.mock.calls[0]?.[0]).toMatchObject({
      data: {
        validatorHotkey: item.outlier.validator_hotkey,
        items: [
          {
            agentId: item.agent_id,
            expectedSnapshot: item.snapshot,
            expectedRunId: item.outlier.run_id,
          },
          {
            agentId: second.agent_id,
            expectedSnapshot: second.snapshot,
            expectedRunId: second.outlier.run_id,
          },
        ],
      },
    })
    expect(await screen.findByText('2 queued')).toBeTruthy()
  })

  it('pages through the queue and reports the range it is showing', () => {
    render(
      <ScoreOutlierPanel
        initialItems={[item]}
        initialCount={130}
        page={2}
        pageSize={50}
        readOnly={false}
      />,
    )
    const nav = screen.getByRole('navigation', { name: 'Score outlier pagination' })
    expect(screen.getByText('Showing 51–100 of 130')).toBeTruthy()
    expect(screen.getByText('Page 2 of 3')).toBeTruthy()
    const links = Array.from(nav.querySelectorAll('a'))
    expect(links.map((link) => link.getAttribute('data-page'))).toEqual(['1', '3'])
  })

  it('refreshes the page being reviewed, not the first one', async () => {
    listScoreOutliers.mockResolvedValue({ items: [item], count: 130, bench_version: 7 })
    render(
      <ScoreOutlierPanel
        initialItems={[item]}
        initialCount={130}
        page={3}
        pageSize={50}
        readOnly={false}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(listScoreOutliers).toHaveBeenCalledTimes(1))
    // A queue decision taken on page three must not silently move the rows
    // under it by snapping the list back to the top of the queue.
    expect(listScoreOutliers.mock.calls[0]?.[0]).toMatchObject({
      data: { limit: 50, offset: 100 },
    })
  })

  it('names the benchmark era it scanned, and stays silent when the platform does not', () => {
    render(
      <ScoreOutlierPanel
        initialItems={[item]}
        initialCount={1}
        initialBenchVersion={7}
        readOnly={false}
      />,
    )
    expect(screen.getByText('Benchmark v7')).toBeTruthy()

    // A platform build predating the scoped scan answers without the era. It
    // is not safe to label that list v7: it may hold every era there is.
    cleanup()
    render(
      <ScoreOutlierPanel
        initialItems={[item]}
        initialCount={1}
        initialBenchVersion={null}
        readOnly={false}
      />,
    )
    expect(screen.queryByText(/^Benchmark v/)).toBeNull()
  })
})
