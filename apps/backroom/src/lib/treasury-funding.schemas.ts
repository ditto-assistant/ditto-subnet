import { z } from 'zod'

const atomic = z.string().regex(/^[1-9][0-9]{0,19}$/)
const asset = z.enum(['TAO', 'SN118_ALPHA', 'SN28_ALPHA'])
export const fundingMovementSchema = z.object({
  reference: z.string().regex(/^[1-9][0-9]{5,11}-[0-9]{1,6}$/),
  kind: z.enum(['sale', 'swap', 'payment']),
  occurred_at: z.string().datetime({ offset: true }),
  block_hash: z.string().regex(/^0x[0-9a-f]{64}$/).nullable().optional(),
  input_asset: asset, input_amount_atomic: atomic,
  output_asset: asset.nullable().optional(), output_amount_atomic: atomic.nullable().optional(),
  recipient: z.string().nullable().optional(),
})
export const fundingRecordSchema = z.object({
  purchase: z.object({
    purchase_id: z.string().uuid(), occurred_at: z.string().datetime({ offset: true }),
    credited_usd_cents: z.number().int().positive().max(100_000_000),
    method: z.literal('x402-relayed').default('x402-relayed'), status: z.literal('completed').default('completed'),
  }),
  payment: fundingMovementSchema,
  preceding_sale: fundingMovementSchema.nullable().optional(),
  earlier_conversions: z.array(fundingMovementSchema).max(8).default([]),
  link_basis: z.literal('operator_time_correlation').default('operator_time_correlation'),
})
export const fundingBatchSchema = z.object({
  version: z.literal(1).default(1), holding_wallet: z.string().min(40).max(64),
  records: z.array(fundingRecordSchema).min(1).max(100), reason: z.string().min(8).max(240),
})
export const fundingSubmitSchema = z.object({
  batch: fundingBatchSchema,
  confirmation_digest: z.string().regex(/^[0-9a-f]{64}$/),
  confirmation: z.string(),
}).refine(v => v.confirmation === `RECORD GM FUNDING HISTORY ${v.confirmation_digest}`, {
  message: 'Exact funding reconciliation confirmation required',
})
export const fundingPreviewSchema = z.object({
  confirmation_digest: z.string().regex(/^[0-9a-f]{64}$/), confirmation: z.string(),
  purchases: z.number().int(), credited_usd_cents: z.number().int(),
  verification_level: z.literal('operator_reconciled'), spending_authority: z.literal('none'),
})
export const fundingResultSchema = fundingPreviewSchema.extend({ inserted: z.number().int(), replayed: z.number().int() })
export const fundingHistorySchema = z.object({
  items: z.array(z.object({
    holding_wallet: z.string(), record: fundingRecordSchema, actor: z.string(), recorded_at: z.string(),
    verification_level: z.literal('operator_reconciled'),
  })).max(500),
  truncated: z.boolean(), totals_scope: z.literal('returned_records'),
  credited_usd_cents: z.number().int(), tao_paid_rao: z.string().regex(/^[0-9]+$/),
  distinct_conversions: z.number().int(), automatic_topups_enabled: z.literal(false),
  spending_authority: z.literal('none'),
})
export type FundingHistory = z.infer<typeof fundingHistorySchema>
export type FundingMovement = z.infer<typeof fundingMovementSchema>

export function fundingAmount(value: string) {
  const amount = BigInt(value)
  const fraction = (amount % 1_000_000_000n).toString().padStart(9, '0').replace(/0+$/, '')
  return `${amount / 1_000_000_000n}${fraction ? `.${fraction}` : ''}`
}
