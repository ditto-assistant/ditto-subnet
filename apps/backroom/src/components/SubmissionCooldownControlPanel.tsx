import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, History, RefreshCw, Timer } from 'lucide-react'
import {
  SUBMISSION_COOLDOWN_MAX_SECONDS,
  SUBMISSION_COOLDOWN_MIN_SECONDS,
  formatRaoAsTao,
  parseTaoToRao,
  type SubmissionSettingsControl,
  type SubmissionSettingsPreview,
} from '../lib/admin.schemas'
import {
  getSubmissionSettingsControl,
  previewSubmissionSettingsChange,
  setSubmissionSettings,
} from '../server/admin.functions'

const presets = [15, 30, 60, 120] as const
const HISTORY_ROWS = 12

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

type Proposal = { cooldownSeconds: number; feeAmountRao: number; expectedRevision: number }

function sameProposal(preview: SubmissionSettingsPreview | null, proposal: Proposal | null) {
  return (
    preview !== null &&
    proposal !== null &&
    preview.expected_revision === proposal.expectedRevision &&
    preview.proposed.cooldown_seconds === proposal.cooldownSeconds &&
    preview.proposed.fee_amount_rao === proposal.feeAmountRao
  )
}

export function SubmissionCooldownControlPanel({
  initialState,
  readOnly,
}: {
  initialState: SubmissionSettingsControl
  readOnly: boolean
}) {
  const refreshState = useServerFn(getSubmissionSettingsControl)
  const previewSettings = useServerFn(previewSubmissionSettingsChange)
  const applySettings = useServerFn(setSubmissionSettings)
  const [state, setState] = useState(initialState)
  const [minutes, setMinutes] = useState(String(initialState.current.cooldown_seconds / 60))
  const [feeTao, setFeeTao] = useState(formatRaoAsTao(initialState.current.fee_amount_rao))
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [preview, setPreview] = useState<SubmissionSettingsPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const bounds = state.bounds
  const parsedMinutes = Number(minutes)
  const selectedSeconds =
    Number.isInteger(parsedMinutes) &&
    parsedMinutes * 60 >= bounds.min_cooldown_seconds &&
    parsedMinutes * 60 <= bounds.max_cooldown_seconds
      ? parsedMinutes * 60
      : null
  const parsedFeeRao = parseTaoToRao(feeTao)
  const selectedFeeRao =
    parsedFeeRao !== null &&
    parsedFeeRao >= bounds.min_fee_amount_rao &&
    parsedFeeRao <= bounds.max_fee_amount_rao
      ? parsedFeeRao
      : null
  const proposal: Proposal | null =
    selectedSeconds !== null && selectedFeeRao !== null
      ? {
          cooldownSeconds: selectedSeconds,
          feeAmountRao: selectedFeeRao,
          expectedRevision: state.current.revision,
        }
      : null
  const changed =
    proposal !== null &&
    (proposal.cooldownSeconds !== state.current.cooldown_seconds ||
      proposal.feeAmountRao !== state.current.fee_amount_rao)
  const currentPreview = sameProposal(preview, proposal) ? preview : null
  const expectedConfirmation = currentPreview?.required_confirmation ?? ''
  const ready =
    !readOnly &&
    currentPreview !== null &&
    currentPreview.applicable &&
    !currentPreview.stale &&
    reason.trim().length >= 8 &&
    confirmation === expectedConfirmation
  const invalidMinutes = minutes.trim() !== '' && selectedSeconds === null
  const invalidFee = feeTao.trim() !== '' && selectedFeeRao === null

  const resetDraft = () => {
    setPreview(null)
    setReason('')
    setConfirmation('')
    setError('')
    setSuccess('')
  }

  const clearForm = (
    feeAmountRao = state.current.fee_amount_rao,
    cooldownSeconds = state.current.cooldown_seconds,
  ) => {
    setMinutes(String(cooldownSeconds / 60))
    setFeeTao(formatRaoAsTao(feeAmountRao))
    setPreview(null)
    setReason('')
    setConfirmation('')
  }

  const selectMinutes = (value: number) => {
    setMinutes(String(value))
    resetDraft()
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
      setError(cause instanceof Error ? cause.message : 'Unable to refresh submission settings')
    } finally {
      setLoading(false)
    }
  }

  const runPreview = async () => {
    if (proposal === null || !changed) return
    setLoading(true)
    setError('')
    setSuccess('')
    setConfirmation('')
    try {
      setPreview(await previewSettings({ data: proposal }))
    } catch (cause) {
      setPreview(null)
      setError(cause instanceof Error ? cause.message : 'Unable to preview submission settings')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!ready || proposal === null) return
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await applySettings({
        data: {
          expectedRevision: proposal.expectedRevision,
          cooldownSeconds: proposal.cooldownSeconds,
          feeAmountRao: proposal.feeAmountRao,
          reason,
          confirmation,
        },
      })
      setState(next)
      setSuccess(
        `Submission settings updated: ${formatDuration(proposal.cooldownSeconds)} cooldown, ${formatRaoAsTao(proposal.feeAmountRao)} TAO fee.`,
      )
      clearForm(next.current.fee_amount_rao, next.current.cooldown_seconds)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to update submission settings')
    } finally {
      setLoading(false)
    }
  }

  const quoteLifetimeHours = state.quote_lifetime_seconds
    ? state.quote_lifetime_seconds / 3600
    : null

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
                Applies per owner coldkey. The fee is a fixed TAO amount (never a USD target) and
                takes effect on apply, without a deploy. Compatible miner clients reserve an upload
                slot and its fee before payment. A finalized payment remains reusable for 24 hours,
                while an unpaid reservation only excludes competing archives for the short
                anti-race window.
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
            <dl className="grid gap-3 text-xs sm:grid-cols-4 sm:text-right">
              <div>
                <dt className="text-[var(--muted)]">Submission fee</dt>
                <dd className="mt-1 font-medium">
                  {formatRaoAsTao(state.current.fee_amount_rao)} TAO
                </dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Denomination</dt>
                <dd className="mt-1 font-medium">Fixed TAO</dd>
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
                issued under until it is consumed or expires
                {quoteLifetimeHours ? ` (${quoteLifetimeHours} hours)` : ''}. Roll back by applying
                the older value as a new revision.
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
                  resetDraft()
                }}
                aria-invalid={invalidMinutes}
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
              />
              {invalidMinutes ? (
                <span className="mt-1 block text-[11px] text-[var(--red)]">
                  Enter a whole number from 1 through 1440.
                </span>
              ) : null}
            </label>
            <div className="text-xs font-medium text-[var(--muted-strong)]">
              <label>
                Submission fee in TAO
                <input
                  type="text"
                  inputMode="decimal"
                  value={feeTao}
                  disabled={readOnly || loading}
                  onChange={(event) => {
                    setFeeTao(event.target.value)
                    resetDraft()
                  }}
                  aria-invalid={invalidFee}
                  aria-describedby="submission-fee-hint"
                  className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
                />
              </label>
              <span
                id="submission-fee-hint"
                className={`mt-1 block text-[11px] ${invalidFee ? 'text-[var(--red)]' : 'text-[var(--muted)]'}`}
              >
                {invalidFee
                  ? `Enter ${formatRaoAsTao(bounds.min_fee_amount_rao)} to ${formatRaoAsTao(bounds.max_fee_amount_rao)} TAO with at most nine decimals.`
                  : selectedFeeRao !== null
                    ? `Exactly ${selectedFeeRao.toLocaleString('en-US')} rao`
                    : ''}
              </span>
            </div>
          </div>

          <div className="mt-4 flex justify-end">
            <button
              type="button"
              onClick={() => void runPreview()}
              disabled={loading || proposal === null || !changed}
              className="min-h-11 rounded-lg border border-[var(--cyan)]/40 px-4 text-xs font-semibold text-[var(--cyan)] transition-colors hover:bg-[var(--cyan-dim)] disabled:cursor-not-allowed disabled:opacity-35"
            >
              Preview change
            </button>
          </div>

          {currentPreview ? (
            <section
              aria-label="Change preview"
              className="mt-4 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4 text-xs leading-5"
            >
              <dl className="grid gap-3 sm:grid-cols-3">
                <div>
                  <dt className="text-[var(--muted)]">Fee</dt>
                  <dd className="mt-1 font-medium">
                    {formatRaoAsTao(currentPreview.current.fee_amount_rao)} →{' '}
                    {formatRaoAsTao(currentPreview.proposed.fee_amount_rao)} TAO
                    {currentPreview.fee_change_ratio
                      ? ` (×${currentPreview.fee_change_ratio})`
                      : ' (unchanged)'}
                  </dd>
                </div>
                <div>
                  <dt className="text-[var(--muted)]">Cooldown</dt>
                  <dd className="mt-1 font-medium">
                    {formatDuration(currentPreview.current.cooldown_seconds)} →{' '}
                    {formatDuration(currentPreview.proposed.cooldown_seconds)}
                  </dd>
                </div>
                <div>
                  <dt className="text-[var(--muted)]">Quotes in flight</dt>
                  <dd className="mt-1 font-medium">
                    {currentPreview.in_flight_quotes_at_other_fees} of{' '}
                    {currentPreview.in_flight_quotes} keep an earlier fee
                    {currentPreview.in_flight_quotes_expire_by
                      ? ` until ${formatWhen(currentPreview.in_flight_quotes_expire_by)}`
                      : ''}
                  </dd>
                </div>
              </dl>
              {currentPreview.stale ? (
                <p className="mt-3 text-[var(--red)]">
                  The policy changed since this page loaded (current revision{' '}
                  {currentPreview.current.revision}). Refresh before applying.
                </p>
              ) : null}
            </section>
          ) : null}

          {currentPreview && currentPreview.applicable ? (
            <div className="mt-4 grid gap-4">
              <label className="text-xs font-medium text-[var(--muted-strong)]">
                Operator reason
                <input
                  type="text"
                  value={reason}
                  disabled={readOnly || loading}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="Measured cost or spam pressure behind this change"
                  className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
                />
              </label>
              <label className="block text-xs font-medium text-[var(--muted-strong)]">
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
            </div>
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
              disabled={loading || !changed}
              className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-medium text-[var(--muted-strong)] transition-colors hover:bg-white/5 disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={loading || !ready}
              className="min-h-11 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-black transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-35"
            >
              {loading ? 'Applying…' : 'Apply settings'}
            </button>
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex items-center gap-2 border-b border-[var(--line)] p-4 sm:px-5">
          <History className="h-4 w-4 text-[var(--muted)]" />
          <h2 className="text-sm font-semibold">Revision history</h2>
        </div>
        {state.history.length === 0 ? (
          <p className="p-4 text-xs text-[var(--muted)] sm:px-5">No revisions recorded.</p>
        ) : (
          <ol className="divide-y divide-[var(--line)]" aria-label="Submission settings revisions">
            {state.history.slice(0, HISTORY_ROWS).map((row) => (
              <li key={row.revision} className="grid gap-1 p-4 text-xs sm:px-5">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-medium">
                    Revision {row.revision} ·{' '}
                    {row.previous_fee_amount_rao != null &&
                    row.previous_fee_amount_rao !== row.fee_amount_rao
                      ? `${formatRaoAsTao(row.previous_fee_amount_rao)} → `
                      : ''}
                    {formatRaoAsTao(row.fee_amount_rao)} TAO ·{' '}
                    {formatDuration(row.cooldown_seconds)}
                  </span>
                  <span className="text-[var(--muted)]">{formatWhen(row.created_at)}</span>
                </div>
                <p className="text-[var(--muted)]">
                  {row.actor}: {row.reason}
                </p>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  )
}
