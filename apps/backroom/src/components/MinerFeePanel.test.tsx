// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { MinerFeePanel } from './MinerFeePanel'

afterEach(cleanup)

describe('MinerFeePanel', () => {
  it('keeps canonical TAO separate from partial historical USD coverage', () => {
    render(
      <MinerFeePanel
        summary={{
          generated_at: '2026-07-22T00:00:00Z',
          payment_address: '5PaymentAddress',
          paid_submissions: 367,
          gross_amount_rao: 13_031_465_759,
          priced_submissions: 2,
          unpriced_submissions: 365,
          gross_value_usd: 10,
          unique_paying_coldkeys: 42,
          first_payment_at: '2026-07-13T23:14:48Z',
          last_payment_at: '2026-07-21T23:52:12Z',
          recent_days: [
            {
              date: '2026-07-21',
              paid_submissions: 2,
              gross_amount_rao: 40_000_000,
              priced_submissions: 2,
              gross_value_usd: 10,
            },
          ],
          readmission: {
            admitted: 3,
            admitted_amount_rao: 225_000_000,
            credit_funded: 1,
            credited: 1,
            credited_amount_rao: 75_000_000,
            upload_readmissions: 2,
            upload_readmission_amount_rao: 150_000_000,
          },
        }}
      />,
    )

    expect(screen.getByText('13.031465759 TAO')).toBeTruthy()
    expect(screen.getAllByText('$10.00')).toHaveLength(2)
    expect(screen.getByText(/365 legacy payments are unpriced/)).toBeTruthy()
    expect(screen.getByText('5PaymentAddress')).toBeTruthy()
    expect(screen.getByLabelText('Readmission fees').textContent).toContain(
      '3 paid readmissions',
    )
    expect(screen.getByText('2/2')).toBeTruthy()
  })
})
