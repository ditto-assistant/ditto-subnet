import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, Gavel, RefreshCw, ShieldCheck, Sparkles } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { CopyReviewConsoleItem, CopyReviewGeneration, CopyReviewResolution } from '../lib/admin.schemas'
import { decideCopyReview, listCopyReviews, openAthReview } from '../server/admin.functions'
import { CopyReviewSourceDiff } from './CopyReviewSourceDiff'
import { Modal } from './Modal'

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const SHA256_PATTERN = /^[0-9a-f]{64}$/i

function short(value: string, length = 12) {
  return value.length > length ? `${value.slice(0, length)}…` : value
}

function formatDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function formatSimilarity(value: number | null | undefined) {
  return typeof value === 'number' ? value.toFixed(3) : '—'
}

function comparisonLabel(item: CopyReviewConsoleItem) {
  if (item.original.deferred_review) return 'Deferred review'
  const comparison = item.current_comparison
  if (comparison.availability !== 'available') return 'Unavailable'
  if (comparison.same_miner_excluded) return 'Same-owner lineage'
  if (comparison.bulk_eligible) return 'Cleared'
  if (comparison.current_decision === 'hold') return 'Still held'
  return 'Needs review'
}

function reviewType(item: CopyReviewConsoleItem) {
  if (item.original.deferred_review || item.original.review_kind === 'deferred_source_review') {
    return 'Score-qualified source review'
  }
  return item.original.review_kind === 'benchmark_overfit'
    ? 'Manual benchmark-overfit review'
    : 'Copy review'
}

function rejectionDescription(item: Pick<CopyReviewConsoleItem, 'original'>) {
  if (item.original.review_kind === 'copy') return 'rejected as a confirmed copy'
  if (item.original.review_kind === 'deferred_source_review') {
    return 'rejected after score-qualified source review'
  }
  return 'rejected after benchmark-overfit review'
}

function triggerLabel(value: string) {
  return value.replace(/[_-]+/g, ' ')
}

function budgetPair(used: number | null, maximum: number | null, unit = '') {
  if (used == null || maximum == null) return null
  return `${used.toLocaleString()}/${maximum.toLocaleString()}${unit}`
}

function BudgetEvidence({
  label,
  used,
  maximum,
  unit = '',
}: {
  label: string
  used: number | null
  maximum: number | null
  unit?: string
}) {
  const value = budgetPair(used, maximum, unit)
  return value ? <span>{label} {value}</span> : null
}

type BulkProgress = {
  done: number
  total: number
  failures: Array<{ agentId: string; message: string }>
}

export function CopyReviewPanel({
  initialItems,
  initialBulkEligibleCount,
  initialGeneration,
  initialActiveBenchVersion,
  initialRolloutBenchVersion,
  readOnly,
}: {
  initialItems: Array<CopyReviewConsoleItem>
  initialBulkEligibleCount: number
  initialGeneration: CopyReviewGeneration | 'all'
  initialActiveBenchVersion: number
  initialRolloutBenchVersion: number | null
  readOnly: boolean
}) {
  const listFn = useServerFn(listCopyReviews)
  const decideFn = useServerFn(decideCopyReview)
  const openFn = useServerFn(openAthReview)
  const [items, setItems] = useState(initialItems)
  const [bulkEligibleCount, setBulkEligibleCount] = useState(initialBulkEligibleCount)
  const [generation, setGeneration] = useState<CopyReviewGeneration>(
    initialGeneration === 'history' || initialGeneration === 'rollout'
      ? initialGeneration
      : 'active',
  )
  const [activeBenchVersion, setActiveBenchVersion] = useState(initialActiveBenchVersion)
  const [rolloutBenchVersion, setRolloutBenchVersion] = useState(initialRolloutBenchVersion)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [resolution, setResolution] = useState<CopyReviewResolution>('clear')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [bulk, setBulk] = useState<BulkProgress | null>(null)
  const [holdAgentId, setHoldAgentId] = useState('')
  const [holdSha256, setHoldSha256] = useState('')
  const [holdScoreCount, setHoldScoreCount] = useState('')
  const [holdReason, setHoldReason] = useState('')
  const [confirmation, setConfirmation] = useState<'hold' | 'decision' | 'bulk' | null>(null)

  const selected = useMemo(
    () => items.find((item) => item.agent_id === selectedId) ?? null,
    [items, selectedId],
  )
  const bulkEligible = useMemo(
    () => items.filter((item) => item.current_comparison.bulk_eligible),
    [items],
  )

  async function refresh(nextGeneration: CopyReviewGeneration = generation) {
    const data = await listFn({ data: { generation: nextGeneration } })
    setItems(data.items)
    setBulkEligibleCount(data.bulk_eligible_count)
    setGeneration(nextGeneration)
    setActiveBenchVersion(data.active_bench_version)
    setRolloutBenchVersion(data.rollout_bench_version)
    setSelectedId((current) =>
      current && data.items.some((item) => item.agent_id === current) ? current : null,
    )
  }

  async function submitDecision() {
    if (!selected) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await decideFn({
        data: { agentId: selected.agent_id, resolution, reason },
      })
      setNotice(
        `${result.review.agent_name} was ${
          result.review.resolution === 'clear'
            ? 'cleared from review'
            : rejectionDescription(result.review)
        }${result.idempotent ? ' (already recorded)' : ''}.`,
      )
      setReason('')
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  async function submitHold() {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await openFn({
        data: {
          agentId: holdAgentId.trim(),
          expectedSha256: holdSha256.trim().toLowerCase(),
          expectedScoreCount: Number(holdScoreCount),
          reason: holdReason,
        },
      })
      setNotice(
        `${result.review.agent_name} ${result.reopened ? 'was reopened' : 'is held'} for review and excluded from emissions${result.idempotent ? ' (already recorded)' : ''}.`,
      )
      setHoldAgentId('')
      setHoldSha256('')
      setHoldScoreCount('')
      setHoldReason('')
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  async function clearAllEligible() {
    if (bulkEligible.length === 0 || reason.trim().length < 3) return
    setBusy(true)
    setError(null)
    setNotice(null)
    const progress: BulkProgress = { done: 0, total: bulkEligible.length, failures: [] }
    setBulk({ ...progress })
    for (const item of bulkEligible) {
      try {
        await decideFn({ data: { agentId: item.agent_id, resolution: 'clear', reason } })
      } catch (cause) {
        progress.failures.push({
          agentId: item.agent_id,
          message: cause instanceof Error ? cause.message : String(cause),
        })
      }
      progress.done += 1
      setBulk({ ...progress })
    }
    setBulk(null)
    const succeeded = progress.total - progress.failures.length
    setNotice(
      progress.failures.length === 0
        ? `Cleared ${progress.total} eligible agent${progress.total === 1 ? '' : 's'}.`
        : `Cleared ${succeeded} of ${progress.total}; ${progress.failures.length} failed and remain pending.`,
    )
    if (progress.failures.length > 0) {
      setError(
        progress.failures
          .map((failure) => `${short(failure.agentId, 8)}: ${failure.message}`)
          .join('\n'),
      )
    }
    setReason('')
    setBusy(false)
    await refresh()
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3 py-2 text-xs">
          <Gavel className="h-3.5 w-3.5" /> {items.length} pending
        </div>
        <div className="flex items-center gap-2 rounded-full border border-[var(--acid)]/25 bg-[var(--acid-dim)] px-3 py-2 text-xs text-[var(--acid)]">
          <Sparkles className="h-3.5 w-3.5" /> {bulkEligibleCount} safe to clear
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={busy}
          className="ml-auto flex items-center gap-2 rounded-lg border border-white/10 px-3 py-2 text-xs hover:bg-white/[0.05] disabled:opacity-50"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2" aria-label="Review generation">
        <button
          type="button"
          aria-pressed={generation === 'active'}
          onClick={() => void refresh('active')}
          disabled={busy}
          className={`rounded-lg border px-3 py-2 text-xs transition-colors disabled:opacity-50 ${
            generation === 'active'
              ? 'border-[var(--acid)]/35 bg-[var(--acid-dim)] text-[var(--acid)]'
              : 'border-white/10 text-[var(--muted-strong)] hover:bg-white/[0.05]'
          }`}
        >
          Active benchmark v{activeBenchVersion}
        </button>
        {rolloutBenchVersion != null ? (
          <button
            type="button"
            aria-pressed={generation === 'rollout'}
            onClick={() => void refresh('rollout')}
            disabled={busy}
            className={`rounded-lg border px-3 py-2 text-xs transition-colors disabled:opacity-50 ${
              generation === 'rollout'
                ? 'border-[var(--acid)]/35 bg-[var(--acid-dim)] text-[var(--acid)]'
                : 'border-white/10 text-[var(--muted-strong)] hover:bg-white/[0.05]'
            }`}
          >
            Rollout target v{rolloutBenchVersion}
          </button>
        ) : null}
        <button
          type="button"
          aria-pressed={generation === 'history'}
          onClick={() => void refresh('history')}
          disabled={busy}
          className={`rounded-lg border px-3 py-2 text-xs transition-colors disabled:opacity-50 ${
            generation === 'history'
              ? 'border-[var(--amber)]/35 bg-[var(--amber-dim)] text-[var(--amber)]'
              : 'border-white/10 text-[var(--muted-strong)] hover:bg-white/[0.05]'
          }`}
        >
          Historical reviews
        </button>
        <span className="text-xs text-[var(--muted)]">
          {generation === 'active'
            ? `Only submissions scored on the active v${activeBenchVersion} contract.`
            : generation === 'rollout'
              ? `Submissions scored on rollout target v${rolloutBenchVersion}; these require review before activation can converge.`
              : `Older benchmark generations, kept separate from active and rollout queues.`}
        </span>
      </div>

      {notice ? (
        <div className="flex items-center gap-2 rounded-lg border border-[var(--acid)]/25 bg-[var(--acid-dim)] px-4 py-3 text-sm text-[var(--acid)]">
          <CheckCircle2 className="h-4 w-4 shrink-0" /> {notice}
        </div>
      ) : null}
      {error ? (
        <div className="flex items-start gap-2 whitespace-pre-line rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] px-4 py-3 text-sm text-[var(--red)]">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {error}
        </div>
      ) : null}

      {!readOnly ? (
        <section className="space-y-3 rounded-xl border border-[var(--amber)]/25 bg-[var(--amber-dim)]/30 p-5">
          <div>
            <h3 className="flex items-center gap-2 text-sm font-semibold text-[var(--amber)]">
              <AlertTriangle className="h-4 w-4" /> Hold or reopen for review
            </h3>
            <p id="ath-hold-help" className="mt-1 max-w-[72ch] text-xs text-[var(--muted-strong)]">
              Removes one exact scored or live artifact from emissions without deleting its scores. A resolved review is reopened with its immutable original evidence and prior decisions preserved. Confirm the artifact digest and current score count before continuing. The reason is public and miner-visible.
            </p>
          </div>
          <fieldset aria-describedby="ath-hold-help" className="grid gap-3 lg:grid-cols-2" disabled={busy}>
            <label className="space-y-1 text-xs text-[var(--muted-strong)]">
              <span>Agent ID</span>
              <input aria-label="Agent ID" value={holdAgentId} onChange={(event) => setHoldAgentId(event.target.value)} placeholder="00000000-0000-4000-8000-000000000000" className="w-full rounded-lg border border-white/10 bg-transparent px-3 py-2 font-mono text-sm text-white" />
            </label>
            <label className="space-y-1 text-xs text-[var(--muted-strong)]">
              <span>Artifact SHA-256</span>
              <input aria-label="Artifact SHA-256" value={holdSha256} onChange={(event) => setHoldSha256(event.target.value)} placeholder="64-character digest" className="w-full rounded-lg border border-white/10 bg-transparent px-3 py-2 font-mono text-sm text-white" />
            </label>
            <label className="space-y-1 text-xs text-[var(--muted-strong)]">
              <span>Current score count</span>
              <input aria-label="Score count" type="number" min="0" step="1" value={holdScoreCount} onChange={(event) => setHoldScoreCount(event.target.value)} placeholder="0" className="w-full rounded-lg border border-white/10 bg-transparent px-3 py-2 text-sm text-white" />
            </label>
            <label className="space-y-1 text-xs text-[var(--muted-strong)]">
              <span>Public hold reason</span>
              <textarea aria-label="Hold reason" value={holdReason} onChange={(event) => setHoldReason(event.target.value)} placeholder="Miner-visible explanation of why this submission is held" rows={2} className="w-full rounded-lg border border-white/10 bg-transparent px-3 py-2 text-sm text-white" />
            </label>
          </fieldset>
          <button
            type="button"
            onClick={() => setConfirmation('hold')}
            disabled={
              busy ||
              !UUID_PATTERN.test(holdAgentId.trim()) ||
              !SHA256_PATTERN.test(holdSha256.trim()) ||
              !/^\d+$/.test(holdScoreCount) ||
              holdReason.trim().length < 3
            }
            className="rounded-lg bg-[var(--amber-dim)] px-4 py-2 text-sm font-medium text-[var(--amber)] transition-colors hover:bg-[#3a2d19] disabled:opacity-50"
          >
            {busy ? 'Updating review…' : 'Preview hold or reopen'}
          </button>
        </section>
      ) : null}

      {items.length === 0 ? (
        <div className="rounded-xl border border-white/10 bg-white/[0.02] p-8 text-center text-sm text-[var(--muted)]">
          {generation === 'active'
            ? `No benchmark v${activeBenchVersion} submissions are awaiting operator review.`
            : generation === 'rollout'
              ? `No rollout target v${rolloutBenchVersion} submissions are awaiting operator review.`
              : 'No historical submissions are awaiting operator review.'}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-white/10">
          <table className="w-full text-left text-sm">
            <thead className="bg-white/[0.03] text-xs uppercase tracking-wide text-[var(--muted)]">
              <tr>
                <th className="px-4 py-3">Agent</th>
                <th className="px-4 py-3">Miner</th>
                <th className="px-4 py-3">Review opened</th>
                <th className="px-4 py-3">Original hold</th>
                <th className="px-4 py-3">Current comparison</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const label = comparisonLabel(item)
                return (
                  <tr
                    key={item.review_id}
                    onClick={() => {
                      setSelectedId(item.agent_id)
                      setResolution(item.current_comparison.bulk_eligible ? 'clear' : 'reject')
                    }}
                    className={`cursor-pointer border-t border-white/5 transition-colors hover:bg-white/[0.04] ${selectedId === item.agent_id ? 'bg-white/[0.06]' : ''}`}
                  >
                    <td className="px-4 py-3 font-medium">
                      {item.agent_name}{item.agent_version != null ? ` v${item.agent_version}` : ''}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs" title={item.miner_hotkey}>{short(item.miner_hotkey)}</td>
                    <td className="px-4 py-3 text-xs">{formatDate(item.opened_at)}</td>
                    <td className="max-w-[18rem] px-4 py-3 text-xs" title={item.original.reason ?? undefined}>
                      {item.original.deferred_review || item.original.review_kind === 'deferred_source_review' ? (
                        <span className="block truncate font-medium">score-qualified source review</span>
                      ) : item.original.review_kind === 'benchmark_overfit' ? (
                        <span className="block truncate font-medium">manual benchmark review</span>
                      ) : item.original.duplicate_of_name ? (
                        <span className="block truncate font-medium">
                          copy of {item.original.duplicate_of_name}
                          {item.original.duplicate_of_version != null
                            ? ` v${item.original.duplicate_of_version}`
                            : ''}
                        </span>
                      ) : null}
                      <span className="block truncate text-[var(--muted-strong)]">
                        {item.original.reason ?? 'No stored reason'}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-xs ${
                        label === 'Cleared'
                          ? 'border-[var(--acid)]/25 bg-[var(--acid-dim)] text-[var(--acid)]'
                          : 'border-[var(--amber)]/25 bg-[var(--amber-dim)] text-[var(--amber)]'
                      }`}>
                        {label === 'Cleared' ? <ShieldCheck className="h-3 w-3" /> : <AlertTriangle className="h-3 w-3" />}
                        {label}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {selected ? (
        <div className="space-y-5 rounded-xl border border-white/10 bg-white/[0.02] p-5">
          <h3 className="text-sm font-semibold">
            {selected.agent_name} · <span className="font-mono">{short(selected.agent_id, 8)}</span>
          </h3>
          <div className="grid gap-4 lg:grid-cols-2">
            <section className="rounded-lg border border-white/10 p-4">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">Review evidence</h4>
              <dl className="mt-3 space-y-2 text-xs text-[var(--muted-strong)]">
                <div><dt className="text-[var(--muted)]">Review type</dt><dd>{reviewType(selected)}</dd></div>
                <div><dt className="text-[var(--muted)]">Reason</dt><dd>{selected.original.reason ?? 'No stored reason'}</dd></div>
                {selected.original.deferred_review ? (
                  <>
                    <div>
                      <dt className="text-[var(--muted)]">Qualification</dt>
                      <dd className="capitalize">
                        {selected.original.deferred_review.triggers.map(triggerLabel).join(', ')}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[var(--muted)]">Score snapshot</dt>
                      <dd>
                        {selected.original.deferred_review.rank != null
                          ? `rank ${selected.original.deferred_review.rank} · `
                          : ''}
                        {selected.original.deferred_review.peer_count} peers from a cohort of{' '}
                        {selected.original.deferred_review.cohort_size}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[var(--muted)]">Candidate axes</dt>
                      <dd className="mt-1 flex flex-wrap gap-x-4 gap-y-1 font-mono">
                        {Object.entries(selected.original.deferred_review.candidate).map(([axis, value]) => (
                          <span key={axis}>{triggerLabel(axis)} {value.toFixed(4)}</span>
                        ))}
                      </dd>
                    </div>
                    {selected.original.deferred_review.thresholds ? (
                      <div>
                        <dt className="text-[var(--muted)]">Anomaly thresholds</dt>
                        <dd className="mt-1 flex flex-wrap gap-x-4 gap-y-1 font-mono">
                          {Object.entries(selected.original.deferred_review.thresholds).flatMap(
                            ([axis, values]) => Object.entries(values).map(([name, value]) => (
                              <span key={`${axis}:${name}`}>
                                {triggerLabel(axis)} {triggerLabel(name)} {value.toFixed(4)}
                              </span>
                            )),
                          )}
                        </dd>
                      </div>
                    ) : null}
                    <div>
                      <dt className="text-[var(--muted)]">Deep-review evidence</dt>
                      <dd>
                        {selected.original.deferred_review.screening_reason_code
                          ? triggerLabel(selected.original.deferred_review.screening_reason_code)
                          : 'Awaiting terminal screener evidence'}
                        {selected.original.deferred_review.screening_attempt_id ? (
                          <span className="ml-2 font-mono text-[var(--muted)]">
                            {short(selected.original.deferred_review.screening_attempt_id, 12)}
                          </span>
                        ) : null}
                      </dd>
                    </div>
                    {selected.original.deferred_review.review_audit ? (
                      <div>
                        <dt className="text-[var(--muted)]">Review budget evidence</dt>
                        <dd className="mt-1 space-y-1">
                          <p className="capitalize">
                            {selected.original.deferred_review.review_audit.stage} ·{' '}
                            {triggerLabel(selected.original.deferred_review.review_audit.reason_code)}
                          </p>
                          <p className="flex flex-wrap gap-x-4 gap-y-1 font-mono">
                            <span>
                              steps{' '}
                              {selected.original.deferred_review.review_audit.steps_used}/
                              {selected.original.deferred_review.review_audit.max_steps}
                            </span>
                            <BudgetEvidence
                              label="read"
                              used={selected.original.deferred_review.review_audit.read_bytes_used}
                              maximum={selected.original.deferred_review.review_audit.max_read_bytes}
                              unit=" bytes"
                            />
                            <BudgetEvidence
                              label="input"
                              used={selected.original.deferred_review.review_audit.input_tokens_used}
                              maximum={selected.original.deferred_review.review_audit.max_input_tokens}
                            />
                            <BudgetEvidence
                              label="output"
                              used={selected.original.deferred_review.review_audit.output_tokens_used}
                              maximum={selected.original.deferred_review.review_audit.max_output_tokens}
                            />
                            <BudgetEvidence
                              label="cost"
                              used={selected.original.deferred_review.review_audit.cost_usd_used}
                              maximum={selected.original.deferred_review.review_audit.max_cost_usd}
                              unit=" USD"
                            />
                          </p>
                          <p className="font-mono text-[10px] text-[var(--muted)]">
                            prompt {selected.original.deferred_review.review_audit.prompt_revision}
                            {selected.original.deferred_review.review_audit.harness_revision
                              ? ` · harness ${selected.original.deferred_review.review_audit.harness_revision}`
                              : ''}
                            {selected.original.deferred_review.review_audit_digest
                              ? ` · digest ${short(selected.original.deferred_review.review_audit_digest, 12)}`
                              : ''}
                          </p>
                        </dd>
                      </div>
                    ) : null}
                  </>
                ) : null}
                {!selected.original.deferred_review ? <div>
                  <dt className="text-[var(--muted)]">Matched submission</dt>
                  <dd>
                    {selected.original.duplicate_of_name ? (
                      <>
                        <span className="font-medium">
                          {selected.original.duplicate_of_name}
                          {selected.original.duplicate_of_version != null
                            ? ` v${selected.original.duplicate_of_version}`
                            : ''}
                        </span>
                        {selected.original.duplicate_of_hotkey ? (
                          <span
                            className="ml-2 font-mono text-[var(--muted)]"
                            title={selected.original.duplicate_of_hotkey}
                          >
                            {short(selected.original.duplicate_of_hotkey)}
                          </span>
                        ) : null}
                        {selected.original.duplicate_of_submitted_at ? (
                          <span className="ml-2 text-[var(--muted)]">
                            submitted {formatDate(selected.original.duplicate_of_submitted_at)}
                          </span>
                        ) : null}
                        <span className="ml-2 font-mono text-[var(--muted)]">
                          {selected.original.duplicate_of
                            ? short(selected.original.duplicate_of, 12)
                            : ''}
                        </span>
                      </>
                    ) : (
                      <span className="font-mono">
                        {selected.original.duplicate_of
                          ? short(selected.original.duplicate_of, 12)
                          : 'Unavailable'}
                      </span>
                    )}
                  </dd>
                </div> : null}
                <div><dt className="text-[var(--muted)]">Policy</dt><dd>{selected.original.policy_version ?? 'Legacy / unknown'}</dd></div>
                <div><dt className="text-[var(--muted)]">Snapshot</dt><dd>{selected.original.backfilled ? 'Backfilled immutable legacy snapshot' : 'Captured when review opened'}</dd></div>
              </dl>
            </section>
            <section className="rounded-lg border border-white/10 p-4">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">Current calibrated comparison</h4>
              {selected.original.deferred_review ? (
                <div className="mt-3 space-y-2 text-xs text-[var(--muted-strong)]">
                  <p>
                    This hold came from score qualification, not a copy comparison. The trigger
                    snapshot is immutable operator evidence; it is not a verdict by itself.
                  </p>
                  <p className="capitalize">
                    Policy mode: {selected.original.deferred_review.mode}
                  </p>
                </div>
              ) : selected.current_comparison.availability !== 'available' ? (
                <p className="mt-3 text-xs text-[var(--amber)]">Unavailable — this row is not eligible for bulk clearing. {selected.current_comparison.reason}</p>
              ) : (
                <dl className="mt-3 space-y-2 text-xs text-[var(--muted-strong)]">
                  <div><dt className="text-[var(--muted)]">Decision</dt><dd>{selected.current_comparison.same_miner_excluded ? 'Excluded — same payment owner lineage' : (selected.current_comparison.current_decision ?? 'Needs review')}</dd></div>
                  <div><dt className="text-[var(--muted)]">Owner rule</dt><dd>{selected.current_comparison.miner_exclusion_mode}</dd></div>
                  <div><dt className="text-[var(--muted)]">Algorithm</dt><dd>{selected.current_comparison.algorithm_version}</dd></div>
                  <div><dt className="text-[var(--muted)]">Reference revision</dt><dd className="font-mono">{selected.current_comparison.canonical_reference_revision ? short(selected.current_comparison.canonical_reference_revision, 12) : '—'}</dd></div>
                  <div>
                    <dt className="text-[var(--muted)]">Similarity (Jaccard / containment)</dt>
                    <dd className="mt-1 flex flex-wrap gap-x-4 gap-y-1 font-mono">
                      <span>lexical {formatSimilarity(selected.current_comparison.lexical?.jaccard)} / {formatSimilarity(selected.current_comparison.lexical?.containment)}</span>
                      <span>structural {formatSimilarity(selected.current_comparison.structural?.jaccard)} / {formatSimilarity(selected.current_comparison.structural?.containment)}</span>
                      <span>prompt {formatSimilarity(selected.current_comparison.prompt?.jaccard)} / {formatSimilarity(selected.current_comparison.prompt?.containment)}</span>
                    </dd>
                  </div>
                </dl>
              )}
            </section>
          </div>

          {selected.original.duplicate_of ? (
            <CopyReviewSourceDiff agentId={selected.agent_id} canView={!readOnly} />
          ) : null}

          {readOnly ? (
            <p className="text-xs text-[var(--muted)]">Read-only access: decisions are disabled.</p>
          ) : (
            <fieldset className="space-y-3" disabled={busy}>
              <div className="flex gap-4 text-sm">
                <label className="flex items-center gap-2"><input type="radio" name="copy-review-resolution" checked={resolution === 'clear'} onChange={() => setResolution('clear')} />Clear hold</label>
                <label className="flex items-center gap-2"><input type="radio" name="copy-review-resolution" checked={resolution === 'reject'} onChange={() => setResolution('reject')} />Reject submission</label>
              </div>
              <textarea value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Miner-visible reason recorded with your operator identity (min 3 characters)" rows={2} className="w-full rounded-lg border border-white/10 bg-transparent px-3 py-2 text-sm" />
              <button type="button" onClick={() => setConfirmation('decision')} disabled={reason.trim().length < 3} className={`rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50 ${resolution === 'clear' ? 'bg-[var(--acid-dim)] text-[var(--acid)]' : 'bg-[var(--red-dim)] text-[var(--red)]'}`}>
                Preview {resolution}
              </button>
            </fieldset>
          )}
        </div>
      ) : null}

      {!readOnly && bulkEligible.length > 0 ? (
        <div className="space-y-3 rounded-xl border border-[var(--acid)]/20 bg-[var(--acid-dim)]/40 p-5">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-[var(--acid)]"><Sparkles className="h-4 w-4" /> Clear all eligible ({bulkEligible.length})</h3>
          <p className="text-xs text-[var(--muted-strong)]">
            Issues one separately audited decision per submission. Only rows explicitly marked bulk eligible by the deployed calibrated comparison are included; failures remain pending and are reported individually.
          </p>
          <textarea value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Shared miner-visible reason recorded on every decision (min 3 characters)" rows={2} disabled={busy} className="w-full rounded-lg border border-white/10 bg-transparent px-3 py-2 text-sm" />
          <button type="button" onClick={() => setConfirmation('bulk')} disabled={busy || reason.trim().length < 3} className="rounded-lg bg-[var(--acid-dim)] px-4 py-2 text-sm font-medium text-[var(--acid)] disabled:opacity-50">
            {bulk ? `Clearing ${bulk.done}/${bulk.total}…` : `Preview clearing ${bulkEligible.length} eligible submission${bulkEligible.length === 1 ? '' : 's'}`}
          </button>
        </div>
      ) : null}
      {confirmation ? (
        <Modal
          title="Confirm ATH review action"
          description="Preview only — no review state has changed. Verify the exact action and miner-visible reason before execution."
          onClose={() => !busy && setConfirmation(null)}
        >
          <dl className="space-y-3 rounded-lg border border-white/10 p-4 text-xs">
            <div>
              <dt className="text-[var(--muted)]">Action</dt>
              <dd className="mt-1 font-medium text-white">
                {confirmation === 'hold'
                  ? `Hold or reopen ${short(holdAgentId, 12)} and block emissions`
                  : confirmation === 'bulk'
                    ? `Clear ${bulkEligible.length} calibrated eligible holds`
                    : `${resolution === 'clear' ? 'Clear hold for' : 'Reject'} ${selected?.agent_name ?? 'selected submission'}`}
              </dd>
            </div>
            <div>
              <dt className="text-[var(--muted)]">Miner-visible reason</dt>
              <dd className="mt-1 whitespace-pre-wrap text-[var(--muted-strong)]">
                {confirmation === 'hold' ? holdReason : reason}
              </dd>
            </div>
            {confirmation === 'hold' ? (
              <div>
                <dt className="text-[var(--muted)]">Concurrency guards</dt>
                <dd className="mt-1 font-mono text-[var(--muted-strong)]">
                  SHA-256 {short(holdSha256, 16)} · {holdScoreCount} scores
                </dd>
              </div>
            ) : null}
          </dl>
          <div className="mt-5 flex justify-end gap-2">
            <button type="button" onClick={() => setConfirmation(null)} disabled={busy} className="rounded-lg border border-white/10 px-4 py-2 text-sm disabled:opacity-50">
              Back
            </button>
            <button
              type="button"
              onClick={() => {
                const action = confirmation
                setConfirmation(null)
                if (action === 'hold') void submitHold()
                else if (action === 'bulk') void clearAllEligible()
                else void submitDecision()
              }}
              disabled={busy}
              className="rounded-lg bg-[var(--red)] px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            >
              Confirm and execute
            </button>
          </div>
        </Modal>
      ) : null}
    </div>
  )
}
