import { describe, expect, it } from 'vitest'
import { treasuryQuoteSchema, treasuryRouteImpactBps, treasurySettingsSchema } from './treasury.schemas'

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

const gm = {
  bucket_id: 'gm_credits',
  purpose: 'GM inference credit',
  allocation_bps: 1000,
  holding_coldkey: '5ccccccccccccccccccccccccccccccccccccccccccccccc',
  service_account_ref: 'reviewed-gm-account',
}

const v2 = {
  mode: 'shadow',
  allocation_version: 2,
  maintenance_bps: 0,
  gm_bps: 0,
  service_buckets: [
    gm,
    {
      bucket_id: 'bitsec_audits',
      purpose: 'independent security audits',
      allocation_bps: 0,
      holding_coldkey: null,
      service_account_ref: null,
    },
  ],
  treasury_hotkey: '5aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  treasury_coldkey: '5bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
  gm_account_ref: null,
  max_daily_outflow_rao: 0,
  max_single_topup_rao: 0,
  max_slippage_bps: 0,
}

describe('shadow treasury policy versions', () => {
  it.each([2500, 7500, 10000])('accepts a %s bps service proposal', (allocation_bps) => {
    expect(treasurySettingsSchema.parse({ ...v2, service_buckets: [{ ...gm, allocation_bps }] })
      .service_buckets[0].allocation_bps).toBe(allocation_bps)
  })
  it('requires exact payee identity and rejects ambiguous matches or seed-like wallet input', () => {
    const rule = {
      rule_id: 'gm_treasury',
      label: 'GM credit payment',
      recipient_coldkey: '5' + 'e'.repeat(47),
      asset: 'TAO',
    }
    const policy = { ...v2, service_buckets: [{ ...gm, payee_rules: [rule] }] }
    expect(treasurySettingsSchema.safeParse(policy).success).toBe(true)
    expect(
      treasurySettingsSchema.safeParse({ ...policy, treasury_coldkey: 'never put a mnemonic here' }).success,
    ).toBe(false)
    expect(
      treasurySettingsSchema.safeParse({
        ...policy,
        service_buckets: [{ ...gm, payee_rules: [rule, { ...rule, rule_id: 'second_rule' }] }],
      }).success,
    ).toBe(false)
    expect(
      treasurySettingsSchema.safeParse({
        ...policy,
        service_buckets: [{ ...gm, payee_rules: [{ ...rule, asset: 'SN28_ALPHA' }] }],
      }).success,
    ).toBe(false)
    expect(treasurySettingsSchema.safeParse({ ...policy, sweep_interval_hours: 0 }).success).toBe(false)
  })
  it('keeps a legacy revision at its original 500 bps cap', () => {
    const old = {
      ...v2,
      allocation_version: undefined,
      service_buckets: [],
      maintenance_bps: 250,
      gm_bps: 250,
      treasury_hotkey: 'legacy-hotkey',
      treasury_coldkey: 'legacy-coldkey',
      gm_account_ref: 'legacy-gm-account',
    }
    expect(treasurySettingsSchema.parse(old).allocation_version).toBe(1)
    expect(treasurySettingsSchema.safeParse({ ...old, gm_bps: 251 }).success).toBe(false)
  })

  it('accepts isolated v2 service wallets only in shadow mode', () => {
    expect(treasurySettingsSchema.parse(v2).service_buckets).toHaveLength(2)
    expect(treasurySettingsSchema.safeParse({ ...v2, mode: 'active' }).success).toBe(false)
    expect(treasurySettingsSchema.safeParse({ ...v2, gm_bps: 1 }).success).toBe(false)
    expect(treasurySettingsSchema.safeParse({ ...v2, service_buckets: [gm, gm] }).success).toBe(false)
    expect(
      treasurySettingsSchema.safeParse({ ...v2, service_buckets: [{ ...gm, service_account_ref: null }] })
        .success,
    ).toBe(true)
    expect(
      treasurySettingsSchema.safeParse({
        ...v2,
        service_buckets: [
          { ...gm, allocation_bps: 10000 },
          {
            ...v2.service_buckets[1],
            allocation_bps: 1,
            holding_coldkey: '5ddddddddddddddddddddddddddddddddddddddddddddddd',
          },
        ],
      }).success,
    ).toBe(false)
    expect(
      treasurySettingsSchema.safeParse({
        ...v2,
        service_buckets: [
          gm,
          {
            ...v2.service_buckets[1],
            allocation_bps: 1,
          },
        ],
      }).success,
    ).toBe(false)
    expect(
      treasurySettingsSchema.safeParse({
        ...v2,
        service_buckets: [
          gm,
          {
            ...v2.service_buckets[1],
            holding_coldkey: gm.holding_coldkey,
          },
        ],
      }).success,
    ).toBe(false)
    expect(treasurySettingsSchema.safeParse({ ...v2, treasury_coldkey: gm.holding_coldkey }).success).toBe(
      false,
    )
    expect(treasurySettingsSchema.safeParse({ ...v2, treasury_hotkey: null }).success).toBe(false)
  })

  it('accepts platform-defaulted zero buckets but rejects empty collector identities', () => {
    const policy = treasurySettingsSchema.parse({
      ...v2,
      treasury_hotkey: null,
      treasury_coldkey: null,
      service_buckets: [{ bucket_id: 'gm_credits', purpose: 'GM inference credits' }],
    })
    expect(policy.service_buckets[0]).toMatchObject({
      allocation_bps: 0,
      holding_coldkey: null,
      service_account_ref: null,
    })
    expect(
      treasurySettingsSchema.safeParse({
        ...policy,
        treasury_hotkey: '',
        treasury_coldkey: '',
      }).success,
    ).toBe(false)
  })
})

describe('treasuryRouteImpactBps', () => {
  it('uses the TAO path impact for the TAO route', () => {
    expect(treasuryRouteImpactBps('tao', quote)).toBe(100)
  })

  it('uses the cumulative GM path impact without re-adding the first hop', () => {
    expect(treasuryRouteImpactBps('gm_alpha', quote)).toBe(199)
  })
})
