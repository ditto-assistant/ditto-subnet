import { z } from 'zod'

// Mirrors Platform api_models/runtime_interface.py. Read-only upgrade report:
// it changes no admission state and is never a treasury signing approval.
export const runtimeSurfaceVerdictSchema = z.object({
  surface: z.enum(['source_emission', 'treasury_receipt']),
  admitted_by: z.enum(['audited_artifact', 'interface']).nullable(),
  interface_digest: z.string().nullable(),
  nearest_audited_release: z.string().nullable(),
  changed_entries: z.array(z.string()),
  error: z.string().nullish().default(null),
})

export const treasurySignerRuntimeViewSchema = z.object({
  approved_runtime_code_hash: z.string(),
  signs_on_this_runtime: z.boolean(),
  call_baseline_release: z.string(),
  changed_calls: z.array(z.string()),
  changed_calls_truncated: z.boolean(),
  // Set when call metadata is unreadable; changed_calls is then not evidence.
  calls_error: z.string().nullish().default(null),
})

export const runtimeInterfaceReportSchema = z.object({
  block: z.number().int().positive(),
  block_hash: z.string(),
  runtime_code_hash: z.string(),
  spec_version: z.number().int().nonnegative(),
  surfaces: z.array(runtimeSurfaceVerdictSchema),
  treasury_signer: treasurySignerRuntimeViewSchema,
})

export const runtimeInterfaceReportInputSchema = {
  block: z.number().int().positive().optional(),
}
