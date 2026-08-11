import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, RefreshCw, RotateCcw } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { StuckSubmissionsList } from '../lib/admin.schemas'
import { batchRetryStuckSubmissions, listStuckSubmissions } from '../server/admin.functions'

function short(value: string, length = 16) {
  return value.length > length ? `${value.slice(0, length)}…` : value
}

export function StuckSubmissionFleetPanel({
  initial,
  readOnly,
}: {
  initial: StuckSubmissionsList
  readOnly: boolean
}) {
  const listFn = useServerFn(listStuckSubmissions)
  const retryFn = useServerFn(batchRetryStuckSubmissions)
  const [data, setData] = useState(initial)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const recoverable = useMemo(
    () => data.submissions.filter((item) => item.recovery_allowed),
    [data.submissions],
  )

  async function refresh() {
    setBusy(true)
    setError(null)
    try {
      const next = await listFn({ data: { state: ['exhausted'], detail: 'summary' } })
      setData(next)
      setSelected((current) => new Set(next.submissions
        .filter((item) => item.recovery_allowed && current.has(item.agent_id))
        .map((item) => item.agent_id)))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  async function submit() {
    const items = data.submissions
      .filter((item) => selected.has(item.agent_id) && item.recovery_allowed)
      .map((item) => ({ agentId: item.agent_id, expectedSnapshot: item.snapshot }))
    if (!confirmed || reason.trim().length < 8 || items.length === 0) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await retryFn({ data: { items, reason: reason.trim() } })
      const skipped = result.results.length - result.granted
      setNotice(`Granted ${result.granted} validator retr${result.granted === 1 ? 'y' : 'ies'}${skipped ? `; ${skipped} skipped after snapshot checks` : ''}.`)
      setReason('')
      setConfirmed(false)
      setSelected(new Set())
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      setBusy(false)
    }
  }

  return (
    <section className="mt-6 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="flex flex-wrap items-start gap-3 border-b border-[var(--line)] px-4 py-4">
        <div>
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-[var(--amber)]" />
            <h2 className="text-sm font-semibold">Fleet retry backlog</h2>
          </div>
          <p className="mt-1 max-w-[76ch] text-xs leading-5 text-[var(--muted)]">
            Exhausted validator assignments that need operator evidence. Batch recovery is snapshot-checked and preserves every score, artifact, payment, screening verdict, and prior attempt.
          </p>
        </div>
        <button type="button" onClick={() => void refresh()} disabled={busy} className="ml-auto flex min-h-10 items-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs disabled:opacity-40">
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </div>

      {notice ? <p className="m-4 flex items-center gap-2 text-xs text-[var(--acid)]"><CheckCircle2 className="h-4 w-4" />{notice}</p> : null}
      {error ? <p role="alert" className="m-4 text-xs text-[var(--red)]">{error}</p> : null}

      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-[var(--muted)]"><tr><th className="p-3">Retry</th><th>Submission</th><th>Scores</th><th>Attempts</th><th>Exhausted validators</th><th>Blocker</th></tr></thead>
          <tbody>
            {data.submissions.map((item) => (
              <tr key={item.agent_id} className="border-t border-[var(--line)]">
                <td className="p-3"><input aria-label={`Select ${item.agent_name}`} type="checkbox" checked={selected.has(item.agent_id)} disabled={readOnly || busy || !item.recovery_allowed} onChange={(event) => setSelected((current) => {
                  const next = new Set(current)
                  if (event.target.checked) next.add(item.agent_id)
                  else next.delete(item.agent_id)
                  return next
                })} /></td>
                <td><span className="font-medium">{item.agent_name}{item.agent_version ? ` v${item.agent_version}` : ''}</span><span className="block font-mono text-[10px] text-[var(--muted)]" title={item.agent_id}>{short(item.agent_id)}</span></td>
                <td>{item.score_count}/{item.quorum}</td>
                <td>{item.attempts_used}</td>
                <td>{item.exhausted_validator_count}</td>
                <td className="max-w-[24rem] pr-3 text-[var(--muted-strong)]">{item.blocking_reason ?? (item.recovery_allowed ? 'Operator evidence required' : 'Not recoverable')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {data.submissions.length === 0 ? <p className="p-6 text-center text-xs text-[var(--muted)]">No exhausted validator assignments.</p> : null}

      {!readOnly && recoverable.length > 0 ? (
        <div className="space-y-3 border-t border-[var(--line)] p-4">
          <button type="button" onClick={() => setSelected(new Set(recoverable.map((item) => item.agent_id)))} disabled={busy} className="text-xs text-[var(--acid)] disabled:opacity-40">Select all {recoverable.length} recoverable</button>
          <textarea aria-label="Fleet retry audit reason" value={reason} onChange={(event) => setReason(event.target.value)} rows={2} placeholder="Shared runtime evidence confirming validator-owned failure" className="w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs" />
          <label className="flex gap-2 text-xs text-[var(--muted-strong)]"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />I confirmed validator-owned failure evidence for the selected submissions.</label>
          <button type="button" onClick={() => void submit()} disabled={busy || selected.size === 0 || !confirmed || reason.trim().length < 8} className="flex min-h-11 items-center gap-2 rounded-lg bg-[var(--amber)] px-4 text-xs font-semibold text-[#161109] disabled:opacity-40"><RotateCcw className="h-4 w-4" />Retry {selected.size} selected submission{selected.size === 1 ? '' : 's'}</button>
        </div>
      ) : null}
    </section>
  )
}
