import { describe, expect, it } from 'vitest'
import { treasuryQuoteSchema, treasuryRouteImpactBps } from './treasury.schemas'

const quote = treasuryQuoteSchema.parse({
  block: 7,
  block_hash: `0x${'b'.repeat(64)}`,
  source_alpha_rao: 10_000,
  tao_path: { deposit_asset: 'TAO', amount_rao: 9_900, price_impact_bps: 100 },
  // Platform reports the GM route's compounded two-hop impact.
  gm_alpha_path: { deposit_asset: 'SN28_ALPHA', amount_rao: 19_800, price_impact_bps: 199 },
  gm_credit_usd: null,
  execution_enabled: false,
  settlement: 'confirmed deposit rate',
})

describe('treasuryRouteImpactBps', () => {
  it('uses the TAO path impact for the TAO route', () => {
    expect(treasuryRouteImpactBps('tao', quote)).toBe(100)
  })

  it('uses the cumulative GM path impact without re-adding the first hop', () => {
    expect(treasuryRouteImpactBps('gm_alpha', quote)).toBe(199)
  })
})
