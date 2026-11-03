import { z } from 'zod'
import { auditReasonSchema } from './admin.schemas'

// Dueling-bench seasons (bench v16+): the frozen generator set every bench
// v16+ agent run is scored on. Mirrors apps/platform/ditto/api_models/dueling.py
// (admin views). Reads strip unknown fields; Platform stays the authority on
// every refusal. The season secret and seed are present only once a season is
// revealed.

export const ROTATE_DUELING_SEASON_CONFIRMATION = 'ROTATE DUELING SEASON'

const uuid = z.string().uuid()
const nullableString = z.string().nullable()
const nullableInt = z.number().int().nullable()

const seasonStatus = z.enum(['committed', 'open', 'closed', 'revealed', 'void'])
const batteryStatus = z.enum(['pending', 'leased', 'passed', 'failed'])
export const duelingSliceStatus = z.enum(['pending', 'leased', 'ready', 'failed'])

const batterySchema = z.object({
  submission_id: uuid,
  miner_hotkey: z.string(),
  owner_key: z.string(),
  tree_sha256: z.string(),
  rank: z.number().int(),
  evidence: z.record(z.string(), z.unknown()).nullable(),
  status: batteryStatus,
  attempts: z.number().int(),
  grading_bench_version: nullableInt,
  error: nullableString,
  scorer_version: nullableString,
  finished_at: nullableString,
})

const columnMeanSchema = z.object({
  column_id: z.string(),
  submission_id: uuid,
  owner_key: z.string(),
  public_default: z.boolean(),
  scores: z.number().int(),
  cases: z.number().int(),
  mean: z.number().nullable(),
  default_gap: z.number().nullable(),
  below_floor: z.boolean(),
})

// Season-open mini-rollout: the champion, then the retest cohort by rank,
// re-scored on the new generator set, and whether this season's scores still
// count (current, provisional for one season, then expired).
const rotationSchema = z.object({
  standing: z.enum(['current', 'provisional', 'expired', 'not_opened']),
  source: nullableString.optional().default(null),
  cohort: z.array(z.record(z.string(), z.unknown())).default([]),
  queued_at: nullableString.optional().default(null),
  queued: z.number().int().default(0),
  already_current: z.number().int().default(0),
  busy: z.number().int().default(0),
  error: nullableString.optional().default(null),
  pending: z.number().int().default(0),
  running: z.number().int().default(0),
  closed: z.number().int().default(0),
  cohort_scores_in_season: z.number().int().default(0),
  cohort_quorum_in_season: z.number().int().default(0),
  scores_in_season: z.number().int().default(0),
  confirmation_scores_in_season: z.number().int().default(0),
})

export const duelingSeasonSchema = z.object({
  id: uuid,
  bench_version: z.number().int(),
  season_index: z.number().int(),
  shadow: z.boolean(),
  status: seasonStatus,
  commitment: z.string(),
  committed_at: z.string(),
  rounds_per_season: z.number().int(),
  settings_revision: nullableInt,
  anchor_block: nullableInt,
  anchor_block_hash: nullableString,
  opened_at: nullableString,
  closes_at_round: nullableInt,
  closed_at: nullableString,
  reveal_at_epoch: nullableInt,
  revealed_at: nullableString,
  void_reason: nullableString,
  rotation_requested_at: nullableString,
  rotation_immediate: z.boolean().nullable(),
  rotation_actor: nullableString,
  rotation_reason: nullableString,
  columns: z.array(z.record(z.string(), z.unknown())).nullable(),
  fallback: z.record(z.string(), z.unknown()).nullable(),
  detail: z.record(z.string(), z.unknown()).nullable(),
  secret_hex: nullableString.optional().default(null),
  season_seed: nullableString.optional().default(null),
  batteries: z.array(batterySchema).default([]),
  column_means: z.array(columnMeanSchema).default([]),
  slices: z.record(z.string(), z.number().int()).default({}),
  rotation: rotationSchema.nullable().optional().default(null),
})

export const duelingSeasonsSchema = z.object({
  target: z.object({
    bench_version: nullableInt,
    shadow: z.boolean(),
    blocked: nullableString,
    live_versions: z.array(z.number().int()),
  }),
  current: z.array(duelingSeasonSchema),
  next: z.array(duelingSeasonSchema),
  recent: z.array(duelingSeasonSchema),
  // Bench v16+ rows with no season or aggregates (never rank or pay; should be 0).
  scores_without_dueling: z.number().int().default(0),
  confirmation_scores_without_dueling: z.number().int().default(0),
})

export const duelingSlicesSchema = z.object({
  slices: z.array(z.object({
    id: uuid,
    season_id: uuid,
    season_index: z.number().int(),
    bench_version: z.number().int(),
    // A run seed is an int64: the admin JSON parser hands values above the
    // safe-integer limit over as decimal strings, so normalize both to text.
    run_seed: z.union([z.string().regex(/^(0|[1-9][0-9]*)$/), z.number().int().nonnegative()])
      .transform(String),
    status: duelingSliceStatus,
    attempts: z.number().int(),
    slice_sha256: nullableString,
    size_bytes: nullableInt,
    bytes_pruned: z.boolean(),
    columns: z.array(z.record(z.string(), z.unknown())).nullable(),
    error: nullableString,
    scorer_version: nullableString,
    created_at: z.string(),
    first_leased_at: nullableString,
    ready_at: nullableString,
    failed_at: nullableString,
    latency_seconds: z.number().nullable(),
    retries: z.number().int().default(0),
    last_retry_at: nullableString.optional().default(null),
    last_retry_actor: nullableString.optional().default(null),
    last_retry_reason: nullableString.optional().default(null),
    retryable: z.boolean().default(false),
    retry_refusal: nullableString.optional().default(null),
  })),
  counts: z.record(z.string(), z.number().int()),
  latency_p50_seconds: z.number().nullable(),
  latency_p95_seconds: z.number().nullable(),
})

// Each pinned generator's source release (bench v16+): release_at =
// max(reveal of the last season that pinned it, close of the first season that
// pinned it + embargo_hours); released once due while the policy publishes.
export const generatorSourceReleaseStatus = z.enum([
  'waiting_close', 'waiting_reveal', 'embargoed', 'due', 'withheld', 'released',
])

export const generatorSourceReleasesSchema = z.object({
  items: z.array(z.object({
    submission_id: uuid,
    miner_hotkey: z.string(),
    owner_key: z.string(),
    tree_sha256: z.string(),
    source_sha256: z.string(),
    bench_version: z.number().int(),
    first_season_id: uuid,
    last_season_id: uuid,
    seasons: z.number().int(),
    first_closed_at: nullableString,
    last_revealed_at: nullableString,
    embargo_hours: z.number().int(),
    disclosure: z.string(),
    release_at: nullableString,
    released_at: nullableString,
    scorer_version: nullableString,
    status: generatorSourceReleaseStatus,
    updated_at: z.string(),
  })),
  counts: z.record(z.string(), z.number().int()).default({}),
})

export const listGeneratorSourceReleasesInputSchema = z.object({
  status: generatorSourceReleaseStatus.optional(),
  submissionId: uuid.nullish(),
  limit: z.number().int().min(1).max(200).default(50),
  offset: z.number().int().min(0).max(100_000).default(0),
})

export const getDuelingSeasonInputSchema = z.object({
  seasonId: uuid.nullish(),
})

export const listDuelingSlicesInputSchema = z.object({
  seasonId: uuid.nullish(),
  preparationId: uuid.nullish(),
  status: duelingSliceStatus.optional(),
  limit: z.number().int().min(1).max(200).default(50),
  offset: z.number().int().min(0).max(100_000).default(0),
})

export const rotateDuelingSeasonInputSchema = z
  .object({
    seasonId: uuid.optional(),
    immediate: z.boolean().default(false),
    reason: auditReasonSchema(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    if (input.confirmation !== ROTATE_DUELING_SEASON_CONFIRMATION) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${ROTATE_DUELING_SEASON_CONFIRMATION}`,
        path: ['confirmation'],
      })
    }
  })

export function retryDuelingSliceConfirmation(preparationId: string) {
  // Platform builds the phrase from the canonical (lowercase) UUID.
  return `RETRY DUELING SLICE ${preparationId.toLowerCase()}`
}

export const retryDuelingSliceInputSchema = z
  .object({
    preparationId: uuid,
    expectedStatus: z.literal('failed'),
    // The preparation's error exactly as list_dueling_slices shows it.
    expectedError: z.string().nullable(),
    reason: auditReasonSchema(8),
    confirmation: z.string(),
    force: z.boolean().default(false),
  })
  .superRefine((input, context) => {
    const expected = retryDuelingSliceConfirmation(input.preparationId)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}`,
        path: ['confirmation'],
      })
    }
  })
