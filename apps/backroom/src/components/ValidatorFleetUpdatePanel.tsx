import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, RefreshCw, ServerCrash } from 'lucide-react'
import {
  VALIDATOR_FLEET_UPDATE_CONFIRMATION,
  type ValidatorFleetUpdatePreview,
} from '../lib/admin.schemas'
import {
  forceValidatorFleetUpdate,
  getValidatorFleetUpdate,
} from '../server/admin.functions'

function formatWhen(value: string) {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed)
}

function shortHotkey(hotkey: string) {
  return hotkey.length > 18 ? `${hotkey.slice(0, 10)}…${hotkey.slice(-6)}` : hotkey
}

export function ValidatorFleetUpdatePanel({
  initialPreview,
  readOnly,
}: {
  initialPreview: ValidatorFleetUpdatePreview
  readOnly: boolean
}) {
  const readPreview = useServerFn(getValidatorFleetUpdate)
  const forceUpdate = useServerFn(forceValidatorFleetUpdate)
  const [preview, setPreview] = useState(initialPreview)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const reasonReady = reason.trim().length >= 8
  const canSubmit = !readOnly && !loading && preview.target_count > 0 && reasonReady
  const latest = preview.latest_operation

  async function refresh() {
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      setPreview(await readPreview())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh validator fleet')
    } finally {
      setLoading(false)
    }
  }

  async function submit() {
    if (!canSubmit) return
    if (confirmation !== VALIDATOR_FLEET_UPDATE_CONFIRMATION) {
      setSuccess('')
      setError(
        `Nothing was sent. Type ${VALIDATOR_FLEET_UPDATE_CONFIRMATION} exactly to confirm the fleet interruption.`,
      )
      return
    }
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const response = await forceUpdate({
        data: {
          requestId: crypto.randomUUID(),
          expectedSnapshot: preview.snapshot,
          reason,
          confirmation,
        },
      })
      const next = await readPreview()
      setPreview(next)
      setReason('')
      setConfirmation('')
      setSuccess(
        `Operation ${response.operation.operation_id} revoked ${response.operation.revoked_lease_count} live lease${response.operation.revoked_lease_count === 1 ? '' : 's'} and was queued for ${response.operation.targets.length} managed validator${response.operation.targets.length === 1 ? '' : 's'}. Follow acknowledgements below; updater success is verified separately.`,
      )
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to force the fleet update')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="overflow-hidden rounded-xl border border-[var(--red)]/30 bg-[var(--panel)]">
      <div className="flex flex-col gap-4 border-b border-[var(--red)]/20 bg-[var(--red-dim)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--red)]/15 text-[var(--red)]">
            <ServerCrash className="h-4 w-4" />
          </div>
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[var(--red)]">
              Emergency control
            </p>
            <h2 className="mt-1 text-sm font-semibold">Stop work and update managed validators</h2>
            <p className="mt-1 max-w-[80ch] text-xs leading-5 text-[var(--muted-strong)]">
              Revokes every ordinary live benchmark lease on the online managed fleet, cancels
              canonical and private confirmation work, and drains each validator for its installed
              updater. Affected submissions receive no-fault compensation and can be dispatched
              again. The updater runs on its next timer poll; this control does not prove that a
              newer release exists or that replacement succeeded.
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-lg border border-[var(--red)]/30 px-3 text-xs font-medium text-[var(--muted-strong)] transition-colors hover:bg-[var(--red)]/10 disabled:opacity-40"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh target
        </button>
      </div>

      <div className="space-y-5 p-4 sm:p-5">
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="rounded-lg border border-[var(--line)] p-3">
            <p className="text-xs text-[var(--muted)]">Managed validators targeted</p>
            <p className="mt-1 text-2xl font-semibold">{preview.target_count}</p>
          </div>
          <div className="rounded-lg border border-[var(--line)] p-3">
            <p className="text-xs text-[var(--muted)]">Live leases revoked on submit</p>
            <p className="mt-1 text-2xl font-semibold">{preview.active_lease_count}</p>
          </div>
          <div className="rounded-lg border border-[var(--line)] p-3">
            <p className="text-xs text-[var(--muted)]">Latest signed acknowledgements</p>
            <p className="mt-1 text-2xl font-semibold">
              {latest ? `${latest.acknowledged_count}/${latest.targets.length}` : '—'}
            </p>
          </div>
        </div>

        {preview.targets.length > 0 ? (
          <div className="overflow-x-auto rounded-lg border border-[var(--line)]">
            <table className="w-full min-w-[620px] text-left text-xs">
              <thead className="bg-white/[0.025] text-[var(--muted)]">
                <tr>
                  <th className="px-3 py-2 font-medium">Validator</th>
                  <th className="px-3 py-2 font-medium">Version</th>
                  <th className="px-3 py-2 font-medium">Revision</th>
                  <th className="px-3 py-2 text-right font-medium">Live leases</th>
                </tr>
              </thead>
              <tbody>
                {preview.targets.map((target) => (
                  <tr key={target.validator_hotkey} className="border-t border-[var(--line)]">
                    <td className="px-3 py-2 font-mono" title={target.validator_hotkey}>
                      {shortHotkey(target.validator_hotkey)}
                    </td>
                    <td className="px-3 py-2">{target.software_version}</td>
                    <td className="px-3 py-2 font-mono text-[var(--muted-strong)]">
                      {target.stack_revision?.slice(0, 12) ?? 'unknown'}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {target.active_lease_count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="rounded-lg border border-[var(--line)] p-4 text-sm text-[var(--muted-strong)]">
            No online validator currently advertises both managed stack identity and updater
            support. Nothing can be forced from this control.
          </div>
        )}

        {latest ? (
          <div className="rounded-lg border border-[var(--line)] bg-white/[0.02] p-3 text-xs leading-5 text-[var(--muted-strong)]">
            <span className="font-medium text-[var(--text)]">Latest operation:</span>{' '}
            {latest.operation_id} · {latest.acknowledged_count}/{latest.targets.length} signed
            receipts · {latest.revoked_lease_count} leases revoked · {formatWhen(latest.created_at)}
            {' · '}{latest.actor}: {latest.reason}
          </div>
        ) : null}

        <div className="grid gap-4 lg:grid-cols-2">
          <label className="block text-xs font-medium text-[var(--muted-strong)]">
            Operator reason
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={3}
              placeholder="Why must every active validator stop now?"
              className="mt-2 w-full rounded-lg border border-[var(--line)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--text)] outline-none transition-colors focus:border-[var(--red)]"
            />
          </label>
          <label className="block text-xs font-medium text-[var(--muted-strong)]">
            Type to confirm
            <input
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              placeholder={VALIDATOR_FLEET_UPDATE_CONFIRMATION}
              autoComplete="off"
              className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--bg)] px-3 text-sm text-[var(--text)] outline-none transition-colors focus:border-[var(--red)]"
            />
            <span className="mt-2 block font-normal leading-5 text-[var(--muted)]">
              Type the phrase yourself. It is never prefilled.
            </span>
          </label>
        </div>

        {error ? (
          <div role="alert" className="flex gap-2 rounded-lg bg-[var(--red-dim)] p-3 text-xs text-[var(--red)]">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{error}</span>
          </div>
        ) : null}
        {success ? (
          <div role="status" className="flex gap-2 rounded-lg bg-[var(--green-dim)] p-3 text-xs text-[var(--green)]">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{success}</span>
          </div>
        ) : null}

        <div className="flex flex-col items-start justify-between gap-3 border-t border-[var(--line)] pt-4 sm:flex-row sm:items-center">
          <p className="max-w-[72ch] text-xs leading-5 text-[var(--muted)]">
            The target snapshot is rechecked inside the Platform transaction. Any heartbeat or
            lease change refuses the request; refresh and inspect the new target before retrying.
          </p>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!canSubmit}
            className="inline-flex min-h-11 shrink-0 items-center justify-center rounded-lg bg-[var(--red)] px-4 text-sm font-semibold text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-35"
          >
            {loading ? 'Stopping validators…' : 'Stop work and force update'}
          </button>
        </div>
      </div>
    </section>
  )
}
