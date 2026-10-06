// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TreasuryManualTransferPanel } from './TreasuryManualTransferPanel'
import { alphaRao } from '../lib/treasury-manual.schemas'

const preview = vi.fn(), queue = vi.fn()
vi.mock('@tanstack/react-start', () => ({ useServerFn: (fn: unknown) => fn }))
vi.mock('../server/treasury-manual.functions', () => ({
  getManualTransfers: () => Promise.resolve(control),
  previewManualTransfer: (input: unknown) => preview(input),
  queueManualTransfer: (input: unknown) => queue(input),
}))
const control = { enabled: true, blocked_reason: null, bridge_error: null, recurring_enabled: false as const,
  readiness: null, destinations: [{ bucket_id: 'gm', holding_coldkey: '5' + 'a'.repeat(47), allocation_bps: 1000 }, { bucket_id: 'bitsec', holding_coldkey: '5' + 'b'.repeat(47), allocation_bps: 0 }], requests: [],
}
const result = { envelope: { version: 1, collector_policy_digest: 'a'.repeat(64), destination: '5' + 'a'.repeat(47), request: { request_id: 'fa1e68a6-5213-4b25-b90a-bec349ab0a0b', after_operation: 2, source_block: 200, bucket_id: 'gm', amount_rao: 100000000, retained_alpha_rao: 55000000000, expires_block: 1000, reason: 'manual inference purchase' } }, confirmation_digest: 'b'.repeat(64), spending_authority: 'not_queued' }
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('previews exact integer amounts and queues only after amount and wallet confirmation', async () => {
  preview.mockResolvedValue(result); queue.mockResolvedValue({ status: 'queued' })
  render(<TreasuryManualTransferPanel initialState={control} readOnly={false} />)
  fireEvent.change(screen.getByLabelText('Amount (SN118 alpha)'), { target: { value: '0.1' } })
  fireEvent.change(screen.getByLabelText('Minimum alpha to retain staked'), { target: { value: '55' } })
  fireEvent.change(screen.getByLabelText('Audit reason'), { target: { value: 'manual inference purchase' } })
  fireEvent.click(screen.getByText('Preview manual transfer'))
  await screen.findByText('Transfer once')
  expect(queue).not.toHaveBeenCalled()
  expect(preview.mock.calls[0][0].data.amount_rao).toBe(100000000)
  expect(preview.mock.calls[0][0].data.retained_alpha_rao).toBe(55000000000)
  expect((screen.getByText('Transfer once') as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('Type “TRANSFER 0.1 ALPHA TO GM”'), { target: { value: 'TRANSFER 0.1 ALPHA TO GM' } })
  fireEvent.click(screen.getByText('Transfer once'))
  await waitFor(() => expect(queue).toHaveBeenCalledOnce())
  expect(queue.mock.calls[0][0].data.envelope).toEqual(result.envelope)
  expect(queue.mock.calls[0][0].data.confirmation_digest).toBe(result.confirmation_digest)
  expect(await screen.findByText('Queued for custody. Track this request below.')).toBeTruthy()
})

it('read-only accounts and stale/disabled custody cannot dispatch', () => {
  render(<TreasuryManualTransferPanel initialState={{ ...control, blocked_reason: 'Waiting for a fresh custody observation' }} readOnly />)
  expect((screen.getByLabelText('Amount (SN118 alpha)') as HTMLInputElement).disabled).toBe(false)
  expect(screen.getByLabelText('Amount (SN118 alpha)').closest('fieldset')?.disabled).toBe(true)
  expect(queue).not.toHaveBeenCalled()
})

it('refuses binary float rounding, scientific notation, zero and excessive precision', () => {
  expect(alphaRao('0.000000001')).toBe(1)
  expect(alphaRao('0.100000001')).toBe(100000001)
  for (const bad of ['1e2', '0', '-1', '0.0000000001', '9007199254740992']) expect(alphaRao(bad)).toBeNull()
})


it('blocks preview when all allocations are disabled or a refresh disables the selection', async () => {
  const fill = () => {
    fireEvent.change(screen.getByLabelText('Amount (SN118 alpha)'), { target: { value: '0.1' } })
    fireEvent.change(screen.getByLabelText('Minimum alpha to retain staked'), { target: { value: '55' } })
    fireEvent.change(screen.getByLabelText('Audit reason'), { target: { value: 'manual inference purchase' } })
  }
  render(<TreasuryManualTransferPanel initialState={{ ...control, destinations: control.destinations.map(d => ({ ...d, allocation_bps: 0 })) }} readOnly={false} />)
  fill()
  expect((screen.getByText('Preview manual transfer') as HTMLButtonElement).disabled).toBe(true)
  cleanup()
  render(<TreasuryManualTransferPanel initialState={{ ...control, destinations: [{ ...control.destinations[0], bucket_id: 'bitsec', allocation_bps: 1000 }] }} readOnly={false} />)
  fill()
  expect((screen.getByText('Preview manual transfer') as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(screen.getByText('Refresh transfer status'))
  await waitFor(() => expect((screen.getByText('Preview manual transfer') as HTMLButtonElement).disabled).toBe(true))
  expect(preview).not.toHaveBeenCalled()
})
