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
const frontierState = z.enum(['oracle_pending', 'audit_pending', 'settled'])
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

// Platform frontier_audit_confirmation / frontier_settle_confirmation.
export function frontierAuditConfirmation(roundIndex: number, columnId: string, valid: boolean) {
  return `RULE FRONTIER AUDIT ${roundIndex} ${columnId} ${valid ? 'VALID' : 'INVALID'}`
}

export function frontierSettleConfirmation(roundIndex: number) {
  return `SETTLE GENERATOR FRONTIER ${roundIndex}`
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
  // Validators that must be able to run a round at a newer bench version
  // before rounds switch to it (the agent rollout's SCORING_QUORUM of 3
  // distinct validators, mirrored). Below it rounds stay on the previous
  // round's version and preview_generator_round says why.
  min_round_validators: z.number().int().min(1).max(32),
  // Require a finalized TAO payment proof on every generator upload, priced by
  // the generator competition's fee in the submission settings (inheriting
  // the default fee until set_submission_cooldown competitionFees prices it).
  // A competitionFees generator fee_required override decides instead when
  // set. Ships off.
  upload_fee_required: z.boolean(),
  // Dueling bench (v16+): open seasons that pin up to five eligible
  // generators into every bench v16+ agent run. Ships off; seasons open only
  // while a v16+ contract is active or rolling out (shadow seasons) and the
  // source release policy discloses.
  dueling_enabled: z.boolean(),
  // Generator rounds per dueling season (default 1). Platform refuses to
  // enable dueling with rounds_per_season x epochs_per_round < 2.
  rounds_per_season: z.number().int().min(1).max(48),
  // A shipped bench version (16+) to run shadow dueling seasons at while no
  // dueling version is active or rolling out, so a canary at it can lease a
  // slice before start_benchmark_rollout. Null by default; needs dueling_enabled.
  dueling_shadow_bench_version: z.number().int().min(16).nullable(),
  // Skip a round that would only repeat the last scored round (same
  // generator sources, panel agents, bench version, column size and score
  // params): no generation or panel inference, and the ledger keeps paying
  // that scored round. Ships on; a write that omits it keeps it on.
  skip_unchanged_rounds: z.boolean().default(true),
  // Frontier cases: a case no two panel owners solved that the frontier
  // oracle (frontier_oracle_agent_id, a subnet-run reference agent answering
  // from a bounded context) passed and no failed sampled audit voided earns
  // frontier_weight of full credit instead of the unsolved penalty. Off while
  // frontier_oracle_agent_id is null (the default); a write that omits these
  // keeps the shipped defaults.
  frontier_weight: z.number().min(0).max(1).default(1),
  frontier_oracle_agent_id: uuid.nullable().default(null),
  frontier_audit_cases: z.number().int().min(0).max(16).default(3),
  frontier_audit_grace_hours: z.number().int().min(1).max(336).default(24),
  frontier_oracle_timeout_hours: z.number().int().min(1).max(168).default(12),
})

// The shortest safe dueling season in subnet epochs (dueling-bench.md
// section 3), mirrored from Platform MIN_DUELING_SEASON_EPOCHS.
export const MIN_DUELING_SEASON_EPOCHS = 2

// Reads tolerate a Platform that predates a field: an older Platform never
// charged generator uploads, so a missing upload_fee_required reads as off.
// A write still refuses to report success when Platform drops the field
// (setGeneratorSettings checks the stored revision).
const generatorSettingsReadSchema = generatorCompetitionSettingsSchema.extend({
  upload_fee_required: z.boolean().default(false),
  // A revision written before the field reads as the shipped 3, as Platform
  // reads it.
  min_round_validators: z.number().int().min(1).max(32).default(3),
  // A Platform or revision that predates dueling reads as off, one round
  // per season.
  dueling_enabled: z.boolean().default(false),
  rounds_per_season: z.number().int().min(1).max(48).default(1),
  dueling_shadow_bench_version: z.number().int().min(16).nullable().default(null),
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
    overrideScorerCapability: z.boolean().default(false),
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
    const seasonEpochs = input.settings.rounds_per_season * input.settings.epochs_per_round
    if (input.settings.dueling_enabled && seasonEpochs < MIN_DUELING_SEASON_EPOCHS) {
      context.addIssue({
        code: 'custom',
        message: `a dueling season must span at least ${MIN_DUELING_SEASON_EPOCHS} epochs: `
          + `rounds_per_season x epochs_per_round = ${seasonEpochs}`,
        path: ['settings', 'rounds_per_season'],
      })
    }
    // Platform refuses this too; checking here saves an operator a round trip.
    if (input.settings.dueling_shadow_bench_version != null && !input.settings.dueling_enabled) {
      context.addIssue({
        code: 'custom',
        message: 'dueling_shadow_bench_version needs dueling_enabled',
        path: ['settings', 'dueling_shadow_bench_version'],
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

const roundStatus = z.enum([
  'committed', 'generating', 'running', 'scoring', 'scored', 'skipped', 'void',
])

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
  // skipped_unchanged on a skipped round, and the scored round it carries
  // forward. Absent from a Platform that never skips.
  skip_reason: z.string().nullable().optional().default(null),
  carried_from_round: z.number().int().nullable().optional().default(null),
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
  // Oracle-attested cases scored as frontier, and the round's frontier lane
  // state. Optional so an older Platform parses.
  frontier: z.number().int().nullable().optional(),
  frontier_state: frontierState.nullable().optional(),
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
  // The scored round's frontier lane (oracle, audits, applied credit).
  // Optional so an older Platform parses.
  frontier: z.lazy(() => generatorFrontierSchema).nullable().optional(),
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

// The bench contract a round opened now would freeze: the active version's
// round contract when a validator can run it, else the newest one below it.
export const roundBenchContractPreviewSchema = z.object({
  active_version: z.number().int(),
  bench_version: z.number().int().nullable(),
  reason: z.string().nullable().optional().default(null),
  round_contracts: z.array(z.number().int()),
  capable_validators: z.record(z.string(), z.number().int()),
  // The version the newest opened round froze; a newer one needs
  // min_round_validators capable validators before rounds switch to it.
  previous_version: z.number().int().nullable().optional().default(null),
  min_round_validators: z.number().int().nullable().optional().default(null),
  // What the live generator scorer grades: its advertised list, else v13.
  scorer_grading_versions: z.array(z.number().int()).nullable().optional().default(null),
})

export const generatorRoundPreviewSchema = z.object({
  schedule: roundScheduleSchema,
  columns: z.array(columnSlotSchema),
  panel: z.array(roundPanelSlotSchema),
  qualified_generators: z.number().int(),
  awaiting_qualification: z.number().int(),
  blockers: z.array(z.string()),
  skipped_panel_agents: z.array(skippedPanelAgentSchema).optional().default([]),
  bench_contract: roundBenchContractPreviewSchema.nullable().optional().default(null),
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
  // The governing skip_unchanged_rounds setting, how many rounds were ever
  // skipped as unchanged, and the newest one. Absent from an older Platform.
  skip_unchanged_rounds: z.boolean().nullable().optional().default(null),
  skipped_rounds: z.number().int().optional().default(0),
  last_skipped_round: z
    .object({
      round_index: z.number().int(),
      reason: z.string(),
      carried_from_round: z.number().int().nullable(),
      skipped_at: z.string().nullable(),
    })
    .nullable()
    .optional()
    .default(null),
})

const generatorScorerHeartbeatSchema = z.object({
  scorer_id: z.string(),
  scorer_version: z.string(),
  min_cases: z.number().int().nullable(),
  max_cases: z.number().int().nullable(),
  sandbox: z.string().nullable(),
  build_revision: z.string().nullable(),
  last_poll: z.string(),
  first_seen_at: z.string(),
  last_seen_at: z.string(),
  live: z.boolean(),
  // Advertised pack grading policies; null predates the explicit list.
  grading_bench_versions: z.array(z.number().int()).nullable().optional().default(null),
  // What Platform matches against: the advertised list, else v13 only.
  effective_grading_bench_versions: z.array(z.number().int()).optional(),
  // Dueling-bench work kinds (slice, battery) advertised on the dueling-work
  // poll; null when the scorer never polled for dueling work.
  dueling_work_kinds: z.array(z.string()).nullable().optional().default(null),
})

export const generatorScorerStatusSchema = z.object({
  scorers: z.array(generatorScorerHeartbeatSchema),
  live_scorer_id: z.string().nullable(),
  capability_ttl_seconds: z.number().int(),
  cases_per_seed: z.number().int(),
  supports_cases_per_seed: z.boolean().nullable(),
  refusal: z.string().nullable(),
  round_contract: z.number().int().nullable().optional().default(null),
  supports_round_contract: z.boolean().nullable().optional().default(null),
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

// --- frontier cases ----------------------------------------------------------

const frontierAuditStatus = z.enum(['pending', 'passed', 'failed'])

export const generatorFrontierAuditSchema = z.object({
  round_index: z.number().int(),
  column_id: z.string(),
  submission_id: uuid.nullable(),
  miner_hotkey: z.string().nullable(),
  status: frontierAuditStatus,
  sample: z.array(z.string()),
  attested_cases: z.number().int(),
  invalid_case_ids: z.array(z.string()).default([]),
  reason: z.string().nullable().optional(),
  actor: z.string().nullable().optional(),
  ruled_at: z.string().nullable().optional(),
  audit_deadline: z.string().nullable().optional(),
})

export const generatorFrontierSchema = z.object({
  round_index: z.number().int(),
  state: frontierState,
  oracle_agent_id: uuid,
  frontier_weight: z.number(),
  audit_cases: z.number().int(),
  candidates: z.array(z.string()),
  oracle_attempts: z.number().int(),
  oracle_deadline: z.string().nullable(),
  oracle: z
    .object({
      validator_hotkey: z.string(),
      run_id: z.string(),
      recorded_at: z.string(),
      passed: z.number().int(),
      failed: z.number().int(),
      faulted: z.number().int(),
      results: z.array(
        z.object({ case_id: z.string(), score: z.number(), fault: z.boolean() }),
      ),
    })
    .nullable(),
  attested: z.record(z.string(), z.array(z.string())),
  audits: z.array(generatorFrontierAuditSchema),
  applied: z.record(z.string(), z.array(z.string())).nullable().optional(),
  column_scores: z.record(z.string(), z.number()),
  base_column_scores: z.record(z.string(), z.number()).nullable().optional(),
  created_at: z.string(),
  settled_at: z.string().nullable().optional(),
  settle_reason: z.string().nullable().optional(),
})

export const generatorFrontierAuditListSchema = z.object({
  audits: z.array(generatorFrontierAuditSchema),
  count: z.number().int(),
})

const frontierMemoryPair = z.object({
  pair_id: z.string(),
  timestamp: z.string(),
  wave: z.number().int(),
  prompt: z.string(),
  response: z.string(),
})

export const generatorFrontierAuditDetailSchema = z.object({
  audit: generatorFrontierAuditSchema,
  cases: z.array(
    z.object({
      case_id: z.string(),
      question: z.string(),
      expected_answer: z.string(),
      answer_kind: z.string().nullable().optional(),
      answer_items: z.array(z.string()).default([]),
      accept_any: z.array(z.string()).default([]),
      distractor_answers: z.array(z.string()).default([]),
      forbidden_answer: z.string().nullable().optional(),
      dump_guard: z.array(z.string()).default([]),
      user_id: z.string(),
      run_after_wave: z.number().int(),
      evidence: z.array(frontierMemoryPair),
      visible_pairs: z.number().int(),
      memory_matches: z.array(frontierMemoryPair),
      memory_truncated: z.boolean(),
      panel_mean_score: z.number().nullable().optional(),
      panel_solvers: z.number().int().nullable().optional(),
      panel_size: z.number().int().nullable().optional(),
      oracle_score: z.number().nullable().optional(),
      oracle_fault: z.boolean().nullable().optional(),
    }),
  ),
})

export const listGeneratorFrontierAuditsInputSchema = z.object({
  includeRuled: z.boolean().default(false),
  limit: z.number().int().min(1).max(200).default(50),
  offset: z.number().int().min(0).default(0),
})

export const generatorFrontierAuditInputSchema = z.object({
  roundIndex: z.number().int().nonnegative(),
  columnId: z.string().min(1).max(32),
  q: z.string().max(200).optional(),
})

export const ruleGeneratorFrontierAuditInputSchema = z
  .object({
    roundIndex: z.number().int().nonnegative(),
    columnId: z.string().min(1).max(32),
    valid: z.boolean(),
    invalidCaseIds: z.array(z.string().min(1)).max(16).default([]),
    reason: auditReasonSchema(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = frontierAuditConfirmation(input.roundIndex, input.columnId, input.valid)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}`,
        path: ['confirmation'],
      })
    }
    if (input.valid !== (input.invalidCaseIds.length === 0)) {
      context.addIssue({
        code: 'custom',
        message: input.valid
          ? 'a valid ruling names no invalid cases'
          : 'an invalid ruling names the sampled cases that are invalid',
        path: ['invalidCaseIds'],
      })
    }
  })

export const settleGeneratorFrontierInputSchema = z
  .object({
    roundIndex: z.number().int().nonnegative(),
    reason: auditReasonSchema(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = frontierSettleConfirmation(input.roundIndex)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}`,
        path: ['confirmation'],
      })
    }
  })
