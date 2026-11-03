import { expect, it } from 'vitest'
import { fundingPreviewSchema, fundingResultSchema } from './treasury-funding.schemas'

const preview = { confirmation_digest: 'a'.repeat(64), confirmation: 'RECORD GM FUNDING HISTORY ' + 'a'.repeat(64), purchases: 0, credited_usd_cents: 0, verification_level: 'operator_reconciled', spending_authority: 'none' }
it('accepts zero and positive totals and rejects negative counts or credits', () => {
  expect(fundingPreviewSchema.safeParse(preview).success).toBe(true)
  expect(fundingPreviewSchema.safeParse({ ...preview, purchases: 1, credited_usd_cents: 25000 }).success).toBe(true)
  for (const change of [{ purchases: -1 }, { credited_usd_cents: -500 }]) {
    expect(fundingPreviewSchema.safeParse({ ...preview, ...change }).success).toBe(false)
  }
  expect(fundingResultSchema.safeParse({ ...preview, inserted: -1, replayed: 0 }).success).toBe(false)
})
