import { z } from 'zod'
import { auditReasonSchema } from './admin.schemas'

// Shadow DittoBench generator competition (ditto-subnet #3094-#3097). Miners
// submit CPU-only Go generator source; Platform runs each generator on secret
// seeds against a top-agent panel and publishes a weight-zero ledger. These
// schemas mirror apps/platform/ditto/api_models/generator.py. Reads strip
// unknown fields; Platform stays the authority on every bound and refusal.

const sha256 = z.string().regex(/^[0-9a-f]{64}$/)
const uuid = z.string().uuid()
const ss58 = z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{47,48}$/)

export const GENERATOR_SETTINGS_SCOPE = '*'
export const PROBE_ROTATION_CONFIRMATION = 'ROTATE GENERATOR PROBE SEED'
export const REJECT_GENERATOR_CONFIRMATION = 'REJECT GENERATOR SUBMISSION'

export function generatorSettingsConfirmation(mode: string) {
  return `APPLY GENERATOR COMPETITION ${mode.toUpperCase()}`
}

// Required, never defaulted: a revision stores the whole policy.
export const generatorCompetitionSettingsSchema = z.object({
  mode: z.enum(['off', 'shadow']),
  panel_size: z.number().int().min(3).max(16),
  min_panel: z.number().int().min(2).max(32),
  min_solver_owners: z.number().int().min(1).max(32),
  solve_threshold: z.number().gt(0).max(1),
  champion_bonus: z.number().min(0).max(2),
  seeds_per_run: z.number().int().min(1).max(4),
  cases_per_seed: z.number().int().min(8).max(32),
  rescore_interval_hours: z.number().int().min(6).max(168),
  window_days: z.number().int().min(1).max(30),
  copy_overlap_threshold: z.number().min(0.2).max(1),
  upload_cooldown_seconds: z.number().int().min(60).max(86_400),
  lease_ttl_seconds: z.number().int().min(600).max(21_600),
  max_attempts: z.number().int().min(1).max(20),
  retry_backoff_seconds: z.number().int().min(60).max(86_400),
})

const settingsRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: generatorCompetitionSettingsSchema,
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  checksum: z.string(),
  settings_valid: z.boolean().optional(),
})

export const generatorSettingsControlSchema = z.object({
  current: z.array(settingsRevisionSchema),
  history: z.array(settingsRevisionSchema),
  default: generatorCompetitionSettingsSchema,
  effective: z.object({
    revision: z.number().int().nonnegative(),
    settings: generatorCompetitionSettingsSchema,
    checksum: z.string(),
    source: z.enum(['revision', 'default']),
    settings_valid: z.boolean().optional(),
  }),
})

export const setGeneratorSettingsInputSchema = z
  .object({
    expectedRevision: z.number().int().nonnegative(),
    settings: generatorCompetitionSettingsSchema,
    reason: auditReasonSchema(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = generatorSettingsConfirmation(input.settings.mode)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}, naming the mode this revision applies`,
        path: ['confirmation'],
      })
    }
  })

const caseCountsSchema = z.object({
  discriminating: z.number().int(),
  trivial: z.number().int(),
  unsolved: z.number().int(),
  duplicate: z.number().int(),
})

const runStatus = z.enum(['leased', 'scored', 'invalid', 'insufficient_panel', 'copy', 'abandoned'])
const submissionStatus = z.enum(['active', 'superseded', 'rejected'])

const runSummarySchema = z.object({
  run_id: uuid,
  attempt: z.number().int(),
  status: runStatus,
  outcome: z.string().nullable(),
  score: z.number().nullable(),
  case_counts: caseCountsSchema.nullable(),
  leased_at: z.string(),
  finished_at: z.string().nullable(),
})

const submissionSummarySchema = z.object({
  submission_id: uuid,
  miner_hotkey: z.string(),
  source_sha256: z.string(),
  tree_sha256: z.string(),
  module: z.string(),
  go_version: z.string(),
  file_count: z.number().int(),
  size_bytes: z.number().int(),
  status: submissionStatus,
  status_reason: z.string().nullable(),
  next_run_at: z.string().nullable(),
  submitted_at: z.string(),
  latest_run: runSummarySchema.nullable(),
})

export const generatorSubmissionListSchema = z.object({
  submissions: z.array(submissionSummarySchema),
  counts_by_status: z.record(z.string(), z.number().int()),
})

export const generatorSubmissionDetailSchema = z.object({
  summary: submissionSummarySchema,
  owner_key: z.string(),
  attempts: z.number().int(),
  lease_run_id: uuid.nullable(),
  lease_expires_at: z.string().nullable(),
  runs: z.array(runSummarySchema),
})

const panelSlotSchema = z.object({
  agent_id: z.string(),
  owner_key: z.string(),
  champion: z.boolean(),
})

const verdictSchema = z.object({
  seed_index: z.number().int(),
  case_id: z.string(),
  position: z.number().int(),
  fingerprint: z.string(),
  status: z.enum(['discriminating', 'trivial', 'unsolved', 'duplicate']),
  panel_size: z.number().int(),
  solvers: z.number().int(),
  solver_owners: z.number().int(),
  mean_score: z.number(),
  champion_failed: z.boolean(),
  credit: z.number(),
})

// The evaluation is Platform's stored projection of the scorer report (build,
// per-seed validation summaries, determinism, probe without its seed). It is
// passed through as a record; Platform already bounds it.
export const generatorRunDetailSchema = z.object({
  summary: runSummarySchema,
  submission_id: uuid,
  miner_hotkey: z.string(),
  seeds: z.array(sha256).nullable(),
  probe_epoch: z.number().int(),
  cases_per_seed: z.number().int(),
  settings_revision: z.number().int(),
  scorer_version: z.string().nullable(),
  panel: z.array(panelSlotSchema),
  evaluation: z.record(z.string(), z.unknown()).nullable(),
  score_summary: z.record(z.string(), z.unknown()).nullable(),
  verdicts: z.array(verdictSchema),
})

const probeEpochSchema = z.object({
  epoch: z.number().int(),
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  retired_at: z.string().nullable(),
  probed_submissions: z.number().int(),
})

export const generatorProbeSchema = z.object({
  current: probeEpochSchema.nullable(),
  history: z.array(probeEpochSchema),
})

export const generatorPromotionCandidatesSchema = z.object({
  candidates: z.array(
    z.object({
      run_id: uuid,
      submission_id: uuid,
      miner_hotkey: z.string(),
      seed_index: z.number().int(),
      case_id: z.string(),
      fingerprint: z.string(),
      mean_score: z.number(),
      solver_owners: z.number().int(),
      champion_failed: z.boolean(),
      credit: z.number(),
      scored_at: z.string(),
    }),
  ),
})

export const generatorBoardSchema = z.object({
  entries: z.array(
    z.object({
      miner_hotkey: z.string(),
      submission_id: uuid,
      run_id: uuid,
      weight_eligible: z.boolean(),
      score: z.number(),
      discriminating_cases: z.number().int(),
      first_seen: z.string(),
      scored_at: z.string(),
    }),
  ),
  count: z.number().int(),
  window_days: z.number().int(),
  mode: z.enum(['off', 'shadow']),
  generated_at: z.string(),
})

export const listGeneratorSubmissionsInputSchema = z.object({
  hotkey: ss58.optional(),
  status: submissionStatus.optional(),
  limit: z.number().int().min(1).max(200).default(50),
  offset: z.number().int().min(0).default(0),
})

export const generatorSubmissionIdInputSchema = z.object({ submissionId: uuid })
export const generatorRunIdInputSchema = z.object({ runId: uuid })
export const generatorPromotionInputSchema = z.object({
  limit: z.number().int().min(1).max(500).default(100),
  offset: z.number().int().min(0).default(0),
})

export const rotateGeneratorProbeInputSchema = z.object({
  reason: auditReasonSchema(8),
  confirmation: z.literal(PROBE_ROTATION_CONFIRMATION),
})

export const rescoreGeneratorInputSchema = z.object({
  submissionId: uuid,
  reason: auditReasonSchema(8),
})

export const rejectGeneratorInputSchema = z.object({
  submissionId: uuid,
  expectedStatus: z.enum(['active', 'superseded']),
  reason: auditReasonSchema(8),
  confirmation: z.literal(REJECT_GENERATOR_CONFIRMATION),
})
