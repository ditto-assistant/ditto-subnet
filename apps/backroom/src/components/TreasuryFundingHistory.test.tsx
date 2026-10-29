// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { fundingAmount, fundingHistorySchema } from '../lib/treasury-funding.schemas'
import { TreasuryFundingHistory } from './TreasuryFundingHistory'

vi.mock('../server/treasury-funding.functions', () => ({ getTreasuryFundingHistory: vi.fn() }))
afterEach(cleanup)
const empty = { items: [], truncated: false, totals_scope: 'returned_records', credited_usd_cents: 0, tao_paid_rao: '0', distinct_conversions: 0, automatic_topups_enabled: false, spending_authority: 'none' }
it('explains the difference between custody and completed funding when empty', () => {
  render(<TreasuryFundingHistory initialState={fundingHistorySchema.parse(empty)} />)
  expect(screen.getByText(/No reconciled purchases yet/)).toBeTruthy()
  expect(screen.getByText(/Automatic top-ups are off/)).toBeTruthy()
})
it('preserves atomic precision above the safe-number limit', () => {
  expect(fundingAmount('18446744073709551615')).toBe('18446744073.709551615')
  expect(fundingAmount('1000000000')).toBe('1')
})
it('shows completed credits with explicit evidence limits and existing TAO', () => {
  const state = fundingHistorySchema.parse({ ...empty, credited_usd_cents: 25000, tao_paid_rao: '2000000000', items: [{
    holding_wallet: '5' + 'a'.repeat(47), actor: 'operator@example.com', recorded_at: '2026-01-01T00:01:00Z', verification_level: 'operator_reconciled',
    record: { purchase: { purchase_id: '11111111-2222-4333-8444-555555555555', occurred_at: '2026-01-01T00:00:30Z', credited_usd_cents: 25000 }, payment: { reference: '9000000-3', kind: 'payment', occurred_at: '2026-01-01T00:00:00Z', input_asset: 'TAO', input_amount_atomic: '2000000000' } },
  }] })
  render(<TreasuryFundingHistory initialState={state} />)
  expect(screen.getByText('Existing TAO balance')).toBeTruthy()
  expect(screen.getAllByText('$250.00')).toHaveLength(2)
  expect(screen.getByText(/GM has not supplied a transaction attestation/)).toBeTruthy()
  expect(screen.getByRole('link', { name: /9000000-3/ }).getAttribute('href')).toBe('https://bittensor.ai/explorer/extrinsic/9000000-3')
})

it('renders repeated conversion references without duplicate React keys', () => {
  const conversion = { reference: '8999990-1', kind: 'swap', occurred_at: '2025-12-31T23:58:00Z', input_asset: 'SN118_ALPHA', input_amount_atomic: '1000000000', output_asset: 'SN28_ALPHA', output_amount_atomic: '200000000' }
  const state = fundingHistorySchema.parse({ ...empty, items: [{
    holding_wallet: '5' + 'a'.repeat(47), actor: 'operator@example.com', recorded_at: '2026-01-01T00:01:00Z', verification_level: 'operator_reconciled',
    record: { purchase: { purchase_id: '11111111-2222-4333-8444-555555555555', occurred_at: '2026-01-01T00:00:30Z', credited_usd_cents: 25000 }, payment: { reference: '9000000-3', kind: 'payment', occurred_at: '2026-01-01T00:00:00Z', input_asset: 'TAO', input_amount_atomic: '2000000000' }, earlier_conversions: [conversion, conversion] },
  }] })
  const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
  try {
    render(<TreasuryFundingHistory initialState={state} />)
    expect(screen.getAllByRole('link', { name: /8999990-1/ })).toHaveLength(2)
    expect(errors.mock.calls.flat().join(' ')).not.toMatch(/same key|unique.*key/i)
  } finally { errors.mockRestore() }
})
