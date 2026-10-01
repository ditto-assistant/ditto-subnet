import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, Hourglass, RefreshCw } from 'lucide-react'
import {
  SCORING_LEASE_SETTINGS_SCOPE,
  scoringLeaseConfirmation,
  type ScoringLeaseSettingsControl,
} from '../lib/admin.schemas'
import { getScoringLeaseSettings, updateScoringLeaseSettings } from '../server/admin.functions'

function formatWhen(value: string | undefined) {
  if (!value) return 'Shipped default'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

export function ScoringLeaseControlPanel({
  initialState,
  readOnly,
}: {
  initialState: ScoringLeaseSettingsControl
  readOnly: boolean
}) {
  const refreshState = useServerFn(getScoringLeaseSettings)
  const applySettings = useServerFn(updateScoringLeaseSettings)
  const [state, setState] = useState(initialState)
  const current = state.effective.settings.scoring_ticket_ttl_minutes
  const min = state.effective.min_scoring_ticket_ttl_minutes
  const max = state.effective.max_scoring_ticket_ttl_minutes
  const [minutes, setMinutes] = useState(String(current))
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const parsed = Number(minutes)
  const selected = Number.isInteger(parsed) && parsed >= min && parsed <= max ? parsed : null
  const invalid = minutes.trim() !== '' && selected === null
  const needsRevision = selected !== current || state.effective.settings_valid === false
  const expectedConfirmation = selected === null ? '' : scoringLeaseConfirmation(selected)
  const ready =
    selected !== null &&
    needsRevision &&
    reason.trim().length >= 8 &&
    confirmation === expectedConfirmation

  const resetForm = (next: ScoringLeaseSettingsControl) => {
    setMinutes(String(next.effective.settings.scoring_ticket_ttl_minutes))
    setReason('')
    setConfirmation('')
  }

  const refresh = async () => {
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await refreshState()
      setState(next)
      resetForm(next)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh scoring lease settings')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!ready || selected === null) return
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await applySettings({
        data: {
          scope: SCORING_LEASE_SETTINGS_SCOPE,
          expectedRevision: state.effective.revision,
          settings: { scoring_ticket_ttl_minutes: selected },
          reason,
          confirmation,
        },
      })
      setState(next)
      resetForm(next)
      setSuccess(`New scoring leases now last ${selected} minutes.`)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to update scoring lease settings')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="mt-6 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
            <Hourglass className="h-4 w-4" />
          </div>
          <div>
            <h2 className="text-sm font-semibold">Scoring lease TTL</h2>
            <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted)]">
              Deadline stamped on new canonical scoring and score-retest replacement tickets. The
              validator run budget follows the lease, so this binds the fleet without a validator
              release.
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] transition-colors hover:border-[var(--line-strong)] hover:bg-white/5 disabled:opacity-40"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh lease policy
        </button>
      </div>

      <div className="p-4 sm:p-5">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="text-xs text-[var(--muted)]">Effective scoring TTL</p>
            <p className="mt-2 text-3xl font-semibold tracking-tight">{current} minutes</p>
          </div>
          <dl className="grid gap-3 text-xs sm:grid-cols-3 sm:text-right">
            <div>
              <dt className="text-[var(--muted)]">Source</dt>
              <dd className="mt-1 font-medium">
                {state.effective.source === 'revision' ? 'Operator revision' : 'Shipped default'}
              </dd>
            </div>
            <div>
              <dt className="text-[var(--muted)]">Revision</dt>
              <dd className="mt-1 font-medium">{state.effective.revision}</dd>
            </div>
            <div>
              <dt className="text-[var(--muted)]">Applied</dt>
              <dd className="mt-1 font-medium">
                {state.effective.settings_valid === false
                  ? 'Not applied (invalid revision)'
                  : formatWhen(state.current[0]?.created_at)}
              </dd>
            </div>
          </dl>
        </div>

        {state.effective.settings_valid === false ? (
          <p role="alert" className="mt-4 text-xs text-[var(--amber)]">
            Stored revision r{state.effective.revision} is invalid. New leases use the shipped
            {state.default.scoring_ticket_ttl_minutes}-minute default. Its audit fields and checksum
            describe the original stored record, not this fallback. Apply a new valid revision to
            repair the policy.
          </p>
        ) : null}
        <div className="mt-5 rounded-lg border border-[var(--amber)]/25 bg-[var(--amber-dim)] px-4 py-3 text-xs leading-5 text-[var(--amber)]">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <p>
              Applies to new leases only. A live ticket keeps the deadline it was issued with, so
              lowering the TTL never cuts off running work. The ceiling of {max} minutes stays
              inside the validator restart drain.
            </p>
          </div>
        </div>

        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <label className="text-xs font-medium text-[var(--muted-strong)]">
            Scoring TTL in minutes ({min}–{max})
            <input
              type="number"
              inputMode="numeric"
              min={min}
              max={max}
              step={1}
              value={minutes}
              disabled={readOnly || loading}
              onChange={(event) => {
                setMinutes(event.target.value)
                setConfirmation('')
                setError('')
                setSuccess('')
              }}
              aria-invalid={invalid}
              className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
            />
            {invalid ? (
              <span className="mt-1 block text-[11px] text-[var(--red)]">
                Enter a whole number from {min} through {max}.
              </span>
            ) : null}
          </label>
          <label className="text-xs font-medium text-[var(--muted-strong)]">
            Lease change reason
            <input
              type="text"
              value={reason}
              disabled={readOnly || loading}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Why the scoring lease is changing"
              className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
            />
          </label>
        </div>

        {selected !== null && needsRevision ? (
          <label className="mt-4 block text-xs font-medium text-[var(--muted-strong)]">
            Type to confirm
            <code className="ml-2 break-all text-[11px] text-[var(--cyan)]">
              {expectedConfirmation}
            </code>
            <input
              type="text"
              value={confirmation}
              disabled={readOnly || loading}
              onChange={(event) => setConfirmation(event.target.value)}
              className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 font-mono text-xs outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
            />
          </label>
        ) : null}

        {error ? <p className="mt-4 text-xs leading-5 text-[var(--red)]">{error}</p> : null}
        {success ? (
          <p className="mt-4 flex items-center gap-2 text-xs text-[var(--acid)]">
            <CheckCircle2 className="h-4 w-4" />
            {success}
          </p>
        ) : null}

        <div className="mt-5 flex justify-end">
          <button
            type="button"
            onClick={() => void submit()}
            disabled={readOnly || loading || !ready}
            className="min-h-11 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-black transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-35"
          >
            {loading ? 'Applying…' : 'Apply scoring TTL'}
          </button>
        </div>

        {state.history.length ? (
          <ol className="mt-5 space-y-2 border-t border-[var(--line)] pt-4 text-xs">
            {state.history.slice(0, 5).map((row) => (
              <li key={row.revision} className="flex flex-wrap gap-x-3 gap-y-1">
                <span className="font-medium">r{row.revision}</span>
                <span>
                  {row.settings_valid === false
                    ? 'Invalid stored policy; default fallback shown'
                    : `${row.settings.scoring_ticket_ttl_minutes} minutes`}
                </span>
                <span className="text-[var(--muted)]">{row.actor}</span>
                <span className="text-[var(--muted)]">{formatWhen(row.created_at)}</span>
                <span className="basis-full text-[var(--muted)]">{row.reason}</span>
              </li>
            ))}
          </ol>
        ) : null}
      </div>
    </section>
  )
}
