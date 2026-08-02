import { useServerFn } from '@tanstack/react-start'
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Copy,
  Eye,
  FileCode2,
  FileWarning,
  Fingerprint,
  History,
  Info,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Users,
} from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import type {
  ScreeningQuarantine,
  ScreeningQuarantineContext,
  ShadowReviewObservation,
  SourceExcerpt,
  SourceListing,
} from '../lib/admin.schemas'
import {
  getScreeningQuarantineContext,
  listScreeningSourceFiles,
  readScreeningSourceFile,
} from '../server/admin.functions'
import { sanitizeSourceLine } from '../lib/source-text'
import { QuarantineBaselineDiff } from './QuarantineBaselineDiff'

const riskTone: Record<'low' | 'medium' | 'high', string> = {
  low: 'bg-[var(--acid-dim)] text-[var(--acid)]',
  medium: 'bg-[var(--amber-dim)] text-[var(--amber)]',
  high: 'bg-[var(--red-dim)] text-[var(--red)]',
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
  }).format(date)
}

function formatBytes(value: number | null) {
  if (value === null) return 'Unknown size'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`
}

function humanize(code: string) {
  return code.replaceAll('_', ' ').replaceAll('-', ' ')
}

type ExcerptRequest = { path: string; line: number | null }

const COLLAPSIBLE_SUMMARY_LENGTH = 180

function CollapsibleSummary({
  text,
  className,
}: {
  text: string
  className: string
}) {
  const [expanded, setExpanded] = useState(false)
  const contentId = useId()
  const isLong = text.length > COLLAPSIBLE_SUMMARY_LENGTH

  return (
    <div>
      <p
        id={contentId}
        className={`${className} ${isLong && !expanded ? 'line-clamp-2' : ''}`}
      >
        {text}
      </p>
      {isLong ? (
        <button
          type="button"
          aria-controls={contentId}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
          className="mt-1 inline-flex min-h-7 items-center text-[11px] font-medium text-[var(--acid)] hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--acid)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--panel)]"
        >
          {expanded ? 'Show less' : 'Show all'}
        </button>
      ) : null}
    </div>
  )
}

// A quarantine is itself L1's adverse verdict, so the shadow disposition is
// read against that: `violation` concurs, `safe` is the divergence worth an
// operator's attention, and the rest reached no verdict at all.
const shadowAgreement = {
  violation: {
    label: 'Concurs with the quarantine',
    tone: 'bg-[var(--red-dim)] text-[var(--red)]',
    detail:
      'The escalated review independently found a violation. It cannot act on that — the L1 quarantine is what holds this submission.',
  },
  safe: {
    label: 'Diverges from the quarantine',
    tone: 'bg-[var(--amber-dim)] text-[var(--amber)]',
    detail:
      'The escalated review adjudicated this submission safe while L1 quarantined it. Neither side is authoritative over the other here; weigh both against the source.',
  },
  inconclusive: {
    label: 'No verdict',
    tone: 'bg-white/[0.06] text-[var(--muted-strong)]',
    detail:
      'The escalated review finished without reaching a disposition. It neither supports nor contradicts the L1 finding.',
  },
  retryable_infra: {
    label: 'Review did not complete',
    tone: 'bg-white/[0.06] text-[var(--muted-strong)]',
    detail:
      'The escalated review aborted on an infrastructure fault, not on anything it found in the source. Read it as absent, not as a clearance.',
  },
} as const

function ShadowReviewSection({ shadow }: { shadow: ShadowReviewObservation | null }) {
  if (!shadow) {
    return (
      <p className="text-xs leading-5 text-[var(--muted)]">
        No shadow review accompanies this quarantine. The L2/L3 reviewer runs only
        while shadow mode is enabled for the screening worker.
      </p>
    )
  }

  const agreement = shadowAgreement[shadow.disposition]
  const tokens =
    Number(shadow.usage.input_tokens ?? 0) + Number(shadow.usage.output_tokens ?? 0)
  const cost = Number(
    shadow.usage.reported_cost_usd ?? shadow.usage.estimated_cost_usd ?? 0,
  )
  // The screener emits a literal "none" when it found nothing to categorize;
  // rendered as a chip it reads like a category of its own.
  const categories = shadow.categories.filter((entry) => entry !== 'none')
  // A real escalation runs 9-25 stages and repeats the same model across
  // consecutive turns, so collapse runs rather than printing each one.
  const stages = shadow.response_models.reduce<
    Array<{ model: string; provider: string | null; count: number }>
  >((acc, model, index) => {
    const provider = shadow.response_providers[index] ?? null
    const last = acc.at(-1)
    if (last && last.model === model && last.provider === provider) {
      last.count += 1
      return acc
    }
    acc.push({ model, provider, count: 1 })
    return acc
  }, [])

  return (
    <section
      aria-label="Shadow source review"
      className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Eye className="h-4 w-4 text-[var(--muted-strong)]" />
        <p className="text-xs font-medium text-white">Shadow source review (L2/L3)</p>
        <span className="rounded-full px-2 py-0.5 text-[10px] font-medium uppercase text-[var(--muted-strong)] ring-1 ring-inset ring-[var(--line)]">
          {humanize(shadow.disposition)}
        </span>
        {shadow.risk_level ? (
          <span
            className={`rounded-full px-2 py-0.5 text-[10px] font-medium uppercase ${riskTone[shadow.risk_level]}`}
          >
            {shadow.risk_level} risk
          </span>
        ) : null}
        <span
          className="ml-auto inline-flex items-center gap-1 rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] text-[var(--muted)]"
          title="Shadow review is advisory telemetry. By design it cannot quarantine, reject, or ban — only the L1 screen and an operator can change this submission's state."
        >
          <Info className="h-3 w-3" /> non-authoritative
        </span>
      </div>

      <div
        className={`mt-3 rounded-lg px-3 py-2 text-[11px] font-medium ${agreement.tone}`}
      >
        {agreement.label}
      </div>
      <p className="mt-2 text-xs leading-5 text-[var(--muted-strong)]">
        {agreement.detail}
      </p>

      {categories.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {categories.map((category) => (
            <span
              key={category}
              className="rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] capitalize text-[var(--muted-strong)]"
            >
              {humanize(category)}
            </span>
          ))}
        </div>
      ) : null}

      <dl className="mt-4 grid gap-x-4 gap-y-3 sm:grid-cols-2">
        <div>
          <dt className="text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">
            Critic
          </dt>
          <dd className="mt-0.5 text-[11px] capitalize text-[var(--muted-strong)]">
            {shadow.critic_disposition ? humanize(shadow.critic_disposition) : 'Not reached'}
          </dd>
        </div>
        <div>
          <dt className="text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">
            Adjudicator
          </dt>
          <dd className="mt-0.5 text-[11px] capitalize text-[var(--muted-strong)]">
            {shadow.adjudicator_disposition
              ? humanize(shadow.adjudicator_disposition)
              : 'Not reached'}
          </dd>
        </div>
        <div>
          <dt className="text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">
            Resolution basis
          </dt>
          <dd className="mt-0.5 text-[11px] capitalize text-[var(--muted-strong)]">
            {shadow.resolution_basis ? humanize(shadow.resolution_basis) : 'Not recorded'}
          </dd>
        </div>
        <div>
          <dt className="text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">
            Clearance path
          </dt>
          <dd className="mt-0.5 text-[11px] capitalize text-[var(--muted-strong)]">
            {shadow.clearance_path ? humanize(shadow.clearance_path) : 'No terminal path'}
          </dd>
        </div>
      </dl>

      {stages.length > 0 ? (
        <div className="mt-4">
          <p className="text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">
            Model trajectory ({shadow.response_models.length}{' '}
            {shadow.response_models.length === 1 ? 'stage' : 'stages'})
          </p>
          <ol className="mt-2 flex flex-wrap items-center gap-1">
            {stages.map((stage, index) => (
              <li
                // Runs are collapsed but a model can legitimately reappear
                // later, so the run index is the only stable key here.
                key={`${index}-${stage.model}`}
                className="flex items-center gap-1"
              >
                {index > 0 ? (
                  <ChevronRight className="h-3 w-3 shrink-0 text-[var(--muted)]" />
                ) : null}
                <span
                  className="rounded-md bg-white/[0.04] px-1.5 py-0.5 font-mono text-[10px] text-[var(--muted-strong)]"
                  title={stage.provider ? `via ${stage.provider}` : undefined}
                >
                  {stage.model}
                  {stage.count > 1 ? (
                    <span className="text-[var(--muted)]"> ×{stage.count}</span>
                  ) : null}
                </span>
              </li>
            ))}
          </ol>
        </div>
      ) : null}

      <p className="mt-4 text-[10px] text-[var(--muted)]">
        Observed {formatDate(shadow.created_at)} · ${cost.toFixed(2)} ·{' '}
        {tokens.toLocaleString()} tokens · settings revision {shadow.settings_revision} (
        {shadow.settings_scope})
      </p>
    </section>
  )
}

function SourceExcerptView({
  excerpt,
  highlightLine,
}: {
  excerpt: SourceExcerpt
  highlightLine: number | null
}) {
  return (
    <div className="mt-2 overflow-hidden rounded-lg border border-[var(--line)] bg-[#0b0d09]">
      <div className="flex items-center justify-between border-b border-[var(--line)] px-3 py-1.5">
        <span className="truncate font-mono text-[10px] text-[var(--muted-strong)]">
          {excerpt.path}
        </span>
        <span className="shrink-0 text-[10px] text-[var(--muted)]">
          lines {excerpt.start_line}–{excerpt.end_line} of {excerpt.total_lines}
        </span>
      </div>
      <pre
        // Untrusted miner source: force LTR plaintext bidi isolation so
        // direction-control characters (rendered as visible escapes by
        // sanitizeSourceLine) can never visually reorder what is reviewed.
        dir="ltr"
        style={{ unicodeBidi: 'plaintext' }}
        className="scrollbar-thin max-h-72 overflow-auto p-2 text-[11px] leading-5"
      >
        {excerpt.lines.map((entry) => (
          <div
            key={entry.line}
            className={`flex gap-3 px-1 ${
              entry.line === highlightLine
                ? 'bg-[var(--amber-dim)] text-[var(--amber)]'
                : 'text-[var(--muted-strong)]'
            }`}
          >
            <span className="w-10 shrink-0 select-none text-right text-[var(--muted)]">
              {entry.line}
            </span>
            <code dir="ltr" style={{ unicodeBidi: 'isolate' }} className="whitespace-pre">
              {sanitizeSourceLine(entry.text) || ' '}
            </code>
          </div>
        ))}
      </pre>
    </div>
  )
}

export function QuarantineEvidencePanel({
  quarantine,
  readOnly,
}: {
  quarantine: ScreeningQuarantine
  readOnly: boolean
}) {
  const loadContext = useServerFn(getScreeningQuarantineContext)
  const loadFiles = useServerFn(listScreeningSourceFiles)
  const loadExcerpt = useServerFn(readScreeningSourceFile)

  const [context, setContext] = useState<ScreeningQuarantineContext | null>(null)
  const [contextError, setContextError] = useState('')
  const [contextLoading, setContextLoading] = useState(false)
  const [excerpt, setExcerpt] = useState<SourceExcerpt | null>(null)
  const [excerptRequest, setExcerptRequest] = useState<ExcerptRequest | null>(null)
  const [excerptLoading, setExcerptLoading] = useState(false)
  const [excerptError, setExcerptError] = useState('')
  const [listing, setListing] = useState<SourceListing | null>(null)
  const [listingOpen, setListingOpen] = useState(false)
  const [listingLoading, setListingLoading] = useState(false)
  // Monotonic token: a slower superseded excerpt response must never commit
  // over the excerpt the reviewer currently has selected.
  const excerptToken = useRef(0)

  useEffect(() => {
    let cancelled = false
    setContext(null)
    setContextError('')
    setContextLoading(true)
    setExcerpt(null)
    setExcerptRequest(null)
    excerptToken.current += 1
    setExcerptError('')
    setListing(null)
    setListingOpen(false)
    loadContext({ data: { quarantineId: quarantine.quarantine_id } })
      .then((result) => {
        if (!cancelled) setContext(result)
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setContextError(
            cause instanceof Error ? cause.message : 'Unable to load review context',
          )
        }
      })
      .finally(() => {
        if (!cancelled) setContextLoading(false)
      })
    return () => {
      cancelled = true
    }
    // Depend on the OBJECT, not just its id: a queue refresh produces a new
    // quarantine object for the same id and must re-fetch the context.
  }, [loadContext, quarantine])

  const openExcerpt = async (path: string, line: number | null) => {
    if (readOnly) return
    const token = ++excerptToken.current
    if (excerptRequest?.path === path && excerptRequest.line === line) {
      setExcerptRequest(null)
      setExcerpt(null)
      return
    }
    setExcerptRequest({ path, line })
    setExcerpt(null)
    setExcerptError('')
    setExcerptLoading(true)
    try {
      const window = 14
      const result = await loadExcerpt({
        data: {
          agentId: quarantine.agent_id,
          path,
          startLine: line === null ? 1 : Math.max(1, line - window),
          endLine: line === null ? 120 : line + window,
        },
      })
      if (token === excerptToken.current) setExcerpt(result)
    } catch (cause) {
      if (token === excerptToken.current) {
        setExcerptError(
          cause instanceof Error ? cause.message : 'Unable to load this source excerpt',
        )
      }
    } finally {
      if (token === excerptToken.current) setExcerptLoading(false)
    }
  }

  const toggleListing = async () => {
    if (listingOpen) {
      setListingOpen(false)
      return
    }
    setListingOpen(true)
    if (listing || readOnly) return
    setListingLoading(true)
    try {
      setListing(await loadFiles({ data: { agentId: quarantine.agent_id } }))
    } catch (cause) {
      setExcerptError(
        cause instanceof Error ? cause.message : 'Unable to list source files',
      )
    } finally {
      setListingLoading(false)
    }
  }

  // Prefer the authoritative record from the context endpoint over the
  // possibly stale list row the queue handed us.
  const record = context?.quarantine ?? quarantine
  const finding = record.finding
  const findingVerified = record.finding_verified === true
  const evidence = record.evidence ?? []
  const miner = context?.miner
  const duplicates = context?.duplicates ?? []
  // Attribution must come from the authoritative aggregate, never from
  // whether the bounded sample happened to include a cross-miner row.
  const duplicateSummary = context?.duplicate_summary ?? null
  // Only the context endpoint carries it; the queue list row never does.
  const shadowReview = context?.shadow_review ?? null
  const crossOwnerCount =
    duplicateSummary?.cross_owner ??
    duplicateSummary?.cross_miner ??
    duplicates.filter((item) => item.miner_hotkey !== quarantine.miner_hotkey).length
  const duplicateTotal = duplicateSummary?.total ?? duplicates.length

  return (
    <div className="space-y-5 py-5">
      {duplicateTotal > 0 ? (
        <div
          role="alert"
          className={`flex gap-3 rounded-lg border px-4 py-3 text-xs ${
            crossOwnerCount > 0
              ? 'border-[var(--red)]/25 bg-[var(--red-dim)] text-[var(--red)]'
              : 'border-[var(--amber)]/25 bg-[var(--amber-dim)] text-[var(--amber)]'
          }`}
        >
          <Copy className="h-4 w-4 shrink-0" />
          <div className="min-w-0">
            <p className="font-medium">
              {crossOwnerCount > 0
                ? `Identical code exists under ${crossOwnerCount === 1 ? 'another payment owner' : `${crossOwnerCount} other payment owners`}`
                : 'Same payment owner submitted identical code before'}
            </p>
            <ul className="mt-1 space-y-1 text-[11px]">
              {duplicates.slice(0, 4).map((item) => (
                <li key={item.agent_id} className="truncate">
                  <span className="font-mono">{short(item.miner_hotkey, 16)}</span>
                  {' · '}
                  {item.agent_name} · {humanize(item.agent_status)} ·{' '}
                  {item.match === 'identical_artifact'
                    ? 'byte-identical tarball'
                    : 'identical source after normalization'}
                  {' · '}
                  {item.same_owner ? 'same-owner lineage' : 'different owner'}
                </li>
              ))}
            </ul>
            {duplicateSummary?.sample_truncated ? (
              <p className="mt-1 text-[10px] opacity-80">
                Showing a sample; {duplicateTotal} duplicates exist in total.
              </p>
            ) : null}
          </div>
        </div>
      ) : null}

      <section aria-label="Screening evidence">
        <div className="flex items-center justify-between">
          <p className="text-xs font-medium text-white">Why it was quarantined</p>
          <span className="text-[10px] capitalize text-[var(--amber)]">
            {humanize(record.reason_code)}
          </span>
        </div>
        {evidence.length === 0 ? (
          <p className="mt-2 text-xs leading-5 text-[var(--muted)]">
            The screener reported only a reason code and digests for this quarantine.
            Newer screenings attach the full evidence trail.
          </p>
        ) : (
          <ol className="mt-3 space-y-2">
            {evidence.map((item, index) => (
              <li
                key={`${item.module_id}-${item.code}-${index}`}
                className="rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2"
              >
                <div className="flex flex-wrap items-center gap-2 text-[10px]">
                  <span className="rounded-full bg-white/[0.06] px-2 py-0.5 font-mono text-[var(--muted-strong)]">
                    {item.module_id}
                  </span>
                  <span className="font-medium capitalize text-[var(--amber)]">
                    {humanize(item.code)}
                  </span>
                  {item.digest ? (
                    <span
                      className="ml-auto font-mono text-[var(--muted)]"
                      title={item.digest}
                    >
                      {short(item.digest, 14)}
                    </span>
                  ) : null}
                </div>
                <CollapsibleSummary
                  key={item.summary}
                  text={item.summary}
                  className="mt-1 text-xs leading-5 text-[var(--muted-strong)]"
                />
              </li>
            ))}
          </ol>
        )}
      </section>

      {finding ? (
        <section
          aria-label="Source review finding"
          className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4"
        >
          <div className="flex flex-wrap items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-[var(--muted-strong)]" />
            <p className="text-xs font-medium text-white">Automated source review</p>
            {findingVerified ? (
              <span
                className={`rounded-full px-2 py-0.5 text-[10px] font-medium uppercase ${riskTone[finding.risk_level]}`}
              >
                {finding.risk_level} risk
              </span>
            ) : (
              <span className="rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] font-medium uppercase text-[var(--muted)]">
                unverified
              </span>
            )}
            <span className="text-[10px] text-[var(--muted-strong)]">
              {Math.round(finding.confidence * 100)}% confidence
            </span>
            {findingVerified ? (
              <span
                className="ml-auto inline-flex items-center gap-1 rounded-full bg-[var(--acid-dim)] px-2 py-0.5 text-[10px] text-[var(--acid)]"
                title="The finding hashes to the digest bound into the screener's signed verdict."
              >
                <Fingerprint className="h-3 w-3" /> digest verified
              </span>
            ) : (
              <span
                className="ml-auto inline-flex items-center gap-1 rounded-full bg-[var(--red-dim)] px-2 py-0.5 text-[10px] text-[var(--red)]"
                title="The finding does not verify against the signed digest for this artifact. Its content may not describe this submission — treat it as unverified."
              >
                <AlertTriangle className="h-3 w-3" /> not verified
              </span>
            )}
          </div>
          <CollapsibleSummary
            key={finding.summary}
            text={finding.summary}
            className="mt-3 text-xs leading-5 text-[var(--muted-strong)]"
          />
          <div className="mt-2 flex flex-wrap gap-1.5">
            {finding.categories.map((category) => (
              <span
                key={category}
                className="rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] capitalize text-[var(--muted-strong)]"
              >
                {humanize(category)}
              </span>
            ))}
          </div>
          {finding.evidence.length > 0 ? (
            <div className="mt-4">
              <p className="text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">
                Flagged locations
              </p>
              <ul className="mt-2 space-y-1">
                {finding.evidence.map((item) => {
                  const isOpen =
                    excerptRequest?.path === item.path &&
                    excerptRequest.line === item.line
                  return (
                    <li key={`${item.path}:${item.line}:${item.category}`}>
                      <button
                        type="button"
                        onClick={() => void openExcerpt(item.path, item.line)}
                        disabled={readOnly}
                        title={
                          readOnly
                            ? 'Reading source requires write access'
                            : 'Show the flagged lines'
                        }
                        className="flex w-full min-h-9 items-center gap-2 rounded-lg border border-[var(--line)] px-3 text-left text-[11px] hover:bg-white/[0.035] disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {isOpen ? (
                          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-[var(--muted)]" />
                        ) : (
                          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-[var(--muted)]" />
                        )}
                        <FileCode2 className="h-3.5 w-3.5 shrink-0 text-[var(--muted-strong)]" />
                        <span className="truncate font-mono text-[var(--muted-strong)]">
                          {item.path}:{item.line}
                        </span>
                        <span className="ml-auto shrink-0 capitalize text-[var(--muted)]">
                          {humanize(item.category)}
                        </span>
                      </button>
                      {isOpen && excerptLoading ? (
                        <div className="mt-2 h-24 animate-pulse rounded-lg bg-white/[0.04]" />
                      ) : null}
                      {isOpen && excerpt ? (
                        <SourceExcerptView excerpt={excerpt} highlightLine={item.line} />
                      ) : null}
                    </li>
                  )
                })}
              </ul>
            </div>
          ) : null}
        </section>
      ) : (
        <p className="text-xs leading-5 text-[var(--muted)]">
          No automated source-review finding accompanies this quarantine
          {record.finding_digest
            ? ' (only its digest was recorded by an older screener).'
            : '.'}
        </p>
      )}

      <ShadowReviewSection shadow={shadowReview} />

      {excerptError ? (
        <p role="alert" className="text-xs text-[var(--red)]">
          {excerptError}
        </p>
      ) : null}

      {/* Placed ahead of raw file browsing: starting from the kit-subtracted
          delta is what turns a whole-crate read into a small one. */}
      <QuarantineBaselineDiff agentId={quarantine.agent_id} canView={!readOnly} />

      <section aria-label="Source files">
        <button
          type="button"
          onClick={() => void toggleListing()}
          disabled={readOnly}
          title={readOnly ? 'Reading source requires write access' : undefined}
          className="inline-flex min-h-9 items-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {listingOpen ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
          Browse source files
          {listing ? (
            <span className="text-[10px] text-[var(--muted)]">
              {listing.file_count} files
            </span>
          ) : null}
        </button>
        {listingOpen ? (
          listingLoading ? (
            <div className="mt-2 h-24 animate-pulse rounded-lg bg-white/[0.04]" />
          ) : listing ? (
            <div className="mt-2 rounded-lg border border-[var(--line)] bg-[var(--panel)]">
              {(listing.opaque_total ?? listing.opaque_blobs.length) > 0 ? (
                <div className="border-b border-[var(--line)] px-3 py-2">
                  <p className="inline-flex items-center gap-1.5 text-[10px] font-medium text-[var(--amber)]">
                    <FileWarning className="h-3.5 w-3.5" />
                    {listing.opaque_total ?? listing.opaque_blobs.length} unreadable{' '}
                    {(listing.opaque_total ?? listing.opaque_blobs.length) === 1
                      ? 'file'
                      : 'files'}{' '}
                    (binary or oversized) — a natural place to hide precomputed
                    answers
                    {(listing.opaque_total ?? 0) > listing.opaque_blobs.length
                      ? `; showing the first ${listing.opaque_blobs.length}`
                      : ''}
                  </p>
                  <ul className="mt-1 space-y-0.5">
                    {listing.opaque_blobs.slice(0, 8).map((blob) => (
                      <li
                        key={blob.path}
                        className="truncate font-mono text-[10px] text-[var(--muted-strong)]"
                      >
                        {blob.path} · {formatBytes(blob.bytes)} ·{' '}
                        {blob.reason === 'oversized' ? 'oversized' : 'not UTF-8'}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              <ul className="scrollbar-thin max-h-56 overflow-y-auto py-1">
                {listing.files.map((file) => (
                  <li key={file.path}>
                    <button
                      type="button"
                      onClick={() => void openExcerpt(file.path, null)}
                      className="flex w-full items-center gap-2 px-3 py-1 text-left font-mono text-[11px] text-[var(--muted-strong)] hover:bg-white/[0.035]"
                    >
                      <span className="truncate">{file.path}</span>
                      <span className="ml-auto shrink-0 text-[10px] text-[var(--muted)]">
                        {formatBytes(file.bytes)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
              {listing.truncated ? (
                <p className="border-t border-[var(--line)] px-3 py-1.5 text-[10px] text-[var(--muted)]">
                  Listing truncated to {listing.files.length} files. Download the
                  artifact for the full tree.
                </p>
              ) : null}
            </div>
          ) : null
        ) : null}
        {excerptRequest && excerptRequest.line === null ? (
          excerptLoading ? (
            <div className="mt-2 h-24 animate-pulse rounded-lg bg-white/[0.04]" />
          ) : excerpt ? (
            <SourceExcerptView excerpt={excerpt} highlightLine={null} />
          ) : null
        ) : null}
      </section>

      <section aria-label="Submission and miner context">
        {contextLoading ? (
          <div className="space-y-2" aria-label="Loading review context">
            <div className="h-16 animate-pulse rounded-lg bg-white/[0.04]" />
            <div className="h-16 animate-pulse rounded-lg bg-white/[0.04]" />
          </div>
        ) : contextError ? (
          <p role="alert" className="text-xs text-[var(--red)]">
            {contextError}
          </p>
        ) : context ? (
          <div className="grid gap-3 lg:grid-cols-2">
            <div className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4">
              <p className="inline-flex items-center gap-1.5 text-xs font-medium text-white">
                <History className="h-3.5 w-3.5 text-[var(--muted-strong)]" />
                Submission
              </p>
              <dl className="mt-3 space-y-1.5 text-[11px]">
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--muted)]">Uploaded</dt>
                  <dd className="text-[var(--muted-strong)]">
                    {formatDate(context.agent.submitted_at)}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--muted)]">Tarball size</dt>
                  <dd className="text-[var(--muted-strong)]">
                    {formatBytes(context.agent.size_bytes)}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--muted)]">Screening attempts</dt>
                  <dd className="text-[var(--muted-strong)]">
                    {context.attempts.length}
                  </dd>
                </div>
              </dl>
              {context.attempts.length > 0 ? (
                <ul className="mt-3 space-y-1 border-t border-[var(--line)] pt-2">
                  {context.attempts.slice(0, 5).map((attempt) => (
                    <li
                      key={attempt.attempt_id}
                      className="flex items-center justify-between gap-2 text-[10px]"
                    >
                      <span className="capitalize text-[var(--muted-strong)]">
                        {humanize(attempt.status)} · policy v{attempt.policy_version}
                      </span>
                      <span className="text-[var(--muted)]">
                        {formatDate(attempt.started_at)}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
            <div className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4">
              <p className="inline-flex items-center gap-1.5 text-xs font-medium text-white">
                <Users className="h-3.5 w-3.5 text-[var(--muted-strong)]" />
                Miner track record
              </p>
              <dl className="mt-3 space-y-1.5 text-[11px]">
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--muted)]">Total submissions</dt>
                  <dd className="text-[var(--muted-strong)]">
                    {miner?.total_submissions}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--muted)]">Quarantines</dt>
                  <dd className="text-[var(--muted-strong)]">
                    {miner?.quarantine_count} total · {miner?.released_count} released ·{' '}
                    {miner?.rescreened_count} rescreened · {miner?.rejected_count}{' '}
                    rejected
                  </dd>
                </div>
              </dl>
              {miner && miner.recent_quarantines.length > 0 ? (
                <ul className="mt-3 space-y-1.5 border-t border-[var(--line)] pt-2">
                  {miner.recent_quarantines.slice(0, 4).map((item) => (
                    <li key={item.quarantine_id} className="text-[10px]">
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-[var(--muted-strong)]">
                          {item.agent_name} · {humanize(item.reason_code)}
                        </span>
                        <span
                          className={`shrink-0 capitalize ${
                            item.resolution === 'reject'
                              ? 'text-[var(--red)]'
                              : item.resolution === 'release'
                                ? 'text-[var(--acid)]'
                                : 'text-[var(--amber)]'
                          }`}
                        >
                          {item.resolution ?? 'active'}
                        </span>
                      </div>
                      {item.resolution_reason ? (
                        <p className="mt-0.5 truncate text-[var(--muted)]">
                          {item.resolution_reason}
                        </p>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : miner ? (
                <p className="mt-3 inline-flex items-center gap-1.5 border-t border-[var(--line)] pt-2 text-[10px] text-[var(--muted)]">
                  <ShieldCheck className="h-3 w-3" />
                  First quarantine for this miner.
                </p>
              ) : null}
            </div>
          </div>
        ) : null}
        {contextLoading ? (
          <p className="mt-2 inline-flex items-center gap-1.5 text-[10px] text-[var(--muted)]">
            <RefreshCw className="h-3 w-3 animate-spin" /> Loading submission and miner
            context
          </p>
        ) : null}
      </section>
    </div>
  )
}
