import { z } from 'zod'

const digest = z.string().regex(/^[0-9a-f]{64}$/)
export const listBenchmarkCanariesInputSchema = z.object({
  limit: z.number().int().min(1).max(50).default(20),
  offset: z.number().int().min(0).default(0),
})
export const issueBenchmarkCanaryInputSchema = z.object({
  canaryId: z.uuid(),
  agentId: z.uuid(),
  benchVersion: z.number().int().positive(),
  validatorHotkey: z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{48}$/),
  slotId: z.string().regex(/^slot-[0-7]$/),
  expectedArtifactSha256: digest,
  expectedScreenedImageSha256: digest,
  expectedActiveVersion: z.number().int().positive(),
  reason: z.string().trim().min(8),
  confirmation: z.string(),
}).superRefine((input, ctx) => {
  if (input.confirmation !== `ISSUE CANARY V${input.benchVersion} ${input.agentId}`) {
    ctx.addIssue({ code: 'custom', path: ['confirmation'], message: 'Exact canary confirmation required' })
  }
})

export const getBenchmarkCanaryInputSchema = z.object({ canaryId: z.uuid() })
export const cancelBenchmarkCanaryInputSchema = getBenchmarkCanaryInputSchema.extend({
  reason: z.string().trim().min(8),
  confirmation: z.string(),
}).superRefine((input, ctx) => {
  if (input.confirmation !== `CANCEL CANARY ${input.canaryId}`) {
    ctx.addIssue({ code: 'custom', path: ['confirmation'], message: 'Exact cancellation confirmation required' })
  }
})

// Never forward arbitrary scorer details, source, traces or capabilities through
// a read-scoped tool. This is a summary of non-authoritative reported results.
export const benchmarkCanarySchema = z.object({
  canary_id: z.uuid(), agent_id: z.uuid(), bench_version: z.number().int(),
  validator_hotkey: z.string(), slot_id: z.string(), artifact_sha256: digest,
  screened_image_sha256: digest, dataset_sha256: digest,
  seed: z.string().regex(/^\d+$/), run_size: z.string(), actor: z.string(), reason: z.string(),
  issued_at: z.string(), deadline: z.string(), status: z.string(),
  finished_at: z.string().nullable(), failure_detail: z.string().nullable(),
  authoritative: z.literal(false),
  result: z.object({
    run_id: z.string(), bench_version: z.number().int().nullable().optional(),
    composite: z.number(), tool_mean: z.number(), memory_mean: z.number(),
    n: z.number().int(), generated_at: z.string(),
  }).nullable(),
})
