import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, RotateCcw, Trash2 } from 'lucide-react'
import { useState } from 'react'
import type { ValidationRetryDetail } from '../lib/admin.schemas'
import {
  getValidationRetry,
  reinstateRemovedValidationInQueue,
  retryValidationAfterInfrastructureFailure,
  withdrawFailedValidationFromQueue,
} from '../server/admin.functions'

function short(value: string) {
  return value.length > 16 ? `${value.slice(0, 16)}…` : value
}

export function ValidatorRetryPanel({ readOnly }: { readOnly: boolean }) {
  const lookup = useServerFn(getValidationRetry)
  const retry = useServerFn(retryValidationAfterInfrastructureFailure)
  const withdraw = useServerFn(withdrawFailedValidationFromQueue)
  const reinstate = useServerFn(reinstateRemovedValidationInQueue)
  const [agentId, setAgentId] = useState('')
  const [detail, setDetail] = useState<ValidationRetryDetail | null>(null)
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [withdrawReason, setWithdrawReason] = useState('')
  const [withdrawConfirmation, setWithdrawConfirmation] = useState('')
  const [reinstateReason, setReinstateReason] = useState('')
  const [reinstateConfirmation, setReinstateConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const inspect = async () => {
    setLoading(true)
    setError('')
    try {
      const next = await lookup({ data: { agentId: agentId.trim() } })
      setDetail(next)
      setReason('')
      setConfirmed(false)
      setWithdrawReason('')
      setWithdrawConfirmation('')
      setReinstateReason('')
      setReinstateConfirmation('')
    } catch (cause) {
      setDetail(null)
      setError(cause instanceof Error ? cause.message : 'Unable to inspect validation state')
    } finally {
      setLoading(false)
    }
  }

  const submitReinstatement = async () => {
    if (
      !detail ||
      detail.reinstatement_allowed !== true ||
      reinstateReason.trim().length < 8 ||
      reinstateConfirmation !== 'REINSTATE TO VALIDATOR QUEUE'
    ) return
    setLoading(true)
    setError('')
    try {
      await reinstate({
        data: {
          agentId: detail.agent_id,
          expectedSnapshot: detail.snapshot,
          reason: reinstateReason.trim(),
          confirmation: 'REINSTATE TO VALIDATOR QUEUE',
        },
      })
      setDetail(await lookup({ data: { agentId: detail.agent_id } }))
      setReinstateReason('')
      setReinstateConfirmation('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to reinstate submission')
    } finally {
      setLoading(false)
    }
  }

  const submitWithdrawal = async () => {
    if (
      !detail ||
      withdrawReason.trim().length < 8 ||
      withdrawConfirmation !== 'REMOVE FROM VALIDATOR QUEUE'
    ) return
    setLoading(true)
    setError('')
    try {
      await withdraw({
        data: {
          agentId: detail.agent_id,
          expectedSnapshot: detail.snapshot,
          reason: withdrawReason.trim(),
          confirmation: 'REMOVE FROM VALIDATOR QUEUE',
        },
      })
      setDetail(await lookup({ data: { agentId: detail.agent_id } }))
      setWithdrawReason('')
      setWithdrawConfirmation('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to remove submission from queue')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!detail || !confirmed || reason.trim().length < 8) return
    setLoading(true)
    setError('')
    try {
      await retry({
        data: {
          agentId: detail.agent_id,
          expectedSnapshot: detail.snapshot,
          reason: reason.trim(),
        },
      })
      setDetail(await lookup({ data: { agentId: detail.agent_id } }))
      setReason('')
      setConfirmed(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to queue validation retry')
    } finally {
      setLoading(false)
    }
  }

  // A removal row is only *in force* while `reinstated_at` is null. A reversed
  // removal is still reported — it happened and remains auditable — so
  // reading `detail.withdrawal` as "this submission is out of the queue" would
  // show a reinstated submission as removed and hide the removal form from an
  // operator who may legitimately need it again.
  const removal =
    detail?.withdrawal && !detail.withdrawal.reinstated_at ? detail.withdrawal : null
  const reversedRemoval =
    detail?.withdrawal?.reinstated_at ? detail.withdrawal : null

  return (
    <section className="mt-6 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="border-b border-[var(--line)] px-4 py-4">
        <div className="flex items-center gap-2">
          <RotateCcw className="h-4 w-4 text-[var(--amber)]" />
          <h2 className="text-sm font-semibold">Validator infrastructure retry</h2>
        </div>
        <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
          Inspect one submission stranded after validator failures. This is validation retry,
          not screening rescreening, and never changes accepted scores or miner ownership.
        </p>
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <input
            aria-label="Agent ID for validation retry"
            value={agentId}
            onChange={(event) => setAgentId(event.target.value)}
            placeholder="Agent UUID"
            className="min-h-11 flex-1 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 font-mono text-xs"
          />
          <button
            type="button"
            onClick={() => void inspect()}
            disabled={loading || agentId.trim().length === 0}
            className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-semibold disabled:opacity-40"
          >
            Inspect retry state
          </button>
        </div>
      </div>

      {error ? <p role="alert" className="m-4 text-xs text-[var(--red)]">{error}</p> : null}
      {detail ? (
        <div className="space-y-4 p-4">
          <div>
            <p className="text-sm font-semibold">{detail.agent_name}</p>
            <p className="font-mono text-[10px] text-[var(--muted)]">{detail.agent_id}</p>
            <p className="mt-2 text-xs text-[var(--muted-strong)]">
              {detail.score_count} of {detail.quorum} accepted scores · {detail.tickets.length} preserved ticket records
            </p>
            {detail.blocking_reason ? (
              <p className="mt-1 text-xs text-[var(--amber)]">{detail.blocking_reason}</p>
            ) : null}
          </div>
          <div className="overflow-x-auto rounded-lg border border-[var(--line)]">
            <table className="w-full text-left text-xs">
              <thead className="text-[var(--muted)]"><tr><th className="p-2">Validator</th><th>State</th><th>Attempts</th><th>Operator grants</th><th title="No-fault grants minted from validator-reported infrastructure failures. Each one raises the attempt cap and re-leases, so a rising count is the platform re-leasing on reports, not a validator gone quiet.">Infra grants</th><th>Failure</th></tr></thead>
              <tbody>{detail.tickets.map((ticket) => (
                <tr key={ticket.validator_hotkey} className="border-t border-[var(--line)]">
                  <td className="p-2 font-mono" title={ticket.validator_hotkey}>{short(ticket.validator_hotkey)}</td>
                  <td title={ticket.failure_reason ? `Last reported: ${ticket.failure_reason}` : undefined}>
                    {ticket.status}{ticket.retry_budget_exhausted ? ' · exhausted' : ''}
                    {ticket.silently_expired ? ' · silent' : ''}
                  </td>
                  <td>{ticket.attempt_count}</td><td>{ticket.manual_retry_grants}</td>
                  <td>{ticket.infra_retry_grants}</td>
                  <td className="max-w-[24rem] text-[var(--muted-strong)]">
                    {ticket.failure_reason ? (
                      <span title={ticket.failure_detail ?? undefined}>
                        {ticket.failure_reason}
                        {ticket.failure_detail ? <span className="block text-[10px] text-[var(--muted)]">{ticket.failure_detail}</span> : null}
                      </span>
                    ) : '—'}
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          {detail.recoveries.length ? (
            <div className="text-xs text-[var(--muted)]">
              Prior operator recoveries: {detail.recoveries.length}. Latest by{' '}
              {detail.recoveries.at(-1)?.actor}: {detail.recoveries.at(-1)?.reason}
            </div>
          ) : null}
          {removal ? (
            <div className="rounded-lg border border-[var(--red)]/30 bg-[var(--red-dim)] p-4 text-xs">
              <p className="font-semibold text-[var(--red)]">Removed from Bench v{removal.bench_version} queue</p>
              <p className="mt-1 text-[var(--muted-strong)]">
                By {removal.actor}: {removal.reason}
              </p>
              <p className="mt-1 text-[var(--muted)]">Submission, payment, artifact, screening, scores, and ticket history are preserved.</p>
              {detail.reinstatement_blocking_reason ? (
                <p className="mt-3 text-[var(--amber)]">{detail.reinstatement_blocking_reason}</p>
              ) : null}
              <textarea
                aria-label="Queue reinstatement audit reason"
                value={reinstateReason}
                onChange={(event) => setReinstateReason(event.target.value)}
                rows={2}
                placeholder="Evidence for returning this submission to validator assignment"
                className="mt-3 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs"
              />
              <label className="mt-3 block text-[var(--muted-strong)]">
                Type <span className="font-mono text-[var(--acid)]">REINSTATE TO VALIDATOR QUEUE</span> to confirm
                <input
                  aria-label="Queue reinstatement confirmation"
                  value={reinstateConfirmation}
                  onChange={(event) => setReinstateConfirmation(event.target.value)}
                  className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 font-mono text-xs"
                />
              </label>
              <button
                type="button"
                onClick={() => void submitReinstatement()}
                disabled={readOnly || loading || detail.reinstatement_allowed !== true || reinstateReason.trim().length < 8 || reinstateConfirmation !== 'REINSTATE TO VALIDATOR QUEUE'}
                className="mt-3 min-h-11 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-[#11150d] hover:bg-[var(--acid-hover)] disabled:opacity-40"
              >
                Reinstate in this benchmark queue
              </button>
            </div>
          ) : null}
          {reversedRemoval ? (
            <div className="rounded-lg border border-[var(--line)] bg-[var(--panel)] p-4 text-xs">
              <p className="font-semibold text-[var(--muted-strong)]">Removed from Bench v{reversedRemoval.bench_version} queue, then reinstated</p>
              <p className="mt-1 text-[var(--muted-strong)]">
                Removed by {reversedRemoval.actor}: {reversedRemoval.reason}
              </p>
              {detail.reinstatement ? (
                <p className="mt-1 text-[var(--muted-strong)]">
                  Reinstated by {detail.reinstatement.actor}: {detail.reinstatement.reason}
                </p>
              ) : null}
              <p className="mt-1 text-[var(--muted)]">The removal record is preserved; the submission is back in the queue with the attempt budget it had.</p>
            </div>
          ) : null}
          <div className="rounded-lg border border-[var(--amber)]/25 bg-[var(--amber-dim)] p-4">
            <div className="flex gap-2"><AlertTriangle className="h-4 w-4 text-[var(--amber)]" /><p className="text-xs font-semibold text-[var(--amber)]">Retry validation after validator infrastructure failure</p></div>
            <textarea
              aria-label="Validation retry audit reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
              placeholder="Runtime evidence confirming OOM, storage exhaustion, or validator-owned failure"
              className="mt-3 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs"
            />
            <label className="mt-3 flex gap-2 text-xs text-[var(--muted-strong)]">
              <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
              I confirmed validator-owned failure evidence. Scores and prior attempts remain unchanged.
            </label>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={readOnly || loading || !detail.recovery_allowed || !confirmed || reason.trim().length < 8}
              className="mt-3 min-h-11 rounded-lg bg-[var(--amber)] px-4 text-xs font-semibold text-[#1c1407] disabled:opacity-40"
            >
              Queue minimum retries needed for quorum
            </button>
          </div>
          {!removal ? (
            <div className="rounded-lg border border-[var(--red)]/30 bg-[var(--red-dim)] p-4">
              <div className="flex gap-2"><Trash2 className="h-4 w-4 text-[var(--red)]" /><p className="text-xs font-semibold text-[var(--red)]">Remove failed submission from this benchmark queue</p></div>
              <p className="mt-2 text-xs text-[var(--muted-strong)]">
                Use only when validator attempts are exhausted and the submission should stop receiving Bench v{detail.tickets[0]?.bench_version ?? 'current'} tickets. This is not deletion or rejection; all records remain auditable.
              </p>
              {detail.withdrawal_blocking_reason ? (
                <p className="mt-2 text-xs text-[var(--amber)]">{detail.withdrawal_blocking_reason}</p>
              ) : null}
              <textarea
                aria-label="Queue withdrawal audit reason"
                value={withdrawReason}
                onChange={(event) => setWithdrawReason(event.target.value)}
                rows={2}
                placeholder="Evidence for stopping further validator assignment"
                className="mt-3 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs"
              />
              <label className="mt-3 block text-xs text-[var(--muted-strong)]">
                Type <span className="font-mono text-[var(--red)]">REMOVE FROM VALIDATOR QUEUE</span> to confirm
                <input
                  aria-label="Queue withdrawal confirmation"
                  value={withdrawConfirmation}
                  onChange={(event) => setWithdrawConfirmation(event.target.value)}
                  className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 font-mono text-xs"
                />
              </label>
              <button
                type="button"
                onClick={() => void submitWithdrawal()}
                disabled={readOnly || loading || !detail.withdrawal_allowed || withdrawReason.trim().length < 8 || withdrawConfirmation !== 'REMOVE FROM VALIDATOR QUEUE'}
                className="mt-3 min-h-11 rounded-lg bg-[var(--red)] px-4 text-xs font-semibold text-white disabled:opacity-40"
              >
                Remove from this benchmark queue
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
