export const CONFIRMATION_LANE_STATES = [
  'blocked_budget',
  'pending',
  'leased',
  'failed',
  'completed',
  'superseded',
] as const

export type ConfirmationLaneState = (typeof CONFIRMATION_LANE_STATES)[number]

export type ConfirmationLaneTicket = {
  ticket_id: string
  validator_hotkey: string
  slot_id: string
  status: string
  issued_at: string
  failed_at: string | null
  failure_reason: string | null
  failure_class: string | null
  failure_stage: string | null
  prepare_rejection: string | null
  prepare_rejected_at: string | null
}

export type ConfirmationLaneBundle = {
  bundle_id: string
  bench_version: number
  state: string
  created_at: string
  tickets: ConfirmationLaneTicket[]
}

export type ConfirmationLanePage = {
  count: number
  items: ConfirmationLaneBundle[]
  budget?: {
    utc_day: string
    revision: number
    issued_attempts: number
    outstanding_reserved_microusd: number
    settled_microusd: number
  }
  shadow_calibration?: {
    completed_bundle_count: number
    failed_bundle_count: number
    superseded_bundle_count: number
    bench_version: number
  }
}

export type ConfirmationLaneDiagnosisInput = {
  observedAt: string
  mode: 'off' | 'shadow' | 'enforce'
  issuanceActive: boolean
  settingsRevision: number
  dailyBundleCap: number
  dailyDollarCapMicrousd: number
  profileRevision: string | null
  fleet: {
    generated_at: string
    active_bench_version: number | null | undefined
    validators: Array<{
      validator_hotkey: string
      online: boolean
      bench_serviceability: string
      active_benchmark_count: number
      updater_status: { current_version?: string | null } | null
    }>
  } | null
  pages: Record<ConfirmationLaneState, ConfirmationLanePage>
}

export type ConfirmationLaneLikelyCauseCode =
  | 'issuance_disabled'
  | 'issuance_inactive'
  | 'budget_blocked'
  | 'leftover_validator_v9_identity_pin'
  | 'claim_identity_rejection'
  | 'claim_without_progress'
  | 'execution_after_preparing'
  | 'prepare_report_rejected'
  | 'healthy'
  | 'unknown_execution_outage'

const IMMEDIATE_FAIL_MS = 30_000
const STALE_LEASE_MS = 15 * 60_000
const SAMPLE_LIMIT = 8

function timestampMs(value: string | null | undefined): number | null {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

function durationMs(start: string, end: string | null, fallbackEnd: string): number | null {
  const from = timestampMs(start)
  const to = timestampMs(end) ?? timestampMs(fallbackEnd)
  if (from === null || to === null || to < from) return null
  return to - from
}

function increment(counts: Record<string, number>, key: string) {
  counts[key] = (counts[key] ?? 0) + 1
}

function sortedHistogram(counts: Record<string, number>) {
  return Object.entries(counts)
    .map(([key, count]) => ({ key, count }))
    .sort((left, right) => right.count - left.count || left.key.localeCompare(right.key))
}

function median(values: number[]): number | null {
  if (values.length === 0) return null
  const ordered = [...values].sort((left, right) => left - right)
  const mid = Math.floor(ordered.length / 2)
  return ordered.length % 2 === 0
    ? Math.round((ordered[mid - 1]! + ordered[mid]!) / 2)
    : ordered[mid]!
}

function share(part: number, whole: number): number {
  return whole === 0 ? 0 : part / whole
}

export function diagnoseConfirmationLane(input: ConfirmationLaneDiagnosisInput) {
  const observedAtMs = timestampMs(input.observedAt) ?? Date.now()
  const counts = Object.fromEntries(
    CONFIRMATION_LANE_STATES.map((state) => [state, input.pages[state].count]),
  ) as Record<ConfirmationLaneState, number>

  const sampledBundles = CONFIRMATION_LANE_STATES.flatMap((state) => input.pages[state].items)
  const benchVersions: Record<string, number> = {}
  for (const bundle of sampledBundles) increment(benchVersions, String(bundle.bench_version))

  const failedTickets = input.pages.failed.items.flatMap((bundle) =>
    bundle.tickets
      .filter((ticket) => ticket.failed_at !== null || ticket.failure_reason !== null)
      .map((ticket) => ({
        bundle_id: bundle.bundle_id,
        bench_version: bundle.bench_version,
        ticket,
        duration_ms: durationMs(ticket.issued_at, ticket.failed_at, input.observedAt),
      })),
  )
  failedTickets.sort((left, right) => {
    const leftFailed = timestampMs(left.ticket.failed_at) ?? 0
    const rightFailed = timestampMs(right.ticket.failed_at) ?? 0
    return rightFailed - leftFailed
  })

  const failureKeys: Record<string, { count: number; durations: number[] }> = {}
  for (const row of failedTickets) {
    const key = [
      row.ticket.failure_class ?? 'null',
      row.ticket.failure_stage ?? 'null',
      row.ticket.failure_reason ?? 'null',
      row.ticket.prepare_rejection ?? 'null',
    ].join('|')
    const bucket = failureKeys[key] ?? { count: 0, durations: [] }
    bucket.count += 1
    if (row.duration_ms !== null) bucket.durations.push(row.duration_ms)
    failureKeys[key] = bucket
  }

  const failureHistogram = Object.entries(failureKeys)
    .map(([key, bucket]) => {
      const [failure_class, failure_stage, failure_reason, prepare_rejection] = key.split('|')
      return {
        failure_class,
        failure_stage,
        failure_reason,
        prepare_rejection,
        count: bucket.count,
        median_duration_ms: median(bucket.durations),
      }
    })
    .sort((left, right) => right.count - left.count || left.failure_class.localeCompare(right.failure_class))

  const leasedTickets = input.pages.leased.items.flatMap((bundle) =>
    bundle.tickets
      .filter((ticket) => ticket.status === 'issued')
      .map((ticket) => {
        const issued = timestampMs(ticket.issued_at)
        return {
          bundle_id: bundle.bundle_id,
          bench_version: bundle.bench_version,
          ticket_id: ticket.ticket_id,
          validator_hotkey: ticket.validator_hotkey,
          slot_id: ticket.slot_id,
          issued_at: ticket.issued_at,
          age_ms: issued === null ? null : Math.max(0, observedAtMs - issued),
          failure_stage: ticket.failure_stage,
        }
      }),
  )
  leasedTickets.sort((left, right) => (right.age_ms ?? -1) - (left.age_ms ?? -1))

  const budget =
    input.pages.failed.budget ??
    input.pages.leased.budget ??
    input.pages.pending.budget ??
    input.pages.completed.budget ??
    null
  const shadow =
    input.pages.failed.shadow_calibration ??
    input.pages.completed.shadow_calibration ??
    input.pages.leased.shadow_calibration ??
    null

  const fleet = input.fleet
    ? {
        generated_at: input.fleet.generated_at,
        active_bench_version: input.fleet.active_bench_version ?? null,
        validator_count: input.fleet.validators.length,
        online_count: input.fleet.validators.filter((row) => row.online).length,
        serving_count: input.fleet.validators.filter(
          (row) => row.bench_serviceability === 'serving',
        ).length,
        active_benchmark_count: input.fleet.validators.reduce(
          (total, row) => total + row.active_benchmark_count,
          0,
        ),
        versions: sortedHistogram(
          input.fleet.validators.reduce<Record<string, number>>((counts, row) => {
            increment(counts, row.updater_status?.current_version ?? 'unknown')
            return counts
          }, {}),
        ),
      }
    : null

  const platformUnknown = failedTickets.filter(
    (row) =>
      row.ticket.failure_class === 'platform' &&
      (row.ticket.failure_stage === 'unknown' || row.ticket.failure_stage === 'preparing'),
  )
  const immediateFails = failedTickets.filter(
    (row) => row.duration_ms !== null && row.duration_ms <= IMMEDIATE_FAIL_MS,
  )
  const nonV9Sample = sampledBundles.filter((bundle) => bundle.bench_version !== 9)
  const laterExecutionFails = failedTickets.filter((row) => {
    const stage = row.ticket.failure_stage
    return (
      stage === 'running_confirmation' ||
      stage === 'finalizing' ||
      stage === 'dimension_execution'
    )
  })
  const prepareRejected = failedTickets.filter(
    (row) => row.ticket.prepare_rejection != null && row.ticket.prepare_rejection !== '',
  )
  const staleLeases = leasedTickets.filter(
    (row) => row.age_ms !== null && row.age_ms >= STALE_LEASE_MS,
  )

  const likelyCause = deriveLikelyCause({
    mode: input.mode,
    issuanceActive: input.issuanceActive,
    dailyBundleCap: input.dailyBundleCap,
    counts,
    completed: shadow?.completed_bundle_count ?? counts.completed,
    failedSample: failedTickets.length,
    platformUnknown: platformUnknown.length,
    immediateFails: immediateFails.length,
    nonV9Share: share(nonV9Sample.length, sampledBundles.length),
    laterExecutionFails: laterExecutionFails.length,
    prepareRejected: prepareRejected.length,
    staleLeases: staleLeases.length,
    leased: counts.leased,
    issuedAttempts: budget?.issued_attempts ?? 0,
  })

  return {
    observed_at: input.observedAt,
    policy: {
      mode: input.mode,
      issuance_active: input.issuanceActive,
      settings_revision: input.settingsRevision,
      daily_bundle_cap: input.dailyBundleCap,
      daily_dollar_cap_microusd: input.dailyDollarCapMicrousd,
      profile_revision: input.profileRevision,
    },
    budget,
    counts,
    shadow_calibration: shadow,
    sample_window: {
      per_state_limit: 100,
      sampled_bundle_count: sampledBundles.length,
      sampled_failed_ticket_count: failedTickets.length,
    },
    bench_versions: sortedHistogram(benchVersions),
    failure_histogram: failureHistogram,
    leased: {
      count: counts.leased,
      sampled_ticket_count: leasedTickets.length,
      oldest_age_ms: leasedTickets[0]?.age_ms ?? null,
      stale_count: staleLeases.length,
      sample: leasedTickets.slice(0, SAMPLE_LIMIT),
    },
    recent_failures: failedTickets.slice(0, SAMPLE_LIMIT).map((row) => ({
      bundle_id: row.bundle_id,
      bench_version: row.bench_version,
      ticket_id: row.ticket.ticket_id,
      validator_hotkey: row.ticket.validator_hotkey,
      slot_id: row.ticket.slot_id,
      failure_class: row.ticket.failure_class,
      failure_stage: row.ticket.failure_stage,
      failure_reason: row.ticket.failure_reason,
      prepare_rejection: row.ticket.prepare_rejection,
      prepare_rejected_at: row.ticket.prepare_rejected_at,
      issued_at: row.ticket.issued_at,
      failed_at: row.ticket.failed_at,
      duration_ms: row.duration_ms,
    })),
    fleet,
    likely_cause: likelyCause,
  }
}

function deriveLikelyCause(input: {
  mode: 'off' | 'shadow' | 'enforce'
  issuanceActive: boolean
  dailyBundleCap: number
  counts: Record<ConfirmationLaneState, number>
  completed: number
  failedSample: number
  platformUnknown: number
  immediateFails: number
  nonV9Share: number
  laterExecutionFails: number
  prepareRejected: number
  staleLeases: number
  leased: number
  issuedAttempts: number
}): { code: ConfirmationLaneLikelyCauseCode; summary: string; evidence: string[] } {
  if (input.mode === 'off') {
    return {
      code: 'issuance_disabled',
      summary: 'Confirmation mode is off, so Platform will not issue LongMem work.',
      evidence: [`mode=${input.mode}`],
    }
  }
  if (!input.issuanceActive) {
    return {
      code: 'issuance_inactive',
      summary:
        'Issuance is inactive. The mode is on but the frozen profile or other effective gate is missing.',
      evidence: [`mode=${input.mode}`, 'issuance_active=false'],
    }
  }
  if (
    input.counts.blocked_budget > 0 &&
    input.dailyBundleCap > 0 &&
    input.issuedAttempts >= input.dailyBundleCap
  ) {
    return {
      code: 'budget_blocked',
      summary: 'Issuance is blocked on the daily confirmation budget.',
      evidence: [
        `blocked_budget=${input.counts.blocked_budget}`,
        `issued_attempts=${input.issuedAttempts}`,
        `daily_bundle_cap=${input.dailyBundleCap}`,
      ],
    }
  }
  if (input.completed > 0 && input.failedSample === 0 && input.counts.leased === 0) {
    return {
      code: 'healthy',
      summary: 'The lane has completed evidence and no sampled execution failures.',
      evidence: [`completed=${input.completed}`],
    }
  }
  if (
    input.completed === 0 &&
    input.failedSample >= 3 &&
    share(input.platformUnknown, input.failedSample) >= 0.6 &&
    share(input.immediateFails, input.failedSample) >= 0.6 &&
    input.nonV9Share >= 0.5
  ) {
    return {
      code: 'leftover_validator_v9_identity_pin',
      summary:
        'Platform is issuing confirmation work, but validators reject it before execution. The leftover bench_version==9 identity pin matches this histogram.',
      evidence: [
        `completed=${input.completed}`,
        `failed_sample=${input.failedSample}`,
        `platform_unknown_or_preparing=${input.platformUnknown}`,
        `immediate_fails=${input.immediateFails}`,
        `non_v9_share=${input.nonV9Share.toFixed(2)}`,
      ],
    }
  }
  if (
    input.completed === 0 &&
    input.failedSample >= 3 &&
    share(input.platformUnknown, input.failedSample) >= 0.6 &&
    share(input.immediateFails, input.failedSample) >= 0.6
  ) {
    return {
      code: 'claim_identity_rejection',
      summary:
        'Claims fail immediately with platform/unknown-or-preparing. The validator is handing the ticket back before LongMem execution starts.',
      evidence: [
        `failed_sample=${input.failedSample}`,
        `platform_unknown_or_preparing=${input.platformUnknown}`,
        `immediate_fails=${input.immediateFails}`,
      ],
    }
  }
  if (input.leased > 0 && input.staleLeases > 0 && input.completed === 0) {
    return {
      code: 'claim_without_progress',
      summary:
        'Tickets are leased but have sat without a completed bundle. Check the holding validator slot and heartbeat stage.',
      evidence: [`leased=${input.leased}`, `stale_leases=${input.staleLeases}`],
    }
  }
  if (input.prepareRejected > 0 && input.prepareRejected >= input.platformUnknown) {
    return {
      code: 'prepare_report_rejected',
      summary:
        'Execute finished and prepare-report rejected the Go evidence. Read prepare_rejection on the ticket; it is the allowlisted convert/rebuild diagnostic, not the later fail-job class.',
      evidence: [
        `prepare_rejected=${input.prepareRejected}`,
        `later_execution_fails=${input.laterExecutionFails}`,
        `completed=${input.completed}`,
      ],
    }
  }
  if (input.laterExecutionFails > 0 && input.laterExecutionFails >= input.platformUnknown) {
    return {
      code: 'execution_after_preparing',
      summary:
        'Failures occur after the validator accepted the lease and entered confirmation execution.',
      evidence: [
        `later_execution_fails=${input.laterExecutionFails}`,
        `platform_unknown_or_preparing=${input.platformUnknown}`,
        `completed=${input.completed}`,
      ],
    }
  }
  if (input.completed > 0) {
    return {
      code: 'healthy',
      summary: 'The lane has completed evidence. Remaining failures are not the dominant sampled pattern.',
      evidence: [`completed=${input.completed}`, `failed_sample=${input.failedSample}`],
    }
  }
  return {
    code: 'unknown_execution_outage',
    summary:
      'Issuance is on and completed evidence is zero, but the sampled histogram does not match a known identity or budget signature.',
    evidence: [
      `completed=${input.completed}`,
      `failed_sample=${input.failedSample}`,
      `leased=${input.leased}`,
      `pending=${input.counts.pending}`,
    ],
  }
}
