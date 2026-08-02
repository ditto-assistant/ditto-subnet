import { Link } from '@tanstack/react-router'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, ArrowDown, ArrowUp, ListPlus, RefreshCcw, TicketX } from 'lucide-react'
import { useState } from 'react'
import type { ScoreOutlier } from '../lib/admin.schemas'
import {
  listScoreOutliers,
  queueValidatorScoreRetests,
  releaseScoreRetestTicket,
  requestScoreRetest,
} from '../server/admin.functions'

function short(value: string) {
  return value.length > 16 ? `${value.slice(0, 16)}…` : value
}

function score(value: number) {
  return value.toFixed(3)
}

function deadline(value: string) {
  return `${new Intl.DateTimeFormat('en-US', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'UTC',
  }).format(new Date(value))} UTC`
}

export function ScoreOutlierPanel({
  initialItems,
  initialCount,
  initialBenchVersion = null,
  page = 1,
  pageSize = 50,
  readOnly,
}: {
  initialItems: ScoreOutlier[]
  initialCount: number
  /** The era the platform scanned, or null on a build that does not report it. */
  initialBenchVersion?: number | null
  page?: number
  pageSize?: number
  readOnly: boolean
}) {
  const list = useServerFn(listScoreOutliers)
  const retest = useServerFn(requestScoreRetest)
  const queueRetests = useServerFn(queueValidatorScoreRetests)
  const release = useServerFn(releaseScoreRetestTicket)
  const [items, setItems] = useState(initialItems)
  const [count, setCount] = useState(initialCount)
  const [benchVersion, setBenchVersion] = useState(initialBenchVersion)
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [groupReasons, setGroupReasons] = useState<Record<string, string>>({})
  const [groupResults, setGroupResults] = useState<Record<string, string>>({})
  const [busyId, setBusyId] = useState('')
  const [error, setError] = useState('')

  // Re-reads the page the operator is on. Refreshing back to the first page
  // would silently move the rows under a queue decision taken on page three.
  const refresh = async () => {
    const next = await list({
      data: { limit: pageSize, offset: (page - 1) * pageSize },
    })
    setItems(next.items)
    setCount(next.count)
    setBenchVersion(next.bench_version)
  }

  const act = async (item: ScoreOutlier, action: 'retest' | 'release') => {
    const reason = reasons[item.agent_id]?.trim() ?? ''
    if (reason.length < 8) return
    setBusyId(item.agent_id)
    setError('')
    try {
      if (action === 'retest') {
        await retest({
          data: {
            agentId: item.agent_id,
            validatorHotkey: item.outlier.validator_hotkey,
            expectedSnapshot: item.snapshot,
            expectedRunId: item.outlier.run_id,
            reason,
          },
        })
      } else {
        if (!item.replacement_deadline) throw new Error('Replacement ticket has no deadline')
        await release({
          data: {
            agentId: item.agent_id,
            validatorHotkey: item.outlier.validator_hotkey,
            expectedSnapshot: item.snapshot,
            expectedDeadline: item.replacement_deadline,
            reason,
          },
        })
      }
      setReasons((current) => ({ ...current, [item.agent_id]: '' }))
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to update score re-test')
    } finally {
      setBusyId('')
    }
  }

  const groups = Object.entries(items.reduce<Record<string, ScoreOutlier[]>>((current, item) => {
    const hotkey = item.outlier.validator_hotkey
    current[hotkey] = [...(current[hotkey] ?? []), item]
    return current
  }, {})).filter(([, group]) => group.length > 1)

  const queueGroup = async (validatorHotkey: string, group: ScoreOutlier[]) => {
    const reason = groupReasons[validatorHotkey]?.trim() ?? ''
    const eligible = group.filter((item) => item.queue_allowed)
    if (reason.length < 8 || eligible.length === 0) return
    setBusyId(`group:${validatorHotkey}`)
    setError('')
    try {
      const result = await queueRetests({
        data: {
          validatorHotkey,
          reason,
          items: eligible.map((item) => ({
            agentId: item.agent_id,
            expectedSnapshot: item.snapshot,
            expectedRunId: item.outlier.run_id,
          })),
        },
      })
      const parts = [
        result.activated ? `${result.activated} started` : '',
        result.queued ? `${result.queued} queued` : '',
        result.skipped ? `${result.skipped} skipped` : '',
      ].filter(Boolean)
      setGroupResults((current) => ({
        ...current,
        [validatorHotkey]: parts.join(' · ') || 'Queue is already up to date',
      }))
      setGroupReasons((current) => ({ ...current, [validatorHotkey]: '' }))
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to queue validator re-tests')
    } finally {
      setBusyId('')
    }
  }

  return (
    <div className="mt-6 space-y-4">
      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-col gap-3 border-b border-[var(--line)] px-4 py-4 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 text-[var(--amber)]" />
              <h2 className="text-sm font-semibold">Score outliers</h2>
              <span className="rounded-full bg-[var(--amber-dim)] px-2 py-0.5 text-[10px] font-semibold text-[var(--amber)]">
                {count}
              </span>
              {benchVersion === null ? null : (
                <span className="rounded-full border border-[var(--line)] px-2 py-0.5 text-[10px] text-[var(--muted-strong)]">
                  Benchmark v{benchVersion}
                </span>
              )}
            </div>
            <p className="mt-1 max-w-3xl text-xs leading-5 text-[var(--muted)]">
              One extreme is flagged when its gap from the median is at least 0.15 and
              at least twice the spread between the other two scores. Re-tests stay on
              the same validator and keep the finalized score live until the replacement lands.
              {benchVersion === null
                ? null
                : ` Only submissions on v${benchVersion} — the era being scored now — are listed: a re-test runs the current contract, and a score finalized under an older era cannot be re-scored into a comparable number.`}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={Boolean(busyId)}
            className="flex min-h-10 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-semibold text-[var(--muted-strong)] hover:bg-white/[0.04] disabled:opacity-40"
          >
            <RefreshCcw className="h-3.5 w-3.5" />
            Refresh
          </button>
        </div>

        {error ? <p role="alert" className="border-b border-[var(--line)] px-4 py-3 text-xs text-[var(--red)]">{error}</p> : null}
        {groups.length > 0 ? (
          <div className="border-b border-[var(--line)] bg-[var(--panel-soft)] px-4 py-4 sm:px-5">
            <div className="mb-3 flex items-center gap-2">
              <ListPlus className="h-4 w-4 text-[var(--cyan)]" />
              <h3 className="text-xs font-semibold">Queue by validator</h3>
              <span className="text-[10px] text-[var(--muted)]">One active re-test at a time</span>
            </div>
            <div className="space-y-3">
              {groups.map(([validatorHotkey, group]) => {
                const eligible = group.filter((item) => item.queue_allowed)
                const active = group.filter((item) => item.replacement_pending).length
                const queued = group.filter((item) => item.replacement_queued).length
                const reason = groupReasons[validatorHotkey] ?? ''
                const busy = busyId === `group:${validatorHotkey}`
                return (
                  <div key={validatorHotkey} className="rounded-lg border border-[var(--line)] bg-[var(--panel)] p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="min-w-0">
                        <p className="font-mono text-[11px] text-[var(--muted-strong)]" title={validatorHotkey}>{short(validatorHotkey)}</p>
                        <p className="mt-1 text-[10px] text-[var(--muted)]">
                          {group.length} detected · {active} active · {queued} queued
                        </p>
                      </div>
                      {groupResults[validatorHotkey] ? (
                        <span role="status" className="text-[10px] font-medium text-[var(--cyan)]">{groupResults[validatorHotkey]}</span>
                      ) : null}
                    </div>
                    <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                      <label className="min-w-0 flex-1 text-[10px] font-medium text-[var(--muted)]">
                        Shared audit reason
                        <input
                          value={reason}
                          onChange={(event) => setGroupReasons((current) => ({ ...current, [validatorHotkey]: event.target.value }))}
                          placeholder="Evidence shared by this validator's outlier runs"
                          className="mt-1 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-xs text-white placeholder:text-[var(--muted)]"
                        />
                      </label>
                      <button
                        type="button"
                        onClick={() => void queueGroup(validatorHotkey, group)}
                        disabled={readOnly || busy || eligible.length === 0 || reason.trim().length < 8}
                        className="mt-auto flex min-h-10 items-center justify-center gap-2 rounded-lg bg-[var(--cyan)] px-4 text-xs font-semibold text-[#071517] hover:brightness-110 disabled:opacity-40"
                      >
                        <ListPlus className="h-3.5 w-3.5" />
                        Queue {eligible.length} re-test{eligible.length === 1 ? '' : 's'}
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        ) : null}
        {items.length === 0 ? (
          <div className="px-4 py-12 text-center">
            <p className="text-sm font-semibold">No score outliers</p>
            <p className="mt-2 text-xs text-[var(--muted)]">The current three-validator score sets are internally consistent.</p>
          </div>
        ) : (
          <div className="divide-y divide-[var(--line)]">
            {items.map((item) => {
              const DirectionIcon = item.direction === 'low' ? ArrowDown : ArrowUp
              const reason = reasons[item.agent_id] ?? ''
              const busy = busyId === item.agent_id
              return (
                <article key={`${item.agent_id}:${item.bench_version}`} className="p-4 sm:p-5">
                  <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <h3 className="text-sm font-semibold">{item.agent_name}</h3>
                        <span className="rounded-full border border-[var(--line)] px-2 py-0.5 text-[10px] text-[var(--muted-strong)]">v{item.bench_version}</span>
                        <span className="flex items-center gap-1 rounded-full bg-[var(--amber-dim)] px-2 py-0.5 text-[10px] font-semibold text-[var(--amber)]">
                          <DirectionIcon className="h-3 w-3" />
                          {item.direction} outlier
                        </span>
                        {item.replacement_pending ? (
                          <span className="rounded-full bg-[var(--cyan-dim)] px-2 py-0.5 text-[10px] font-semibold text-[var(--cyan)]">Re-test pending</span>
                        ) : null}
                        {item.replacement_queued ? (
                          <span className="rounded-full bg-[var(--cyan-dim)] px-2 py-0.5 text-[10px] font-semibold text-[var(--cyan)]">
                            Queued{item.queue_position ? ` #${item.queue_position}` : ''}
                          </span>
                        ) : null}
                      </div>
                      <p className="mt-1 font-mono text-[10px] text-[var(--muted)]">{item.agent_id}</p>
                      <p className="mt-2 text-xs text-[var(--muted-strong)]">
                        Median {score(item.median_composite)} · outlier gap {score(item.deviation)} · peer spread {score(item.peer_spread)}
                      </p>
                    </div>

                    <div className="grid min-w-0 gap-2 sm:grid-cols-3 xl:w-[38rem]">
                      {[...item.peers, item.outlier]
                        .sort((left, right) => left.composite - right.composite)
                        .map((entry) => {
                          const isOutlier = entry.validator_hotkey === item.outlier.validator_hotkey
                          return (
                            <div
                              key={entry.validator_hotkey}
                              className={isOutlier ? 'rounded-lg bg-[var(--amber-dim)] p-3' : 'rounded-lg bg-[var(--panel-raised)] p-3'}
                            >
                              <div className="flex items-center justify-between gap-2">
                                <span className="font-mono text-[10px] text-[var(--muted)]" title={entry.validator_hotkey}>{short(entry.validator_hotkey)}</span>
                                {isOutlier ? <span className="text-[9px] font-semibold text-[var(--amber)]">OUTLIER</span> : null}
                              </div>
                              <p className="mt-2 text-lg font-semibold tabular-nums">{score(entry.composite)}</p>
                              <p className="mt-1 truncate font-mono text-[9px] text-[var(--muted)]" title={entry.run_id}>{entry.run_id}</p>
                            </div>
                          )
                        })}
                    </div>
                  </div>

                  {item.replacement_queued ? (
                    <div className="mt-4 rounded-lg bg-[var(--panel-soft)] p-3 text-xs text-[var(--muted-strong)]">
                      Original score remains canonical. This re-test starts automatically when the validator finishes its current assignment.
                    </div>
                  ) : (
                  <div className="mt-4 flex flex-col gap-3 rounded-lg bg-[var(--panel-soft)] p-3 sm:flex-row sm:items-end">
                    <label className="min-w-0 flex-1 text-[10px] font-medium text-[var(--muted)]">
                      Audit reason
                      <input
                        value={reason}
                        onChange={(event) => setReasons((current) => ({ ...current, [item.agent_id]: event.target.value }))}
                        placeholder={item.replacement_pending ? 'Why this replacement ticket should be released' : 'Evidence that this validator score should be re-tested'}
                        className="mt-1 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-xs text-white placeholder:text-[var(--muted)]"
                      />
                    </label>
                    {item.replacement_pending ? (
                      <button
                        type="button"
                        onClick={() => void act(item, 'release')}
                        disabled={readOnly || busy || reason.trim().length < 8}
                        className="flex min-h-10 items-center justify-center gap-2 rounded-lg border border-[var(--red)]/40 px-4 text-xs font-semibold text-[var(--red)] hover:bg-[var(--red-dim)] disabled:opacity-40"
                      >
                        <TicketX className="h-3.5 w-3.5" />
                        Release ticket
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => void act(item, 'retest')}
                        disabled={readOnly || busy || !item.replacement_allowed || reason.trim().length < 8}
                        className="flex min-h-10 items-center justify-center gap-2 rounded-lg bg-[var(--amber)] px-4 text-xs font-semibold text-[#1c1407] hover:bg-[#ffc978] disabled:opacity-40"
                        title={item.blocking_reason ?? undefined}
                      >
                        <RefreshCcw className="h-3.5 w-3.5" />
                        Re-test same validator
                      </button>
                    )}
                  </div>
                  )}
                  {item.blocking_reason && !item.replacement_pending && !item.replacement_queued ? (
                    <p className="mt-2 text-xs text-[var(--amber)]">{item.blocking_reason}</p>
                  ) : null}
                  {item.replacement_pending && item.replacement_deadline ? (
                    <p className="mt-2 text-[10px] text-[var(--muted)]">
                      Original score remains canonical. Replacement ticket expires {deadline(item.replacement_deadline)}.
                    </p>
                  ) : null}
                </article>
              )
            })}
          </div>
        )}
      </section>
      <ScoreOutlierPagination page={page} pageSize={pageSize} total={count} />
    </div>
  )
}

function ScoreOutlierPagination({
  page,
  pageSize,
  total,
}: {
  page: number
  pageSize: number
  total: number
}) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  if (pageCount <= 1) return null

  const first = (page - 1) * pageSize + 1
  const last = Math.min(page * pageSize, total)

  return (
    <nav
      className="mt-4 flex flex-col gap-3 border-y border-[var(--line)] py-3 sm:flex-row sm:items-center sm:justify-between"
      aria-label="Score outlier pagination"
    >
      <p className="text-xs text-[var(--muted-strong)]">
        Showing {first.toLocaleString()}–{last.toLocaleString()} of {total.toLocaleString()}
      </p>
      <div className="flex items-center gap-2">
        <Link
          to="/score-outliers"
          search={{ page: Math.max(1, page - 1) }}
          disabled={page <= 1}
          className="inline-flex min-h-11 items-center rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:pointer-events-none disabled:opacity-40"
        >
          Previous
        </Link>
        <span className="min-w-20 text-center text-xs text-[var(--muted)]">
          Page {page} of {pageCount}
        </span>
        <Link
          to="/score-outliers"
          search={{ page: Math.min(pageCount, page + 1) }}
          disabled={page >= pageCount}
          className="inline-flex min-h-11 items-center rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:pointer-events-none disabled:opacity-40"
        >
          Next
        </Link>
      </div>
    </nav>
  )
}
