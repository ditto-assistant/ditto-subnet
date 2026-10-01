import { useServerFn } from '@tanstack/react-start'
import { RefreshCw } from 'lucide-react'
import { useState } from 'react'
import type {
  SubnetLiveness,
  SubnetLivenessOutcome,
  SubnetLivenessSignal,
} from '../lib/admin.schemas'
import { getSubnetLiveness } from '../server/admin.functions'

const SIGNAL_LABELS: Record<SubnetLivenessSignal['name'], string> = {
  screening_admission: 'Screening admission at 0',
  oldest_claimable_upload: 'Oldest claimable upload',
  scoring_throughput: 'No accepted score',
  v13_scorer_cohort_pin: 'Stale v13 pin members',
  oldest_actionable_hold: 'Oldest actionable hold',
  lease_overrun: 'Lease past deadline',
  source_emission_collector: 'Emission collector cursor',
}

const STATUS_TONES: Record<SubnetLiveness['status'], string> = {
  ok: 'bg-[var(--acid-dim)] text-[var(--acid)]',
  warn: 'bg-[var(--amber-dim)] text-[var(--amber)]',
  breach: 'bg-[var(--red-dim)] text-[var(--red)]',
}

function failureLine(failure: { status: number | null; message: string }) {
  if (failure.status === 404) {
    return 'This Platform build does not expose /api/v1/admin/subnet-liveness yet (HTTP 404). It is likely older than this Backroom.'
  }
  const status = failure.status === null ? '' : ` (HTTP ${failure.status})`
  return `Subnet liveness could not be read${status}: ${failure.message}`
}

export function formatLivenessValue(
  value: number | null,
  unit: SubnetLivenessSignal['unit'],
) {
  if (value === null) return 'n/a'
  if (unit === 'members') return `${value} ${value === 1 ? 'member' : 'members'}`
  // Round down, so a value just under a threshold never displays as equal to it.
  const floorTo = (amount: number, step: number) => Math.floor(amount * step) / step
  if (value < 60) return `${Math.floor(value)} s`
  if (value < 3600) return `${Math.floor(value / 60)} min`
  if (value < 86_400) return `${floorTo(value / 3600, 10).toFixed(1)} h`
  return `${floorTo(value / 86_400, 10).toFixed(1)} d`
}

function formatWhen(value: string | null) {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'UTC',
    timeZoneName: 'short',
  }).format(parsed)
}

function StatusChip({ status }: { status: SubnetLiveness['status'] }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase ${STATUS_TONES[status]}`}>
      {status}
    </span>
  )
}

export function SubnetLivenessPanel({ initialState }: { initialState: SubnetLivenessOutcome }) {
  const fetchState = useServerFn(getSubnetLiveness)
  const [state, setState] = useState<SubnetLiveness | null>(
    initialState.ok ? initialState.view : null,
  )
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(
    initialState.ok ? null : failureLine(initialState),
  )

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      const outcome = await fetchState()
      if (outcome.ok) setState(outcome.view)
      else setError(failureLine(outcome))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not refresh subnet liveness.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section
      aria-label="Subnet liveness"
      className="mt-5 space-y-3 rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4 sm:p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            Subnet liveness {state ? <StatusChip status={state.status} /> : null}
          </h2>
          <p className="mt-1 max-w-[72ch] text-xs leading-5 text-[var(--muted)]">
            Conditions that stop every miner at once, from durable Platform state. Higher is worse;
            thresholds are liveness alarms, not policy clocks. Nothing here pages anyone.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh liveness
        </button>
      </div>

      {error ? (
        <p role="alert" className="rounded-lg border border-[var(--red)]/30 bg-[var(--red-dim)] p-3 text-xs text-[var(--red)]">
          {error}
        </p>
      ) : null}

      {state ? (
        <>
          <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {state.signals.map((signal) => {
              const since = formatWhen(signal.since)
              return (
                <li
                  key={signal.name}
                  className="rounded-lg border border-[var(--line)] p-3 text-xs"
                  title={signal.hint}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{SIGNAL_LABELS[signal.name]}</span>
                    <StatusChip status={signal.status} />
                  </div>
                  <p className="mt-1.5 text-lg font-semibold tracking-[-0.02em]">
                    {formatLivenessValue(signal.value, signal.unit)}
                  </p>
                  <p className="text-[var(--muted)]">
                    breach at {formatLivenessValue(signal.threshold, signal.unit)}
                    {signal.warn_threshold !== null
                      ? ` · warn at ${formatLivenessValue(signal.warn_threshold, signal.unit)}`
                      : ''}
                    {since ? ` · since ${since}` : ''}
                  </p>
                  {signal.status === 'ok' ? null : (
                    <p className="mt-1.5 leading-4 text-[var(--muted-strong)]">{signal.hint}</p>
                  )}
                </li>
              )
            })}
          </ul>
          <p className="text-[11px] leading-4 text-[var(--muted)]">
            Not derivable here: {state.unavailable.map((item) => item.name).join(', ') || 'none'} ·
            as of {formatWhen(state.generated_at)}
          </p>
        </>
      ) : null}
    </section>
  )
}
