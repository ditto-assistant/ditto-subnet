import { z } from 'zod'

/** Exact rao decimal string; Platform never sends rao as a JSON number. */
const rao = z.string().regex(/^-?[0-9]+$/)

const holding = z.object({
  bucket_id: z.string(),
  coldkey: z.string(),
  alpha_total_rao: rao,
  tao_free_rao: rao,
})

const current = z.object({
  epoch_index: z.number().int(),
  block_number: z.number().int(),
  block_hash: z.string(),
  block_at: z.string(),
  collector_hotkey: z.string(),
  collector_coldkey: z.string(),
  collector_uid: z.number().int().nullable(),
  collector_incentive: z.number().nullable(),
  earned_per_epoch_rao: rao.nullable(),
  total_rao: rao,
  locked_rao: rao,
  available_rao: rao,
  lock_status: z.enum(['none', 'perpetual', 'decaying']),
  owner_hotkey: z.string().nullable(),
  owner_conviction_rao: rao.nullable(),
  owner_holds_most_conviction: z.boolean().nullable(),
  holdings: z.array(holding),
})

const interval = z.object({
  epoch_index: z.number().int(),
  previous_epoch_index: z.number().int(),
  block_number: z.number().int(),
  block_at: z.string(),
  earned_rao: rao.nullable(),
  estimated: z.boolean(),
  balance_change_rao: rao,
  outflow_rao: rao,
  attributed_rao: rao,
  unattributed_rao: rao,
  receipt_count: z.number().int(),
  receipt_ids: z.array(z.number().int()),
})

export const treasuryGammaLedgerSchema = z.object({
  current: current.nullable(),
  totals: z.object({
    window_epochs: z.number().int(),
    earned_rao: rao,
    outflow_rao: rao,
    attributed_rao: rao,
    unattributed_rao: rao,
  }),
  intervals: z.array(interval),
  sampler: z.object({
    enabled: z.boolean(),
    state: z.enum(['running', 'not_configured', 'blocked', 'stopped']),
    last_sampled_at: z.string().nullable().optional(),
    last_checked_at: z.string().nullable().optional(),
    last_error: z.string().nullable().optional(),
  }),
})
