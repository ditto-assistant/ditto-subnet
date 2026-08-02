// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ValidatorRetryPanel } from './ValidatorRetryPanel'

const getValidationRetry = vi.fn()
const retryValidationAfterInfrastructureFailure = vi.fn()
const withdrawFailedValidationFromQueue = vi.fn()
const reinstateRemovedValidationInQueue = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getValidationRetry: (input: unknown) => getValidationRetry(input),
  retryValidationAfterInfrastructureFailure: (input: unknown) =>
    retryValidationAfterInfrastructureFailure(input),
  withdrawFailedValidationFromQueue: (input: unknown) =>
    withdrawFailedValidationFromQueue(input),
  reinstateRemovedValidationInQueue: (input: unknown) =>
    reinstateRemovedValidationInQueue(input),
}))

const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
const detail = {
  agent_id: agentId,
  miner_hotkey: '5Miner',
  agent_name: 'valid-agent',
  agent_version: 1,
  agent_status: 'evaluating',
  score_count: 0,
  quorum: 3,
  snapshot: 'ab'.repeat(32),
  automatic_retry_available: false,
  recovery_allowed: true,
  blocking_reason: null,
  withdrawal_allowed: true,
  withdrawal_blocking_reason: null,
  reinstatement_allowed: false,
  reinstatement_blocking_reason: null,
  withdrawal: null,
  reinstatement: null,
  tickets: [
    {
      validator_hotkey: '5Validator',
      status: 'expired',
      issued_at: '2026-07-18T12:00:00Z',
      deadline: '2026-07-18T13:30:00Z',
      bench_version: 2,
      attempt_count: 2,
      manual_retry_grants: 0,
      infra_retry_grants: 0,
      retry_after: '2026-07-18T19:30:00Z',
      retry_budget_exhausted: true,
      failed_at: '2026-07-18T13:05:00Z',
      failure_reason: 'sandbox_oom',
      failure_detail: 'container killed: oom-killer, peak 14.2 GiB',
    },
  ],
  recoveries: [],
}

describe('ValidatorRetryPanel', () => {
  afterEach(cleanup)
  beforeEach(() => {
    getValidationRetry.mockReset().mockResolvedValue(detail)
    retryValidationAfterInfrastructureFailure.mockReset().mockResolvedValue({
      recovery: {},
      idempotent: false,
    })
    withdrawFailedValidationFromQueue.mockReset().mockResolvedValue({
      withdrawal: {},
      idempotent: false,
    })
    reinstateRemovedValidationInQueue.mockReset().mockResolvedValue({
      reinstatement: {},
      idempotent: false,
    })
  })

  it('surfaces the failure reason and validator diagnostic on an expired ticket', async () => {
    render(<ValidatorRetryPanel readOnly={false} />)
    fireEvent.change(screen.getByLabelText('Agent ID for validation retry'), {
      target: { value: agentId },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Inspect retry state' }))

    expect(await screen.findByText('sandbox_oom')).toBeTruthy()
    expect(
      screen.getByText('container killed: oom-killer, peak 14.2 GiB'),
    ).toBeTruthy()
  })

  it('shows no-fault grants and silent expiry alongside the ticket state', async () => {
    getValidationRetry.mockResolvedValue({
      ...detail,
      tickets: [
        {
          ...detail.tickets[0],
          slot_id: 'slot-1',
          attempt_count: 9,
          infra_retry_grants: 8,
          retry_budget_exhausted: false,
          failure_reason: 'infrastructure',
          failed_at: '2026-07-27T16:29:00Z',
          silently_expired: false,
        },
      ],
    })
    render(<ValidatorRetryPanel readOnly />)
    fireEvent.change(screen.getByLabelText('Agent ID for validation retry'), {
      target: { value: agentId },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Inspect retry state' }))
    await screen.findByText('valid-agent')

    // Without this column the row reads: expired, nine attempts, no operator
    // grants — indistinguishable from a validator that stopped answering. The
    // eight no-fault grants are what say the failures were being reported and
    // the platform kept raising the cap and re-leasing.
    expect(screen.getByText('Infra grants')).toBeTruthy()
    expect(screen.getByText('8')).toBeTruthy()
    expect(
      screen.getByTitle('Last reported: infrastructure').textContent,
    ).toContain('expired')
  })

  it('requires evidence confirmation before one guarded retry', async () => {
    render(<ValidatorRetryPanel readOnly={false} />)
    fireEvent.change(screen.getByLabelText('Agent ID for validation retry'), {
      target: { value: agentId },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Inspect retry state' }))

    expect(await screen.findByText('valid-agent')).toBeTruthy()
    const action = screen.getByRole('button', {
      name: 'Queue minimum retries needed for quorum',
    })
    expect((action as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Validation retry audit reason'), {
      target: { value: 'Verified validator OOM and cgroup evidence' },
    })
    fireEvent.click(
      screen.getByLabelText(
        'I confirmed validator-owned failure evidence. Scores and prior attempts remain unchanged.',
      ),
    )
    expect((action as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(action)

    await waitFor(() => expect(retryValidationAfterInfrastructureFailure).toHaveBeenCalledTimes(1))
    expect(retryValidationAfterInfrastructureFailure.mock.calls[0]?.[0]).toMatchObject({
      data: {
        agentId,
        expectedSnapshot: detail.snapshot,
        reason: 'Verified validator OOM and cgroup evidence',
      },
    })
  })

  it('keeps the mutation disabled for read-only operators', async () => {
    render(<ValidatorRetryPanel readOnly />)
    fireEvent.change(screen.getByLabelText('Agent ID for validation retry'), {
      target: { value: agentId },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Inspect retry state' }))
    await screen.findByText('valid-agent')
    fireEvent.change(screen.getByLabelText('Validation retry audit reason'), {
      target: { value: 'Verified validator OOM and cgroup evidence' },
    })
    fireEvent.click(
      screen.getByLabelText(
        'I confirmed validator-owned failure evidence. Scores and prior attempts remain unchanged.',
      ),
    )
    expect(
      (
        screen.getByRole('button', {
          name: 'Queue minimum retries needed for quorum',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
  })

  it('requires exact typed confirmation for audited queue withdrawal', async () => {
    render(<ValidatorRetryPanel readOnly={false} />)
    fireEvent.change(screen.getByLabelText('Agent ID for validation retry'), {
      target: { value: agentId },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Inspect retry state' }))
    await screen.findByText('valid-agent')
    const action = screen.getByRole('button', {
      name: 'Remove from this benchmark queue',
    })
    fireEvent.change(screen.getByLabelText('Queue withdrawal audit reason'), {
      target: { value: 'Three validators exhausted scoring attempts' },
    })
    fireEvent.change(screen.getByLabelText('Queue withdrawal confirmation'), {
      target: { value: 'REMOVE FROM VALIDATOR QUEUE' },
    })
    expect((action as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(action)

    await waitFor(() => expect(withdrawFailedValidationFromQueue).toHaveBeenCalledTimes(1))
    expect(withdrawFailedValidationFromQueue.mock.calls[0]?.[0]).toMatchObject({
      data: {
        agentId,
        expectedSnapshot: detail.snapshot,
        reason: 'Three validators exhausted scoring attempts',
        confirmation: 'REMOVE FROM VALIDATOR QUEUE',
      },
    })
  })

  it('reinstates either kind of queue removal with an audited confirmation', async () => {
    getValidationRetry.mockResolvedValue({
      ...detail,
      recovery_allowed: false,
      blocking_reason: 'submission is removed from this benchmark queue',
      withdrawal_allowed: false,
      reinstatement_allowed: true,
      withdrawal: {
        withdrawal_id: 'bd704e5b-1fc0-4ebf-8c03-391847b08fa1',
        agent_id: agentId,
        bench_version: 7,
        actor: 'operator@example.com',
        reason: 'Validator failures exhausted the available attempt budget',
        expected_snapshot: detail.snapshot,
        score_count: 2,
        evicted_validator_hotkeys: null,
        created_at: '2026-07-29T14:26:02Z',
        reinstated_at: null,
      },
    })
    render(<ValidatorRetryPanel readOnly={false} />)
    fireEvent.change(screen.getByLabelText('Agent ID for validation retry'), {
      target: { value: agentId },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Inspect retry state' }))

    const action = await screen.findByRole('button', {
      name: 'Reinstate in this benchmark queue',
    })
    expect((action as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Queue reinstatement audit reason'), {
      target: { value: 'Validator infrastructure is repaired; resume scoring' },
    })
    fireEvent.change(screen.getByLabelText('Queue reinstatement confirmation'), {
      target: { value: 'REINSTATE TO VALIDATOR QUEUE' },
    })
    expect((action as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(action)

    await waitFor(() => expect(reinstateRemovedValidationInQueue).toHaveBeenCalledTimes(1))
    expect(reinstateRemovedValidationInQueue.mock.calls[0]?.[0]).toMatchObject({
      data: {
        agentId,
        expectedSnapshot: detail.snapshot,
        reason: 'Validator infrastructure is repaired; resume scoring',
        confirmation: 'REINSTATE TO VALIDATOR QUEUE',
      },
    })
  })
})
