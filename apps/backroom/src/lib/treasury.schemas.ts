import { z } from 'zod'

export const treasurySettingsSchema = z.object({
  mode: z.literal('shadow'),
  maintenance_bps: z.number().int().min(0).max(500),
  gm_bps: z.number().int().min(0).max(500),
  treasury_hotkey: z.string().nullable(),
  treasury_coldkey: z.string().nullable(),
  gm_account_ref: z.string().nullable(),
  max_daily_outflow_rao: z.number().int().nonnegative(),
  max_single_topup_rao: z.number().int().nonnegative(),
  max_slippage_bps: z.number().int().min(0).max(500),
}).superRefine((value, context) => {
  if (value.maintenance_bps + value.gm_bps > 500) {
    context.addIssue({ code: 'custom', message: 'combined allocation exceeds 500 bps' })
  }
  if ((value.maintenance_bps || value.gm_bps) &&
      (!value.treasury_hotkey || !value.treasury_coldkey)) {
    context.addIssue({ code: 'custom', message: 'treasury keys are required' })
  }
  if (value.gm_bps && !value.gm_account_ref) {
    context.addIssue({ code: 'custom', message: 'GM account reference is required' })
  }
  if (value.max_single_topup_rao > value.max_daily_outflow_rao) {
    context.addIssue({ code: 'custom', message: 'single top-up exceeds daily limit' })
  }
})

export const treasuryRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  settings: treasurySettingsSchema,
  checksum: z.string().regex(/^[0-9a-f]{64}$/),
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
})

export const treasuryControlSchema = z.object({
  effective: treasurySettingsSchema,
  revision: z.number().int().nonnegative(),
  miner_bps: z.number().int().min(9500).max(10000),
  history: z.array(treasuryRevisionSchema),
  weight_effect: z.literal('none'),
})

export const recordTreasurySettingsInputSchema = z.object({
  expectedRevision: z.number().int().nonnegative(),
  settings: treasurySettingsSchema,
  reason: z.string().trim().min(8),
  confirmation: z.literal('RECORD TREASURY SHADOW POLICY'),
})

export const treasuryQuoteInputSchema = z.object({
  sourceAlphaRao: z.number().int().positive().max(10_000_000_000),
})

export const treasuryPreviewInputSchema = treasuryQuoteInputSchema.extend({
  route: z.enum(['tao', 'gm_alpha']),
})

export const treasuryQuoteSchema = z.object({
  block: z.number().int().nonnegative(),
  block_hash: z.string().regex(/^0x[0-9a-f]{64}$/),
  source_alpha_rao: z.number().int().positive(),
  tao_path: z.object({
    deposit_asset: z.literal('TAO'),
    amount_rao: z.number().int().nonnegative(),
    price_impact_bps: z.number().int().nonnegative(),
  }),
  gm_alpha_path: z.object({
    deposit_asset: z.literal('SN28_ALPHA'),
    amount_rao: z.number().int().nonnegative(),
    price_impact_bps: z.number().int().nonnegative(),
  }),
  gm_credit_usd: z.null(),
  execution_enabled: z.literal(false),
  settlement: z.string(),
})

// Each path's price_impact_bps is already cumulative for that route: Platform
// compounds both swaps into gm_alpha_path, so adding tao_path would count the
// DITTO-to-TAO hop twice.
export function treasuryRouteImpactBps(
  route: z.infer<typeof treasuryPreviewInputSchema>['route'],
  quote: z.infer<typeof treasuryQuoteSchema>,
): number {
  return route === 'tao'
    ? quote.tao_path.price_impact_bps
    : quote.gm_alpha_path.price_impact_bps
}
