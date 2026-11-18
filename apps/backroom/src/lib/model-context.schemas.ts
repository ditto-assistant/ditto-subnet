import { z } from 'zod'
import { auditReasonSchema, seedSchema } from './admin.schemas'

// SN118 model_context (CPM) competition, contract docs/research/cpm/
// competition-v1.md sections 5 and 8. Miners submit small open-weight models
// (a LoRA or full fine-tune of an allowlisted Qwen3.5 base) pinned at a
// Hugging Face revision; Platform screens them, opens weekly seasons with
// three public seeds from the season-open block hash, leases runs to the CPM
// scorer, and serves a shadow ledger. These schemas mirror
// apps/platform/ditto/api_models/model_context.py. Reads strip unknown
// fields; Platform stays the authority on every bound and refusal.

const uuid = z.string().uuid()
const ss58 = z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{47,48}$/)
const revision = z.string().regex(/^[0-9a-f]{40}$/)
const baseId = z.string().regex(/^[a-z0-9][a-z0-9._-]{0,63}$/)
const hfRepo = z
  .string()
  .regex(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$/)
const GIB = 1024 ** 3

export const MODEL_CONTEXT_MODES = ['off', 'shadow', 'enforce'] as const
export const MODEL_CONTEXT_SUBMISSION_STATUSES = [
  'screening', 'queued', 'scored', 'rejected', 'superseded',
] as const
const RUN_STATUSES = ['queued', 'leased', 'scored', 'failed', 'abandoned'] as const

export const ROTATE_MODEL_CONTEXT_SEASON_CONFIRMATION = 'ROTATE MODEL CONTEXT SEASON'
export const RESCORE_MODEL_CONTEXT_CONFIRMATION = 'RESCORE MODEL CONTEXT SUBMISSION'
export const REJECT_MODEL_CONTEXT_CONFIRMATION = 'REJECT MODEL CONTEXT SUBMISSION'

// Platform model_context_settings_confirmation.
export function modelContextSettingsConfirmation(mode: string) {
  return `APPLY MODEL CONTEXT COMPETITION ${mode.toUpperCase()}`
}

const modelContextBaseSchema = z.object({
  base_id: baseId,
  hf_repo: hfRepo,
  revision,
  parameters: z.number().int().min(1),
  architectures: z.array(z.string()).min(1).max(4),
  lora_target_modules: z.array(z.string()).min(1).max(32),
})

// Required, never defaulted: a revision stores the whole policy, and
// Platform refuses a write that omits any field.
export const modelContextSettingsSchema = z.object({
  mode: z.enum(MODEL_CONTEXT_MODES),
  base_allowlist: z.array(modelContextBaseSchema).min(1).max(8),
  max_lora_rank: z.number().int().min(1).max(256),
  max_total_bytes: z.number().int().min(1 << 20).max(16 * GIB),
  upload_cooldown_seconds: z.number().int().min(60).max(7 * 86_400),
  // The fee path is not built in v1: Platform refuses true (422).
  upload_fee_required: z.boolean(),
  season_days: z.number().int().min(1).max(60),
  floor_margin: z.number().min(0).max(1),
  budget_tokens: z.number().int().min(128).max(65_536),
  tool_k: z.number().int().min(1).max(128),
  rank_shares: z.array(z.number().min(0).max(1)).min(1).max(16),
  bench_version: z.number().int().min(1).max(999),
  lease_ttl_seconds: z.number().int().min(600).max(86_400),
  max_attempts: z.number().int().min(1).max(20),
})

export const modelContextSettingsRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: modelContextSettingsSchema,
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  checksum: z.string(),
  settings_valid: z.boolean().optional(),
})

export const modelContextSettingsControlSchema = z.object({
  current: z.array(modelContextSettingsRevisionSchema),
  history: z.array(modelContextSettingsRevisionSchema),
  default: modelContextSettingsSchema,
  effective: z.object({
    revision: z.number().int().nonnegative(),
    settings: modelContextSettingsSchema,
    checksum: z.string(),
    source: z.enum(['revision', 'default']),
    settings_valid: z.boolean().optional(),
  }),
})

export const setModelContextSettingsInputSchema = z
  .object({
    expectedRevision: z.number().int().nonnegative(),
    settings: modelContextSettingsSchema,
    reason: auditReasonSchema(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = modelContextSettingsConfirmation(input.settings.mode)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}, naming the mode this revision applies`,
        path: ['confirmation'],
      })
    }
    if (input.settings.upload_fee_required) {
      context.addIssue({
        code: 'custom',
        message: 'upload_fee_required must be false: the model_context fee path is not built in v1',
        path: ['settings', 'upload_fee_required'],
      })
    }
    const shares = input.settings.rank_shares
    if (shares.reduce((total, share) => total + share, 0) > 1 + 1e-9) {
      context.addIssue({
        code: 'custom',
        message: 'rank_shares must sum to at most 1',
        path: ['settings', 'rank_shares'],
      })
    }
    if (shares.some((share, index) => index > 0 && share > shares[index - 1])) {
      context.addIssue({
        code: 'custom',
        message: 'rank_shares must not increase with rank',
        path: ['settings', 'rank_shares'],
      })
    }
    const ids = input.settings.base_allowlist.map((base) => base.base_id)
    if (new Set(ids).size !== ids.length) {
      context.addIssue({
        code: 'custom',
        message: 'base_allowlist repeats a base_id',
        path: ['settings', 'base_allowlist'],
      })
    }
  })

const runSummarySchema = z.object({
  run_id: uuid,
  season: z.number().int(),
  cpm_score_version: z.number().int(),
  status: z.enum(RUN_STATUSES),
  score: z.number().nullable(),
  fault: z.string().nullable(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
})

const submissionSummarySchema = z.object({
  submission_id: uuid,
  hotkey: z.string(),
  hf_repo: z.string(),
  revision: z.string(),
  manifest_sha256: z.string(),
  weights_sha256: z.string().nullable(),
  base_id: z.string(),
  kind: z.enum(['lora', 'full']),
  commitment_block: z.number().int(),
  status: z.enum(MODEL_CONTEXT_SUBMISSION_STATUSES),
  reject_reason: z.string().nullable(),
  mirror_repo: z.string().nullable(),
  model_url: z.string(),
  created_at: z.string(),
  latest_run: runSummarySchema.nullable().optional().default(null),
})

// One run as Platform stores it. Season seeds are public by contract, so
// per_seed rows (with their uint64 seed, parsed losslessly) pass through.
const runViewSchema = z.object({
  run_id: uuid,
  kind: z.enum(['submission', 'reference']),
  submission_id: uuid.nullable(),
  base_id: z.string(),
  season: z.number().int(),
  cpm_score_version: z.number().int(),
  status: z.enum(RUN_STATUSES),
  score: z.number().nullable(),
  components: z.record(z.string(), z.unknown()).nullable(),
  per_seed: z.array(z.record(z.string(), z.unknown())).nullable(),
  scorer_id: z.string().nullable(),
  scorer_version: z.string().nullable(),
  gpu_sku: z.string().nullable(),
  attempts: z.number().int(),
  lease_expires_at: z.string().nullable(),
  created_at: z.string(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  fault: z.string().nullable(),
})

const operatorActionSchema = z.object({
  id: z.number().int(),
  action: z.string(),
  subject: z.string(),
  actor: z.string(),
  reason: z.string(),
  detail: z.record(z.string(), z.unknown()).nullable(),
  created_at: z.string(),
})

export const modelContextSeasonSchema = z.object({
  season: z.number().int(),
  open_block: z.number().int(),
  open_block_hash: z.string(),
  seeds: z.array(seedSchema),
  bench_version: z.number().int(),
  cpm_score_version: z.number().int(),
  base_floors: z.record(z.string(), z.number()),
  gpu_sku: z.string().nullable(),
  opened_at: z.string(),
  closes_at: z.string(),
  closed_at: z.string().nullable(),
})

export const modelContextSeasonListSchema = z.object({
  current: z.number().int().nullable(),
  seasons: z.array(modelContextSeasonSchema),
})

export const modelContextSubmissionListSchema = z.object({
  submissions: z.array(submissionSummarySchema),
  // Subnet-wide, whatever the filter.
  counts_by_status: z.record(z.string(), z.number().int()),
  // Submissions matching this request's filters across all pages; null from
  // a Platform that predates it.
  total: z.number().int().nonnegative().nullable().optional().default(null),
})

export const modelContextSubmissionDetailSchema = z.object({
  summary: submissionSummarySchema,
  manifest: z.record(z.string(), z.unknown()),
  total_bytes: z.number().int(),
  screening_detail: z.record(z.string(), z.unknown()).nullable(),
  screening_attempts: z.number().int(),
  screening_lease_until: z.string().nullable(),
  updated_at: z.string(),
  runs: z.array(runViewSchema),
  actions: z.array(operatorActionSchema),
})

export const modelContextRunDetailSchema = z.object({
  run: runViewSchema,
  submission: submissionSummarySchema.nullable(),
  season: modelContextSeasonSchema.nullable(),
  floor: z.number().nullable(),
})

export const modelContextBoardSchema = z.object({
  entries: z.array(
    z.object({
      rank: z.number().int(),
      hotkey: z.string(),
      submission_id: uuid,
      score: z.number(),
      eligible: z.boolean(),
      commitment_block: z.number().int(),
      season: z.number().int(),
      base_id: z.string(),
      kind: z.enum(['lora', 'full']),
      hf_repo: z.string(),
      revision: z.string(),
      model_url: z.string(),
      components: z.record(z.string(), z.number().nullable()),
      floor: z.number().nullable(),
    }),
  ),
  count: z.number().int(),
  season: z.number().int().nullable(),
  mode: z.enum(MODEL_CONTEXT_MODES),
  floor_margin: z.number(),
  base_floors: z.record(z.string(), z.number()),
  generated_at: z.string(),
})

export const modelContextScorerStatusSchema = z.object({
  scorers: z.array(
    z.object({
      scorer_id: z.string(),
      scorer_version: z.string().nullable(),
      gpu_sku: z.string().nullable(),
      last_poll: z.string(),
      first_seen_at: z.string(),
      last_seen_at: z.string(),
    }),
  ),
  queued_runs: z.number().int(),
  leased_runs: z.number().int(),
  screening_submissions: z.number().int(),
  current_season: z.number().int().nullable(),
  // A Platform that predates the loop fields omits them (null = not reported).
  loop_running: z.boolean().nullable().optional().default(null),
  loop_last_error: z.string().nullable().optional().default(null),
})

export const listModelContextSubmissionsInputSchema = z.object({
  hotkey: ss58.optional(),
  status: z.enum(MODEL_CONTEXT_SUBMISSION_STATUSES).optional(),
  limit: z.number().int().min(1).max(200).default(50),
  offset: z.number().int().min(0).default(0),
})

export const modelContextSubmissionIdInputSchema = z.object({ submissionId: uuid })
export const modelContextRunIdInputSchema = z.object({ runId: uuid })

export const rescoreModelContextInputSchema = z.object({
  submissionId: uuid,
  expectedStatus: z.enum(['queued', 'scored']),
  reason: auditReasonSchema(8),
  confirmation: z.literal(RESCORE_MODEL_CONTEXT_CONFIRMATION),
})

export const rejectModelContextInputSchema = z.object({
  submissionId: uuid,
  expectedStatus: z.enum(['screening', 'queued', 'scored', 'superseded']),
  reason: auditReasonSchema(8),
  confirmation: z.literal(REJECT_MODEL_CONTEXT_CONFIRMATION),
})

export const rotateModelContextSeasonInputSchema = z.object({
  reason: auditReasonSchema(8),
  confirmation: z.literal(ROTATE_MODEL_CONTEXT_SEASON_CONFIRMATION),
})
