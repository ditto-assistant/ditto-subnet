import { useServerFn } from '@tanstack/react-start'
import { RefreshCcw, RotateCcwKey, ShieldAlert } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { V9ContractRetestItem } from '../lib/admin.schemas'
import {
  listV9ContractRetests,
  queueValidatorScoreRetests,
} from '../server/admin.functions'

const CONFIRMATION = 'QUEUE V9 CONTRACT RETESTS'

function short(value: string) {
  return value.length > 18 ? `${value.slice(0, 18)}…` : value
}

export function V9ContractRetestPanel({
  initialItems,
  initialCount,
  requiredRevision,
  requiredManifestSha256,
  readOnly,
}: {
  initialItems: V9ContractRetestItem[]
  initialCount: number
  requiredRevision: string
  requiredManifestSha256: string
  readOnly: boolean
}) {
  const list = useServerFn(listV9ContractRetests)
  const queue = useServerFn(queueValidatorScoreRetests)
  const [items, setItems] = useState(initialItems)
  const [count, setCount] = useState(initialCount)
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [confirmations, setConfirmations] = useState<Record<string, string>>({})
  const [results, setResults] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  const groups = useMemo(() => {
    const grouped = items.reduce<Record<string, V9ContractRetestItem[]>>((all, item) => {
      all[item.validator_hotkey] = [...(all[item.validator_hotkey] ?? []), item]
      return all
    }, {})
    return Object.entries(grouped)
  }, [items])

  const refresh = async () => {
    const next = await list({ data: { limit: 100, offset: 0 } })
    setItems(next.items)
    setCount(next.count)
  }

  const queueGroup = async (validatorHotkey: string, group: V9ContractRetestItem[]) => {
    const reason = reasons[validatorHotkey]?.trim() ?? ''
    const confirmation = confirmations[validatorHotkey]?.trim() ?? ''
    const eligible = group.filter((item) => item.queue_allowed)
    if (reason.length < 8 || confirmation !== CONFIRMATION || eligible.length === 0) return
    setBusy(validatorHotkey)
    setError('')
    try {
      const result = await queue({
        data: {
          validatorHotkey,
          basis: 'v9_contract_mismatch',
          confirmation: CONFIRMATION,
          reason,
          items: eligible.map((item) => ({
            agentId: item.agent_id,
            expectedSnapshot: item.snapshot,
            expectedRunId: item.run_id,
          })),
        },
      })
      const summary = [
        result.activated ? `${result.activated} started` : '',
        result.queued ? `${result.queued} queued` : '',
        result.idempotent ? `${result.idempotent} unchanged` : '',
        result.skipped ? `${result.skipped} skipped` : '',
      ].filter(Boolean)
      setResults((current) => ({
        ...current,
        [validatorHotkey]: summary.join(' · ') || 'Queue is already current',
      }))
      setReasons((current) => ({ ...current, [validatorHotkey]: '' }))
      setConfirmations((current) => ({ ...current, [validatorHotkey]: '' }))
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to queue v9 contract retests')
    } finally {
      setBusy('')
    }
  }

  return (
    <section className="mt-6 overflow-hidden rounded-xl border border-[var(--amber)]/30 bg-[var(--panel)]">
      <div className="flex flex-col gap-3 border-b border-[var(--line)] px-4 py-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-[var(--amber)]" />
            <h2 className="text-sm font-semibold">V9 contract retests</h2>
            <span className="rounded-full bg-[var(--amber-dim)] px-2 py-0.5 text-[10px] font-semibold text-[var(--amber)]">
              {count}
            </span>
          </div>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-[var(--muted-strong)]">
            These accepted v9 scores were produced by the shadow or a missing contract. This is
            deterministic contract repair, not statistical outlier detection. Each score stays
            canonical until the same validator submits its enforce replacement.
          </p>
          <p className="mt-2 font-mono text-[10px] text-[var(--muted)]" title={requiredManifestSha256}>
            Required {requiredRevision} · {short(requiredManifestSha256)}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={Boolean(busy)}
          className="flex min-h-10 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-semibold text-[var(--muted-strong)] hover:bg-white/[0.04] disabled:opacity-40"
        >
          <RefreshCcw className="h-3.5 w-3.5" />
          Refresh
        </button>
      </div>

      {error ? (
        <p role="alert" className="border-b border-[var(--line)] px-4 py-3 text-xs text-[var(--red)]">
          {error}
        </p>
      ) : null}

      {groups.length === 0 ? (
        <div className="px-4 py-10 text-center">
          <p className="text-sm font-semibold">No invalid v9 score contracts</p>
          <p className="mt-2 text-xs text-[var(--muted)]">
            Every accepted v9 score uses the current enforce contract.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-[var(--line)]">
          {groups.map(([validatorHotkey, group]) => {
            const eligible = group.filter((item) => item.queue_allowed)
            const reason = reasons[validatorHotkey] ?? ''
            const confirmation = confirmations[validatorHotkey] ?? ''
            const isBusy = busy === validatorHotkey
            return (
              <div key={validatorHotkey} className="px-4 py-4 sm:px-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="font-mono text-[11px] text-[var(--muted-strong)]" title={validatorHotkey}>
                      {short(validatorHotkey)}
                    </p>
                    <p className="mt-1 text-[10px] text-[var(--muted)]">
                      {group.length} mismatched · {eligible.length} ready ·{' '}
                      {group.filter((item) => item.replacement_pending).length} active ·{' '}
                      {group.filter((item) => item.replacement_queued).length} queued
                    </p>
                  </div>
                  {results[validatorHotkey] ? (
                    <span role="status" className="text-[10px] font-medium text-[var(--cyan)]">
                      {results[validatorHotkey]}
                    </span>
                  ) : null}
                </div>

                <div className="mt-3 overflow-x-auto rounded-lg bg-[var(--panel-soft)]">
                  <table className="w-full min-w-[46rem] text-left text-[10px]">
                    <thead className="text-[var(--muted)]">
                      <tr>
                        <th className="px-3 py-2 font-medium">Agent</th>
                        <th className="px-3 py-2 font-medium">Observed contract</th>
                        <th className="px-3 py-2 font-medium">Score</th>
                        <th className="px-3 py-2 font-medium">State</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--line)] text-[var(--muted-strong)]">
                      {group.map((item) => (
                        <tr key={`${item.agent_id}:${item.run_id}`}>
                          <td className="px-3 py-2.5">
                            <span className="font-medium text-white">{item.agent_name}</span>
                            <span className="ml-2 font-mono text-[var(--muted)]">{short(item.agent_id)}</span>
                          </td>
                          <td className="px-3 py-2.5 font-mono">
                            {item.observed_revision ?? 'missing'} · {item.observed_rollout_mode ?? 'missing'}
                          </td>
                          <td className="px-3 py-2.5 tabular-nums">{item.composite.toFixed(3)}</td>
                          <td className="px-3 py-2.5">
                            {item.replacement_pending
                              ? 'Active'
                              : item.replacement_queued
                                ? `Queued${item.queue_position ? ` #${item.queue_position}` : ''}`
                                : item.queue_allowed
                                  ? 'Ready'
                                  : item.queue_blocking_reason ?? 'Blocked'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="mt-3 grid gap-2 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,0.8fr)_auto] lg:items-end">
                  <label className="text-[10px] font-medium text-[var(--muted)]">
                    Audit reason
                    <input
                      value={reason}
                      onChange={(event) => setReasons((current) => ({ ...current, [validatorHotkey]: event.target.value }))}
                      placeholder="Why these signed scores require contract replacement"
                      className="mt-1 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-xs text-white placeholder:text-[var(--muted)]"
                    />
                  </label>
                  <label className="text-[10px] font-medium text-[var(--muted)]">
                    Type {CONFIRMATION}
                    <input
                      value={confirmation}
                      onChange={(event) => setConfirmations((current) => ({ ...current, [validatorHotkey]: event.target.value }))}
                      className="mt-1 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 font-mono text-xs text-white"
                    />
                  </label>
                  <button
                    type="button"
                    onClick={() => void queueGroup(validatorHotkey, group)}
                    disabled={
                      readOnly ||
                      isBusy ||
                      eligible.length === 0 ||
                      reason.trim().length < 8 ||
                      confirmation.trim() !== CONFIRMATION
                    }
                    className="flex min-h-10 items-center justify-center gap-2 rounded-lg bg-[var(--amber)] px-4 text-xs font-semibold text-[#171006] hover:brightness-110 disabled:opacity-40"
                  >
                    <RotateCcwKey className="h-3.5 w-3.5" />
                    Queue {eligible.length}
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
