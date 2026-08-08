import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, Flame, RefreshCw } from 'lucide-react'
import { BURN_CONFIRMATION, type BurnSettingsControl } from '../lib/admin.schemas'
import { getBurnSettings, updateBurnSettings } from '../server/admin.functions'

// Round trips through percent are exact enough for the shares an operator types
// and keep 0.1% resolution, which is finer than any burn worth setting.
const percentOf = (share: number) => Math.round(share * 1000) / 10
const shareOf = (percent: number) => Math.round(percent * 10) / 10 / 100

const presets = [0, 10, 25, 50] as const

function formatWhen(value: string | null | undefined) {
  if (!value) return 'Never — built-in default'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

export function BurnControlPanel({
  initialState,
  readOnly,
}: {
  initialState: BurnSettingsControl
  readOnly: boolean
}) {
  const refreshState = useServerFn(getBurnSettings)
  const applySettings = useServerFn(updateBurnSettings)
  const [state, setState] = useState(initialState)
  const [percent, setPercent] = useState(String(percentOf(initialState.effective.settings.burn_share)))
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const effective = state.effective
  const currentPercent = percentOf(effective.settings.burn_share)
  const minPercent = percentOf(effective.min_burn_share)
  const maxPercent = percentOf(effective.max_burn_share)
  const parsedPercent = Number(percent)
  const selectedShare =
    percent.trim() !== '' &&
    Number.isFinite(parsedPercent) &&
    parsedPercent >= minPercent &&
    parsedPercent <= maxPercent
      ? shareOf(parsedPercent)
      : null
  const invalid = percent.trim() !== '' && selectedShare === null
  const ready =
    selectedShare !== null &&
    selectedShare !== effective.settings.burn_share &&
    reason.trim().length >= 8 &&
    confirmation === BURN_CONFIRMATION
  const detached = effective.live_validator_count === 0

  const resetForm = (share = effective.settings.burn_share) => {
    setPercent(String(percentOf(share)))
    setReason('')
    setConfirmation('')
  }

  const selectPercent = (value: number) => {
    setPercent(String(value))
    setReason('')
    setConfirmation('')
    setError('')
    setSuccess('')
  }

  const refresh = async () => {
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await refreshState()
      setState(next)
      resetForm(next.effective.settings.burn_share)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh burn policy')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!ready || selectedShare === null) return
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await applySettings({
        data: {
          expectedRevision: effective.revision,
          settings: { burn_share: selectedShare },
          reason,
          confirmation,
        },
      })
      setState(next)
      setSuccess(
        `Burn set to ${percentOf(next.effective.settings.burn_share)}% of miner emission at revision ${next.effective.revision}. Validators pick it up on their next ledger read and apply it at their next weight epoch.`,
      )
      resetForm(next.effective.settings.burn_share)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to update burn policy')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mt-6 space-y-5">
      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--amber-dim)] text-[var(--amber)]">
              <Flame className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-semibold">Miner emission burn</h2>
              <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted)]">
                The share of miner emission validators route to the subnet owner&rsquo;s burn
                hotkey. The remainder is normalized across the eligible miner weights, so this
                scales the competitive vector without re-ordering it: every miner keeps the same
                share of what miners receive.
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
            Refresh policy
          </button>
        </div>

        <div className="p-4 sm:p-5">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="text-xs text-[var(--muted)]">Burned</p>
              <p className="mt-2 text-3xl font-semibold tracking-tight">{currentPercent}%</p>
            </div>
            <dl className="grid gap-3 text-xs sm:grid-cols-4 sm:text-right">
              <div>
                <dt className="text-[var(--muted)]">To miners</dt>
                <dd className="mt-1 font-medium">{percentOf(effective.miner_emission_share)}%</dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Revision</dt>
                <dd className="mt-1 font-medium">
                  {effective.revision}
                  {effective.source === 'default' ? ' (default)' : ''}
                </dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Applied</dt>
                <dd className="mt-1 font-medium">{formatWhen(state.current[0]?.created_at)}</dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Live validators</dt>
                <dd className="mt-1 font-medium">{effective.live_validator_count ?? '—'}</dd>
              </div>
            </dl>
          </div>

          <div className="mt-5 rounded-lg border border-[var(--amber)]/25 bg-[var(--amber-dim)] px-4 py-3 text-xs leading-5 text-[var(--amber)]">
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <p>
                This moves TAO. Validators read the new share on their next ledger poll (within{' '}
                {effective.max_age_seconds}s of applying it), but one that has already submitted
                weights this epoch keeps its vector until the next one — so the subnet-wide effect
                lands over roughly an epoch, not at once. Scores, rankings and the champion are
                untouched.
              </p>
            </div>
          </div>

          {detached ? (
            <div className="mt-3 rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] px-4 py-3 text-xs leading-5 text-[var(--red)]">
              No validator has heartbeated recently. A revision applied now is recorded and served,
              but nothing is currently folding it onto the chain.
            </div>
          ) : null}

          <div className="mt-5 grid gap-2 sm:grid-cols-4">
            {presets.map((value) => (
              <button
                key={value}
                type="button"
                disabled={readOnly || loading}
                onClick={() => selectPercent(value)}
                className={`min-h-16 rounded-lg border px-4 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                  Number(percent) === value
                    ? 'border-[var(--amber)]/40 bg-[var(--amber-dim)]'
                    : 'border-[var(--line)] bg-[var(--panel-soft)] hover:border-[var(--line-strong)]'
                }`}
              >
                <span className="block text-sm font-semibold">Burn {value}%</span>
                <span className="mt-1 block text-[11px] text-[var(--muted)]">
                  {currentPercent === value ? 'Current value' : `${100 - value}% to miners`}
                </span>
              </button>
            ))}
          </div>

          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Burn percentage ({minPercent}–{maxPercent})
              <input
                type="number"
                inputMode="decimal"
                min={minPercent}
                max={maxPercent}
                step={0.1}
                value={percent}
                disabled={readOnly || loading}
                onChange={(event) => {
                  setPercent(event.target.value)
                  setReason('')
                  setConfirmation('')
                  setError('')
                  setSuccess('')
                }}
                aria-invalid={invalid}
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
              />
              {invalid ? (
                <span className="mt-1 block text-[11px] text-[var(--red)]">
                  Enter a percentage from {minPercent} through {maxPercent}.
                </span>
              ) : null}
            </label>
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Operator reason
              <input
                type="text"
                value={reason}
                disabled={readOnly || loading || selectedShare === null}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Why the emission split is changing"
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
              />
            </label>
          </div>

          {selectedShare !== null && selectedShare !== effective.settings.burn_share ? (
            <label className="mt-4 block text-xs font-medium text-[var(--muted-strong)]">
              Type to confirm
              <code className="ml-2 break-all text-[11px] text-[var(--cyan)]">
                {BURN_CONFIRMATION}
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

          <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={() => resetForm()}
              disabled={loading}
              className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-medium text-[var(--muted-strong)] transition-colors hover:bg-white/5 disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={readOnly || loading || !ready}
              className="min-h-11 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-black transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-35"
            >
              {loading ? 'Applying…' : 'Apply burn'}
            </button>
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] p-4 sm:p-5">
          <h2 className="text-sm font-semibold">Revision history</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Append-only. Every burn the subnet has run, who set it and why.
          </p>
        </div>
        {state.history.length === 0 ? (
          <p className="p-4 text-xs text-[var(--muted)] sm:p-5">
            No revision yet — the subnet is on the built-in default of no burn.
          </p>
        ) : (
          <ul className="divide-y divide-[var(--line)]">
            {state.history.map((revision) => (
              <li key={revision.revision} className="p-4 text-xs sm:p-5">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-semibold">
                    r{revision.revision} — burn {percentOf(revision.settings.burn_share)}%
                  </span>
                  <span className="text-[var(--muted)]">{formatWhen(revision.created_at)}</span>
                </div>
                <p className="mt-1 leading-5 text-[var(--muted-strong)]">{revision.reason}</p>
                <p className="mt-1 text-[var(--muted)]">{revision.actor}</p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
