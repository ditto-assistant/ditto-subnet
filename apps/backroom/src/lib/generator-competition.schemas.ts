import { z } from 'zod'
import { auditReasonSchema } from './admin.schemas'

// DittoBench generator competition. Miners submit CPU-only Go generator
// source; the scorer qualifies it (build, valid packs on secret seeds,
// determinism, probe copy check), and qualified generators compete in
// chain-epoch rounds whose cases validators run against the top agents. These
// schemas mirror apps/platform/ditto/api_models/generator.py and
// generator_rounds.py. Reads strip unknown fields; Platform stays the
// authority on every bound and refusal.

const sha256 = z.string().regex(/^[0-9a-f]{64}$/)
const uuid = z.string().uuid()
const ss58 = z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{47,48}$/)

export const GENERATOR_SETTINGS_SCOPE = '*'
export const PROBE_ROTATION_CONFIRMATION = 'ROTATE GENERATOR PROBE SEED'
export const REJECT_GENERATOR_CONFIRMATION = 'REJECT GENERATOR SUBMISSION'
export const REINSTATE_GENERATOR_CONFIRMATION = 'REINSTATE GENERATOR SUBMISSION'
export const REGISTER_DEFAULT_GENERATOR_CONFIRMATION = 'REGISTER DEFAULT GENERATOR'
export const REPUBLISH_COMMITMENT_CONFIRMATION = 'REPUBLISH GENERATOR ROUND COMMITMENT'
export const RELEASE_ROUND_LEASE_CONFIRMATION = 'RELEASE GENERATOR ROUND LEASE'

export function voidRoundConfirmation(roundIndex: number) {
  return `VOID GENERATOR ROUND ${roundIndex}`
}

export function closeRoundConfirmation(roundIndex: number) {
  return `CLOSE GENERATOR ROUND ${roundIndex}`
}

export function generatorSettingsConfirmation(mode: string) {
  return `APPLY GENERATOR COMPETITION ${mode.toUpperCase()}`
}

// off | shadow | enforce. enforce marks qualified miner generators
// weight_eligible on the ledger; it pays nothing until track-share governance
// also serves a generator share (get_track_share_settings).
export const GENERATOR_MODES = ['off', 'shadow', 'enforce'] as const

// Required, never defaulted: a revision stores the whole policy.
export const generatorCompetitionSettingsSchema = z.object({
  mode: z.enum(GENERATOR_MODES),
  panel_size: z.number().int().min(3).max(16),
  min_panel: z.number().int().min(2).max(32),
  min_solver_owners: z.number().int().min(1).max(32),
  solve_threshold: z.number().gt(0).max(1),
  champion_bonus: z.number().min(0).max(2),
  seeds_per_run: z.number().int().min(1).max(4),
  // Platform MAX_CASES_PER_SEED, the Go pack contract's challenge.MaxCases.
  cases_per_seed: z.number().int().min(8).max(80),
  window_days: z.number().int().min(1).max(30),
  copy_overlap_threshold: z.number().min(0.2).max(1),
  upload_cooldown_seconds: z.number().int().min(60).max(86_400),
  lease_ttl_seconds: z.number().int().min(600).max(21_600),
  max_attempts: z.number().int().min(1).max(20),
  retry_backoff_seconds: z.number().int().min(60).max(86_400),
  epochs_per_round: z.number().int().min(1).max(48),
  counted_columns: z.number().int().min(1).max(6),
  challenger_columns: z.number().int().min(0).max(2),
  reference_agent_ids: z.array(uuid).max(3),
  privacy_window_rounds: z.number().int().min(1).max(180),
  onchain_commitment: z.boolean(),
  validator_rounds_enabled: z.boolean(),
  // Require a finalized TAO payment proof on every generator upload, priced by
  // the generator competition's fee in the submission settings (inheriting
  // the default fee until set_submission_cooldown competitionFees prices it).
  // A competitionFees generator fee_required override decides instead when
  // set. Ships off.
  upload_fee_required: z.boolean(),
})

// Reads tolerate a Platform that predates a field: an older Platform never
// charged generator uploads, so a missing upload_fee_required reads as off.
// A write still refuses to report success when Platform drops the field
// (setGeneratorSettings checks the stored revision).
const generatorSettingsReadSchema = generatorCompetitionSettingsSchema.extend({
  upload_fee_required: z.boolean().default(false),
})

export const generatorSettingsRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: generatorSettingsReadSchema,
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  checksum: z.string(),
  settings_valid: z.boolean().optional(),
})

export const generatorSettingsControlSchema = z.object({
  current: z.array(generatorSettingsRevisionSchema),
  history: z.array(generatorSettingsRevisionSchema),
  default: generatorSettingsReadSchema,
  effective: z.object({
    revision: z.number().int().nonnegative(),
    settings: generatorSettingsReadSchema,
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

const runStatus = z.enum([
  'leased',
  'scored',
  'qualified',
  'invalid',
  'insufficient_panel',
  'copy',
  'abandoned',
])
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
  qualified_at: z.string().nullable().optional(),
  public_default: z.boolean().optional(),
  round_score: z.number().nullable().optional(),
  round_scored_index: z.number().int().nullable().optional(),
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
  // Older Platforms omit both; a missing field reads as "not computed".
  operator_ruling: z.boolean().optional(),
  reinstate_blockers: z.array(z.string()).optional(),
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
  // Only a run that reported reveals its seeds: a leased run's are live
  // secrets and an abandoned run's reached a scorer that never reported.
  seeds: z.array(sha256).nullable(),
  probe_epoch: z.number().int(),
  cases_per_seed: z.number().int(),
  settings_revision: z.number().int(),
  scorer_version: z.string().nullable(),
  panel: z.array(panelSlotSchema),
  evaluation: z.record(z.string(), z.unknown()).nullable(),
  score_summary: z.record(z.string(), z.unknown()).nullable(),
  verdicts: z.array(verdictSchema),
}).refine(
  (run) => run.seeds === null || !['leased', 'abandoned'].includes(run.summary.status),
  { message: 'Platform served seeds for a run that has not reported', path: ['seeds'] },
)

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
      round_index: z.number().int(),
      column_id: z.string(),
      submission_id: uuid,
      miner_hotkey: z.string(),
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
      round_index: z.number().int(),
      weight_eligible: z.boolean(),
      score: z.number(),
      discriminating_cases: z.number().int(),
      first_seen: z.string(),
      scored_at: z.string(),
    }),
  ),
  count: z.number().int(),
  window_days: z.number().int(),
  mode: z.enum(GENERATOR_MODES),
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

export const reinstateGeneratorInputSchema = z.object({
  submissionId: uuid,
  expectedStatus: z.literal('rejected'),
  // The exact status_reason read from get_generator_submission (null when none).
  expectedStatusReason: z.string().nullable(),
  reason: auditReasonSchema(8),
  confirmation: z.literal(REINSTATE_GENERATOR_CONFIRMATION),
  overrideOperatorRuling: z.boolean().default(false),
})

// --- rounds ----------------------------------------------------------------

const roundStatus = z.enum(['committed', 'generating', 'running', 'scoring', 'scored', 'void'])

const roundScheduleSchema = z.object({
  mode: z.string(),
  epochs_per_round: z.number().int(),
  subnet_epoch_index: z.number().int().nullable().optional(),
  current_round: z.number().int().nullable().optional(),
  next_round_epoch: z.number().int().nullable().optional(),
  tempo: z.number().int().nullable().optional(),
  block: z.number().int().nullable().optional(),
  observed_at: z.string().nullable().optional(),
  last_error: z.string().nullable().optional(),
})

const roundSummarySchema = z.object({
  round_index: z.number().int(),
  status: roundStatus,
  epochs_per_round: z.number().int(),
  commitment: z.string(),
  commitment_publication: z.enum(['pending', 'published', 'failed', 'disabled']),
  // Validators that confirmed the commitment on chain. Absent from a Platform
  // that predates validator-published commitments.
  commitment_confirmations: z.number().int().optional(),
  committed_at: z.string(),
  open_epoch_index: z.number().int().nullable().optional(),
  open_block: z.number().int().nullable().optional(),
  open_block_hash: z.string().nullable().optional(),
  opened_at: z.string().nullable().optional(),
  column_count: z.number().int(),
  panel_size: z.number().int(),
  // Older Platforms omit these. They never drop panel agents, so that count
  // is zero; their answered-result count is unknown and stays absent.
  panel_dropped: z.number().int().optional().default(0),
  results: z.number().int(),
  answered_results: z.number().int().optional(),
  closed_at: z.string().nullable().optional(),
  scored_at: z.string().nullable().optional(),
  void_reason: z.string().nullable().optional(),
  revealed_at: z.string().nullable().optional(),
  secret_hex: z.string().nullable().optional(),
  seed: z.string().nullable().optional(),
  dataset_sha256: z.string().nullable().optional(),
}).transform((summary) =>
  // An unrevealed round's seed (and secret) regenerate its private cases, so
  // only the artifact-scoped dataset tool may return them.
  summary.revealed_at ? summary : { ...summary, secret_hex: null, seed: null },
)

export const generatorRoundListSchema = z.object({
  schedule: roundScheduleSchema,
  rounds: z.array(roundSummarySchema),
  counts_by_status: z.record(z.string(), z.number().int()),
})

const columnSlotSchema = z.object({
  column_id: z.string(),
  submission_id: uuid,
  miner_hotkey: z.string(),
  owner_key: z.string(),
  source_sha256: z.string(),
  counted: z.boolean(),
  public_default: z.boolean(),
})

const roundPanelSlotSchema = z.object({
  agent_id: uuid,
  owner_key: z.string(),
  miner_hotkey: z.string().nullable().optional(),
  champion: z.boolean(),
  reference: z.boolean(),
  // Set when the agent became ineligible while the round ran.
  dropped_reason: z.string().nullable().optional().default(null),
  dropped_at: z.string().nullable().optional().default(null),
})

const skippedPanelAgentSchema = z.object({
  agent_id: uuid,
  owner_key: z.string(),
  miner_hotkey: z.string().nullable().optional(),
  reason: z.string(),
})

const standingSchema = z.object({
  agent_id: uuid,
  rank: z.number().int(),
  pairwise_score: z.number(),
  wins: z.number().int(),
  ties: z.number().int(),
  losses: z.number().int(),
  opponents: z.number().int(),
  common_mean: z.number(),
  common_cases: z.number().int(),
  reference: z.boolean(),
})

const columnViewSchema = z.object({
  column_id: z.string(),
  submission_id: uuid,
  miner_hotkey: z.string().nullable(),
  counted: z.boolean(),
  public_default: z.boolean(),
  generated: z.boolean().nullable().optional(),
  score: z.number().nullable().optional(),
  discriminating: z.number().int().nullable().optional(),
  trivial: z.number().int().nullable().optional(),
  unsolved: z.number().int().nullable().optional(),
  duplicate: z.number().int().nullable().optional(),
})

export const generatorRoundDetailSchema = z.object({
  summary: roundSummarySchema,
  settings_revision: z.number().int().nullable(),
  cases_per_column: z.number().int().nullable(),
  // Frozen bench contract of the round. Optional so an older Platform parses.
  bench_version: z.number().int().nullable().optional(),
  seed: z.string().nullable(),
  columns: z.array(columnSlotSchema),
  panel: z.array(roundPanelSlotSchema),
  generation: z.record(z.string(), z.unknown()).nullable(),
  column_case_counts: z.record(z.string(), z.number().int()),
  dataset_bytes: z.number().int().nullable(),
  lease_id: uuid.nullable(),
  lease_expires_at: z.string().nullable(),
  lease_attempts: z.number().int(),
  commitment_published_at: z.string().nullable(),
  commitment_error: z.string().nullable(),
  // Validator-published commitments (DITTO-1429). Optional so an older
  // Platform, which published from its own identity, still parses.
  commitment_publishers_target: z.number().int().optional(),
  commitment_publications: z.array(
    z.object({
      validator_hotkey: z.string(),
      status: z.enum(['claimed', 'confirmed', 'failed', 'expired']),
      claimed_at: z.string(),
      claim_expires_at: z.string(),
      reported_at: z.string().nullable().optional(),
      block_number: z.number().int().nullable().optional(),
      extrinsic_hash: z.string().nullable().optional(),
      detail: z.string().nullable().optional(),
    }),
  ).optional(),
  commitment_elected: z.array(z.string()).optional(),
  attempts: z.array(
    z.object({
      agent_id: uuid,
      validator_hotkey: z.string(),
      ticket_deadline: z.string(),
      slot_id: z.string(),
      status: z.enum(['leased', 'completed', 'failed']),
      detail: z.string().nullable(),
      issued_at: z.string(),
      finished_at: z.string().nullable(),
    }),
  ),
  results: z.array(
    z.object({
      agent_id: uuid,
      validator_hotkey: z.string(),
      run_id: z.string(),
      answered: z.number().int(),
      faulted: z.number().int(),
      mean_score: z.number().nullable(),
      recorded_at: z.string(),
      excluded_reason: z.string().nullable().optional().default(null),
    }),
  ),
  pending_agents: z.array(uuid),
  skipped_panel_agents: z.array(skippedPanelAgentSchema).optional().default([]),
  score: z.record(z.string(), z.unknown()).nullable(),
  scorer_version: z.string().nullable(),
  standings: z.array(standingSchema),
  columns_scored: z.array(columnViewSchema),
}).transform((detail) => (detail.summary.revealed_at ? detail : { ...detail, seed: null }))

export const generatorRoundPreviewSchema = z.object({
  schedule: roundScheduleSchema,
  columns: z.array(columnSlotSchema),
  panel: z.array(roundPanelSlotSchema),
  qualified_generators: z.number().int(),
  awaiting_qualification: z.number().int(),
  blockers: z.array(z.string()),
  skipped_panel_agents: z.array(skippedPanelAgentSchema).optional().default([]),
})

export const generatorRoundLoopSchema = z.object({
  available: z.boolean(),
  status: z
    .object({
      last_tick_at: z.string().nullable(),
      last_success_at: z.string().nullable(),
      last_error: z.string().nullable(),
      last_actions: z.array(z.string()),
      ticks: z.number().int(),
    })
    .nullable(),
  schedule: roundScheduleSchema.nullable(),
})

export const generatorRoundTickSchema = z.object({
  actions: z.array(z.string()),
  schedule: roundScheduleSchema.nullable(),
  error: z.string().nullable().optional(),
})

export const generatorRoundDatasetSchema = z.object({
  round_index: z.number().int(),
  dataset_sha256: sha256,
  seed: sha256,
  size_bytes: z.number().int(),
  dataset_base64: z.string(),
})

export const defaultGeneratorResponseSchema = z.object({
  submission_id: uuid,
  source_sha256: z.string(),
  tree_sha256: z.string(),
  status: submissionStatus,
  superseded: z.array(uuid),
})

export const tickGeneratorRoundsInputSchema = z.object({
  reason: auditReasonSchema(8),
})

export const listGeneratorRoundsInputSchema = z.object({
  limit: z.number().int().min(1).max(100).default(20),
  offset: z.number().int().min(0).default(0),
})

export const generatorRoundIndexInputSchema = z.object({
  roundIndex: z.number().int().nonnegative(),
})

export const voidGeneratorRoundInputSchema = z
  .object({
    roundIndex: z.number().int().nonnegative(),
    expectedStatus: roundStatus,
    reason: auditReasonSchema(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = voidRoundConfirmation(input.roundIndex)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}`,
        path: ['confirmation'],
      })
    }
  })

export const closeGeneratorRoundInputSchema = z
  .object({
    roundIndex: z.number().int().nonnegative(),
    reason: auditReasonSchema(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = closeRoundConfirmation(input.roundIndex)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}`,
        path: ['confirmation'],
      })
    }
  })

export const republishRoundCommitmentInputSchema = z.object({
  roundIndex: z.number().int().nonnegative(),
  reason: auditReasonSchema(8),
  confirmation: z.literal(REPUBLISH_COMMITMENT_CONFIRMATION),
})

export const releaseRoundLeaseInputSchema = z.object({
  roundIndex: z.number().int().nonnegative(),
  agentId: uuid,
  validatorHotkey: ss58,
  reason: auditReasonSchema(8),
  confirmation: z.literal(RELEASE_ROUND_LEASE_CONFIRMATION),
})

// Exactly one of sourceBase64 or uploadKey; the service checks that, so the
// object stays extendable for the MCP input schema.
export const registerDefaultGeneratorInputSchema = z.object({
  minerHotkey: ss58,
  sourceSha256: sha256,
  sourceBase64: z.string().min(4).max(5_600_000).optional(),
  uploadKey: z.string().min(1).max(512).optional(),
  reason: auditReasonSchema(8),
  confirmation: z.literal(REGISTER_DEFAULT_GENERATOR_CONFIRMATION),
})

export const defaultGeneratorUploadResponseSchema = z.object({
  bucket: z.string(),
  key: z.string(),
  url: z.string(),
  method: z.literal('PUT'),
  content_type: z.string(),
  expires_in: z.number().int().positive(),
  max_bytes: z.number().int().positive(),
})
