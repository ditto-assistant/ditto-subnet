import { useServerFn } from '@tanstack/react-start'
import { Link } from '@tanstack/react-router'
import {
  AlertTriangle,
  ArrowUpDown,
  CheckCircle2,
  Download,
  History,
  ListChecks,
  MessageSquareText,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  XCircle,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import type {
  AttestationEvidenceGrade,
  OwnerAttestations,
  QuarantineResolution,
  ScreeningDispute,
  ScreeningDisputeResolution,
  ScreeningQuarantine,
  ScreeningQuarantineBatchDecision,
  ScreeningQuarantineBatchPreview,
  ScreeningSubmission,
} from '../lib/admin.schemas'
import {
  decideScreeningDispute,
  executeScreeningQuarantineDecisions,
  getOwnerAttestations,
  getScreeningArtifact,
  listScreeningQuarantines,
  listScreeningDisputes,
  previewScreeningQuarantineDecisions,
} from '../server/admin.functions'
import { Modal } from './Modal'
import { QuarantineEvidencePanel } from './QuarantineEvidencePanel'

type QuarantineFilter = 'active' | 'resolved' | 'all'
type QuarantineSort = 'oldest' | 'newest'
type QuarantineView = 'queue' | 'disputes' | 'history'

const resolutionCopy: Record<
  QuarantineResolution,
  { label: string; description: string; icon: typeof ShieldCheck; tone: string }
> = {
  release: {
    label: 'Release to validation',
    description: 'Accept the review and allow validators to score this submission.',
    icon: ShieldCheck,
    tone: 'text-[var(--acid)]',
  },
  rescreen: {
    label: 'Rescreen submission',
    description: 'Return the preserved artifact to the current screening queue.',
    icon: RotateCcw,
    tone: 'text-[var(--amber)]',
  },
  reject: {
    label: 'Reject submission',
    description: 'Permanently reject this submission after review.',
    icon: XCircle,
    tone: 'text-[var(--red)]',
  },
}

function short(value: string, length = 12) {
  return value.length > length ? `${value.slice(0, length)}…` : value
}

function formatDate(value: string | null) {
  if (!value) return 'Not recorded'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Not recorded'
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(date)
}

function submissionLabel(version: number | null | undefined) {
  return version == null ? 'Legacy submission' : `Submission v${version}`
}

function SubmissionBadge({ version }: { version: number | null | undefined }) {
  return (
    <span className="inline-flex shrink-0 rounded-full border border-[var(--line)] bg-white/[0.035] px-2 py-0.5 font-mono text-[10px] font-medium text-[var(--muted-strong)]">
      {submissionLabel(version)}
    </span>
  )
}

/**
 * How the two halves of a link were proven. Reviewer context only: every grade
 * establishes the link identically, so this label must never read as a
 * strength threshold the reviewer is supposed to apply.
 */
const evidenceGradeLabel: Record<AttestationEvidenceGrade, string> = {
  'hotkey-hotkey': 'Both halves signed by hotkey',
  mixed: 'One half by hotkey, one by payment coldkey',
  'coldkey-coldkey': 'Both halves signed by payment coldkey',
}

/**
 * Signed owner links for the held agent's miner hotkey.
 *
 * A different and stronger class of evidence than the payment-coldkey
 * inference a reviewer otherwise falls back on: this is a symmetric link that
 * BOTH hotkeys signed, so it is a claim of control rather than a shared-wallet
 * coincidence. Each endpoint may sign with its own hotkey or with the coldkey
 * bound to it by payments, which is what `evidence_grade` reports — context
 * for the reviewer, not a gate, which is why the grade is shown as a plain
 * caption rather than a ranked badge.
 *
 * It is also narrower, which is why the scope line is not optional copy: a link
 * exempts plagiarism screening between the two hotkeys' submissions and buys
 * nothing else — it does not touch emission-slot allocation. Links are direct
 * only and never chained; the relation is not transitive.
 *
 * Revoked links are shown rather than hidden, because the question a dispute
 * turns on is whether the link was live when the held submission was made.
 *
 * Renders only when there is something to report: a link, or a failed lookup —
 * silence would otherwise read as "no linked identity", which is the one thing
 * a failed check does not establish.
 */
function LinkedIdentity({ hotkey }: { hotkey: string }) {
  const loadAttestations = useServerFn(getOwnerAttestations)
  const [links, setLinks] = useState<OwnerAttestations | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLinks(null)
    setFailed(false)
    loadAttestations({ data: { hotkey } })
      .then((result) => {
        if (!cancelled) setLinks(result)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [hotkey, loadAttestations])

  // Every link naming this hotkey, revoked ones included. The counterparty is
  // already resolved by the platform relative to the hotkey we asked about.
  const rows = links?.attestations ?? []

  if (!failed && rows.length === 0) return null

  return (
    <div className="sm:col-span-2 xl:col-span-1">
      <dt
        className="flex items-center gap-1.5 text-[var(--muted)]"
        title="A symmetric owner link that both hotkeys signed: each end proved its half with its own hotkey or with the coldkey bound to it by payments. A signature is stronger evidence of ownership than payment-coldkey inference. The link exempts plagiarism screening between these two hotkeys and nothing else — it does not affect emission-slot allocation, and it is not transitive."
      >
        <ShieldCheck className="h-3.5 w-3.5 text-[var(--acid)]" />
        Linked identity (signed)
      </dt>
      {failed ? (
        <dd className="mt-1 text-[var(--muted-strong)]">
          Link check unavailable — attestations were not read, so treat linked identity
          as unknown rather than absent.
        </dd>
      ) : (
        <dd className="mt-1 space-y-1.5">
          {rows.map((row) => (
            <div key={row.attestation_id} className="space-y-0.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="break-all font-mono text-[var(--muted-strong)]">
                  {row.counterparty}
                </span>
                {row.revoked_at ? (
                  <span className="rounded-full bg-[var(--amber-dim)] px-2 py-0.5 text-[10px] font-medium text-[var(--amber)]">
                    Revoked {formatDate(row.revoked_at)}
                  </span>
                ) : null}
              </div>
              <p className="text-[10px] text-[var(--muted)]">
                {row.evidence_grade
                  ? evidenceGradeLabel[row.evidence_grade]
                  : 'Evidence grade not reported'}{' '}
                — context only, every grade establishes the link.
              </p>
            </div>
          ))}
          <p className="text-[10px] leading-4 text-[var(--muted)]">
            Signed owner link, not payment-coldkey inference. Exempts plagiarism
            screening between these hotkeys only; it does not affect emission-slot
            allocation, and it is not transitive.
          </p>
        </dd>
      )}
    </div>
  )
}

export function ScreeningQuarantinePanel({
  view = 'queue',
  initialItems,
  initialDisputes = [],
  initialSubmissions,
  quarantineCount = initialItems.length,
  disputeCount = initialDisputes.length,
  submissionCount = initialSubmissions.length,
  page = 1,
  pageSize = 50,
  readOnly,
}: {
  view?: QuarantineView
  initialItems: Array<ScreeningQuarantine>
  initialDisputes?: Array<ScreeningDispute>
  initialSubmissions: Array<ScreeningSubmission>
  quarantineCount?: number
  disputeCount?: number
  submissionCount?: number
  page?: number
  pageSize?: number
  readOnly: boolean
}) {
  const listQuarantines = useServerFn(listScreeningQuarantines)
  const previewBatch = useServerFn(previewScreeningQuarantineDecisions)
  const executeBatch = useServerFn(executeScreeningQuarantineDecisions)
  const getArtifact = useServerFn(getScreeningArtifact)
  const [items, setItems] = useState(initialItems)
  const [filter, setFilter] = useState<QuarantineFilter>('active')
  const [sort, setSort] = useState<QuarantineSort>('oldest')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<ScreeningQuarantine | null>(initialItems[0] ?? null)
  const [resolution, setResolution] = useState<QuarantineResolution>('rescreen')
  const [reason, setReason] = useState('')
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [historyQuery, setHistoryQuery] = useState('')
  const [downloading, setDownloading] = useState<string | null>(null)
  const [batchSelection, setBatchSelection] = useState<Array<string>>([])
  const [batchDrafts, setBatchDrafts] = useState<
    Record<string, { resolution: QuarantineResolution; reason: string }>
  >({})
  const [sharedReason, setSharedReason] = useState('')
  const [pendingDecisions, setPendingDecisions] = useState<
    Array<ScreeningQuarantineBatchDecision>
  >([])
  const [batchPreview, setBatchPreview] = useState<ScreeningQuarantineBatchPreview | null>(
    null,
  )
  const [notice, setNotice] = useState('')

  const screeningHistory = useMemo(() => {
    const normalized = historyQuery.trim().toLowerCase()
    return initialSubmissions.filter((item) => {
      const latest = item.attempts[0]
      return (
        !normalized ||
        [
          item.agent_name,
          submissionLabel(item.agent_version),
          item.agent_id,
          item.miner_hotkey,
          item.agent_status,
          item.screening_reason ?? '',
          latest?.status ?? '',
          latest?.reason ?? '',
          latest?.duplicate_name ?? '',
          submissionLabel(latest?.duplicate_version),
        ]
          .join(' ')
          .toLowerCase()
          .includes(normalized)
      )
    })
  }, [historyQuery, initialSubmissions])

  const downloadArtifact = async (agentId: string) => {
    setDownloading(agentId)
    setError('')
    try {
      const artifact = await getArtifact({ data: { agentId } })
      window.location.assign(artifact.download_url)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to prepare artifact download')
    } finally {
      setDownloading(null)
    }
  }

  const refresh = async (nextFilter = filter, nextSort = sort) => {
    setLoading(true)
    setError('')
    try {
      const result = await listQuarantines({ data: { status: nextFilter, sort: nextSort } })
      setItems(result.items)
      const resultIds = new Set(result.items.map((item) => item.quarantine_id))
      setBatchSelection((current) => current.filter((id) => resultIds.has(id)))
      setSelected((current) =>
        result.items.find((item) => item.quarantine_id === current?.quarantine_id) ??
        result.items[0] ??
        null,
      )
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to load quarantines')
    } finally {
      setLoading(false)
    }
  }

  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    return items.filter(
      (item) =>
        !normalized ||
        [
          item.agent_name,
          submissionLabel(item.agent_version),
          item.agent_id,
          item.miner_hotkey,
          item.reason_code,
          item.artifact_sha256,
        ]
          .join(' ')
          .toLowerCase()
          .includes(normalized),
    )
  }, [items, query])

  const selectedBatchItems = useMemo(
    () => items.filter((item) => batchSelection.includes(item.quarantine_id)),
    [batchSelection, items],
  )

  const toggleBatchItem = (item: ScreeningQuarantine) => {
    if (item.status !== 'active') return
    setBatchSelection((current) =>
      current.includes(item.quarantine_id)
        ? current.filter((id) => id !== item.quarantine_id)
        : [...current, item.quarantine_id],
    )
    setBatchDrafts((current) => ({
      ...current,
      [item.quarantine_id]: current[item.quarantine_id] ?? {
        resolution: 'rescreen',
        reason: '',
      },
    }))
  }

  const buildBatchDecisions = (batchItems: Array<ScreeningQuarantine>) =>
    batchItems.map((item) => {
      const draft = batchDrafts[item.quarantine_id] ?? {
        resolution: 'rescreen' as const,
        reason: '',
      }
      return {
        quarantineId: item.quarantine_id,
        expectedAgentId: item.agent_id,
        expectedArtifactSha256: item.artifact_sha256,
        resolution: draft.resolution,
        reason: draft.reason.trim(),
      }
    })

  const requestPreview = async (decisions: Array<ScreeningQuarantineBatchDecision>) => {
    if (decisions.length === 0 || decisions.some((decision) => decision.reason.length < 3)) return
    setSubmitting(true)
    setError('')
    setNotice('')
    try {
      const preview = await previewBatch({ data: { decisions } })
      setPendingDecisions(decisions)
      setBatchPreview(preview)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to preview these decisions')
    } finally {
      setSubmitting(false)
    }
  }

  const executePreview = async () => {
    if (!batchPreview || pendingDecisions.length === 0) return
    setSubmitting(true)
    setError('')
    try {
      const result = await executeBatch({
        data: {
          decisions: pendingDecisions,
          previewToken: batchPreview.preview_token,
          confirmed: true,
        },
      })
      setNotice(
        `${result.applied_count} applied, ${result.already_applied_count} already recorded, ${result.failed_count} failed.`,
      )
      if (result.failed_count > 0) {
        setError(
          result.items
            .filter((item) => item.status === 'failed')
            .map((item) => `${short(item.quarantine_id, 8)}: ${item.message}`)
            .join('\n'),
        )
      }
      const completed = new Set(
        result.items
          .filter((item) => item.status !== 'failed')
          .map((item) => item.quarantine_id),
      )
      setBatchSelection((current) => current.filter((id) => !completed.has(id)))
      setBatchPreview(null)
      setPendingDecisions([])
      setReason('')
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to execute this preview')
    } finally {
      setSubmitting(false)
    }
  }

  const submitDecision = async () => {
    if (!selected || reason.trim().length < 3) return
    await requestPreview([
      {
        quarantineId: selected.quarantine_id,
        expectedAgentId: selected.agent_id,
        expectedArtifactSha256: selected.artifact_sha256,
        resolution,
        reason: reason.trim(),
      },
    ])
  }

  return (
    <>
      {readOnly ? (
        <div className="mt-6 flex gap-3 rounded-xl border border-[var(--cyan)]/25 bg-[var(--cyan-dim)] px-4 py-3 text-xs text-[var(--cyan)]">
          <ShieldCheck className="h-4 w-4 shrink-0" />
          <p>You have read-only access. Quarantine decisions and artifact downloads are disabled.</p>
        </div>
      ) : null}

      <div
        className="scrollbar-thin mt-6 flex gap-1 overflow-x-auto border-b border-[var(--line)]"
        role="tablist"
        aria-label="Screening quarantine views"
      >
        <Link
          to="/screening-quarantine/disputes"
          search={{ page: 1 }}
          role="tab"
          aria-selected={view === 'disputes'}
          className={`inline-flex min-h-11 shrink-0 items-center gap-2 border-b-2 px-3 text-sm font-medium transition-colors ${
            view === 'disputes'
              ? 'border-[var(--acid)] text-white'
              : 'border-transparent text-[var(--muted)] hover:text-white'
          }`}
        >
          <MessageSquareText className="h-4 w-4" />
          Miner disputes
          <span className="rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] text-[var(--muted-strong)]">
            {disputeCount}
          </span>
        </Link>
        <Link
          to="/screening-quarantine"
          role="tab"
          aria-selected={view === 'queue'}
          className={`inline-flex min-h-11 shrink-0 items-center gap-2 border-b-2 px-3 text-sm font-medium transition-colors ${
            view === 'queue'
              ? 'border-[var(--acid)] text-white'
              : 'border-transparent text-[var(--muted)] hover:text-white'
          }`}
        >
          <ListChecks className="h-4 w-4" />
          Review queue
          <span className="rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] text-[var(--muted-strong)]">
            {quarantineCount}
          </span>
        </Link>
        <Link
          to="/screening-quarantine/history"
          search={{ page: 1 }}
          role="tab"
          aria-selected={view === 'history'}
          className={`inline-flex min-h-11 shrink-0 items-center gap-2 border-b-2 px-3 text-sm font-medium transition-colors ${
            view === 'history'
              ? 'border-[var(--acid)] text-white'
              : 'border-transparent text-[var(--muted)] hover:text-white'
          }`}
        >
          <History className="h-4 w-4" />
          Screening history
          <span className="rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] text-[var(--muted-strong)]">
            {submissionCount}
          </span>
        </Link>
      </div>

      {error ? (
        <div role="alert" className="mt-4 rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] px-4 py-3 text-xs text-[var(--red)]">
          {error}
        </div>
      ) : null}
      {notice ? (
        <div className="mt-4 rounded-lg border border-[var(--acid)]/25 bg-[var(--acid-dim)] px-4 py-3 text-xs text-[var(--acid)]">
          {notice}
        </div>
      ) : null}

      {view === 'queue' ? (
        <>
          <div className="mt-4 flex flex-col gap-3 border-y border-[var(--line)] py-3 md:flex-row md:items-center md:justify-between">
            <label className="relative min-w-0 flex-1 md:max-w-md">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--muted)]" />
              <span className="sr-only">Search screening quarantines</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search agent, miner, reason, or digest"
                className="h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] pl-10 pr-3 text-sm placeholder:text-[var(--muted)] focus:border-[var(--acid)]/60"
              />
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex flex-1 gap-1 rounded-lg bg-[var(--panel)] p-1" aria-label="Filter quarantines">
                {(['active', 'resolved', 'all'] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => {
                      setFilter(value)
                      void refresh(value)
                    }}
                    aria-pressed={filter === value}
                    className={`min-h-11 flex-1 rounded-md px-3 text-xs font-medium capitalize transition-colors ${
                      filter === value
                        ? 'bg-[var(--panel-raised)] text-white'
                        : 'text-[var(--muted)] hover:bg-white/5 hover:text-white'
                    }`}
                  >
                    {value}
                  </button>
                ))}
              </div>
              <label className="relative inline-flex h-11 items-center rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] text-xs font-medium text-[var(--muted-strong)] hover:bg-white/[0.025]">
                <ArrowUpDown className="pointer-events-none absolute left-3 h-3.5 w-3.5" />
                <span className="sr-only">Sort quarantines</span>
                <select
                  aria-label="Sort quarantines"
                  value={sort}
                  onChange={(event) => {
                    const nextSort = event.target.value as QuarantineSort
                    setSort(nextSort)
                    void refresh(filter, nextSort)
                  }}
                  className="h-full appearance-none rounded-lg bg-transparent pl-9 pr-8"
                >
                  <option value="oldest">Oldest first</option>
                  <option value="newest">Newest first</option>
                </select>
                <span aria-hidden="true" className="pointer-events-none absolute right-3 text-[10px] text-[var(--muted)]">▾</span>
              </label>
              <button
                type="button"
                onClick={() => void refresh()}
                disabled={loading}
                className="inline-flex h-11 items-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
                <span className="hidden sm:inline">Refresh</span>
              </button>
            </div>
          </div>

          {batchSelection.length > 0 ? (
            <section
              className="mt-4 overflow-hidden rounded-xl border border-[var(--line-strong)] bg-[var(--panel)]"
              aria-labelledby="batch-workbench-heading"
            >
              <div className="flex flex-col gap-3 border-b border-[var(--line)] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <h2 id="batch-workbench-heading" className="text-sm font-semibold">
                    Batch decision workbench · {selectedBatchItems.length} selected
                  </h2>
                  <p className="mt-1 text-[11px] text-[var(--muted)]">
                    Review each evidence summary, action, and miner-visible reason before previewing.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setBatchSelection([])}
                  className="min-h-10 rounded-lg border border-[var(--line)] px-3 text-xs text-[var(--muted-strong)] hover:bg-white/5"
                >
                  Clear selection
                </button>
              </div>
              <div className="scrollbar-thin max-h-[34rem] overflow-auto">
                <table className="w-full text-left text-xs">
                  <thead className="hidden bg-white/[0.025] text-[10px] text-[var(--muted)] sm:table-header-group">
                    <tr>
                      <th className="px-4 py-2 font-medium">Submission and evidence</th>
                      <th className="px-4 py-2 font-medium">Decision</th>
                      <th className="px-4 py-2 font-medium">Miner-visible reason</th>
                    </tr>
                  </thead>
                  <tbody className="block divide-y divide-[var(--line)] sm:table-row-group">
                    {selectedBatchItems.map((item) => {
                      const draft = batchDrafts[item.quarantine_id] ?? {
                        resolution: 'rescreen' as const,
                        reason: '',
                      }
                      return (
                        <tr key={item.quarantine_id} className="grid gap-3 p-4 sm:table-row sm:p-0">
                          <td className="block max-w-sm align-top sm:table-cell sm:px-4 sm:py-3">
                            <p className="font-medium text-white">{item.agent_name}</p>
                            <p className="mt-1 text-[10px] text-[var(--muted)]">
                              {item.reason_code.replaceAll('_', ' ')} · policy v{item.policy_version}
                            </p>
                            <p className="mt-1 line-clamp-2 leading-4 text-[var(--muted-strong)]">
                              {item.finding?.summary ?? 'No source-review summary was recorded.'}
                            </p>
                          </td>
                          <td className="block w-full align-top sm:table-cell sm:w-44 sm:px-4 sm:py-3">
                            <select
                              aria-label={`Decision for ${item.agent_name}`}
                              value={draft.resolution}
                              onChange={(event) =>
                                setBatchDrafts((current) => ({
                                  ...current,
                                  [item.quarantine_id]: {
                                    ...draft,
                                    resolution: event.target.value as QuarantineResolution,
                                  },
                                }))
                              }
                              className="h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-2 text-xs"
                            >
                              <option value="release">Release</option>
                              <option value="rescreen">Rescreen</option>
                              <option value="reject">Reject</option>
                            </select>
                          </td>
                          <td className="block min-w-0 align-top sm:table-cell sm:min-w-80 sm:px-4 sm:py-3">
                            <textarea
                              aria-label={`Reason for ${item.agent_name}`}
                              value={draft.reason}
                              onChange={(event) =>
                                setBatchDrafts((current) => ({
                                  ...current,
                                  [item.quarantine_id]: {
                                    ...draft,
                                    reason: event.target.value,
                                  },
                                }))
                              }
                              rows={2}
                              placeholder="Explain this submission's decision"
                              className="w-full resize-y rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 py-2 leading-5 placeholder:text-[var(--muted)]"
                            />
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              <div className="flex flex-col gap-3 border-t border-[var(--line)] p-4 lg:flex-row lg:items-end">
                <label className="min-w-0 flex-1 text-[11px] text-[var(--muted-strong)]">
                  Shared reason
                  <textarea
                    aria-label="Shared batch reason"
                    value={sharedReason}
                    onChange={(event) => setSharedReason(event.target.value)}
                    rows={2}
                    placeholder="Apply one reason to every selected row, then adjust exceptions above"
                    className="mt-1 w-full resize-y rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 py-2 text-xs leading-5 placeholder:text-[var(--muted)]"
                  />
                </label>
                <button
                  type="button"
                  onClick={() =>
                    setBatchDrafts((current) => ({
                      ...current,
                      ...Object.fromEntries(
                        selectedBatchItems.map((item) => [
                          item.quarantine_id,
                          {
                            resolution:
                              current[item.quarantine_id]?.resolution ?? 'rescreen',
                            reason: sharedReason,
                          },
                        ]),
                      ),
                    }))
                  }
                  disabled={sharedReason.trim().length < 3}
                  className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
                >
                  Apply reason to selected
                </button>
                <button
                  type="button"
                  onClick={() => void requestPreview(buildBatchDecisions(selectedBatchItems))}
                  disabled={
                    submitting ||
                    selectedBatchItems.length === 0 ||
                    buildBatchDecisions(selectedBatchItems).some(
                      (decision) => decision.reason.length < 3,
                    )
                  }
                  className="min-h-11 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-[#11150d] hover:bg-[var(--acid-hover)] disabled:opacity-40"
                >
                  Preview {selectedBatchItems.length} decision
                  {selectedBatchItems.length === 1 ? '' : 's'}
                </button>
              </div>
            </section>
          ) : null}

          <section
            className="mt-4 grid overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)] lg:grid-cols-[minmax(18rem,0.72fr)_minmax(0,1.28fr)]"
            aria-label="Screening quarantine review workspace"
          >
            <div className="min-w-0 border-b border-[var(--line)] lg:border-b-0 lg:border-r">
              <div className="flex items-center justify-between border-b border-[var(--line)] px-4 py-3">
                <div>
                  <h2 className="text-sm font-semibold">Submissions</h2>
                  <p className="mt-1 text-[11px] text-[var(--muted)]">
                    Select one to inspect and decide.
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  {filter === 'active' && visible.length > 0 && !readOnly ? (
                    <label className="flex items-center gap-2 text-[10px] text-[var(--muted-strong)]">
                      <input
                        type="checkbox"
                        aria-label="Select all shown quarantines"
                        checked={visible.every((item) =>
                          batchSelection.includes(item.quarantine_id),
                        )}
                        onChange={() => {
                          const allSelected = visible.every((item) =>
                            batchSelection.includes(item.quarantine_id),
                          )
                          if (allSelected) {
                            const visibleIds = new Set(
                              visible.map((item) => item.quarantine_id),
                            )
                            setBatchSelection((current) =>
                              current.filter((id) => !visibleIds.has(id)),
                            )
                          } else {
                            setBatchSelection((current) => [
                              ...new Set([
                                ...current,
                                ...visible
                                  .filter((item) => item.status === 'active')
                                  .map((item) => item.quarantine_id),
                              ]),
                            ])
                            setBatchDrafts((current) => ({
                              ...current,
                              ...Object.fromEntries(
                                visible
                                  .filter((item) => item.status === 'active')
                                  .map((item) => [
                                    item.quarantine_id,
                                    current[item.quarantine_id] ?? {
                                      resolution: 'rescreen',
                                      reason: '',
                                    },
                                  ]),
                              ),
                            }))
                          }
                        }}
                      />
                      Select shown
                    </label>
                  ) : null}
                  <span className="text-xs text-[var(--muted-strong)]">
                    {visible.length} shown
                  </span>
                </div>
              </div>
              <div className="scrollbar-thin max-h-[28rem] min-h-72 overflow-y-auto lg:max-h-[calc(100vh-20rem)]">
                {loading && items.length === 0 ? (
                  <div className="space-y-3 p-4" aria-label="Loading quarantines">
                    {[0, 1, 2].map((item) => (
                      <div key={item} className="h-20 animate-pulse rounded-lg bg-white/[0.04]" />
                    ))}
                  </div>
                ) : visible.length === 0 ? (
                  <div className="px-6 py-14 text-center">
                    <CheckCircle2 className="mx-auto h-7 w-7 text-[var(--acid)]" />
                    <p className="mt-3 text-sm font-medium">
                      {filter === 'active' ? 'The quarantine queue is clear' : 'No quarantines match'}
                    </p>
                    <p className="mt-1 text-xs text-[var(--muted)]">
                      {filter === 'active'
                        ? 'No submission currently needs a decision.'
                        : 'Try another filter or search.'}
                    </p>
                  </div>
                ) : (
                  <div className="divide-y divide-[var(--line)]">
                    {visible.map((item) => {
                      const isSelected = selected?.quarantine_id === item.quarantine_id
                      return (
                        <div
                          key={item.quarantine_id}
                          className={`flex items-start transition-colors ${
                            isSelected ? 'bg-[var(--acid-dim)] text-white' : 'hover:bg-white/[0.035]'
                          }`}
                        >
                          {item.status === 'active' && !readOnly ? (
                            <label className="flex min-h-11 shrink-0 items-center px-3 pt-2">
                              <span className="sr-only">Select {item.agent_name} for batch review</span>
                              <input
                                type="checkbox"
                                aria-label={`Select ${item.agent_name} for batch review`}
                                checked={batchSelection.includes(item.quarantine_id)}
                                onChange={() => toggleBatchItem(item)}
                              />
                            </label>
                          ) : null}
                          <button
                            type="button"
                            aria-pressed={isSelected}
                            onClick={() => {
                              setSelected(item)
                              setResolution('rescreen')
                              setReason('')
                            }}
                            className="min-w-0 flex-1 px-4 py-3 text-left"
                          >
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <p className="truncate text-sm font-semibold">{item.agent_name}</p>
                              <SubmissionBadge version={item.agent_version} />
                              <p className="mt-1 truncate font-mono text-[10px] text-[var(--muted)]">
                                {item.agent_id}
                              </p>
                            </div>
                            <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${item.status === 'active' ? 'bg-[var(--amber-dim)] text-[var(--amber)]' : 'bg-white/[0.06] text-[var(--muted-strong)]'}`}>
                              {item.status === 'active' ? 'Needs decision' : item.resolution ?? 'Resolved'}
                            </span>
                          </div>
                          <div className="mt-3 flex flex-wrap items-center gap-2">
                            <p className="text-xs capitalize text-[var(--muted-strong)]">
                              {item.reason_code.replaceAll('_', ' ')}
                            </p>
                            {item.finding && item.finding_verified ? (
                              <span
                                className={`rounded-full px-2 py-0.5 text-[10px] font-medium uppercase ${
                                  item.finding.risk_level === 'high'
                                    ? 'bg-[var(--red-dim)] text-[var(--red)]'
                                    : item.finding.risk_level === 'medium'
                                      ? 'bg-[var(--amber-dim)] text-[var(--amber)]'
                                      : 'bg-[var(--acid-dim)] text-[var(--acid)]'
                                }`}
                              >
                                {item.finding.risk_level}
                              </span>
                            ) : null}
                          </div>
                          <div className="mt-2 flex items-center justify-between gap-3 text-[10px] text-[var(--muted)]">
                            <span>Policy v{item.policy_version}</span>
                            <span>{formatDate(item.created_at)}</span>
                          </div>
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>

            <div className="min-w-0 bg-[var(--panel-soft)] p-4 sm:p-5">
              {selected ? (
                <>
                  <div className="flex flex-col gap-4 border-b border-[var(--line)] pb-5 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className="text-lg font-semibold tracking-[-0.02em]">{selected.agent_name}</h2>
                        <SubmissionBadge version={selected.agent_version} />
                        <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${selected.status === 'active' ? 'bg-[var(--amber-dim)] text-[var(--amber)]' : 'bg-white/[0.06] text-[var(--muted-strong)]'}`}>
                          {selected.status === 'active' ? 'Awaiting review' : selected.resolution ?? 'Resolved'}
                        </span>
                      </div>
                      <p className="mt-2 break-all font-mono text-[10px] text-[var(--muted)]">
                        {selected.agent_id}
                      </p>
                      <p className="mt-2 text-xs text-[var(--muted-strong)]">
                        Received {formatDate(selected.created_at)} · Policy v{selected.policy_version}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => void downloadArtifact(selected.agent_id)}
                      disabled={readOnly || downloading === selected.agent_id}
                      title={readOnly ? 'Artifact downloads require write access' : undefined}
                      className="inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-[#11150d] hover:bg-[var(--acid-hover)] disabled:opacity-40"
                    >
                      {downloading === selected.agent_id ? (
                        <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Download className="h-3.5 w-3.5" />
                      )}
                      Download source
                    </button>
                  </div>

                  <div className="border-b border-[var(--line)]">
                    <QuarantineEvidencePanel
                      key={selected.quarantine_id}
                      quarantine={selected}
                      readOnly={readOnly}
                    />
                    <dl className="grid gap-4 pb-5 text-xs sm:grid-cols-2 xl:grid-cols-3">
                      <div>
                        <dt className="text-[var(--muted)]">Miner hotkey</dt>
                        <dd className="mt-1 break-all font-mono text-[var(--muted-strong)]">{selected.miner_hotkey}</dd>
                      </div>
                      <div>
                        {/* Wording matters here: a reviewer must not read this
                            as an ownership determination. Miners routinely pay
                            from several coldkeys, so a shared coldkey is one
                            signal and different coldkeys prove nothing. */}
                        <dt
                          className="text-[var(--muted)]"
                          title="Coldkey that paid for this evaluation. Payment provenance, not on-chain ownership: miners routinely pay from several coldkeys, so a match is one signal to follow and a mismatch is not evidence of a different operator."
                        >
                          Payment coldkey
                        </dt>
                        <dd className="mt-1 break-all font-mono text-[var(--muted-strong)]">
                          {selected.miner_coldkey ?? 'No payment record'}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-[var(--muted)]">Artifact SHA-256</dt>
                        <dd className="mt-1 font-mono text-[var(--muted-strong)]" title={selected.artifact_sha256}>{short(selected.artifact_sha256, 20)}</dd>
                      </div>
                      <div>
                        <dt className="text-[var(--muted)]">Manifest digest</dt>
                        <dd className="mt-1 font-mono text-[var(--muted-strong)]" title={selected.manifest_digest}>{short(selected.manifest_digest, 20)}</dd>
                      </div>
                      <div>
                        <dt className="text-[var(--muted)]">Finding digest</dt>
                        <dd className="mt-1 font-mono text-[var(--muted-strong)]" title={selected.finding_digest ?? ''}>{selected.finding_digest ? short(selected.finding_digest, 20) : 'Not recorded'}</dd>
                      </div>
                      <LinkedIdentity
                        key={selected.miner_hotkey}
                        hotkey={selected.miner_hotkey}
                      />
                    </dl>
                  </div>

                  {selected.status === 'resolved' ? (
                    <div className="pt-5 text-xs leading-5 text-[var(--muted-strong)]">
                      <p>
                        <span className="font-medium text-white">{selected.resolved_by ?? 'Unknown operator'}</span>{' '}
                        chose <span className="font-medium text-white">{selected.resolution ?? 'a resolution'}</span> on {formatDate(selected.resolved_at)}.
                      </p>
                      {selected.resolution_reason ? <p className="mt-2 text-[var(--muted)]">{selected.resolution_reason}</p> : null}
                    </div>
                  ) : (
                    <div className="pt-6">
                      <fieldset disabled={readOnly || submitting}>
                        <legend className="text-xs font-medium">Decision</legend>
                        <div className="mt-3 grid gap-2 sm:grid-cols-3">
                          {(Object.keys(resolutionCopy) as Array<QuarantineResolution>).map((value) => {
                            const copy = resolutionCopy[value]
                            const Icon = copy.icon
                            return (
                              <label key={value} className={`cursor-pointer rounded-lg border p-3 transition-colors ${resolution === value ? 'border-[var(--line-strong)] bg-white/[0.05]' : 'border-[var(--line)] hover:bg-white/[0.025]'}`}>
                                <input type="radio" name="resolution" value={value} checked={resolution === value} onChange={() => setResolution(value)} className="sr-only" />
                                <Icon className={`h-4 w-4 ${copy.tone}`} />
                                <span className="mt-2 block text-xs font-medium">{copy.label}</span>
                                <span className="mt-1 block text-[10px] leading-4 text-[var(--muted)]">{copy.description}</span>
                              </label>
                            )
                          })}
                        </div>
                      </fieldset>
                      <div className="mt-6">
                        <label className="text-xs font-medium" htmlFor={`reason-${selected.quarantine_id}`}>Miner-visible reason</label>
                        <p className="mt-1 text-[10px] leading-4 text-[var(--muted)]">
                          Explain why this decision was made and what to change, if anything. Do not include private evidence or secrets.
                        </p>
                        <textarea
                          id={`reason-${selected.quarantine_id}`}
                          value={reason}
                          onChange={(event) => setReason(event.target.value)}
                          disabled={readOnly || submitting}
                          rows={3}
                          placeholder="Explain the decision and any corrective action"
                          className="mt-2 w-full resize-y rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs leading-5 placeholder:text-[var(--muted)] focus:border-[var(--acid)]/60"
                        />
                        <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                          <span className="text-[10px] text-[var(--muted)]">{reason.trim().length} characters · minimum 3</span>
                          <button
                            type="button"
                            onClick={() => void submitDecision()}
                            disabled={readOnly || submitting || reason.trim().length < 3}
                            className={`inline-flex min-h-11 items-center justify-center gap-2 rounded-lg px-4 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-40 ${resolution === 'reject' ? 'bg-[var(--red)] text-white' : resolution === 'rescreen' ? 'bg-[var(--amber)] text-[#1c1407]' : 'bg-[var(--acid)] text-[#11150d]'}`}
                          >
                            {submitting ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <AlertTriangle className="h-3.5 w-3.5" />}
                            Confirm {resolution}
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                </>
              ) : (
                <div className="grid min-h-72 place-items-center px-6 text-center">
                  <div>
                    <ListChecks className="mx-auto h-7 w-7 text-[var(--muted)]" />
                    <p className="mt-3 text-sm font-medium">Select a submission</p>
                    <p className="mt-1 text-xs text-[var(--muted)]">
                      Its evidence, source download, and decision controls will appear here.
                    </p>
                  </div>
                </div>
              )}
            </div>
          </section>
        </>
      ) : view === 'disputes' ? (
        <>
          <ScreeningDisputeQueue
            initialDisputes={initialDisputes}
            page={page}
            pageSize={pageSize}
            readOnly={readOnly}
            downloadArtifact={downloadArtifact}
            downloading={downloading}
            reportError={setError}
          />
          <ScreeningPagination
            view="disputes"
            page={page}
            pageSize={pageSize}
            total={disputeCount}
          />
        </>
      ) : (
        <>
          <div className="mt-4 flex flex-col gap-3 border-y border-[var(--line)] py-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-sm font-semibold">All screening outcomes</h2>
              <p className="mt-1 text-xs text-[var(--muted)]">
                Build failures, rejections, passes, and previous quarantine attempts.
              </p>
            </div>
            <label className="relative w-full sm:max-w-sm">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--muted)]" />
              <span className="sr-only">Search screening history</span>
              <input
                value={historyQuery}
                onChange={(event) => setHistoryQuery(event.target.value)}
                placeholder="Search all screening outcomes"
                className="h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] pl-10 pr-3 text-sm placeholder:text-[var(--muted)] focus:border-[var(--acid)]/60"
              />
            </label>
          </div>
          <section className="mt-4 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]" aria-labelledby="screening-history-heading">
            <h2 id="screening-history-heading" className="sr-only">Screening history</h2>
            {screeningHistory.length === 0 ? (
              <div className="px-6 py-14 text-center">
                <Search className="mx-auto h-7 w-7 text-[var(--muted)]" />
                <p className="mt-3 text-sm font-medium">No screening outcomes match</p>
                <p className="mt-1 text-xs text-[var(--muted)]">Clear the search to see all history.</p>
              </div>
            ) : (
              <div className="scrollbar-thin max-h-[70vh] divide-y divide-[var(--line)] overflow-y-auto">
                {screeningHistory.map((item) => {
                  const latest = item.attempts[0]
                  const outcome = latest?.status ?? item.agent_status
                  const historyReason = latest?.reason ?? item.screening_reason ?? 'No screening detail recorded'
                  return (
                    <article
                      key={item.agent_id}
                      className="grid gap-3 px-4 py-3 md:grid-cols-[minmax(14rem,1fr)_minmax(11rem,0.7fr)_minmax(16rem,1.2fr)_auto] md:items-center"
                    >
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium">{item.agent_name}</p>
                        <div className="mt-1">
                          <SubmissionBadge version={item.agent_version} />
                        </div>
                        <p className="mt-1 truncate font-mono text-[10px] text-[var(--muted)]">{item.agent_id}</p>
                      </div>
                      <div>
                        <span className="rounded-full bg-white/[0.06] px-2 py-1 text-[10px] font-medium capitalize text-[var(--muted-strong)]">
                          {outcome.replaceAll('_', ' ')}
                        </span>
                        <p className="mt-2 text-[10px] text-[var(--muted)]">
                          Policy v{latest?.policy_version ?? item.screening_policy_version} · {item.attempts.length} attempt{item.attempts.length === 1 ? '' : 's'}
                        </p>
                      </div>
                      <div className="text-xs leading-5 text-[var(--muted-strong)]">
                        <p>{historyReason}</p>
                        {latest?.duplicate_of ? (
                          <p className="mt-1 text-[var(--amber)]">
                            Compared with{' '}
                            <span className="font-medium text-white">
                              {latest.duplicate_name ?? short(latest.duplicate_of)}
                            </span>{' '}
                            · {submissionLabel(latest.duplicate_version)}
                          </p>
                        ) : null}
                      </div>
                      <button
                        type="button"
                        onClick={() => void downloadArtifact(item.agent_id)}
                        disabled={readOnly || downloading === item.agent_id}
                        title={readOnly ? 'Artifact downloads require write access' : undefined}
                        className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
                      >
                        {downloading === item.agent_id ? (
                          <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Download className="h-3.5 w-3.5" />
                        )}
                        Download source
                      </button>
                    </article>
                  )
                })}
              </div>
            )}
          </section>
          <ScreeningPagination
            view="history"
            page={page}
            pageSize={pageSize}
            total={submissionCount}
          />
        </>
      )}
      {batchPreview ? (
        <Modal
          title={`Confirm ${pendingDecisions.length} review decision${pendingDecisions.length === 1 ? '' : 's'}`}
          description="Preview only — no review state has changed. Confirm the exact per-item effects below."
          onClose={() => {
            if (!submitting) {
              setBatchPreview(null)
              setPendingDecisions([])
            }
          }}
        >
          <div className="flex flex-wrap gap-2 text-[11px]">
            <span className="rounded-full bg-[var(--acid-dim)] px-2 py-1 text-[var(--acid)]">
              {batchPreview.ready_count} ready
            </span>
            <span className="rounded-full bg-white/[0.06] px-2 py-1 text-[var(--muted-strong)]">
              {batchPreview.already_applied_count} already recorded
            </span>
            {batchPreview.blocked_count > 0 ? (
              <span className="rounded-full bg-[var(--red-dim)] px-2 py-1 text-[var(--red)]">
                {batchPreview.blocked_count} blocked
              </span>
            ) : null}
          </div>
          <div className="mt-4 max-h-80 divide-y divide-[var(--line)] overflow-y-auto rounded-lg border border-[var(--line)]">
            {batchPreview.items.map((item) => (
              <div key={item.quarantine_id} className="px-3 py-3 text-xs">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate font-medium text-white">
                      {item.agent_name ?? short(item.quarantine_id)}
                    </p>
                    <p className="mt-1 capitalize text-[var(--muted-strong)]">
                      {item.resolution} · {item.resulting_agent_status ?? item.disposition}
                    </p>
                  </div>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${
                      item.disposition === 'ready'
                        ? 'bg-[var(--acid-dim)] text-[var(--acid)]'
                        : item.disposition === 'already_applied'
                          ? 'bg-white/[0.06] text-[var(--muted-strong)]'
                          : 'bg-[var(--red-dim)] text-[var(--red)]'
                    }`}
                  >
                    {item.disposition.replaceAll('_', ' ')}
                  </span>
                </div>
                <p className="mt-2 leading-5 text-[var(--muted)]">{item.reason}</p>
                <p className="mt-1 text-[10px] text-[var(--muted)]">{item.message}</p>
              </div>
            ))}
          </div>
          <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={() => {
                setBatchPreview(null)
                setPendingDecisions([])
              }}
              disabled={submitting}
              className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
            >
              Back to review
            </button>
            <button
              type="button"
              onClick={() => void executePreview()}
              disabled={
                submitting ||
                batchPreview.ready_count + batchPreview.already_applied_count === 0
              }
              className="min-h-11 rounded-lg bg-[var(--red)] px-4 text-xs font-semibold text-white hover:brightness-110 disabled:opacity-40"
            >
              {submitting ? 'Executing…' : 'Confirm and execute reviewed decisions'}
            </button>
          </div>
        </Modal>
      ) : null}
    </>
  )
}

function ScreeningPagination({
  view,
  page,
  pageSize,
  total,
}: {
  view: 'disputes' | 'history'
  page: number
  pageSize: number
  total: number
}) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  if (pageCount <= 1) return null

  const first = (page - 1) * pageSize + 1
  const last = Math.min(page * pageSize, total)
  const to = view === 'history' ? '/screening-quarantine/history' : '/screening-quarantine/disputes'

  return (
    <nav
      className="mt-4 flex flex-col gap-3 border-y border-[var(--line)] py-3 sm:flex-row sm:items-center sm:justify-between"
      aria-label={`${view === 'history' ? 'Screening history' : 'Miner disputes'} pagination`}
    >
      <p className="text-xs text-[var(--muted-strong)]">
        Showing {first.toLocaleString()}–{last.toLocaleString()} of {total.toLocaleString()}
      </p>
      <div className="flex items-center gap-2">
        <Link
          to={to}
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
          to={to}
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

function ScreeningDisputeQueue({
  initialDisputes,
  page,
  pageSize,
  readOnly,
  downloadArtifact,
  downloading,
  reportError,
}: {
  initialDisputes: Array<ScreeningDispute>
  page: number
  pageSize: number
  readOnly: boolean
  downloadArtifact: (agentId: string) => Promise<void>
  downloading: string | null
  reportError: (message: string) => void
}) {
  const listDisputes = useServerFn(listScreeningDisputes)
  const resolveDispute = useServerFn(decideScreeningDispute)
  const [items, setItems] = useState(initialDisputes)
  const [selected, setSelected] = useState<ScreeningDispute | null>(initialDisputes[0] ?? null)
  const [query, setQuery] = useState('')
  const [resolution, setResolution] = useState<ScreeningDisputeResolution | null>(null)
  const [reason, setReason] = useState('')
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    return items.filter(
      (item) =>
        !normalized ||
        [
          item.agent_name,
          item.agent_id,
          item.miner_hotkey,
          item.message,
          item.original_reason ?? '',
        ]
          .join(' ')
          .toLowerCase()
          .includes(normalized),
    )
  }, [items, query])

  const refresh = async () => {
    setLoading(true)
    reportError('')
    try {
      const result = await listDisputes({
        data: { status: 'pending', limit: pageSize, offset: (page - 1) * pageSize },
      })
      setItems(result.items)
      setSelected((current) =>
        result.items.find((item) => item.dispute_id === current?.dispute_id) ??
        result.items[0] ??
        null,
      )
    } catch (cause) {
      reportError(cause instanceof Error ? cause.message : 'Unable to load disputes')
    } finally {
      setLoading(false)
    }
  }

  const submitDecision = async () => {
    if (!selected || !resolution || reason.trim().length < 3) return
    setSubmitting(true)
    reportError('')
    try {
      await resolveDispute({
        data: {
          disputeId: selected.dispute_id,
          resolution,
          reason: reason.trim(),
        },
      })
      setResolution(null)
      setReason('')
      await refresh()
    } catch (cause) {
      reportError(cause instanceof Error ? cause.message : 'Unable to resolve this dispute')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <div className="mt-4 flex flex-col gap-3 border-y border-[var(--line)] py-3 md:flex-row md:items-center md:justify-between">
        <label className="relative min-w-0 flex-1 md:max-w-md">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--muted)]" />
          <span className="sr-only">Search miner disputes</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search agent, miner, or dispute text"
            className="h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] pl-10 pr-3 text-sm placeholder:text-[var(--muted)] focus:border-[var(--acid)]/60"
          />
        </label>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex h-11 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh disputes
        </button>
      </div>

      <section
        className="mt-4 grid overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)] lg:grid-cols-[minmax(18rem,0.72fr)_minmax(0,1.28fr)]"
        aria-label="Miner dispute review workspace"
      >
        <div className="min-w-0 border-b border-[var(--line)] lg:border-b-0 lg:border-r">
          <div className="flex items-center justify-between border-b border-[var(--line)] px-4 py-3">
            <div>
              <h2 className="text-sm font-semibold">Pending disputes</h2>
              <p className="mt-1 text-[11px] text-[var(--muted)]">One final appeal per submission.</p>
            </div>
            <span className="text-xs text-[var(--muted-strong)]">{visible.length} shown</span>
          </div>
          <div className="scrollbar-thin max-h-[28rem] min-h-72 overflow-y-auto lg:max-h-[calc(100vh-20rem)]">
            {visible.length === 0 ? (
              <div className="px-6 py-14 text-center">
                <CheckCircle2 className="mx-auto h-7 w-7 text-[var(--acid)]" />
                <p className="mt-3 text-sm font-medium">The dispute queue is clear</p>
                <p className="mt-1 text-xs text-[var(--muted)]">No miner appeal currently needs a decision.</p>
              </div>
            ) : (
              <div className="divide-y divide-[var(--line)]">
                {visible.map((item) => {
                  const isSelected = selected?.dispute_id === item.dispute_id
                  return (
                    <button
                      key={item.dispute_id}
                      type="button"
                      aria-pressed={isSelected}
                      onClick={() => {
                        setSelected(item)
                        setResolution(null)
                        setReason('')
                      }}
                      className={`w-full px-4 py-3 text-left transition-colors ${
                        isSelected ? 'bg-[var(--acid-dim)] text-white' : 'hover:bg-white/[0.035]'
                      }`}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-semibold">{item.agent_name}</p>
                          <div className="mt-1"><SubmissionBadge version={item.agent_version} /></div>
                          <p className="mt-1 truncate font-mono text-[10px] text-[var(--muted)]">{item.agent_id}</p>
                        </div>
                        <span className="shrink-0 rounded-full bg-[var(--amber-dim)] px-2 py-0.5 text-[10px] font-medium text-[var(--amber)]">
                          Needs review
                        </span>
                      </div>
                      <p className="mt-3 line-clamp-2 text-xs leading-5 text-[var(--muted-strong)]">{item.message}</p>
                      <p className="mt-2 text-[10px] text-[var(--muted)]">Submitted {formatDate(item.created_at)}</p>
                    </button>
                  )
                })}
              </div>
            )}
          </div>
        </div>

        <div className="min-w-0 bg-[var(--panel-soft)] p-4 sm:p-5">
          {selected ? (
            <>
              <div className="flex flex-col gap-4 border-b border-[var(--line)] pb-5 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-lg font-semibold tracking-[-0.02em]">{selected.agent_name}</h2>
                    <SubmissionBadge version={selected.agent_version} />
                    <span className="rounded-full bg-[var(--amber-dim)] px-2 py-0.5 text-[10px] font-medium text-[var(--amber)]">Disputed</span>
                  </div>
                  <p className="mt-2 break-all font-mono text-[10px] text-[var(--muted)]">{selected.agent_id}</p>
                  <p className="mt-2 text-xs text-[var(--muted-strong)]">Submitted {formatDate(selected.created_at)}</p>
                </div>
                <button
                  type="button"
                  onClick={() => void downloadArtifact(selected.agent_id)}
                  disabled={readOnly || downloading === selected.agent_id}
                  title={readOnly ? 'Artifact downloads require write access' : undefined}
                  className="inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-[#11150d] hover:bg-[var(--acid-hover)] disabled:opacity-40"
                >
                  {downloading === selected.agent_id ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                  Download source
                </button>
              </div>

              <div className="grid gap-5 border-b border-[var(--line)] py-5 xl:grid-cols-2">
                <div>
                  <p className="text-xs font-medium text-white">Miner dispute</p>
                  <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-[var(--muted-strong)]">{selected.message}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-white">Original rejection reason</p>
                  <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-[var(--muted-strong)]">{selected.original_reason ?? 'No operator reason was recorded.'}</p>
                </div>
              </div>

              <dl className="grid gap-4 border-b border-[var(--line)] py-5 text-xs sm:grid-cols-2">
                <div>
                  <dt className="text-[var(--muted)]">Miner hotkey</dt>
                  <dd className="mt-1 break-all font-mono text-[var(--muted-strong)]">{selected.miner_hotkey}</dd>
                </div>
                <div>
                  <dt className="text-[var(--muted)]">Artifact SHA-256</dt>
                  <dd className="mt-1 break-all font-mono text-[var(--muted-strong)]">{selected.artifact_sha256}</dd>
                </div>
              </dl>

              <div className="pt-6">
                <fieldset disabled={readOnly || submitting}>
                  <legend className="text-xs font-medium">Dispute decision</legend>
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    {(['release', 'uphold'] as const).map((value) => (
                      <label key={value} className={`cursor-pointer rounded-lg border p-3 transition-colors ${resolution === value ? 'border-[var(--line-strong)] bg-white/[0.05]' : 'border-[var(--line)] hover:bg-white/[0.025]'}`}>
                        <input type="radio" name="dispute-resolution" value={value} checked={resolution === value} onChange={() => setResolution(value)} className="sr-only" />
                        {value === 'release' ? <ShieldCheck className="h-4 w-4 text-[var(--acid)]" /> : <XCircle className="h-4 w-4 text-[var(--red)]" />}
                        <span className="mt-2 block text-xs font-medium">{value === 'release' ? 'Accept and release' : 'Uphold rejection'}</span>
                        <span className="mt-1 block text-[10px] leading-4 text-[var(--muted)]">{value === 'release' ? 'Return the submission to validator evaluation.' : 'Keep the submission rejected after final review.'}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
                <label className="mt-6 block text-xs font-medium" htmlFor={`dispute-reason-${selected.dispute_id}`}>Miner-visible response</label>
                <p className="mt-1 text-[10px] leading-4 text-[var(--muted)]">State what the review found. Do not include private evidence or secrets.</p>
                <textarea
                  id={`dispute-reason-${selected.dispute_id}`}
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  disabled={readOnly || submitting}
                  rows={3}
                  placeholder="Explain why the dispute was accepted or upheld"
                  className="mt-2 w-full resize-y rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs leading-5 placeholder:text-[var(--muted)] focus:border-[var(--acid)]/60"
                />
                <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <span className="text-[10px] text-[var(--muted)]">{reason.trim().length} characters · minimum 3</span>
                  <button
                    type="button"
                    onClick={() => void submitDecision()}
                    disabled={readOnly || submitting || !resolution || reason.trim().length < 3}
                    className={`inline-flex min-h-11 items-center justify-center gap-2 rounded-lg px-4 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-40 ${resolution === 'uphold' ? 'bg-[var(--red)] text-white' : 'bg-[var(--acid)] text-[#11150d]'}`}
                  >
                    {submitting ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <AlertTriangle className="h-3.5 w-3.5" />}
                    Confirm {resolution ?? 'decision'}
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div className="grid min-h-72 place-items-center px-6 text-center">
              <div>
                <MessageSquareText className="mx-auto h-7 w-7 text-[var(--muted)]" />
                <p className="mt-3 text-sm font-medium">Select a dispute</p>
                <p className="mt-1 text-xs text-[var(--muted)]">The miner's message, original reason, source, and decision controls will appear here.</p>
              </div>
            </div>
          )}
        </div>
      </section>
    </>
  )
}
