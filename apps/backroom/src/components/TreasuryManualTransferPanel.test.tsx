// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TreasuryManualTransferPanel } from './TreasuryManualTransferPanel'
import { alphaRao } from '../lib/treasury-manual.schemas'

const preview = vi.fn(), queue = vi.fn(), refresh = vi.fn(() => Promise.resolve(control))
vi.mock('@tanstack/react-start', () => ({ useServerFn: (fn: unknown) => fn }))
vi.mock('../server/treasury-manual.functions', () => ({
  getManualTransfers: () => refresh(),
  previewManualTransfer: (input: unknown) => preview(input),
  queueManualTransfer: (input: unknown) => queue(input),
}))
const control = { enabled: true, blocked_reason: null, bridge_error: null, recurring_enabled: false as const,
  readiness: null, destinations: [{ bucket_id: 'gm', holding_coldkey: '5' + 'a'.repeat(47), allocation_bps: 1000 }, { bucket_id: 'bitsec', holding_coldkey: '5' + 'b'.repeat(47), allocation_bps: 0 }], requests: [],
}
const result = { envelope: { version: 1, collector_policy_digest: 'a'.repeat(64), destination: '5' + 'a'.repeat(47), request: { request_id: 'fa1e68a6-5213-4b25-b90a-bec349ab0a0b', after_operation: 2, source_block: 200, bucket_id: 'gm', amount_rao: 100000000, retained_alpha_rao: 55000000000, expires_block: 1000, reason: 'manual inference purchase' } }, confirmation_digest: 'b'.repeat(64), spending_authority: 'not_queued' }
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); refresh.mockImplementation(() => Promise.resolve(control)) })

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

it.each(['allocation off', 'wallet changed'])('blocks an existing confirmation after refreshed %s', async (change) => {
  preview.mockResolvedValue(result)
  render(<TreasuryManualTransferPanel initialState={control} readOnly={false} />)
  fireEvent.change(screen.getByLabelText('Amount (SN118 alpha)'), { target: { value: '0.1' } })
  fireEvent.change(screen.getByLabelText('Minimum alpha to retain staked'), { target: { value: '55' } })
  fireEvent.change(screen.getByLabelText('Audit reason'), { target: { value: 'manual inference purchase' } })
  fireEvent.click(screen.getByText('Preview manual transfer'))
  await screen.findByText('Transfer once')
  fireEvent.change(screen.getByLabelText('Type “TRANSFER 0.1 ALPHA TO GM”'), { target: { value: 'TRANSFER 0.1 ALPHA TO GM' } })
  expect((screen.getByText('Transfer once') as HTMLButtonElement).disabled).toBe(false)
  refresh.mockResolvedValueOnce({ ...control, destinations: control.destinations.map(d => d.bucket_id === 'gm' ? {
    ...d, allocation_bps: change === 'allocation off' ? 0 : d.allocation_bps,
    holding_coldkey: change === 'wallet changed' ? '5' + 'c'.repeat(47) : d.holding_coldkey,
  } : d) })
  fireEvent.click(screen.getByText('Refresh transfer status'))
  await waitFor(() => expect((screen.getByText('Transfer once') as HTMLButtonElement).disabled).toBe(true))
  fireEvent.click(screen.getByText('Transfer once'))
  expect(queue).not.toHaveBeenCalled()
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

// An older poll must never restore an enabled state over newer custody status.
it.each(['older success', 'older failure', 'newer failure'])('keeps the newest refresh authoritative: %s', async (scenario) => {
  const pending: { resolve: (value: typeof control) => void; reject: (error: Error) => void }[] = []
  refresh.mockImplementation(() => new Promise<typeof control>((resolve, reject) => { pending.push({ resolve, reject }) }))
  render(<TreasuryManualTransferPanel initialState={control} readOnly={false} />)
  fireEvent.click(screen.getByText('Refresh transfer status'))
  fireEvent.click(screen.getByText('Refresh transfer status'))
  await act(async () => {
    if (scenario === 'newer failure') pending[1].reject(new Error('offline'))
    else pending[1].resolve({ ...control, destinations: control.destinations.map(d => ({ ...d, allocation_bps: 0 })) })
  })
  await act(async () => {
    if (scenario === 'older failure') pending[0].reject(new Error('old outage'))
    else pending[0].resolve(control)
  })
  if (scenario === 'newer failure') {
    expect(screen.getByRole('alert').textContent).toContain('Transfer status unavailable')
    expect(screen.getByLabelText('Amount (SN118 alpha)').closest('fieldset')?.disabled).toBe(true)
  } else {
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('option', { name: 'GM — allocation off' }).hasAttribute('disabled')).toBe(true)
  }
})

function fillAndPreview() {
  fireEvent.change(screen.getByLabelText('Amount (SN118 alpha)'), { target: { value: '0.1' } })
  fireEvent.change(screen.getByLabelText('Minimum alpha to retain staked'), { target: { value: '55' } })
  fireEvent.change(screen.getByLabelText('Audit reason'), { target: { value: 'manual inference purchase' } })
  fireEvent.click(screen.getByText('Preview manual transfer'))
}

it('renders and renews valid request IDs without secure-context randomUUID', async () => {
  const getRandomValues = crypto.getRandomValues.bind(crypto)
  vi.stubGlobal('crypto', { getRandomValues })
  preview.mockResolvedValue(result); queue.mockResolvedValue({ status: 'queued' })
  render(<TreasuryManualTransferPanel initialState={control} readOnly={false} />)
  fillAndPreview()
  await screen.findByText('Transfer once')
  const first = preview.mock.calls[0][0].data.request_id
  expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  fireEvent.change(screen.getByLabelText('Type “TRANSFER 0.1 ALPHA TO GM”'), { target: { value: 'TRANSFER 0.1 ALPHA TO GM' } })
  fireEvent.click(screen.getByText('Transfer once'))
  await screen.findByText('Queued for custody. Track this request below.')
  fireEvent.click(screen.getByText('Preview manual transfer'))
  await waitFor(() => expect(preview).toHaveBeenCalledTimes(2))
  const second = preview.mock.calls[1][0].data.request_id
  expect(second).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  expect(second).not.toBe(first)
})

it('preserves queue failure through successful polling and retries the same confirmation', async () => {
  preview.mockResolvedValue(result)
  queue.mockRejectedValueOnce(new Error('Dispatch unavailable')).mockResolvedValueOnce({ status: 'queued' })
  render(<TreasuryManualTransferPanel initialState={control} readOnly={false} />)
  fillAndPreview()
  await screen.findByText('Transfer once')
  fireEvent.change(screen.getByLabelText('Type “TRANSFER 0.1 ALPHA TO GM”'), { target: { value: 'TRANSFER 0.1 ALPHA TO GM' } })
  fireEvent.click(screen.getByText('Transfer once'))
  await screen.findByRole('alert')
  fireEvent.click(screen.getByText('Refresh transfer status'))
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce())
  expect(screen.getByRole('alert').textContent).toBe('Dispatch unavailable')
  expect((screen.getByText('Transfer once') as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(screen.getByText('Transfer once'))
  await screen.findByText('Queued for custody. Track this request below.')
  expect(queue.mock.calls[1][0]).toEqual(queue.mock.calls[0][0])
  expect(screen.queryByRole('alert')).toBeNull()
})


it('uses a new intent ID after editing inputs following an uncertain queue response', async () => {
  preview.mockImplementation(input => Promise.resolve({ ...result, envelope: { ...result.envelope, request: { ...result.envelope.request, ...input.data } } }))
  queue.mockRejectedValueOnce(new Error('Response lost'))
  render(<TreasuryManualTransferPanel initialState={control} readOnly={false} />)
  fillAndPreview()
  await screen.findByText('Transfer once')
  const original = preview.mock.calls[0][0].data.request_id
  fireEvent.change(screen.getByLabelText('Type “TRANSFER 0.1 ALPHA TO GM”'), { target: { value: 'TRANSFER 0.1 ALPHA TO GM' } })
  fireEvent.click(screen.getByText('Transfer once'))
  await screen.findByRole('alert')
  fireEvent.change(screen.getByLabelText('Amount (SN118 alpha)'), { target: { value: '0.2' } })
  fireEvent.click(screen.getByText('Preview manual transfer'))
  await screen.findByLabelText('Type “TRANSFER 0.2 ALPHA TO GM”')
  expect(preview.mock.calls[1][0].data.request_id).not.toBe(original)
  expect(preview.mock.calls[1][0].data.amount_rao).toBe(200000000)
  expect(queue).toHaveBeenCalledOnce()
})
