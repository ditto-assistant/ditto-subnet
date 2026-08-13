// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  submissionDepositAddressConfirmation,
  type SubmissionDepositAddressControl as Control,
} from '../lib/submission-deposit-address'
import { SubmissionDepositAddressControl } from './SubmissionDepositAddressControl'

const getControl = vi.fn()
const setAddress = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getSubmissionDepositAddressControl: () => getControl(),
  setSubmissionDepositAddress: (input: unknown) => setAddress(input),
}))

const oldAddress = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'
const newAddress = '5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty'
const initial: Control = {
  current: {
    revision: 0,
    parent_revision: 0,
    payment_address: oldAddress,
    reason: 'Boot-configured submission deposit address',
    actor: 'platform',
    created_at: null,
  },
  history: [],
}

describe('SubmissionDepositAddressControl', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getControl.mockReset().mockResolvedValue(initial)
    setAddress.mockReset().mockResolvedValue({
      current: { ...initial.current, revision: 1, payment_address: newAddress },
      history: [],
    })
  })

  it('requires a valid changed address, reason, and exact confirmation', async () => {
    render(<SubmissionDepositAddressControl initialState={initial} readOnly={false} />)

    const submit = screen.getByRole('button', { name: 'Change deposit address' })
    expect((submit as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('New SS58 receive address'), {
      target: { value: newAddress },
    })
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'move submission earnings to the treasury coldkey' },
    })
    const expected = submissionDepositAddressConfirmation(newAddress)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(submit)

    await waitFor(() => expect(setAddress).toHaveBeenCalledTimes(1))
    expect(setAddress).toHaveBeenCalledWith({
      data: {
        expectedRevision: 0,
        paymentAddress: newAddress,
        reason: 'move submission earnings to the treasury coldkey',
        confirmation: expected,
      },
    })
  })

  it('renders no mutation form for read-only users', () => {
    render(<SubmissionDepositAddressControl initialState={initial} readOnly />)

    expect(screen.getByText(/Your account has read access/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Change deposit address' })).toBeNull()
  })
})
