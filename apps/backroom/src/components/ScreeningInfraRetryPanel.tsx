import { useServerFn } from '@tanstack/react-start'
import { RefreshCw } from 'lucide-react'
import { useState } from 'react'
import type { ScreeningInfraRetryOutcome, ScreeningInfraRetryView } from '../lib/admin.schemas'
import { getScreeningInfraRetries } from '../server/admin.functions'

type Agent = ScreeningInfraRetryView['agents'][number]

const STATE_LABELS: Record<Agent['state'], string> = {
  backoff: 'In backoff',
  breaker_held: 'Held by breaker',
  probe_due: 'Probe due',
  due: 'Due now',
  capped: 'Capped (operator)',
}

const OUTLOOK_LABELS: Record<Agent['claim_outlook'], string> = {
  ready: 'Ready; the claim may still skip it',
  waiting_backoff: 'Waiting for backoff',
  waiting_breaker: 'Waiting for breaker',
  needs_operator: 'Needs an operator retry',
  not_admitted: 'Not admitted by the claim guard',
}

const PHASE_LABELS: Record<ScreeningInfraRetryView['breakers'][number]['phase'], string> = {
  closed: 'Closed',
  open: 'Open: holding retries',
  half_open: 'Half-open: probing',
}

function failureLine(failure: { status: number | null; message: string }) {
  if (failure.status === 404) {
    return 'This Platform build does not expose /api/v1/admin/screening-infra-retries yet (HTTP 404). It is likely older than this Backroom.'
  }
  const status = failure.status === null ? '' : ` (HTTP ${failure.status})`
  return `Infrastructure retry state could not be read${status}: ${failure.message}`
}

function formatWhen(value: string | null) {
  if (!value) return '—'
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

function formatSeconds(seconds: number) {
  if (seconds % 3600 === 0) return `${seconds / 3600} h`
  if (seconds % 60 === 0) return `${seconds / 60} min`
  return `${seconds} s`
}

function signatureLabel(provider: string | null, lane: string | null) {
  return `${provider ?? 'unknown provider'} / ${lane ?? 'unknown lane'}`
}

function stateTone(state: Agent['state']) {
  if (state === 'capped') return 'bg-[var(--red-dim)] text-[var(--red)]'
  if (state === 'breaker_held' || state === 'backoff') return 'bg-[var(--amber-dim)] text-[var(--amber)]'
  return 'bg-[var(--cyan-dim)] text-[var(--cyan)]'
}

export function ScreeningInfraRetryPanel({
  initialState,
}: {
  initialState: ScreeningInfraRetryOutcome
}) {
  const fetchState = useServerFn(getScreeningInfraRetries)
  const [state, setState] = useState<ScreeningInfraRetryView | null>(
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
      setError(caught instanceof Error ? caught.message : 'Could not refresh retry state.')
    } finally {
      setLoading(false)
    }
  }

  const policy = state?.policy
  const policyRows: Array<[string, string]> = policy
    ? [
        ['Backoff', `${formatSeconds(policy.base_backoff_seconds)} doubling to ${formatSeconds(policy.max_backoff_seconds)}, ±${Math.round(policy.jitter_fraction * 100)}% jitter`],
        ['Stops after', `${policy.auto_retry_max_streak} consecutive failures or ${formatSeconds(policy.auto_retry_max_age_seconds)} since the last failure`],
        ['Breaker trips at', `${policy.breaker_distinct_agents} distinct agents within ${formatSeconds(policy.breaker_window_seconds)}`],
        ['Breaker open / probe', `open ${formatSeconds(policy.breaker_open_seconds)}, then one probe per ${formatSeconds(policy.breaker_probe_interval_seconds)}`],
        ['History read', formatSeconds(policy.breaker_history_lookback_seconds)],
        ['Retried codes', policy.auto_retry_reason_codes.join(', ')],
      ]
    : []

  return (
    <section
      aria-label="Infrastructure retries"
      className="mt-5 space-y-4 rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4 sm:p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Infrastructure retries</h2>
          <p className="mt-1 max-w-[72ch] text-xs leading-5 text-[var(--muted)]">
            Derived from screening attempt history when this page loads; nothing is stored. Capped
            agents, and aged-out agents (last infrastructure failure older than the maximum age;
            counted, not listed individually), wait for an operator retry. The breaker is per signature: reason code,
            provider, and lane.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh retries
        </button>
      </div>

      {error ? (
        <p role="alert" className="rounded-lg border border-[var(--red)]/30 bg-[var(--red-dim)] p-3 text-xs text-[var(--red)]">
          {error}
        </p>
      ) : null}

      {state ? (
        <>
          <p className="text-xs text-[var(--muted-strong)]">
            {state.summary.parked_agents} parked ·{' '}
            {(Object.keys(STATE_LABELS) as Array<Agent['state']>)
              .map((key) => `${STATE_LABELS[key]} ${state.summary.by_state[key] ?? 0}`)
              .join(' · ')}{' '}
            · {state.summary.not_admitted} not admitted · {state.summary.aged_out_agents} aged out
            (not listed; need an operator) · {state.summary.open_breakers} open and{' '}
            {state.summary.half_open_breakers} half-open of {state.summary.breakers_total} breakers · as of {formatWhen(state.generated_at)}
          </p>

          <dl className="grid gap-x-6 gap-y-1.5 text-xs sm:grid-cols-2">
            {policyRows.map(([label, value]) => (
              <div key={label} className="flex gap-2">
                <dt className="w-36 shrink-0 text-[var(--muted)]">{label}</dt>
                <dd className="text-[var(--muted-strong)]">{value}</dd>
              </div>
            ))}
          </dl>

          <div>
            <h3 className="text-xs font-semibold">Circuit breakers</h3>
            {state.breakers.length === 0 ? (
              <p className="mt-2 text-xs text-[var(--muted)]">No infrastructure failures in the history window.</p>
            ) : (
              <div className="mt-2 overflow-x-auto">
                <table className="w-full min-w-[720px] text-left text-xs">
                  <thead className="text-[var(--muted)]">
                    <tr>
                      <th className="py-2 pr-4 font-medium">Signature</th>
                      <th className="py-2 pr-4 font-medium">Breaker</th>
                      <th className="py-2 pr-4 font-medium">Opened</th>
                      <th className="py-2 pr-4 font-medium">Open until</th>
                      <th className="py-2 pr-4 font-medium">Last probe</th>
                      <th className="py-2 pr-4 font-medium">Next probe</th>
                      <th className="py-2 font-medium">Parked now</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--line)]">
                    {state.breakers.map((breaker) => (
                      <tr key={`${breaker.reason_code}|${breaker.provider}|${breaker.lane}`}>
                        <td className="py-2.5 pr-4">
                          <span className="font-medium">{breaker.reason_code}</span>
                          <span className="block text-[var(--muted)]">{signatureLabel(breaker.provider, breaker.lane)}</span>
                        </td>
                        <td className="py-2.5 pr-4">{PHASE_LABELS[breaker.phase]}</td>
                        <td className="py-2.5 pr-4">{formatWhen(breaker.opened_at)}</td>
                        <td className="py-2.5 pr-4">{formatWhen(breaker.open_until)}</td>
                        <td className="py-2.5 pr-4">{formatWhen(breaker.last_probe_at)}</td>
                        <td className="py-2.5 pr-4">{formatWhen(breaker.next_probe_at)}</td>
                        <td className="py-2.5 tabular-nums">{breaker.parked_agents}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {state.breakers_truncated ? (
              <p className="mt-2 text-xs text-[var(--amber)]">
                Showing {state.breakers.length} of {state.summary.breakers_total} signatures.
              </p>
            ) : null}
          </div>

          <div>
            <h3 className="text-xs font-semibold">Parked agents</h3>
            {state.agents.length === 0 ? (
              <p className="mt-2 text-xs text-[var(--muted)]">No agent is parked on an infrastructure failure.</p>
            ) : (
              <div className="mt-2 overflow-x-auto">
                <table className="w-full min-w-[900px] text-left text-xs">
                  <thead className="text-[var(--muted)]">
                    <tr>
                      <th className="py-2 pr-4 font-medium">Agent</th>
                      <th className="py-2 pr-4 font-medium">Signature</th>
                      <th className="py-2 pr-4 font-medium">State</th>
                      <th className="py-2 pr-4 font-medium">Failures</th>
                      <th className="py-2 pr-4 font-medium">Failed</th>
                      <th className="py-2 pr-4 font-medium">Next retry</th>
                      <th className="py-2 font-medium">Claim outlook</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--line)]">
                    {state.agents.map((agent) => (
                      <tr key={agent.agent_id}>
                        <td className="py-2.5 pr-4 font-mono" title={`latest attempt ${agent.attempt_id}`}>
                          {agent.agent_id}
                        </td>
                        <td className="py-2.5 pr-4 text-[var(--muted-strong)]">
                          {agent.reason_code}
                          <span className="block text-[var(--muted)]">{signatureLabel(agent.provider, agent.lane)}</span>
                        </td>
                        <td className="py-2.5 pr-4">
                          <span className={`rounded-full px-2 py-1 font-medium ${stateTone(agent.state)}`}>
                            {STATE_LABELS[agent.state]}
                          </span>
                        </td>
                        <td className="py-2.5 pr-4 tabular-nums">{agent.consecutive_failures}</td>
                        <td className="py-2.5 pr-4">{formatWhen(agent.failed_at)}</td>
                        <td className="py-2.5 pr-4">{agent.state === 'capped' ? '—' : formatWhen(agent.next_retry_at)}</td>
                        <td className="py-2.5">{OUTLOOK_LABELS[agent.claim_outlook]}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {state.agents_truncated ? (
              <p className="mt-2 text-xs text-[var(--amber)]">
                Showing the {state.agents.length} agents with the earliest next retry, of{' '}
                {state.summary.parked_agents} parked.
              </p>
            ) : null}
          </div>
        </>
      ) : null}
    </section>
  )
}
