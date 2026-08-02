import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, RefreshCw, Timer } from 'lucide-react'
import {
  SUBMISSION_COOLDOWN_MAX_SECONDS,
  SUBMISSION_COOLDOWN_MIN_SECONDS,
  RAO_PER_TAO,
  submissionSettingsConfirmation,
  type SubmissionSettingsControl,
} from '../lib/admin.schemas'
import {
  getSubmissionSettingsControl,
  setSubmissionSettings,
} from '../server/admin.functions'

const presets = [15, 30, 60, 120] as const

function formatWhen(value: string | null) {
  if (!value) return 'Built-in default'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

function formatDuration(seconds: number) {
  if (seconds % 3600 === 0) return `${seconds / 3600} ${seconds === 3600 ? 'hour' : 'hours'}`
  if (seconds % 60 === 0) return `${seconds / 60} minutes`
  return `${seconds} seconds`
}

export function SubmissionCooldownControlPanel({
  initialState,
  readOnly,
}: {
  initialState: SubmissionSettingsControl
  readOnly: boolean
}) {
  const refreshState = useServerFn(getSubmissionSettingsControl)
  const applySettings = useServerFn(setSubmissionSettings)
  const [state, setState] = useState(initialState)
  const [minutes, setMinutes] = useState(String(initialState.current.cooldown_seconds / 60))
  const [feeTao, setFeeTao] = useState(String(initialState.current.fee_amount_rao / RAO_PER_TAO))
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const parsedMinutes = Number(minutes)
  const selectedSeconds =
    Number.isInteger(parsedMinutes) && parsedMinutes >= 1 && parsedMinutes <= 1440
      ? parsedMinutes * 60
      : null
  const parsedFeeTao = Number(feeTao)
  const selectedFeeRao =
    Number.isFinite(parsedFeeTao) && parsedFeeTao > 0
      ? Math.round(parsedFeeTao * RAO_PER_TAO)
      : null
  const expectedConfirmation = selectedSeconds && selectedFeeRao
    ? submissionSettingsConfirmation(selectedSeconds, selectedFeeRao)
    : ''
  const ready =
    selectedSeconds !== null &&
    selectedFeeRao !== null &&
    (selectedSeconds !== state.current.cooldown_seconds ||
      selectedFeeRao !== state.current.fee_amount_rao) &&
    reason.trim().length >= 8 &&
    confirmation === expectedConfirmation
  const invalid = minutes.trim() !== '' && selectedSeconds === null

  const clearForm = (
    feeAmountRao = state.current.fee_amount_rao,
    cooldownSeconds = state.current.cooldown_seconds,
  ) => {
    setMinutes(String(cooldownSeconds / 60))
    setFeeTao(String(feeAmountRao / RAO_PER_TAO))
    setReason('')
    setConfirmation('')
  }

  const selectMinutes = (value: number) => {
    setMinutes(String(value))
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
      clearForm(next.current.fee_amount_rao, next.current.cooldown_seconds)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh upload cooldown')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!ready || selectedSeconds === null || selectedFeeRao === null) return
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await applySettings({
        data: {
          expectedRevision: state.current.revision,
          cooldownSeconds: selectedSeconds,
          feeAmountRao: selectedFeeRao,
          reason,
          confirmation,
        },
      })
      setState(next)
      setSuccess(
        `Submission settings updated: ${formatDuration(selectedSeconds)} cooldown, ${feeTao} TAO fee.`,
      )
      clearForm(next.current.fee_amount_rao, next.current.cooldown_seconds)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to update upload cooldown')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mt-6 space-y-5">
      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
              <Timer className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-semibold">Submission cadence and fee</h2>
              <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted)]">
                Applies per owner coldkey. Compatible miner clients reserve an upload slot before
                payment. A finalized payment remains reusable for 24 hours, while an unpaid
                reservation only excludes competing archives for the short anti-race window.
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
              <p className="text-xs text-[var(--muted)]">Effective cooldown</p>
              <p className="mt-2 text-3xl font-semibold tracking-tight">
                {formatDuration(state.current.cooldown_seconds)}
              </p>
            </div>
            <dl className="grid gap-3 text-xs sm:grid-cols-3 sm:text-right">
              <div>
                <dt className="text-[var(--muted)]">Submission fee</dt>
                <dd className="mt-1 font-medium">
                  {state.current.fee_amount_rao / RAO_PER_TAO} TAO
                </dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Revision</dt>
                <dd className="mt-1 font-medium">{state.current.revision}</dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Applied</dt>
                <dd className="mt-1 font-medium">{formatWhen(state.current.created_at)}</dd>
              </div>
            </dl>
          </div>

          <div className="mt-5 rounded-lg border border-[var(--amber)]/25 bg-[var(--amber-dim)] px-4 py-3 text-xs leading-5 text-[var(--amber)]">
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <p>
                This changes admission for future uploads. Existing scores and submissions are not
                rewritten. An already-issued reservation keeps the fee and cooldown revision it was
                issued under until it is consumed or expires.
              </p>
            </div>
          </div>

          <div className="mt-5 grid gap-2 sm:grid-cols-4">
            {presets.map((value) => (
              <button
                key={value}
                type="button"
                disabled={readOnly || loading}
                onClick={() => selectMinutes(value)}
                className={`min-h-16 rounded-lg border px-4 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                  Number(minutes) === value
                    ? 'border-[var(--amber)]/40 bg-[var(--amber-dim)]'
                    : 'border-[var(--line)] bg-[var(--panel-soft)] hover:border-[var(--line-strong)]'
                }`}
              >
                <span className="block text-sm font-semibold">{formatDuration(value * 60)}</span>
                <span className="mt-1 block text-[11px] text-[var(--muted)]">
                  {state.current.cooldown_seconds === value * 60 ? 'Current value' : 'Set cadence'}
                </span>
              </button>
            ))}
          </div>

          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Cooldown in minutes (1–1440)
              <input
                type="number"
                inputMode="numeric"
                min={SUBMISSION_COOLDOWN_MIN_SECONDS / 60}
                max={SUBMISSION_COOLDOWN_MAX_SECONDS / 60}
                step={1}
                value={minutes}
                disabled={readOnly || loading}
                onChange={(event) => {
                  setMinutes(event.target.value)
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
                  Enter a whole number from 1 through 1440.
                </span>
              ) : null}
            </label>
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Submission fee in TAO
              <input
                type="number"
                inputMode="decimal"
                min="0.000000001"
                step="0.001"
                value={feeTao}
                disabled={readOnly || loading}
                onChange={(event) => {
                  setFeeTao(event.target.value)
                  setReason('')
                  setConfirmation('')
                  setError('')
                  setSuccess('')
                }}
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
              />
            </label>
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Operator reason
              <input
                type="text"
                value={reason}
                disabled={readOnly || loading || selectedSeconds === null}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Why this cadence is changing"
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
              />
            </label>
          </div>

          {selectedSeconds !== null ? (
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

          <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={() => clearForm()}
              disabled={loading || selectedSeconds === null}
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
              {loading ? 'Applying…' : 'Apply settings'}
            </button>
          </div>
        </div>
      </section>
    </div>
  )
}
