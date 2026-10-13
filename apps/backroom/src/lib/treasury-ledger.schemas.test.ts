import { expect, it } from 'vitest'
import { publicTreasuryApprovalSchema } from './treasury-ledger.schemas'

const approval = {
  signature: `0x${'a'.repeat(128)}`,
  policy: {
    version: 1,
    revision: 6,
    genesis_hash: `0x${'b'.repeat(64)}`,
    netuid: 118,
    collector_hotkey: '5' + 'a'.repeat(47),
    collector_coldkey: '5' + 'b'.repeat(47),
    collector_policy_digest: 'c'.repeat(64),
    buckets: [
      { bucket_id: 'gm', allocation_bps: 7500, holding_coldkey: '5' + 'c'.repeat(47) },
      { bucket_id: 'bitsec', allocation_bps: 2500, holding_coldkey: '5' + 'd'.repeat(47) },
    ],
  },
}

it('accepts a signed policy totaling 100% and rejects an aggregate above 100%', () => {
  expect(publicTreasuryApprovalSchema.safeParse(approval).success).toBe(true)
  const overflow = structuredClone(approval)
  overflow.policy.buckets[1].allocation_bps = 2501
  const result = publicTreasuryApprovalSchema.safeParse(overflow)
  expect(result.success).toBe(false)
  if (!result.success) expect(result.error.issues[0].message).toBe('combined service allocation exceeds 10000 bps')
})

const ranged = {
  ...approval,
  policy: {
    ...approval.policy,
    buckets: [
      { bucket_id: 'gm', allocation_bps: 2500, holding_coldkey: '5' + 'c'.repeat(47), min_allocation_bps: 0, max_allocation_bps: 7500 },
    ],
  },
}

it('keeps the signed Gamma range so the forwarded approval digest is unchanged', () => {
  const parsed = publicTreasuryApprovalSchema.parse(ranged)
  expect(parsed.policy.buckets[0]).toEqual(ranged.policy.buckets[0])
  // A fixed bucket stays byte-identical: no bound keys appear.
  expect(Object.keys(publicTreasuryApprovalSchema.parse(approval).policy.buckets[0]).sort())
    .toEqual(['allocation_bps', 'bucket_id', 'holding_coldkey'])
})

it.each([
  [{ min_allocation_bps: 0 }, 'allocation range requires both bounds'],
  [{ min_allocation_bps: 3000, max_allocation_bps: 7500 }, 'signed default allocation is outside its range'],
])('rejects a malformed signed range %#', (bounds, message) => {
  const bad = structuredClone(approval)
  bad.policy.buckets = [{ bucket_id: 'gm', allocation_bps: 2500, holding_coldkey: '5' + 'c'.repeat(47), ...bounds }]
  const result = publicTreasuryApprovalSchema.safeParse(bad)
  expect(result.success).toBe(false)
  if (!result.success) expect(result.error.issues[0].message).toBe(message)
})

it('bounds the aggregate by every bucket ceiling, not only the signed default', () => {
  const wide = structuredClone(ranged)
  wide.policy.buckets.push({ bucket_id: 'bitsec', allocation_bps: 0, holding_coldkey: '5' + 'd'.repeat(47), min_allocation_bps: 0, max_allocation_bps: 2501 })
  expect(publicTreasuryApprovalSchema.safeParse(wide).success).toBe(false)
})
