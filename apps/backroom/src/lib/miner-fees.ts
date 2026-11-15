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
  // SN-485 readmission lane. The totals above are the upload lane; add
  // readmission.admitted_amount_rao for the whole. Credited readmissions and
  // classified uploads are already above. Null: Platform predates it.
  readmission: z
    .object({
      admitted: z.number().int().nonnegative(),
      admitted_amount_rao: z.number().int().nonnegative(),
      credit_funded: z.number().int().nonnegative(),
      credited: z.number().int().nonnegative(),
      credited_amount_rao: z.number().int().nonnegative(),
      upload_readmissions: z.number().int().nonnegative(),
      upload_readmission_amount_rao: z.number().int().nonnegative(),
    })
    .nullable()
    .default(null),
})

export type MinerFeeSummary = z.infer<typeof minerFeeSummarySchema>
