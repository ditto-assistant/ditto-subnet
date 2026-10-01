import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, History, RefreshCw, Timer } from 'lucide-react'
import {
  effectiveSubmissionPolicy,
  formatRaoAsTao,
  parseTaoToRaoExact,
  type SubmissionSettingsControl,
  type SubmissionSettingsPreview,
} from '../lib/admin.schemas'
import {
  getSubmissionSettingsControl,
  previewSubmissionSettingsChange,
  setSubmissionSettings,
} from '../server/admin.functions'

const presets = [15, 30, 60, 120] as const

// An unsupported effective fee has no TAO rendering; the fee input starts
// empty so recovery always names an explicit fixed-TAO amount.
function feeText(feeAmountRao: number | null) {
  return feeAmountRao === null ? '' : formatRaoAsTao(feeAmountRao)
}

function formatTimestamp(value: string | null | undefined, missing: string) {
  if (!value) return missing
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

// Only the effective policy can be the built-in default (revision 0 has no
// timestamp); anywhere else a missing time is shown neutrally.
function formatPolicyApplied(value: string | null) {
  return formatTimestamp(value, 'Built-in default')
}

function formatWhen(value: string | null | undefined) {
  return formatTimestamp(value, '—')
}

function formatDuration(seconds: number) {
  if (seconds % 3600 === 0) return `${seconds / 3600} ${seconds === 3600 ? 'hour' : 'hours'}`
  if (seconds % 60 === 0) return `${seconds / 60} minutes`
  return `${seconds.toLocaleString('en-US')} seconds`
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
  const initialPolicy = effectiveSubmissionPolicy(initialState)
  const [minutes, setMinutes] = useState(String(initialPolicy.cooldownSeconds / 60))
  const [feeTao, setFeeTao] = useState(feeText(initialPolicy.feeAmountRao))
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [preview, setPreview] = useState<SubmissionSettingsPreview | null>(null)
  // Which request is in flight, so each control names its own pending action.
  const [busy, setBusy] = useState<'refresh' | 'preview' | 'apply' | null>(null)
  const loading = busy !== null
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const bounds = state.bounds
  const policy = effectiveSubmissionPolicy(state)
  // Server-provided bounds are authoritative; every hint and input limit is
  // derived from them so a Platform change cannot leave stale UI copy.
  const minCooldownMinutes = Math.ceil(bounds.min_cooldown_seconds / 60)
  const maxCooldownMinutes = Math.floor(bounds.max_cooldown_seconds / 60)
  // New values are whole minutes written as plain digits ("0x10", "1e1",
  // " 5", "-1" and "1.5" are malformed, not silently converted). The applied
  // cooldown may be any whole number of seconds (the API and MCP accept them),
  // so keeping it unchanged must not block a fee-only change.
  const minutesUntouched = minutes === String(policy.cooldownSeconds / 60)
  const minutesWellFormed = minutesUntouched || /^\d+$/.test(minutes)
  const candidateSeconds = minutesUntouched
    ? policy.cooldownSeconds
    : minutesWellFormed
      ? Number(minutes) * 60
      : null
  const selectedSeconds =
    candidateSeconds !== null &&
    candidateSeconds >= bounds.min_cooldown_seconds &&
    candidateSeconds <= bounds.max_cooldown_seconds
      ? candidateSeconds
      : null
  // Parse exactly (BigInt) and compare to bounds before any narrowing, so a
  // well-formed but huge amount reports the bounds, not a format error.
  const parsedFeeRao = parseTaoToRaoExact(feeTao)
  const feeInBounds =
    parsedFeeRao !== null &&
    parsedFeeRao >= BigInt(bounds.min_fee_amount_rao) &&
    parsedFeeRao <= BigInt(bounds.max_fee_amount_rao)
  const selectedFeeRao = feeInBounds ? Number(parsedFeeRao) : null
  const proposal: Proposal | null =
    selectedSeconds !== null && selectedFeeRao !== null
      ? {
          cooldownSeconds: selectedSeconds,
          feeAmountRao: selectedFeeRao,
          expectedRevision: policy.revision,
        }
      : null
  // Any fixed-TAO fee changes an unsupported effective fee (its denomination).
  const changed =
    proposal !== null &&
    (proposal.cooldownSeconds !== policy.cooldownSeconds ||
      proposal.feeAmountRao !== policy.feeAmountRao)
  const currentPreview = sameProposal(preview, proposal) ? preview : null
  const expectedConfirmation = currentPreview?.required_confirmation ?? ''
  const ready =
    !readOnly &&
    currentPreview !== null &&
    currentPreview.applicable &&
    !currentPreview.stale &&
    reason.trim().length >= 8 &&
    confirmation === expectedConfirmation
  // The draft differs from the applied policy in any way, including text that
  // does not parse yet; Cancel must stay available so an invalid draft can
  // always be discarded.
  const dirty =
    minutes !== String(policy.cooldownSeconds / 60) ||
    feeTao !== feeText(policy.feeAmountRao) ||
    preview !== null ||
    reason !== '' ||
    confirmation !== ''
  const invalidMinutes = minutes !== '' && selectedSeconds === null
  const minutesFormatInvalid = invalidMinutes && !minutesWellFormed
  const invalidFee = feeTao.trim() !== '' && selectedFeeRao === null
  const feeFormatInvalid = invalidFee && parsedFeeRao === null

  const resetDraft = () => {
    setPreview(null)
    setReason('')
    setConfirmation('')
    setError('')
    setSuccess('')
  }

  // Discarding the draft also discards any message about it, so Cancel never
  // leaves a stale error (or an earlier success) on screen.
  const clearForm = (next = policy) => {
    setMinutes(String(next.cooldownSeconds / 60))
    setFeeTao(feeText(next.feeAmountRao))
    setPreview(null)
    setReason('')
    setConfirmation('')
    setError('')
    setSuccess('')
  }

  const selectMinutes = (value: number) => {
    setMinutes(String(value))
    resetDraft()
  }

  const refresh = async () => {
    // A draft survives a refresh (only its preview and confirmation are
    // dropped), so "refresh, then preview again" after a stale preview does not
    // make the operator re-enter the change.
    const keepDraft = dirty
    // Fields the operator has not touched follow the refreshed policy, so a
    // concurrent change cannot leave an untouched field stale or invalid.
    const cooldownUntouched = minutes === String(policy.cooldownSeconds / 60)
    const feeUntouched = feeTao === feeText(policy.feeAmountRao)
    setBusy('refresh')
    setError('')
    setSuccess('')
    try {
      const next = await refreshState()
      const nextPolicy = effectiveSubmissionPolicy(next)
      setState(next)
      if (keepDraft) {
        setPreview(null)
        setConfirmation('')
        if (cooldownUntouched) setMinutes(String(nextPolicy.cooldownSeconds / 60))
        if (feeUntouched) setFeeTao(feeText(nextPolicy.feeAmountRao))
      } else {
        clearForm(nextPolicy)
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh submission settings')
    } finally {
      setBusy(null)
    }
  }

  const runPreview = async () => {
    if (proposal === null || !changed) return
    setBusy('preview')
    setError('')
    setSuccess('')
    setConfirmation('')
    try {
      setPreview(await previewSettings({ data: proposal }))
    } catch (cause) {
      setPreview(null)
      setError(cause instanceof Error ? cause.message : 'Unable to preview submission settings')
    } finally {
      setBusy(null)
    }
  }

  const submit = async () => {
    if (!ready || proposal === null) return
    setBusy('apply')
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
      clearForm(effectiveSubmissionPolicy(next))
      setSuccess(
        `Submission settings updated: ${formatDuration(proposal.cooldownSeconds)} cooldown, ${formatRaoAsTao(proposal.feeAmountRao)} TAO fee.`,
      )
    } catch (cause) {
      // The policy may have changed under a failed apply (409): its preview and
      // confirmation can no longer be trusted, so they must be redone. The
      // draft values and reason are kept.
      setPreview(null)
      setConfirmation('')
      setError(cause instanceof Error ? cause.message : 'Unable to update submission settings')
    } finally {
      setBusy(null)
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
                slot and its fee before payment. A finalized payment remains reusable for{' '}
                {quoteLifetimeHours ? `${quoteLifetimeHours} hours` : 'the quote lifetime'},
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
                {formatDuration(policy.cooldownSeconds)}
              </p>
            </div>
            <dl className="grid gap-3 text-xs sm:grid-cols-4 sm:text-right">
              <div>
                <dt className="text-[var(--muted)]">Submission fee</dt>
                <dd className="mt-1 font-medium">
                  {policy.feeAmountRao === null
                    ? 'Not quotable'
                    : `${formatRaoAsTao(policy.feeAmountRao)} TAO`}
                </dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Denomination</dt>
                <dd className="mt-1 font-medium">
                  {policy.unsupported ? `Unsupported: ${policy.unsupported.denomination}` : 'Fixed TAO'}
                </dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Revision</dt>
                <dd className="mt-1 font-medium">{policy.revision}</dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Applied</dt>
                <dd className="mt-1 font-medium">{formatPolicyApplied(policy.createdAt)}</dd>
              </div>
            </dl>
          </div>

          {policy.unsupported ? (
            <div
              role="alert"
              className="mt-5 rounded-lg border border-[var(--red)]/30 px-4 py-3 text-xs leading-5 text-[var(--red)]"
            >
              Revision {policy.revision} uses the {policy.unsupported.denomination} denomination,
              which Platform cannot price (stored amount {policy.unsupported.amountRaw} in that
              denomination&apos;s unit, not TAO). New quotes are refused; quotes already issued are
              still honoured. Enter a fixed TAO fee and apply it to recover.
            </div>
          ) : null}

          <div className="mt-5 rounded-lg border border-[var(--amber)]/25 bg-[var(--amber-dim)] px-4 py-3 text-xs leading-5 text-[var(--amber)]">
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <p>
                This changes admission for future uploads. Existing scores and submissions are not
                rewritten. A payment made before its reservation expires
                {quoteLifetimeHours ? ` (${quoteLifetimeHours} hours after it is issued)` : ''} keeps
                the fee it was quoted at, even if it is uploaded or recovered after that; a later
                payment must match the new fee. Roll back by applying the older value as a new
                revision.
              </p>
            </div>
          </div>

          <div className="mt-5 grid gap-2 sm:grid-cols-4">
            {presets.map((value) => {
              // From the validated cooldown only: malformed text such as "15."
              // or " 15" selects no preset, while an untouched applied value
              // (900 s) still selects its preset and 90 s selects none.
              const selected = selectedSeconds === value * 60
              return (
                <button
                  key={value}
                  type="button"
                  aria-pressed={selected}
                  disabled={readOnly || loading}
                  onClick={() => selectMinutes(value)}
                  className={`min-h-16 rounded-lg border px-4 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                    selected
                      ? 'border-[var(--amber)]/40 bg-[var(--amber-dim)]'
                      : 'border-[var(--line)] bg-[var(--panel-soft)] hover:border-[var(--line-strong)]'
                  }`}
                >
                  <span className="block text-sm font-semibold">{formatDuration(value * 60)}</span>
                  <span className="mt-1 block text-[11px] text-[var(--muted)]">
                    {policy.cooldownSeconds === value * 60 ? 'Current value' : 'Set cadence'}
                  </span>
                </button>
              )
            })}
          </div>

          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Cooldown in minutes ({minCooldownMinutes}–{maxCooldownMinutes})
              <input
                // Text, not number: the browser must not coerce or silently
                // drop what the operator typed; validation below is exact.
                type="text"
                inputMode="numeric"
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
                  {minutesFormatInvalid
                    ? 'Enter whole minutes as digits only.'
                    : `Enter a whole number from ${minCooldownMinutes} through ${maxCooldownMinutes} minutes.`}
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
                {feeFormatInvalid
                  ? 'Enter a TAO amount as digits with at most nine decimals.'
                  : invalidFee
                    ? `Enter ${formatRaoAsTao(bounds.min_fee_amount_rao)} to ${formatRaoAsTao(bounds.max_fee_amount_rao)} TAO.`
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
                    {currentPreview.current
                      ? `${formatRaoAsTao(currentPreview.current.fee_amount_rao)} → `
                      : `Unsupported (${currentPreview.unsupported_current?.fee_denomination ?? 'unknown'}) → `}
                    {formatRaoAsTao(currentPreview.proposed.fee_amount_rao)} TAO
                    {currentPreview.fee_change_ratio
                      ? ` (×${currentPreview.fee_change_ratio})`
                      : currentPreview.fee_changed
                        ? ''
                        : ' (unchanged)'}
                  </dd>
                </div>
                <div>
                  <dt className="text-[var(--muted)]">Cooldown</dt>
                  <dd className="mt-1 font-medium">
                    {formatDuration(effectiveSubmissionPolicy(currentPreview).cooldownSeconds)} →{' '}
                    {formatDuration(currentPreview.proposed.cooldown_seconds)}
                  </dd>
                </div>
                <div>
                  <dt className="text-[var(--muted)]">Quotes in flight</dt>
                  <dd className="mt-1 font-medium">
                    {currentPreview.in_flight_quotes_at_other_fees} of{' '}
                    {currentPreview.in_flight_quotes} keep an earlier fee
                    {currentPreview.in_flight_quotes_expire_by
                      ? ` for payments made by ${formatWhen(currentPreview.in_flight_quotes_expire_by)}`
                      : ''}
                    {currentPreview.recoverable_expired_quotes > 0 ? (
                      <span className="mt-1 block font-normal text-[var(--muted)]">
                        Plus up to {currentPreview.recoverable_expired_quotes} recently expired (
                        {currentPreview.recoverable_expired_quotes_at_other_fees} at another fee):
                        each still binds only a payment made before it expired, recoverable until{' '}
                        {formatWhen(currentPreview.recoverable_expired_quotes_until)}.
                      </span>
                    ) : null}
                  </dd>
                </div>
              </dl>
              {currentPreview.stale ? (
                <p className="mt-3 text-[var(--red)]">
                  The policy changed since this page loaded (current revision{' '}
                  {effectiveSubmissionPolicy(currentPreview).revision}). Refresh policy (your draft is kept), then
                  preview again.
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
              disabled={loading || !dirty}
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
              {busy === 'apply' ? 'Applying…' : 'Apply settings'}
            </button>
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex items-center gap-2 border-b border-[var(--line)] p-4 sm:px-5">
          <History className="h-4 w-4 text-[var(--muted)]" />
          <h2 className="text-sm font-semibold">Revision history</h2>
        </div>
        {state.history_incomplete ? (
          <p className="border-b border-[var(--line)] px-4 py-2 text-xs text-[var(--muted)] sm:px-5">
            Some revisions are not shown.
          </p>
        ) : null}
        {state.history.length === 0 ? (
          <p className="p-4 text-xs text-[var(--muted)] sm:px-5">No revisions recorded.</p>
        ) : (
          <ol className="divide-y divide-[var(--line)]" aria-label="Submission settings revisions">
            {state.history.map((row) => (
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
