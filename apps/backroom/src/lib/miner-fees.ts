import { z } from 'zod'

export const minerFeeSummarySchema = z.object({
  generated_at: z.string().datetime({ offset: true }),
  payment_address: z.string().min(1),
  paid_submissions: z.number().int().nonnegative(),
  gross_amount_rao: z.number().int().nonnegative(),
  priced_submissions: z.number().int().nonnegative(),
  unpriced_submissions: z.number().int().nonnegative(),
  gross_value_usd: z.coerce.number().nonnegative(),
  unique_paying_coldkeys: z.number().int().nonnegative(),
  first_payment_at: z.string().datetime({ offset: true }).nullable(),
  last_payment_at: z.string().datetime({ offset: true }).nullable(),
  recent_days: z.array(
    z.object({
      date: z.string(),
      paid_submissions: z.number().int().nonnegative(),
      gross_amount_rao: z.number().int().nonnegative(),
      priced_submissions: z.number().int().nonnegative(),
      gross_value_usd: z.coerce.number().nonnegative(),
    }),
  ),
})

export type MinerFeeSummary = z.infer<typeof minerFeeSummarySchema>
