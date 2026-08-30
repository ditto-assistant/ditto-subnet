import { useServerFn } from '@tanstack/react-start'
import { CheckCircle2, ChevronDown, ChevronRight, RefreshCw, ShieldAlert } from 'lucide-react'
import { useMemo, useState } from 'react'
import {
  CONFIRMATION_BUNDLE_RETEST_CONFIRMATION,
  confirmationBundleSettingsConfirmation,
  type ConfirmationBundleList,
  type ConfirmationBundleSettings,
  type ConfirmationBundleSettingsControl,
  type ConfirmationBundleView,
} from '../lib/admin.schemas'
import {
  authorizeConfirmationBundleRetest,
  getConfirmationBundleSettings,
  listConfirmationBundles,
  updateConfirmationBundleSettings,
} from '../server/admin.functions'

type BundleState = ConfirmationBundleView['state']

const FILTERS: Array<{ label: string; value: BundleState | 'all' }> = [
  { label: 'All', value: 'all' },
  { label: 'Pending', value: 'pending' },
  { label: 'Leased', value: 'leased' },
  { label: 'Completed', value: 'completed' },
  { label: 'Failed', value: 'failed' },
  { label: 'Superseded', value: 'superseded' },
]
export const CONFIRMATION_BUNDLE_PAGE_SIZE = 20

function compact(value: string | null, width = 10) {
  if (!value) return '—'
  return `${value.slice(0, width)}…${value.slice(-6)}`
}

function micros(value: number | null) {
  return value === null ? '—' : (value / 1_000_000).toFixed(4)
}

function integer(value: number) {
  return new Intl.NumberFormat('en-US').format(value)
}

function cost(value: number | null) {
  return value === null ? 'Unavailable' : `${integer(value)} μUSD`
}

function rate(value: number | null) {
  return value === null ? 'Unavailable' : `${(value / 100).toFixed(1)}%`
}

function statusTone(value: string) {
  if (['qualified', 'completed', 'full_confirmed', 'passed', 'scored'].includes(value)) {
    return 'border-[var(--acid)]/30 bg-[var(--acid-dim)] text-[var(--acid)]'
  }
  if (['failed', 'unqualified', 'unavailable', 'expired'].includes(value)) {
    return 'border-[var(--red)]/30 bg-[var(--red-dim)] text-[var(--red)]'
  }
  return 'border-[var(--amber)]/30 bg-[var(--amber)]/10 text-[var(--amber)]'
}

function Status({ children }: { children: string }) {
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] font-medium ${statusTone(children)}`}>
      {children.replaceAll('_', ' ')}
    </span>
  )
}

export function ConfirmationBundleControlPanel({
  initialSettings,
  initialBundles,
  readOnly,
}: {
  initialSettings: ConfirmationBundleSettingsControl
  initialBundles: ConfirmationBundleList
  readOnly: boolean
}) {
  const readSettings = useServerFn(getConfirmationBundleSettings)
  const readBundles = useServerFn(listConfirmationBundles)
  const writeSettings = useServerFn(updateConfirmationBundleSettings)
  const [control, setControl] = useState(initialSettings)
  const [bundles, setBundles] = useState(initialBundles)
  const [draft, setDraft] = useState<ConfirmationBundleSettings>(
    initialSettings.effective.settings,
  )
  const [filter, setFilter] = useState<BundleState | 'all'>('all')
  const [offset, setOffset] = useState(0)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const expectedConfirmation = confirmationBundleSettingsConfirmation(draft.mode)
  const settingsChanged = JSON.stringify(draft) !== JSON.stringify(control.effective.settings)

  async function refresh(nextFilter = filter, nextOffset = offset) {
    setBusy(true)
    setError(null)
    try {
      const [nextControl, nextBundles] = await Promise.all([
        readSettings(),
        readBundles({
          data:
            nextFilter === 'all'
              ? {
                  generation: 'active',
                  limit: CONFIRMATION_BUNDLE_PAGE_SIZE,
                  offset: nextOffset,
                }
              : {
                  generation: 'active',
                  state: nextFilter,
                  limit: CONFIRMATION_BUNDLE_PAGE_SIZE,
                  offset: nextOffset,
                },
        }),
      ])
      setControl(nextControl)
      setDraft(nextControl.effective.settings)
      setBundles(nextBundles)
      setOffset(nextOffset)
      setNotice('Refreshed from Platform.')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Refresh failed')
    } finally {
      setBusy(false)
    }
  }

  async function applySettings() {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await writeSettings({
        data: {
          scope: '*',
          expectedRevision: control.effective.revision,
          settings: draft,
          reason,
          confirmation,
        },
      })
      await refresh()
      setReason('')
      setConfirmation('')
      setNotice(`Applied revision ${control.effective.revision + 1}.`)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Settings update failed')
    } finally {
      setBusy(false)
    }
  }

  function numeric<K extends keyof ConfirmationBundleSettings>(key: K, value: string) {
    setDraft((current) => ({ ...current, [key]: Number(value) }))
  }

  const spend = bundles.budget.outstanding_reserved_microusd + bundles.budget.settled_microusd
  const calibration = bundles.shadow_calibration

  return (
    <div className="mt-6 space-y-5">
      <section className="grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
        <div className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-xs font-medium text-[var(--muted)]">Issuance policy</p>
              <h2 className="mt-1 text-lg font-semibold">Revision {control.effective.revision}</h2>
            </div>
            <Status>{control.effective.issuance_active ? control.effective.settings.mode : 'off'}</Status>
          </div>

          <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="Mode">
              <select
                aria-label="Mode"
                value={draft.mode}
                disabled={readOnly || busy}
                onChange={(event) => setDraft((current) => ({ ...current, mode: event.target.value as ConfirmationBundleSettings['mode'] }))}
              >
                <option value="off">Off</option>
                <option value="shadow">Shadow</option>
                <option value="enforce">Enforce</option>
              </select>
            </Field>
            <Field label="Eligibility">
              <select
                aria-label="Eligibility"
                value={draft.eligibility_mode}
                disabled={readOnly || busy}
                onChange={(event) => setDraft((current) => ({ ...current, eligibility_mode: event.target.value as ConfirmationBundleSettings['eligibility_mode'] }))}
              >
                <option value="rank">Rank + challenger band</option>
                <option value="score_threshold">Fixed base score</option>
              </select>
            </Field>
            {draft.eligibility_mode === 'rank' ? (
              <NumberField label="Top N" value={draft.top_n} disabled={readOnly || busy} onChange={(value) => numeric('top_n', value)} />
            ) : (
              <NumberField label="Minimum base score (μ)" value={draft.min_base_score_micros} disabled={readOnly || busy} onChange={(value) => numeric('min_base_score_micros', value)} />
            )}
            <NumberField label="Daily bundles" value={draft.daily_bundle_cap} disabled={readOnly || busy} onChange={(value) => numeric('daily_bundle_cap', value)} />
            <NumberField label="Daily μUSD" value={draft.daily_dollar_cap_microusd} disabled={readOnly || busy} onChange={(value) => numeric('daily_dollar_cap_microusd', value)} />
            <NumberField label="Requests / bundle" value={draft.per_bundle_request_cap} disabled={readOnly || busy} onChange={(value) => numeric('per_bundle_request_cap', value)} />
            <NumberField label="Tokens / bundle" value={draft.per_bundle_token_cap} disabled={readOnly || busy} onChange={(value) => numeric('per_bundle_token_cap', value)} />
          </div>

          <div className="mt-4 grid gap-4 sm:grid-cols-[1fr_1.3fr]">
            <Field label="Profile revision">
              <input
                value={draft.profile_revision ?? ''}
                disabled={readOnly || busy}
                onChange={(event) => setDraft((current) => ({ ...current, profile_revision: event.target.value || null }))}
              />
            </Field>
            <Field label="Profile checksum">
              <input
                className="font-mono"
                value={draft.profile_checksum ?? ''}
                disabled={readOnly || busy}
                onChange={(event) => setDraft((current) => ({ ...current, profile_checksum: event.target.value || null }))}
              />
            </Field>
          </div>

          {!readOnly && (
            <div className="mt-5 border-t border-[var(--line)] pt-5">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Audit reason">
                  <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why this policy should change" />
                </Field>
                <Field label="Exact confirmation">
                  <input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder={expectedConfirmation} />
                </Field>
              </div>
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-[var(--muted)]">Type <span className="font-mono text-[var(--muted-strong)]">{expectedConfirmation}</span></p>
                <button
                  className="rounded-lg bg-[var(--acid)] px-4 py-2 text-sm font-semibold text-[var(--ink)] hover:bg-[var(--acid-hover)] active:translate-y-px disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-[var(--acid)]"
                  disabled={!settingsChanged || reason.length < 8 || confirmation !== expectedConfirmation || busy}
                  onClick={applySettings}
                  type="button"
                >
                  Apply audited revision
                </button>
              </div>
            </div>
          )}
        </div>

        <div className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-5">
          <p className="text-xs font-medium text-[var(--muted)]">UTC budget</p>
          <p className="mt-2 text-3xl font-semibold tabular-nums">{integer(spend)} <span className="text-sm font-normal text-[var(--muted)]">μUSD committed</span></p>
          <dl className="mt-5 grid grid-cols-2 gap-4 text-sm">
            <Metric label="Issued attempts" value={integer(bundles.budget.issued_attempts)} />
            <Metric label="Budget revision" value={integer(bundles.budget.revision)} />
            <Metric label="Reserved" value={`${integer(bundles.budget.outstanding_reserved_microusd)} μUSD`} />
            <Metric label="Settled" value={`${integer(bundles.budget.settled_microusd)} μUSD`} />
          </dl>
          <div className="mt-5 border-t border-[var(--line)] pt-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold">Shadow calibration</h3>
                <p className="mt-1 text-xs text-[var(--muted)]">
                  Settled Platform rows only
                  {calibration.observation_days > 0
                    ? ` · ${integer(calibration.observation_days)} UTC day${calibration.observation_days === 1 ? '' : 's'}`
                    : ' · no cost samples yet'}
                </p>
                {calibration.confirmation_profile_revision && (
                  <p className="mt-1 text-xs text-[var(--muted)]">
                    Profile {calibration.confirmation_profile_revision} · {compact(calibration.confirmation_profile_checksum)}
                  </p>
                )}
              </div>
              <Status>{control.effective.settings.mode === 'shadow' ? 'shadow' : 'measured'}</Status>
            </div>
            <dl className="mt-4 grid grid-cols-2 gap-4 text-sm">
              <Metric label={`Measured base cost · ${integer(calibration.base_run_count)} runs`} value={cost(calibration.measured_base_cost_microusd)} />
              <Metric label={`Average bundle cost · ${integer(calibration.confirmation_bundle_count)} bundles`} value={cost(calibration.measured_bundle_cost_microusd)} />
              <Metric label={`Promotion rate · ${integer(calibration.completed_bundle_count)} completed`} value={rate(calibration.promotion_rate_bps)} />
              {/* Superseded and failed generations used to be folded into the
                  completed count, which made a lane that had never once produced
                  evidence read as a populated window with a 0% promotion rate. */}
              <Metric
                label={`Failed · ${integer(calibration.superseded_bundle_count)} superseded`}
                value={integer(calibration.failed_bundle_count)}
              />
              <Metric label="Projected daily spend" value={cost(calibration.projected_daily_spend_microusd)} />
              <Metric label="Projected epoch spend" value={cost(calibration.projected_epoch_spend_microusd)} />
            </dl>
            {calibration.epoch_projection_unavailable_reason && (
              <p className="mt-3 text-xs leading-5 text-[var(--muted)]">
                {calibration.epoch_projection_unavailable_reason}
              </p>
            )}
          </div>
          <div className="mt-5 rounded-lg border border-[var(--line)] bg-black/10 p-3 text-xs leading-5 text-[var(--muted-strong)]">
            Shadow records verified previews but never marks a subject full-confirmed. Enforce can confirm only qualified evidence. This console cannot submit evidence or activate rewards.
          </div>
        </div>
      </section>

      {(error || notice) && (
        <div
          role={error ? 'alert' : 'status'}
          aria-live={error ? 'assertive' : 'polite'}
          className={`rounded-lg border px-4 py-3 text-sm ${error ? 'border-[var(--red)]/30 bg-[var(--red-dim)] text-[var(--red)]' : 'border-[var(--acid)]/30 bg-[var(--acid-dim)] text-[var(--acid)]'}`}
        >
          {error ?? notice}
        </div>
      )}

      <section className="rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--line)] p-4">
          <div>
            <h2 className="font-semibold">Confirmation evidence</h2>
            <p className="mt-1 text-xs text-[var(--muted)]">
              {integer(bundles.items.length)} {bundles.items.length === 1 ? 'bundle' : 'bundles'} returned · {integer(bundles.count)} total · newest first
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {FILTERS.map((item) => (
              <button
                key={item.value}
                type="button"
                className={`rounded-full border px-3 py-1 text-xs hover:border-[var(--line-strong)] hover:bg-[var(--panel-raised)] disabled:opacity-40 ${filter === item.value ? 'border-[var(--cyan)]/50 bg-[var(--cyan-dim)] text-[var(--cyan)]' : 'border-[var(--line)] text-[var(--muted-strong)]'}`}
                disabled={busy}
                onClick={() => {
                  setFilter(item.value)
                  void refresh(item.value, 0)
                }}
              >
                {item.label}
              </button>
            ))}
            <button type="button" aria-label="Refresh bundles" className="rounded-lg border border-[var(--line)] p-2 text-[var(--muted-strong)] hover:border-[var(--line-strong)] hover:bg-[var(--panel-raised)] disabled:opacity-40" disabled={busy} onClick={() => void refresh()}>
              <RefreshCw className={`h-4 w-4 ${busy ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>
        {bundles.items.length === 0 ? (
          <div className="p-10 text-center text-sm text-[var(--muted)]">No bundles match this lifecycle state.</div>
        ) : (
          <div className="divide-y divide-[var(--line)]">
            {bundles.items.map((bundle) => (
              <BundleRow key={bundle.bundle_id} bundle={bundle} readOnly={readOnly} onChanged={() => refresh(filter, offset)} />
            ))}
          </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] p-4 text-xs text-[var(--muted)]">
          <span>
            {bundles.count === 0
              ? 'No rows'
              : `${integer(offset + 1)}–${integer(offset + bundles.items.length)} of ${integer(bundles.count)}`}
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              className="rounded-lg border border-[var(--line)] px-3 py-1.5 hover:border-[var(--line-strong)] hover:bg-[var(--panel-raised)] disabled:cursor-not-allowed disabled:opacity-40"
              disabled={busy || offset === 0}
              onClick={() => void refresh(filter, Math.max(0, offset - CONFIRMATION_BUNDLE_PAGE_SIZE))}
            >
              Previous
            </button>
            <button
              type="button"
              className="rounded-lg border border-[var(--line)] px-3 py-1.5 hover:border-[var(--line-strong)] hover:bg-[var(--panel-raised)] disabled:cursor-not-allowed disabled:opacity-40"
              disabled={busy || offset + bundles.items.length >= bundles.count}
              onClick={() => void refresh(filter, offset + CONFIRMATION_BUNDLE_PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        </div>
      </section>
    </div>
  )
}

function BundleRow({ bundle, readOnly, onChanged }: { bundle: ConfirmationBundleView; readOnly: boolean; onChanged: () => Promise<void> }) {
  const retest = useServerFn(authorizeConfirmationBundleRetest)
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const detailsId = `confirmation-bundle-${bundle.bundle_id}`
  const root = bundle.evidence_root
  const hasCompletedEvidence = root !== null && bundle.completed_at !== null
  const canAuthorizeRetest = bundle.state === 'failed' || (bundle.state === 'completed' && hasCompletedEvidence)
  const lanes = root?.longmemeval.evidence.provider_evidence ?? []
  const ablations = useMemo(
    () => root ? [root.inference_ablation, root.embedding_ablation] : [],
    [root],
  )

  async function authorizeRetest() {
    setBusy(true)
    setError(null)
    try {
      await retest({
        data: {
          bundleId: bundle.bundle_id,
          requestId: crypto.randomUUID(),
          expectedGeneration: bundle.retest_generation,
          reason,
          confirmation: CONFIRMATION_BUNDLE_RETEST_CONFIRMATION,
        },
      })
      await onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Retest authorization failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <article>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={detailsId}
        className="grid w-full gap-3 p-4 text-left hover:bg-[var(--panel-raised)] md:grid-cols-[1.5fr_0.7fr_0.7fr_0.7fr_0.7fr_auto] md:items-center"
        onClick={() => setOpen((value) => !value)}
      >
        <span className="flex min-w-0 items-center gap-3">
          {open ? <ChevronDown className="h-4 w-4 shrink-0 text-[var(--muted)]" /> : <ChevronRight className="h-4 w-4 shrink-0 text-[var(--muted)]" />}
          <span className="min-w-0"><span className="block font-mono text-xs text-[#f3f6f1]">{compact(bundle.artifact_sha256, 14)}</span><span className="mt-1 block text-xs text-[var(--muted)]">generation {bundle.retest_generation} · {humanizeGenerationReason(bundle.generation_reason)} · {bundle.subjects.length} subject{bundle.subjects.length === 1 ? '' : 's'}</span></span>
        </span>
        <span><Status>{bundle.state}</Status></span>
        <span className="text-xs text-[var(--muted-strong)]">{bundle.completion_mode ?? 'not completed'}</span>
        <span className="text-xs tabular-nums text-[var(--muted-strong)]">
          LongMem {micros(root?.longmemeval.evidence.score.longmem_mean_micros ?? null)}
        </span>
        <span>{bundle.qualification_status ? <Status>{bundle.qualification_status}</Status> : <span className="text-xs text-[var(--muted)]">not qualified</span>}</span>
        <span className="text-xs text-[var(--muted)]">rev {bundle.settings_revision}</span>
      </button>

      {open && (
        <div id={detailsId} className="border-t border-[var(--line)] bg-black/10 p-5">
          <div className="grid gap-4 lg:grid-cols-3">
            <AuditBlock title="Root proof">
              <AuditLine label="Generation reason" value={humanizeGenerationReason(bundle.generation_reason)} />
              <AuditLine label="Source bundle" value={bundle.source_bundle_id ?? '—'} mono />
              <AuditLine label="Evidence" value={compact(bundle.evidence_sha256)} mono />
              <AuditLine label="Reporter" value={compact(bundle.reporter_hotkey)} mono />
              <AuditLine label="Signature" value={compact(bundle.bundle_signature)} mono />
              <AuditLine label="Settings" value={`${bundle.settings_revision} · ${compact(bundle.settings_checksum)}`} mono />
              <AuditLine label="Profile" value={`${bundle.profile_revision} · ${compact(bundle.profile_checksum)}`} mono />
              <AuditLine label="Verified" value={bundle.verified_at ? new Date(bundle.verified_at).toLocaleString() : '—'} />
              <AuditLine label="Composite" value={root ? `${root.composite_policy.revision} · ${root.composite_policy.base_weight_bps}/${root.composite_policy.longmem_weight_bps} bps` : '—'} />
              <AuditLine label="Ablation coordinator" value={root ? `${integer(root.ablation_coordinator_latency_ms)} ms · ${compact(root.inference_ablation.evidence.coordinator_sha256)}` : '—'} mono />
            </AuditBlock>

            <AuditBlock title="LongMem provider lanes">
              {root?.longmemeval.evidence.score ? (
                <p className="mb-3 text-sm tabular-nums text-[#f3f6f1]">
                  Mean {micros(root.longmemeval.evidence.score.longmem_mean_micros)} · {integer(root.longmemeval.evidence.score.case_count)} cases
                </p>
              ) : null}
              {lanes.length === 0 ? <p className="text-xs text-[var(--muted)]">No verified lane evidence yet.</p> : lanes.map((lane) => (
                <div key={lane.lane} className="mb-3 last:mb-0">
                  <div className="flex items-center justify-between"><span className="text-xs font-semibold">{lane.lane}</span><CheckCircle2 className="h-3.5 w-3.5 text-[var(--acid)]" /></div>
                  <p className="mt-1 text-xs text-[var(--muted)]">{lane.provider} · {lane.model}</p>
                  <p className="mt-1 text-xs tabular-nums text-[var(--muted-strong)]">{integer(lane.requests)} requests · {integer(lane.total_tokens)} tokens · {integer(lane.cost_usd_micros)} μUSD</p>
                </div>
              ))}
            </AuditBlock>

            <AuditBlock title="Binary ablations">
              {ablations.length === 0 ? <p className="text-xs text-[var(--muted)]">No verified ablation evidence yet.</p> : ablations.map((dimension) => (
                <div key={dimension.evidence.intervention} className="mb-3 flex items-start justify-between gap-3 last:mb-0">
                  <div><p className="text-xs font-semibold capitalize">{dimension.evidence.intervention}</p><p className="mt-1 text-xs text-[var(--muted)]">{dimension.evidence.reason}</p></div>
                  <Status>{dimension.evidence.status}</Status>
                </div>
              ))}
            </AuditBlock>
          </div>

          <div className="mt-4 overflow-x-auto rounded-lg border border-[var(--line)]">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="bg-white/[0.025] text-[var(--muted)]"><tr><th className="px-3 py-2">Subject</th><th className="px-3 py-2">Status</th><th className="px-3 py-2">Base quality</th><th className="px-3 py-2">Full quality</th><th className="px-3 py-2">Effective</th><th className="px-3 py-2">Semantic / applied</th></tr></thead>
              <tbody className="divide-y divide-[var(--line)]">
                {bundle.subjects.map((subject) => (
                  <tr key={subject.agent_id}><td className="px-3 py-2 font-mono">{compact(subject.agent_id)}</td><td className="px-3 py-2"><Status>{subject.result_status}</Status></td><td className="px-3 py-2 tabular-nums">{micros(subject.base_quality_micros)}</td><td className="px-3 py-2 tabular-nums">{micros(subject.full_quality_micros)}</td><td className="px-3 py-2 tabular-nums">{micros(subject.full_effective_micros)}</td><td className="px-3 py-2 tabular-nums">{subject.semantic_factor_bps ?? '—'} / {subject.applied_factor_bps ?? '—'}</td></tr>
                ))}
              </tbody>
            </table>
          </div>

          {!readOnly && canAuthorizeRetest && (
            <div className="mt-4 rounded-lg border border-[var(--amber)]/25 bg-[var(--amber)]/5 p-4">
              <div className="flex items-start gap-3"><ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-[var(--amber)]" /><div><p className="text-sm font-medium">Authorize one manual retest</p><p className="mt-1 text-xs leading-5 text-[var(--muted)]">This creates exactly one audited generation for a completed or failed bundle. Automatic retries stay disabled. It does not activate v9 or rewards.</p></div></div>
              <div className="mt-3 grid gap-3 sm:grid-cols-2"><Field label="Audit reason"><input value={reason} onChange={(event) => setReason(event.target.value)} /></Field><Field label="Exact confirmation"><input value={confirmation} placeholder={CONFIRMATION_BUNDLE_RETEST_CONFIRMATION} onChange={(event) => setConfirmation(event.target.value)} /></Field></div>
              {error && <p role="alert" className="mt-3 text-xs text-[var(--red)]">{error}</p>}
              <div className="mt-3 flex justify-end"><button type="button" disabled={busy || reason.length < 8 || confirmation !== CONFIRMATION_BUNDLE_RETEST_CONFIRMATION} onClick={authorizeRetest} className="rounded-lg border border-[var(--amber)]/40 px-3 py-2 text-xs font-semibold text-[var(--amber)] hover:bg-[var(--amber-dim)] active:translate-y-px disabled:opacity-40 disabled:hover:bg-transparent">Authorize retest</button></div>
            </div>
          )}
        </div>
      )}
    </article>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block text-xs text-[var(--muted)]"><span className="mb-1.5 block">{label}</span><span className="[&_input]:w-full [&_input]:rounded-lg [&_input]:border [&_input]:border-[var(--line)] [&_input]:bg-black/20 [&_input]:px-3 [&_input]:py-2 [&_input]:text-sm [&_input]:text-[#f3f6f1] [&_input]:disabled:opacity-50 [&_select]:w-full [&_select]:rounded-lg [&_select]:border [&_select]:border-[var(--line)] [&_select]:bg-black/20 [&_select]:px-3 [&_select]:py-2 [&_select]:text-sm [&_select]:text-[#f3f6f1] [&_select]:disabled:opacity-50">{children}</span></label>
}

function NumberField({ label, value, disabled, onChange }: { label: string; value: number; disabled: boolean; onChange: (value: string) => void }) {
  return <Field label={label}><input type="number" min={0} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)} /></Field>
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><dt className="text-xs text-[var(--muted)]">{label}</dt><dd className="mt-1 font-medium tabular-nums">{value}</dd></div>
}

function AuditBlock({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="rounded-lg border border-[var(--line)] bg-[var(--panel)] p-4"><h3 className="mb-3 text-xs font-semibold text-[var(--muted-strong)]">{title}</h3>{children}</div>
}

function AuditLine({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div className="mb-2 flex min-w-0 items-start justify-between gap-3 text-xs last:mb-0"><span className="shrink-0 text-[var(--muted)]">{label}</span><span className={`min-w-0 break-words text-right text-[var(--muted-strong)] ${mono ? 'font-mono break-all' : ''}`}>{value}</span></div>
}

function humanizeGenerationReason(value: ConfirmationBundleView['generation_reason']) {
  return value.replaceAll('_', ' ')
}
