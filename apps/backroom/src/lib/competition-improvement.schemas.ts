import { z } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'

// Competition improvement metrics (GET /api/v1/admin/competition-improvement/metrics):
// measurement only. Frontier and owner top-N per UTC day, new highs and what
// drove them, exact-SHA source releases with their detected forks, the
// embargo windows the release policy has had, and each bench's noise floor.

type PlatformResponseShape<Response extends object> = {
  [Field in keyof Response]-?: z.ZodType<Response[Field]>
}

type Schemas = PlatformComponents['schemas']

const timestamp = () => z.string().datetime({ offset: true })
const day = () => z.string().regex(/^\d{4}-\d{2}-\d{2}$/)
const counts = () => z.record(z.string(), z.number())

const topEntrySchema = z.object({
  agent_id: z.string().uuid(),
  name: z.string(),
  version: z.number().int().nullable(),
  composite: z.number(),
} satisfies PlatformResponseShape<Schemas['CompetitionTopEntry']>)

const newHighSchema = z.object({
  agent_id: z.string().uuid(),
  name: z.string(),
  version: z.number().int().nullable(),
  bench_version: z.number().int(),
  finalized_at: timestamp(),
  composite: z.number(),
  previous_frontier: z.number(),
  delta: z.number(),
  days_since_previous_high: z.number().nullable(),
  driver: z.enum(['original', 'same_owner_iteration', 'released_fork']),
  base_agent_id: z.string().uuid().nullable(),
} satisfies PlatformResponseShape<Schemas['CompetitionNewHigh']>)

const dailyRowSchema = z.object({
  bench_version: z.number().int(),
  day: day(),
  frontier_composite: z.number().nullable(),
  frontier_agent_id: z.string().uuid().nullable(),
  champion_agent_id: z.string().uuid().nullable(),
  champion_composite: z.number().nullable(),
  top_n_mean: z.number().nullable(),
  top_n_min: z.number().nullable(),
  finalized_agent_count: z.number().int().nonnegative(),
  new_high_count: z.number().int().nonnegative(),
  releases_on_day: z.number().int().nonnegative(),
  releases_to_date: z.number().int().nonnegative(),
  forks_on_day: z.number().int().nonnegative(),
  forks_to_date: z.number().int().nonnegative(),
  thin_forks_to_date: z.number().int().nonnegative(),
  embargo_hours: z.number().int(),
  disclosure: z.string(),
  top: z.array(topEntrySchema),
} satisfies PlatformResponseShape<Schemas['CompetitionDailyRow']>)

const benchSummarySchema = z.object({
  bench_version: z.number().int(),
  first_day: day().nullable(),
  last_day: day().nullable(),
  days: z.number().int().nonnegative(),
  frontier_start: z.number().nullable(),
  frontier_end: z.number().nullable(),
  frontier_gain: z.number().nullable(),
  gain_per_day: z.number().nullable(),
  new_high_count: z.number().int().nonnegative(),
  beyond_noise_high_count: z.number().int().nonnegative().nullable(),
  median_days_between_highs: z.number().nullable(),
  max_days_between_highs: z.number().nullable(),
  days_since_last_high: z.number().nullable(),
  champion_changes: z.number().int().nonnegative(),
  drivers: counts(),
  gain_by_driver: counts(),
  largest_steps: z.array(newHighSchema),
} satisfies PlatformResponseShape<Schemas['CompetitionBenchSummary']>)

const noiseAgentSchema = z.object({
  agent_id: z.string().uuid(),
  name: z.string(),
  version: z.number().int().nullable(),
  composite: z.number(),
  seed_count: z.number().int().nonnegative(),
  seed_sd: z.number().nullable(),
  quorum_sem: z.number().nullable(),
} satisfies PlatformResponseShape<Schemas['CompetitionNoiseAgent']>)

const noiseFloorSchema = z.object({
  bench_version: z.number().int(),
  agents_measured: z.number().int().nonnegative(),
  median_seed_sd: z.number().nullable(),
  max_seed_sd: z.number().nullable(),
  median_quorum_sem: z.number().nullable(),
  single_seed_delta_sd: z.number().nullable(),
  beyond_noise_step: z.number().nullable(),
  agents: z.array(noiseAgentSchema),
} satisfies PlatformResponseShape<Schemas['CompetitionNoiseFloor']>)

const benchSchema = z.object({
  summary: benchSummarySchema,
  noise: noiseFloorSchema,
  new_highs: z.array(newHighSchema),
  daily: z.array(dailyRowSchema),
} satisfies PlatformResponseShape<Schemas['CompetitionBench']>)

const forkSchema = z.object({
  fork_agent_id: z.string().uuid(),
  fork_name: z.string(),
  fork_version: z.number().int().nullable(),
  fork_status: z.string(),
  bench_version: z.number().int().nullable(),
  fork_uploaded_at: timestamp(),
  lag_hours: z.number(),
  containment: z.number().nullable(),
  novel_fraction: z.number().nullable(),
  declared: z.boolean(),
  thin: z.boolean(),
  fork_composite: z.number().nullable(),
  delta: z.number().nullable(),
  shared_seed_count: z.number().int().nonnegative(),
  shared_seed_delta: z.number().nullable(),
} satisfies PlatformResponseShape<Schemas['CompetitionFork']>)

const releaseSchema = z.object({
  agent_id: z.string().uuid(),
  name: z.string(),
  version: z.number().int().nullable(),
  bench_version: z.number().int().nullable(),
  uploaded_at: timestamp(),
  released_at: timestamp(),
  embargo_hours: z.number().int(),
  disclosure: z.string(),
  base_composite: z.number().nullable(),
  fork_count: z.number().int().nonnegative(),
  thin_fork_count: z.number().int().nonnegative(),
  first_fork_lag_hours: z.number().nullable(),
  median_fork_lag_hours: z.number().nullable(),
  median_novel_fraction: z.number().nullable(),
  median_delta: z.number().nullable(),
  best_delta: z.number().nullable(),
  forks_beating_base: z.number().int().nonnegative(),
  forks: z.array(forkSchema),
} satisfies PlatformResponseShape<Schemas['CompetitionRelease']>)

const embargoWindowSchema = z.object({
  revision: z.number().int(),
  embargo_hours: z.number().int(),
  disclosure: z.string(),
  starts_at: timestamp().nullable(),
  ends_at: timestamp().nullable(),
  days: z.number().nullable(),
  releases: z.number().int().nonnegative(),
  forks: z.number().int().nonnegative(),
  forks_per_release: z.number().nullable(),
  thin_forks: z.number().int().nonnegative(),
  thin_fork_share: z.number().nullable(),
  median_fork_lag_hours: z.number().nullable(),
  median_fork_delta: z.number().nullable(),
  new_highs: z.number().int().nonnegative(),
  new_highs_by_driver: counts(),
  frontier_gain_by_bench: counts(),
  gain_per_day_by_bench: counts(),
} satisfies PlatformResponseShape<Schemas['CompetitionEmbargoWindow']>)

export const competitionImprovementMetricsSchema = z.object({
  metrics_version: z.string(),
  generated_at: timestamp(),
  bench_version: z.number().int().nullable(),
  since: day().nullable(),
  definitions: z.record(z.string(), z.string()),
  thresholds: z.object({
    first_bench_version: z.number().int(),
    fork_containment: z.number(),
    thin_fork_novel_fraction: z.number(),
    top_n: z.number().int(),
    noise_agents: z.number().int(),
    min_noise_seeds: z.number().int(),
    noise_sigmas: z.number(),
    close_after_days: z.number().int(),
  } satisfies PlatformResponseShape<Schemas['CompetitionThresholds']>),
  coverage: z.object({
    stored_days: z.number().int().nonnegative(),
    closed_days: z.number().int().nonnegative(),
    backfilled_days: z.number().int().nonnegative(),
    last_computed_at: timestamp().nullable(),
    agents: z.number().int().nonnegative(),
    indexed_agents: z.number().int().nonnegative(),
  } satisfies PlatformResponseShape<Schemas['CompetitionCoverage']>),
  runtime: z
    .object({
      running: z.boolean(),
      metrics_version: z.string(),
      interval_seconds: z.number(),
      last_tick_started_at: timestamp().nullable(),
      last_tick_finished_at: timestamp().nullable(),
      last_tick_counters: z.record(z.string(), z.number().int()),
      last_tick_error: z.string().nullable(),
    } satisfies PlatformResponseShape<Schemas['CompetitionJobRuntime']>)
    .nullable(),
  benches: z.array(benchSchema),
  releases: z.array(releaseSchema),
  embargo_windows: z.array(embargoWindowSchema),
} satisfies PlatformResponseShape<Schemas['CompetitionImprovementMetrics']>)

export const competitionImprovementMetricsInputSchema = z.object({
  benchVersion: z.number().int().positive().optional(),
  since: day().optional(),
})
