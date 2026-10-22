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
const manualLimitApprovalSchema = z.object({
  policy: z.object({
    version: z.literal(1), scope: z.literal('manual_once'),
    collector_policy_digest: digest, collector_coldkey: address,
    revision: integer, min_amount_rao: z.literal(1),
    max_amount_rao: integer.max(2_500_000_000_000), max_sources: z.literal(512),
  }),
  signature: z.string().regex(/^0x[0-9a-f]{128}$/),
})
const batchRequestSchema = manualRequestSchema.extend({
  sources: z.array(z.object({ source_block: integer, amount_rao: integer })).min(1).max(512),
}).superRefine((data, ctx) => {
  if (data.sources[0]?.source_block !== data.source_block ||
      data.sources.some((s, i) => i > 0 && s.source_block <= data.sources[i - 1].source_block) ||
      data.sources.reduce((sum, s) => sum + BigInt(s.amount_rao), 0n) !== BigInt(data.amount_rao)) {
    ctx.addIssue({ code: 'custom', message: 'Exact ordered source amounts must conserve the transfer' })
  }
})
export const manualEnvelopeSchema = z.discriminatedUnion('version', [
  z.object({ version: z.literal(1), collector_policy_digest: digest, destination: address, request: manualRequestSchema }),
  z.object({ version: z.literal(2), collector_policy_digest: digest, destination: address, request: batchRequestSchema, manual_approval: manualLimitApprovalSchema }),
])
export const manualPreviewInputSchema = manualRequestSchema.pick({
  request_id: true, bucket_id: true, amount_rao: true, retained_alpha_rao: true, reason: true,
})
export const manualPreviewSchema = z.object({
  envelope: manualEnvelopeSchema, confirmation_digest: digest,
  spending_authority: z.literal('not_queued'),
})
export const manualSubmitSchema = z.object({
  envelope: manualEnvelopeSchema, confirmation_digest: digest,
  confirmation: z.string().max(200),
}).superRefine((data, ctx) => {
  if (data.confirmation !== manualTransferConfirmation(data.envelope)) {
    ctx.addIssue({ code: 'custom', path: ['confirmation'], message: 'Type the exact transfer amount and destination confirmation' })
  }
})
export const manualTransferSchema = z.object({
  request_id: z.string().uuid(), envelope: manualEnvelopeSchema,
  status: z.enum(['queued', 'dispatched', 'pending', 'audit_pending', 'published', 'failed', 'refused']),
  actor: z.string(), created_at: z.string(), updated_at: z.string(),
  last_error: z.string().nullable(),
  receipt: z.object({ receipt_id: digest, extrinsic_hash: z.string(), published: z.boolean(), source_attribution: z.literal("incomplete").optional() }).nullable(),
})
export const manualControlSchema = z.object({
  enabled: z.boolean(), blocked_reason: z.string().nullable(), bridge_error: z.string().nullable(),
  recurring_enabled: z.literal(false),
  readiness: z.object({
    policy: digest, after_operation: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER), previous_state: z.string().nullable(),
    bounded_claim_available: z.boolean(), finalized_block: integer,
    available_alpha_rao: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
    max_distribution_rao: integer,
    manual_approval: manualLimitApprovalSchema.nullish(),
    sources: z.array(z.object({ source_block: integer, remaining: z.array(z.object({
      bucket_id: bucket, holding_coldkey: address, alpha_rao: integer,
    })).max(20) })).max(512),
  }).nullable(),
  destinations: z.array(z.object({
    bucket_id: bucket, holding_coldkey: address, allocation_bps: z.number().int().min(0).max(10000),
    min_allocation_bps: z.number().int().min(0).max(10000).nullish(),
    max_allocation_bps: z.number().int().min(0).max(10000).nullish(),
  })).max(20),
  requests: z.array(manualTransferSchema).max(20),
})

// Platform accepts a ranged bucket whenever its signed ceiling is positive.
export const fundable = (d: { allocation_bps: number, max_allocation_bps?: number | null }) =>
  (d.max_allocation_bps ?? d.allocation_bps) > 0

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

export function manualRequestId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function manualTransferConfirmation(envelope: z.infer<typeof manualEnvelopeSchema>): string {
  return `TRANSFER ${alphaDisplay(envelope.request.amount_rao)} ALPHA TO ${envelope.request.bucket_id.toUpperCase()} ${envelope.destination}`
}

// Preview still rechecks on Platform and custody; this is a display helper.
export function manualMaximum(state: z.infer<typeof manualControlSchema>, bucketId: string, reserve: number): number {
  const readiness = state.readiness
  const destination = state.destinations.find(d => d.bucket_id === bucketId && fundable(d))
  if (!readiness || !destination || reserve <= 0) return 0
  const parts = readiness.sources.flatMap(s => s.remaining.filter(p =>
    p.bucket_id === bucketId && p.holding_coldkey === destination.holding_coldkey))
  const entitlement = readiness.manual_approval
    ? parts.reduce((sum, p) => sum + BigInt(p.alpha_rao), 0n)
    : parts.reduce((max, p) => BigInt(p.alpha_rao) > max ? BigInt(p.alpha_rao) : max, 0n)
  const cap = BigInt(readiness.manual_approval?.policy.max_amount_rao ?? readiness.max_distribution_rao)
  const available = BigInt(readiness.available_alpha_rao) - BigInt(reserve)
  if (available <= 0n) return 0
  return Number([entitlement, cap, available].reduce((min, n) => n < min ? n : min, available))
}
