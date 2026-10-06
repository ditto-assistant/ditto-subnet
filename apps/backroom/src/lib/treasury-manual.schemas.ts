import { z } from 'zod'

const integer = z.number().int().positive().max(Number.MAX_SAFE_INTEGER)
const digest = z.string().regex(/^[0-9a-f]{64}$/)
const bucket = z.string().regex(/^[a-z][a-z0-9_]{1,47}$/)
const address = z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{47,48}$/)
export const manualRequestSchema = z.object({
  request_id: z.string().uuid(), after_operation: integer, source_block: integer,
  bucket_id: bucket, amount_rao: integer, retained_alpha_rao: integer,
  expires_block: integer, reason: z.string().trim().min(8).max(240),
})
export const manualEnvelopeSchema = z.object({
  version: z.literal(1), collector_policy_digest: digest, destination: address,
  request: manualRequestSchema,
})
export const manualPreviewInputSchema = manualRequestSchema.pick({
  request_id: true, bucket_id: true, amount_rao: true, retained_alpha_rao: true, reason: true,
})
export const manualPreviewSchema = z.object({
  envelope: manualEnvelopeSchema, confirmation_digest: digest,
  spending_authority: z.literal('not_queued'),
})
export const manualSubmitSchema = z.object({
  envelope: manualEnvelopeSchema, confirmation_digest: digest,
  confirmation: z.literal('TRANSFER SN118 ALPHA ONCE'),
})
export const manualTransferSchema = z.object({
  request_id: z.string().uuid(), envelope: manualEnvelopeSchema,
  status: z.enum(['queued', 'dispatched', 'pending', 'audit_pending', 'published', 'failed', 'refused']),
  actor: z.string(), created_at: z.string(), updated_at: z.string(),
  last_error: z.string().nullable(),
  receipt: z.object({ receipt_id: digest, extrinsic_hash: z.string(), published: z.boolean() }).nullable(),
})
export const manualControlSchema = z.object({
  enabled: z.boolean(), blocked_reason: z.string().nullable(), bridge_error: z.string().nullable(),
  recurring_enabled: z.literal(false),
  readiness: z.object({
    policy: digest, after_operation: integer, previous_state: z.string().nullable(),
    bounded_claim_available: z.boolean(), finalized_block: integer,
    available_alpha_rao: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
    max_distribution_rao: integer,
    sources: z.array(z.object({ source_block: integer, remaining: z.array(z.object({
      bucket_id: bucket, holding_coldkey: address, alpha_rao: integer,
    })).max(20) })).max(100),
  }).nullable(),
  destinations: z.array(z.object({ bucket_id: bucket, holding_coldkey: address, allocation_bps: z.number().int().min(0).max(1000) })).max(20),
  requests: z.array(manualTransferSchema).max(20),
})

export function alphaRao(value: string): number | null {
  if (!/^(0|[1-9]\d*)(\.\d{1,9})?$/.test(value)) return null
  const [whole, fraction = ''] = value.split('.')
  const amount = BigInt(whole) * 1_000_000_000n + BigInt(fraction.padEnd(9, '0'))
  return amount > 0n && amount <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(amount) : null
}
export function alphaDisplay(value: number): string {
  const amount = BigInt(value)
  const fraction = (amount % 1_000_000_000n).toString().padStart(9, '0').replace(/0+$/, '')
  return `${amount / 1_000_000_000n}${fraction ? `.${fraction}` : ''}`
}
