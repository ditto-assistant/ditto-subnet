// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TreasuryControlPanel } from './TreasuryControlPanel'
import { treasuryControlSchema } from '../lib/treasury.schemas'

const save = vi.fn()
vi.mock('@tanstack/react-start', () => ({ useServerFn: (fn: unknown) => fn }))
vi.mock('../server/treasury.functions', () => ({
  getTreasurySettings: () => Promise.resolve(control),
  saveTreasurySettings: (input: unknown) => save(input),
}))
const control = treasuryControlSchema.parse({
  revision: 3,
  miner_bps: 9000,
  weight_effect: 'none',
  history: [],
  effective: {
    mode: 'shadow',
    allocation_version: 2,
    maintenance_bps: 0,
    gm_bps: 0,
    treasury_hotkey: '5' + 'a'.repeat(47),
    treasury_coldkey: '5' + 'b'.repeat(47),
    service_buckets: [
      {
        bucket_id: 'gm_credits',
        purpose: 'GM inference credits',
        allocation_bps: 1000,
        holding_coldkey: '5' + 'c'.repeat(47),
        service_account_ref: 'private-account',
      },
    ],
    gm_account_ref: null,
    max_daily_outflow_rao: 0,
    max_single_topup_rao: 0,
    max_slippage_bps: 0,
  },
})
afterEach(() => {
  cleanup()
  save.mockReset()
})

it('records a wallet policy with CAS, reason and exact confirmation, without claiming funding', async () => {
  save.mockResolvedValue({ ...control, revision: 4 })
  render(<TreasuryControlPanel initialState={control} readOnly={false} />)
  const button = screen.getByRole('button', { name: 'Record wallet policy' }) as HTMLButtonElement
  expect(button.disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('Reason for change'), {
    target: { value: 'reviewed wallet transparency change' },
  })
  fireEvent.change(screen.getByLabelText('Type RECORD TREASURY SHADOW POLICY'), {
    target: { value: 'RECORD TREASURY SHADOW POLICY' },
  })
  fireEvent.click(button)
  await waitFor(() => expect(save).toHaveBeenCalledOnce())
  expect(save.mock.calls[0]?.[0].data.expectedRevision).toBe(3)
  expect(save.mock.calls[0]?.[0].data.settings.mode).toBe('shadow')
  await screen.findByText(/Funding, distribution, and payment observation remain inactive/)
})

it('blocks changed wallet data until confirmation is entered again', () => {
  render(<TreasuryControlPanel initialState={control} readOnly={false} />)
  fireEvent.change(screen.getByLabelText('Reason for change'), {
    target: { value: 'reviewed wallet change' },
  })
  fireEvent.change(screen.getByLabelText('Type RECORD TREASURY SHADOW POLICY'), {
    target: { value: 'RECORD TREASURY SHADOW POLICY' },
  })
  fireEvent.change(screen.getByLabelText('Holding coldkey'), { target: { value: '5' + 'd'.repeat(47) } })
  expect((screen.getByRole('button', { name: 'Record wallet policy' }) as HTMLButtonElement).disabled).toBe(
    true,
  )
})

it('keeps read-only users out of the wallet mutation', () => {
  render(<TreasuryControlPanel initialState={control} readOnly />)
  expect(
    (
      screen
        .getByLabelText('Holding coldkey')
        .closest('fieldset')
        ?.parentElement?.closest('fieldset') as HTMLFieldSetElement
    ).disabled,
  ).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Record wallet policy' }))
  expect(save).not.toHaveBeenCalled()
})
