import { AlertTriangle, Clock } from 'lucide-react'
import type { OverdueQueueClock } from '../lib/review-queue-slo'

const CLOCK_LABEL: Record<string, string> = {
  source_review: 'Source review (pre-score)',
  ath: 'All ATH holds',
  copy: 'Copy',
  benchmark_overfit: 'Benchmark overfit',
  deferred_source_review: 'Deferred source review',
  integrity_double_check: 'Top-five double check',
  anomalous_score: 'Anomalous score',
}

function age(hoursValue: number | null): string {
  if (hoursValue === null) return 'n/a'
  return hoursValue >= 48 ? `${Math.round(hoursValue / 24)}d` : `${hoursValue}h`
}

/**
 * Review clocks past their SLO (SN-499). Read-only: it names the breach and
 * the oldest held agent, and never acts on a hold.
 */
export function ReviewQueueOverdue({
  clocks,
  error,
}: {
  clocks: OverdueQueueClock[]
  error: string | null
}) {
  if (clocks.length === 0 && error === null) {
    return (
      <p className="mb-4 flex items-center gap-2 text-xs text-[var(--muted)]">
        <Clock className="h-3.5 w-3.5" />
        Every review clock is within its SLO.
      </p>
    )
  }
  return (
    <section
      aria-label="Review queues past SLO"
      className="mb-5 rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-4"
    >
      {clocks.length > 0 ? (
        <>
          <h2 className="flex items-center gap-2 text-sm font-semibold text-[var(--red)]">
            <AlertTriangle className="h-4 w-4" />
            {clocks.length} review {clocks.length === 1 ? 'clock is' : 'clocks are'} past SLO
          </h2>
          <ul className="mt-3 space-y-1.5 text-xs text-[var(--muted-strong)]">
            {clocks.map((clock) => (
              <li key={clock.clock} className="flex flex-wrap gap-x-3 gap-y-1">
                <span className="font-medium text-[var(--ink)]">
                  {CLOCK_LABEL[clock.clock] ?? clock.clock}
                </span>
                {clock.overdue_count !== null ? (
                  <span>
                    {clock.overdue_count} of {clock.backlog_count} older than{' '}
                    {age(clock.max_age_threshold_hours)}
                  </span>
                ) : null}
                {clock.p95_exceeds_threshold ? (
                  <span>
                    p95 {age(clock.p95_age_hours)} &gt; {age(clock.p95_threshold_hours)}
                  </span>
                ) : null}
                <span>
                  oldest {age(clock.oldest_age_hours)}
                  {clock.oldest_reason ? ` (${clock.oldest_reason})` : ''}
                </span>
                {clock.oldest_agent_id ? (
                  <span className="font-mono text-[var(--muted)]" title={clock.oldest_agent_id}>
                    {clock.oldest_agent_id.slice(0, 8)}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {error !== null ? (
        <p className="mt-2 text-xs text-[var(--muted)]">Queue SLO read failed: {error}</p>
      ) : null}
    </section>
  )
}
