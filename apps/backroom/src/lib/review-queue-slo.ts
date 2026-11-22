import type { AthReviewQueueSlo, SourceReviewQueueSlo } from './admin.schemas'

/**
 * One review clock that is past its SLO (SN-499).
 *
 * `clock` is `source_review` for the ordinary pre-score queue, `ath` for the
 * aggregate over every ATH hold, or the ATH queue class. Ages are hours,
 * rounded to one decimal, because a breach is read by a person.
 */
export type OverdueQueueClock = {
  clock: string
  backlog_count: number
  overdue_count: number | null
  max_age_threshold_hours: number | null
  oldest_age_hours: number | null
  oldest_agent_id: string | null
  oldest_reason: string | null
  p95_age_hours: number | null
  p95_threshold_hours: number | null
  p95_exceeds_threshold: boolean | null
}

type ClockStats = {
  backlog_count: number
  overdue_count: number | null
  max_actionable_age_threshold_seconds: number | null
  oldest_age_seconds: number | null
  p95_age_seconds: number | null
  p95_age_threshold_seconds: number | null
  p95_exceeds_threshold: boolean | null
  oldest_agent_id?: string | null
  oldest_reason?: string | null
}

function hours(seconds: number | null): number | null {
  return seconds === null ? null : Math.round((seconds / 3600) * 10) / 10
}

function clockRow(clock: string, stats: ClockStats): OverdueQueueClock {
  return {
    clock,
    backlog_count: stats.backlog_count,
    overdue_count: stats.overdue_count,
    max_age_threshold_hours: hours(stats.max_actionable_age_threshold_seconds),
    oldest_age_hours: hours(stats.oldest_age_seconds),
    oldest_agent_id: stats.oldest_agent_id ?? null,
    oldest_reason: stats.oldest_reason ?? null,
    p95_age_hours: hours(stats.p95_age_seconds),
    p95_threshold_hours: hours(stats.p95_age_threshold_seconds),
    p95_exceeds_threshold: stats.p95_exceeds_threshold,
  }
}

function breached(stats: ClockStats): boolean {
  return (stats.overdue_count ?? 0) > 0 || stats.p95_exceeds_threshold === true
}

/** Every clock with an overdue item or a p95 past its threshold. */
export function overdueQueueClocks(
  ordinary: SourceReviewQueueSlo | null,
  ath: AthReviewQueueSlo | null,
): OverdueQueueClock[] {
  const rows: OverdueQueueClock[] = []
  if (ordinary !== null && breached(ordinary)) {
    rows.push(clockRow('source_review', ordinary))
  }
  if (ath !== null) {
    if (breached(ath.ath)) rows.push(clockRow('ath', ath.ath))
    for (const stats of ath.classes) {
      if (stats.queue_class !== null && breached(stats)) {
        rows.push(clockRow(stats.queue_class, stats))
      }
    }
  }
  return rows
}
