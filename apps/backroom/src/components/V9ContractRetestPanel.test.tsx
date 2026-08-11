// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { V9ContractRetestPanel } from './V9ContractRetestPanel'

const listV9ContractRetests = vi.fn()
const queueValidatorScoreRetests = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  listV9ContractRetests: (input: unknown) => listV9ContractRetests(input),
  queueValidatorScoreRetests: (input: unknown) => queueValidatorScoreRetests(input),
}))

const item = {
  agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
  agent_name: 'shadow-agent',
  miner_hotkey: '5Miner',
  agent_status: 'evaluating',
  validator_hotkey: '5Validator',
  run_id: 'run-shadow',
  composite: 0.7,
  snapshot: 'ab'.repeat(32),
  observed_revision: 'v9-base-shadow-calibration-v1',
  observed_manifest_sha256: 'cd'.repeat(32),
  observed_rollout_mode: 'shadow',
  semantic_gate_factor_bps: 0,
  ticket_status: 'scored' as const,
  replacement_pending: false,
  replacement_queued: false,
  queue_position: null,
  queue_allowed: true,
  queue_blocking_reason: null,
}

const requiredRevision = 'v9-base-enforce-efficiency-v1'
const requiredManifestSha256 = 'ef'.repeat(32)

describe('V9ContractRetestPanel', () => {
  afterEach(cleanup)
  beforeEach(() => {
    listV9ContractRetests.mockReset().mockResolvedValue({
      items: [item],
      count: 1,
      required_revision: requiredRevision,
      required_manifest_sha256: requiredManifestSha256,
      required_rollout_mode: 'enforce',
    })
    queueValidatorScoreRetests.mockReset().mockResolvedValue({
      activated: 0,
      queued: 1,
      idempotent: 0,
      skipped: 0,
    })
  })

  it('requires an audit reason and exact confirmation before queueing', async () => {
    render(
      <V9ContractRetestPanel
        initialItems={[item]}
        initialCount={1}
        requiredRevision={requiredRevision}
        requiredManifestSha256={requiredManifestSha256}
        readOnly={false}
      />,
    )
    const action = screen.getByRole('button', { name: 'Queue 1' }) as HTMLButtonElement
    expect(action.disabled).toBe(true)
    fireEvent.change(screen.getByPlaceholderText('Why these signed scores require contract replacement'), {
      target: { value: 'The signed run used the retired shadow score contract' },
    })
    const confirmation = screen.getByLabelText('Type QUEUE V9 CONTRACT RETESTS')
    fireEvent.change(confirmation, { target: { value: 'QUEUE RETESTS' } })
    expect(action.disabled).toBe(true)
    fireEvent.change(confirmation, { target: { value: 'QUEUE V9 CONTRACT RETESTS' } })
    expect(action.disabled).toBe(false)
    fireEvent.click(action)

    await waitFor(() => expect(queueValidatorScoreRetests).toHaveBeenCalledTimes(1))
    expect(queueValidatorScoreRetests.mock.calls[0]?.[0]).toMatchObject({
      data: {
        validatorHotkey: item.validator_hotkey,
        basis: 'v9_contract_mismatch',
        confirmation: 'QUEUE V9 CONTRACT RETESTS',
        items: [{
          agentId: item.agent_id,
          expectedSnapshot: item.snapshot,
          expectedRunId: item.run_id,
        }],
      },
    })
    expect(await screen.findByText('1 queued')).toBeTruthy()
  })

  it('shows queued and blocked states and never submits them twice', () => {
    const queued = {
      ...item,
      replacement_queued: true,
      queue_position: 2,
      queue_allowed: false,
      queue_blocking_reason: 'replacement score is already queued or pending',
    }
    render(
      <V9ContractRetestPanel
        initialItems={[queued]}
        initialCount={1}
        requiredRevision={requiredRevision}
        requiredManifestSha256={requiredManifestSha256}
        readOnly={false}
      />,
    )
    expect(screen.getByText('Queued #2')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Queue 0' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('refreshes the exact contract worklist and teaches the empty state', async () => {
    listV9ContractRetests.mockResolvedValue({
      items: [],
      count: 0,
      required_revision: requiredRevision,
      required_manifest_sha256: requiredManifestSha256,
      required_rollout_mode: 'enforce',
    })
    render(
      <V9ContractRetestPanel
        initialItems={[item]}
        initialCount={1}
        requiredRevision={requiredRevision}
        requiredManifestSha256={requiredManifestSha256}
        readOnly={false}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(listV9ContractRetests).toHaveBeenCalledWith({
      data: { limit: 100, offset: 0 },
    }))
    expect(await screen.findByText('No invalid v9 score contracts')).toBeTruthy()
    expect(screen.getByText('Every accepted v9 score uses the current enforce contract.')).toBeTruthy()
  })
})
