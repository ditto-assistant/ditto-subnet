import { describe, expect, it } from 'vitest'
import type { components } from '../generated/platform-api'
import { treasuryGammaLedgerSchema } from './treasury-gamma-ledger.schemas'

type Ledger = components['schemas']['PublicTreasuryLedger']

const ledger: Ledger = {
  current: null,
  totals: { window_epochs: 0, earned_rao: '0', outflow_rao: '0', attributed_rao: '0', unattributed_rao: '0' },
  intervals: [],
  sampler: { enabled: true, state: 'running', last_sampled_at: null, last_error: null },
}

describe('treasuryGammaLedgerSchema', () => {
  it('accepts the generated Platform contract shape', () => {
    expect(treasuryGammaLedgerSchema.parse(ledger)).toEqual(ledger)
  })

  it('refuses a malformed payload or a numeric rao amount', () => {
    expect(() => treasuryGammaLedgerSchema.parse({})).toThrow()
    expect(() =>
      treasuryGammaLedgerSchema.parse({ ...ledger, totals: { ...ledger.totals, earned_rao: 5 } }),
    ).toThrow()
  })
})
