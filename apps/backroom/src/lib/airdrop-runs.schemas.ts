import { z } from 'zod'

// SN118 airdrop runs (SN-490), validated at the Platform boundary. These are
// Platform-owned run ledgers settled by Platform executors. Ditto product
// credit airdrops by email stay in the private product Backroom.

const integer = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER)
const positive = z.number().int().positive().max(Number.MAX_SAFE_INTEGER)
const digest = z.string().regex(/^[0-9a-f]{64}$/)
const timestamp = z.string()

export const airdropAssetSchema = z.enum(['alpha', 'ditto_credits'])
export const airdropModeSchema = z.enum(['rehearsal', 'live'])
const recipientKindSchema = z.enum(['wallet', 'account'])

export const AIRDROP_IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/
export const AIRDROP_TOOL_MAX_RECIPIENTS = 1000

export const airdropRecipientInputSchema = z.object({
  kind: recipientKindSchema,
  address: z.string().trim().min(1).max(64),
  amount: positive,
}).strict()

export const airdropPreviewInputSchema = z.object({
  asset: airdropAssetSchema,
  mode: airdropModeSchema.default('rehearsal'),
  idempotency_key: z.string().regex(AIRDROP_IDEMPOTENCY_KEY),
  reason: z.string().trim().min(8),
  recipients: z.array(airdropRecipientInputSchema).min(1).max(AIRDROP_TOOL_MAX_RECIPIENTS),
}).strict()

export const airdropExecuteInputSchema = airdropPreviewInputSchema.extend({
  dry_run: z.boolean().default(true),
  confirmation_digest: digest.optional(),
  confirmation: z.string().max(200).optional(),
}).strict().superRefine((data, ctx) => {
  if (data.dry_run) return
  if (!data.confirmation_digest) {
    ctx.addIssue({ code: 'custom', path: ['confirmation_digest'], message: 'Pass the preview confirmation_digest to execute' })
  }
  if (!data.confirmation?.startsWith('AIRDROP ')) {
    ctx.addIssue({ code: 'custom', path: ['confirmation'], message: 'Type the exact AIRDROP confirmation phrase from the preview' })
  }
})

const previewRecipientSchema = z.object({
  position: integer,
  kind: recipientKindSchema,
  address: z.string(),
  amount: positive,
  payee: z.string().nullable(),
  recipient_key: digest.nullable(),
  refused: z.string().nullable(),
  prior_amount: integer,
})

export const airdropPreviewSchema = z.object({
  dry_run: z.literal(true),
  asset: airdropAssetSchema,
  unit: z.string(),
  mode: airdropModeSchema,
  idempotency_key: z.string(),
  reason: z.string(),
  executor: z.string().nullable(),
  recipients: z.array(previewRecipientSchema),
  recipient_count: integer,
  refused_count: integer,
  total_amount: integer,
  cap: z.object({
    max_recipients: integer,
    max_total_amount: integer,
    recipient_count: integer,
    total_amount: integer,
    within_cap: z.boolean(),
  }),
  balance: z.object({
    status: z.enum(['sufficient', 'insufficient', 'unavailable']),
    source: z.string().nullable(),
    available: integer.nullable(),
    required: integer,
    detail: z.string(),
  }),
  blockers: z.array(z.string()),
  ready: z.boolean(),
  existing_run_id: z.string().uuid().nullable(),
  existing_run_matches: z.boolean().nullable(),
  warnings: z.array(z.string()),
  confirmation_digest: digest,
  confirmation: z.string(),
  spending_authority: z.literal('none'),
})

const runSummaryShape = {
  run_id: z.string().uuid(),
  idempotency_key: z.string(),
  asset: airdropAssetSchema,
  unit: z.string(),
  mode: airdropModeSchema,
  executor: z.string(),
  reason: z.string(),
  actor: z.string(),
  digest,
  created_at: timestamp,
  completed_at: timestamp.nullable(),
  status: z.enum(['in_progress', 'settled', 'needs_attention']),
  stale_dispatching: integer,
  recipient_count: integer,
  total_amount: integer,
  settled_amount: integer,
  counts: z.record(z.string(), integer),
}

export const airdropRunSummarySchema = z.object(runSummaryShape)

export const airdropRunReportSchema = z.object({
  ...runSummaryShape,
  recipients: z.array(z.object({
    position: integer,
    kind: recipientKindSchema,
    address: z.string(),
    payee: z.string(),
    amount: positive,
    recipient_key: digest,
    status: z.enum(['pending', 'dispatching', 'settled', 'failed', 'uncertain']),
    receipt: z.string().nullable(),
    error: z.string().nullable(),
    attempted_at: timestamp.nullable(),
    settled_at: timestamp.nullable(),
  })),
  resumed: z.boolean(),
})

export const airdropExecuteResultSchema = z.object({
  dry_run: z.boolean(),
  preview: airdropPreviewSchema,
  run: airdropRunReportSchema.nullable(),
})

export const airdropRunListSchema = z.object({
  policy: z.object({
    max_recipients: integer,
    assets: z.array(z.object({
      asset: airdropAssetSchema,
      unit: z.string(),
      recipient_kind: recipientKindSchema,
      max_total_amount: integer,
      rehearsal_executor: z.string().nullable(),
      live_executor: z.string().nullable(),
      funding_source: z.string().nullable(),
    })),
  }),
  runs: z.array(airdropRunSummarySchema),
})

export const airdropRunListInputSchema = z.object({
  limit: z.number().int().min(1).max(100).default(20),
  offset: z.number().int().min(0).max(10_000).default(0),
  idempotency_key: z.string().regex(AIRDROP_IDEMPOTENCY_KEY).optional(),
  asset: airdropAssetSchema.optional(),
  mode: airdropModeSchema.optional(),
}).strict()

export type AirdropPreviewInput = z.input<typeof airdropPreviewInputSchema>
export type AirdropExecuteInput = z.input<typeof airdropExecuteInputSchema>
export type AirdropRunListInput = z.input<typeof airdropRunListInputSchema>
