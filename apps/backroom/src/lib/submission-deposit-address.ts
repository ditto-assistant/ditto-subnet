import { z } from 'zod'

export const SS58_ADDRESS_PATTERN = /^[1-9A-HJ-NP-Za-km-z]{32,64}$/

export const submissionDepositAddressRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  payment_address: z.string().regex(SS58_ADDRESS_PATTERN),
  reason: z.string(),
  actor: z.string(),
  created_at: z.string().datetime({ offset: true }).nullable(),
})

export const submissionDepositAddressControlSchema = z.object({
  current: submissionDepositAddressRevisionSchema,
  history: z.array(submissionDepositAddressRevisionSchema).max(100),
})

export const updateSubmissionDepositAddressInputSchema = z.object({
  expectedRevision: z.number().int().nonnegative(),
  paymentAddress: z.string().trim().regex(SS58_ADDRESS_PATTERN),
  reason: z.string().trim().min(8),
  confirmation: z.string(),
})

export function submissionDepositAddressConfirmation(address: string) {
  return `SET SUBMISSION DEPOSIT ADDRESS ${address}`
}

export type SubmissionDepositAddressControl = z.infer<
  typeof submissionDepositAddressControlSchema
>
