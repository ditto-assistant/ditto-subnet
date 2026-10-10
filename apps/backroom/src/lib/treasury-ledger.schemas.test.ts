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
