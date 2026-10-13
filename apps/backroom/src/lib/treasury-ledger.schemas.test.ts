import { expect, it } from 'vitest'
import { enforcingTreasuryPinSchema, publicTreasuryApprovalSchema } from './treasury-ledger.schemas'

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

const enforcing = (allocations: unknown) => ({
  version: 2, mode: 'enforce', epoch_index: 9, first_block: 100, pinned_block: 101,
  pinned_block_hash: `0x${'e'.repeat(64)}`, policy: ranged.policy, policy_digest: 'd'.repeat(64),
  identity: {
    genesis_hash: `0x${'b'.repeat(64)}`, netuid: 118, finalized_block: 101, finalized_block_hash: `0x${'e'.repeat(64)}`,
    uid: 4, hotkey: '5' + 'a'.repeat(47), owner_coldkey: '5' + 'b'.repeat(47), uid_hotkey: '5' + 'a'.repeat(47),
    subnet_owner_coldkey: '5' + 'f'.repeat(47),
  },
  approval: ranged,
  fleet: [{
    validator_hotkey: '5' + 'e'.repeat(47), protocol_version: 30, treasury_pin_version: 2,
    treasury_dispatch_version: 2, approved_policy_digest: 'd'.repeat(64), collector_policy_digest: 'c'.repeat(64),
  }],
  allocations,
})

it.each([
  [[{ bucket_id: 'gm', allocation_bps: 6000 }], true],
  [null, false],
  [[{ bucket_id: 'gm', allocation_bps: 7501 }], false],
  [[{ bucket_id: 'other', allocation_bps: 10 }], false],
  [[{ bucket_id: 'gm', allocation_bps: 10 }, { bucket_id: 'gm', allocation_bps: 10 }], false],
])('accepts only an in-range pinned allocation for a ranged policy %#', (allocations, ok) => {
  expect(enforcingTreasuryPinSchema.safeParse(enforcing(allocations)).success).toBe(ok)
})

it('rejects duplicate bucket IDs, as the protocol does', () => {
  const dup = structuredClone(ranged)
  dup.policy.buckets.push({ ...dup.policy.buckets[0], holding_coldkey: '5' + 'd'.repeat(47), max_allocation_bps: 2000, allocation_bps: 0 })
  const result = publicTreasuryApprovalSchema.safeParse(dup)
  expect(result.success).toBe(false)
  if (!result.success) expect(result.error.issues[0].message).toBe('duplicate treasury bucket')
})

it.each([
  ['shared wallet', '5' + 'c'.repeat(47)],
  ['collector wallet', '5' + 'b'.repeat(47)],
])('rejects a %s as a holding destination, as the protocol does', (_, wallet) => {
  const bad = structuredClone(ranged)
  bad.policy.buckets.push({ bucket_id: 'bitsec', allocation_bps: 0, holding_coldkey: wallet, min_allocation_bps: 0, max_allocation_bps: 1000 })
  const result = publicTreasuryApprovalSchema.safeParse(bad)
  expect(result.success).toBe(false)
  if (!result.success) expect(result.error.issues[0].message).toBe('holding coldkeys must be distinct from collector')
})
