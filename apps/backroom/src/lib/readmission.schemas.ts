import { z } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'

// SN-485 readmission activation, read-only. Platform resolves it from the
// submission settings revision's readmission_announced_at and the live active
// benchmark: active only when the announcement is set, the active benchmark
// is v19 or later and seven days have passed since the announcement. The
// price is always the upload default fee. Backroom never writes the
// announcement; that is a Platform admin API write.

type PlatformResponseShape<Response extends object> = {
  [Field in keyof Response]-?: z.ZodType<Response[Field]>
}

type GeneratedReadmissionActivationState =
  PlatformComponents['schemas']['ReadmissionActivationState']

export const READMISSION_INACTIVE_REASONS = [
  'announcement_unset',
  'bench_version_below_minimum',
  'notice_period',
] as const

export const readmissionActivationSchema = z.object({
  active: z.boolean(),
  inactive_reasons: z.array(z.enum(READMISSION_INACTIVE_REASONS)).max(8).default([]),
  announced_at: z.string().nullable().default(null),
  effective_at: z.string().nullable().default(null),
  active_bench_version: z.number().int().nonnegative(),
  min_bench_version: z.number().int().nonnegative(),
  notice_period_seconds: z.number().int().nonnegative(),
  fee_amount_rao: z.number().int().positive(),
  fee_source: z.literal('upload_default').default('upload_default'),
  settings_revision: z.number().int().nonnegative(),
} satisfies PlatformResponseShape<GeneratedReadmissionActivationState>)

export type ReadmissionActivation = z.infer<typeof readmissionActivationSchema>
