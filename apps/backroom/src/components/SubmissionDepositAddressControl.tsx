import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, RefreshCw, WalletCards } from 'lucide-react'
import {
  SS58_ADDRESS_PATTERN,
  submissionDepositAddressConfirmation,
  type SubmissionDepositAddressControl as Control,
} from '../lib/submission-deposit-address'
import {
  getSubmissionDepositAddressControl,
  setSubmissionDepositAddress,
} from '../server/admin.functions'

function formatWhen(value: string | null) {
  if (!value) return 'Boot configuration'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

export function SubmissionDepositAddressControl({
  initialState,
  readOnly,
}: {
  initialState: Control
  readOnly: boolean
}) {
  const refreshControl = useServerFn(getSubmissionDepositAddressControl)
  const applyAddress = useServerFn(setSubmissionDepositAddress)
  const [state, setState] = useState(initialState)
  const [address, setAddress] = useState(initialState.current.payment_address)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const normalizedAddress = address.trim()
  const validAddress = SS58_ADDRESS_PATTERN.test(normalizedAddress)
  const expectedConfirmation = validAddress
    ? submissionDepositAddressConfirmation(normalizedAddress)
    : ''
  const ready =
    validAddress &&
    normalizedAddress !== state.current.payment_address &&
    reason.trim().length >= 8 &&
    confirmation === expectedConfirmation

  const resetForm = (next: Control) => {
    setAddress(next.current.payment_address)
    setReason('')
    setConfirmation('')
  }

  const refresh = async () => {
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await refreshControl()
      setState(next)
      resetForm(next)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh deposit address')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!ready) return
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await applyAddress({
        data: {
          expectedRevision: state.current.revision,
          paymentAddress: normalizedAddress,
          reason,
          confirmation,
        },
      })
      setState(next)
      resetForm(next)
      setSuccess('Submission deposit address updated for new payment reservations.')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to update deposit address')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="flex flex-col gap-4 border-b border-[var(--line)] px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
            <WalletCards className="h-4 w-4" />
          </div>
          <div>
            <h2 className="text-sm font-semibold">Submission deposit address</h2>
            <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted)]">
              New miner submission fees are sent here. The address is published in payment quotes
              and verified on chain before an upload is accepted.
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
          Refresh address
        </button>
      </div>

      <div className="space-y-5 p-5">
        <dl className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <dt className="text-xs text-[var(--muted)]">Current receive address</dt>
            <dd className="mt-2 break-all font-mono text-sm text-white">
              {state.current.payment_address}
            </dd>
          </div>
          <div className="shrink-0 sm:text-right">
            <dt className="text-xs text-[var(--muted)]">Revision</dt>
            <dd className="mt-1 text-xs font-medium text-[var(--muted-strong)]">
              {state.current.revision} · {formatWhen(state.current.created_at)}
            </dd>
          </div>
        </dl>

        <div className="flex items-start gap-3 rounded-lg border border-[var(--amber)]/25 bg-[var(--amber-dim)] px-4 py-3 text-xs leading-5 text-[var(--amber)]">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <p>
            This changes future payment reservations immediately. Already-issued reservations keep
            the address they quoted for their full recovery window, so in-flight miners are not
            stranded.
          </p>
        </div>

        {readOnly ? (
          <p className="rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-4 py-3 text-xs text-[var(--muted-strong)]">
            Your account has read access. A Backroom writer must apply address changes.
          </p>
        ) : (
          <div className="space-y-4 border-t border-[var(--line)] pt-5">
            <label className="block text-xs font-medium text-[var(--muted-strong)]">
              New SS58 receive address
              <input
                type="text"
                autoComplete="off"
                spellCheck={false}
                value={address}
                disabled={loading}
                onChange={(event) => {
                  setAddress(event.target.value)
                  setConfirmation('')
                  setError('')
                  setSuccess('')
                }}
                aria-invalid={address.trim() !== '' && !validAddress}
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 font-mono text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
              />
              {address.trim() !== '' && !validAddress ? (
                <span className="mt-1 block text-[11px] text-[var(--red)]">
                  Enter a 32–64 character SS58 address.
                </span>
              ) : null}
            </label>

            <label className="block text-xs font-medium text-[var(--muted-strong)]">
              Operator reason
              <textarea
                rows={2}
                value={reason}
                disabled={loading}
                onChange={(event) => {
                  setReason(event.target.value)
                  setError('')
                  setSuccess('')
                }}
                className="mt-2 w-full resize-y rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 py-2.5 text-sm leading-5 outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
                placeholder="Why earnings should move to this address"
              />
            </label>

            <label className="block text-xs font-medium text-[var(--muted-strong)]">
              Type <span className="break-all font-mono text-[var(--amber)]">{expectedConfirmation || 'a valid address above'}</span>
              <input
                type="text"
                autoComplete="off"
                spellCheck={false}
                value={confirmation}
                disabled={loading || !expectedConfirmation}
                onChange={(event) => {
                  setConfirmation(event.target.value)
                  setError('')
                  setSuccess('')
                }}
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 font-mono text-xs outline-none transition-colors focus:border-[var(--amber)] disabled:opacity-45"
              />
            </label>

            {error ? (
              <div role="alert" className="rounded-lg border border-[var(--red)]/30 bg-[var(--red-dim)] px-4 py-3 text-sm text-[var(--red)]">
                {error}
              </div>
            ) : null}
            {success ? (
              <div role="status" className="flex items-center gap-2 rounded-lg border border-[var(--acid)]/25 bg-[var(--acid-dim)] px-4 py-3 text-sm text-[var(--acid)]">
                <CheckCircle2 className="h-4 w-4" />
                {success}
              </div>
            ) : null}

            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => void submit()}
                disabled={!ready || loading}
                className="inline-flex min-h-11 items-center justify-center rounded-lg bg-[var(--acid)] px-4 text-sm font-semibold text-[var(--ink)] transition-colors hover:bg-[var(--acid-hover)] disabled:opacity-35"
              >
                {loading ? 'Applying…' : 'Change deposit address'}
              </button>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}
