import { z } from 'zod'

const hash = z.string().regex(/^0x[0-9a-f]{64}$/)
const digest = z.string().regex(/^[0-9a-f]{64}$/)
const id = z.string().regex(/^[a-z][a-z0-9_]{1,47}$/)
const uint = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER)

export const treasuryReceiptInputSchema = z.object({
  stage: z.enum(['service_distribution', 'vendor_payment', 'provider_credit']),
  epoch_index: uint,
  bucket_id: id,
  source_block: uint.min(1).nullable().default(null),
  block: uint.min(1),
  block_hash: hash,
  extrinsic_index: uint,
  extrinsic_hash: hash,
  amount_atomic: uint.min(1),
  payee_rule_id: id.nullable().default(null),
  parent_receipt_id: digest.nullable().default(null),
  reason: z.string().trim().min(8).max(240),
  confirmation: z.literal('INGEST VERIFIED TREASURY RECEIPT'),
})

export const treasuryReceiptSchema = z.object({
  receipt_id: digest,
  epoch_index: uint,
  bucket_id: id,
  policy_digest: digest,
  source_block: uint.nullable(),
  block: uint.min(1),
  block_hash: hash,
  extrinsic_index: uint,
  extrinsic_hash: hash,
  amount_atomic: z.string().regex(/^[1-9][0-9]*$/),
  stage: z.enum(['service_distribution', 'vendor_payment']),
  status: z.literal('chain_finalized'),
  public_event_id: uint.nullable(),
  published: z.boolean(),
  replayed: z.boolean(),
  provider_credit_status: z.literal('not_proven'),
})

export const treasuryReceiptPageSchema = z.object({ items: z.array(treasuryReceiptSchema).max(100) })


export const treasuryReceiptPreflightInputSchema = treasuryReceiptInputSchema.omit({ confirmation: true })
export const treasuryReceiptPreflightSchema = z.object({
  ready: z.boolean(), receipt_id: digest.nullable(), already_recorded: z.boolean(),
  refusal: z.enum(['unavailable', 'invalid_or_unsupported', 'conflict']).nullable(),
  read_phase: z.enum(['connection', 'pinned_finality', 'payment_finality', 'pinned_identity',
    'source_finality', 'receipt_identity', 'source_epoch', 'autostake_route', 'source_events',
    'payment_extrinsic', 'payment_events', 'timestamp', 'connection_close']).nullable(),
  attempt_count: uint, timed_out: z.boolean(), publication: z.literal('not_performed'),
  spending_authority: z.literal('none'), provider_credit_status: z.literal('not_proven'),
})
