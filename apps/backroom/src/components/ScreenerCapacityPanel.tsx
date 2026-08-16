import { useServerFn } from '@tanstack/react-start'
import {
  AlertTriangle,
  CheckCircle2,
  Cloud,
  Container,
  Hammer,
  Route,
  RefreshCw,
  ServerCog,
} from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import type {
  ScreenerCapacityNode,
  ScreenerCapacityView,
  ScreenerProviderSettings,
  ScreenerProviderSettingsControl,
  TrustedImageBuild,
} from '../lib/admin.schemas'
import { screenerProviderSettingsConfirmation } from '../lib/admin.schemas'
import {
  getScreenerCapacity,
  updateScreenerProviderSettings,
} from '../server/admin.functions'

function formatWhen(value: string | null) {
  if (!value) return 'Never'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'UTC',
    timeZoneName: 'short',
  }).format(parsed)
}

function shortIdentity(value: string) {
  return value.length > 24 ? `${value.slice(0, 14)}…${value.slice(-6)}` : value
}

function nodeTone(status: ScreenerCapacityNode['status']) {
  if (status === 'active') return 'bg-[var(--acid-dim)] text-[var(--acid)]'
  if (status === 'draining') return 'bg-[var(--amber-dim)] text-[var(--amber)]'
  return 'bg-[var(--red-dim)] text-[var(--red)]'
}

function buildTone(status: TrustedImageBuild['status']) {
  if (status === 'succeeded') return 'bg-[var(--acid-dim)] text-[var(--acid)]'
  if (status === 'failed' || status === 'canceled') return 'bg-[var(--red-dim)] text-[var(--red)]'
  if (status === 'fallback_required') return 'bg-[var(--amber-dim)] text-[var(--amber)]'
  return 'bg-[var(--cyan-dim)] text-[var(--cyan)]'
}

function providerJobTone(status: string) {
  if (status === 'succeeded' || status === 'consumed') {
    return 'bg-[var(--acid-dim)] text-[var(--acid)]'
  }
  if (status === 'skipped') {
    return 'bg-white/[0.05] text-[var(--muted)]'
  }
  if (status === 'fallback_required' || status === 'cleanup_required') {
    return 'bg-[var(--amber-dim)] text-[var(--amber)]'
  }
  if (status === 'failed' || status === 'canceled' || status === 'error') {
    return 'bg-[var(--red-dim)] text-[var(--red)]'
  }
  return 'bg-[var(--cyan-dim)] text-[var(--cyan)]'
}

function capabilityGuidance(reason: string | null) {
  if (reason === 'TARGON_CAPABILITY_ATTESTATION_EXPIRED') {
    return 'The safety record expired; this is not a Targon API outage. A fresh hostile-runtime probe and reviewed GO result are required before full workers can start.'
  }
  if (reason?.includes('ROOTLESSKIT') || reason?.includes('NESTED_RUNTIME')) {
    return 'The nested hostile-runtime boundary did not pass its safety probe. Keep full workers on GCE until a fresh probe returns GO.'
  }
  return 'A fresh reviewed hostile-runtime capability is required before full workers can start.'
}

type ProviderMode = 'targon-first' | 'gcp-only'
type RoutingPosture = 'targon-first' | 'gce-only' | 'mixed'

const TARGON_FIRST_PRIORITY: ('targon' | 'gcp')[] = ['targon', 'gcp']
const GCE_ONLY_PRIORITY: ('targon' | 'gcp')[] = ['gcp']

function providerMode(priority: ScreenerProviderSettings['runtime_provider_priority']): ProviderMode {
  // Legacy ['gcp', 'targon'] is Targon-off / GCE only; first-provider wins.
  return priority[0] === 'targon' ? 'targon-first' : 'gcp-only'
}

function priorityForMode(mode: ProviderMode): ('targon' | 'gcp')[] {
  return mode === 'targon-first' ? [...TARGON_FIRST_PRIORITY] : [...GCE_ONLY_PRIORITY]
}

function routingPosture(settings: ScreenerProviderSettings): RoutingPosture {
  const modes = [
    providerMode(settings.build_provider_priority),
    providerMode(settings.runtime_provider_priority),
    providerMode(settings.source_review_provider_priority),
  ]
  if (modes.every((mode) => mode === 'targon-first')) return 'targon-first'
  if (modes.every((mode) => mode === 'gcp-only')) return 'gce-only'
  return 'mixed'
}

function postureChip(posture: RoutingPosture) {
  if (posture === 'targon-first') {
    return {
      label: 'All lanes Targon-first',
      className: 'bg-[var(--acid-dim)] text-[var(--acid)]',
    }
  }
  if (posture === 'gce-only') {
    return {
      label: 'All lanes GCE only',
      className: 'bg-[var(--amber-dim)] text-[var(--amber)]',
    }
  }
  return {
    label: 'Mixed lane routing',
    className: 'bg-[var(--cyan-dim)] text-[var(--cyan)]',
  }
}

function ProviderRoutingControl({
  control,
  appliedRevision,
  readOnly,
  onApplied,
}: {
  control: ScreenerProviderSettingsControl
  appliedRevision: number | null
  readOnly: boolean
  onApplied: (control: ScreenerProviderSettingsControl) => void
}) {
  const apply = useServerFn(updateScreenerProviderSettings)
  const [settings, setSettings] = useState(control.current.settings)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const expectedConfirmation = screenerProviderSettingsConfirmation(settings)
  const ready = reason.trim().length >= 8 && confirmation === expectedConfirmation
  const caughtUp = appliedRevision === control.current.revision

  useEffect(() => {
    setSettings(control.current.settings)
    setConfirmation('')
  }, [control.current.revision, control.current.settings])

  const setMode = (lane: 'build' | 'runtime' | 'source-review', mode: ProviderMode) => {
    const priority = priorityForMode(mode)
    const field =
      lane === 'build'
        ? 'build_provider_priority'
        : lane === 'runtime'
          ? 'runtime_provider_priority'
          : 'source_review_provider_priority'
    setSettings((current) => ({
      ...current,
      [field]: priority,
    }))
    setConfirmation('')
  }

  const draftAllLanes = (priority: ('targon' | 'gcp')[]) => {
    setSettings({
      build_provider_priority: priority,
      runtime_provider_priority: priority,
      source_review_provider_priority: priority,
    })
    setConfirmation('')
  }

  const posture = routingPosture(settings)
  const chip = postureChip(posture)

  const submit = async () => {
    if (!ready || readOnly) return
    setLoading(true)
    setError('')
    try {
      const next = await apply({
        data: {
          expectedRevision: control.current.revision,
          settings,
          reason,
          confirmation,
        },
      })
      onApplied(next)
      setSettings(next.current.settings)
      setReason('')
      setConfirmation('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to apply provider settings')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="flex items-start gap-3 border-b border-[var(--line)] p-4 sm:p-5">
        <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--acid-dim)] text-[var(--acid)]">
          <Route className="h-4 w-4" />
        </div>
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold">Provider routing</h2>
            <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${caughtUp ? 'bg-[var(--acid-dim)] text-[var(--acid)]' : 'bg-[var(--amber-dim)] text-[var(--amber)]'}`}>
              {caughtUp ? `Applied r${control.current.revision}` : `Awaiting controller r${control.current.revision}`}
            </span>
            <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${chip.className}`}>
              {chip.label}
            </span>
          </div>
          <p className="mt-1 max-w-[78ch] text-xs leading-5 text-[var(--muted)]">
            Builds, runtime smoke, and source review are independent lanes. Targon is enabled only
            when a lane starts with Targon. Any other list, including legacy <code>gcp&gt;targon</code>,
            is GCE only. GCP VMs stay as the safety path until Targon is validated; then this
            cutover UI and the GCE MIGs can be removed.
          </p>
        </div>
      </div>
      <div className="space-y-5 p-4 sm:p-5">
        <div className="rounded-lg border border-[var(--amber)]/40 bg-[var(--amber-dim)] p-4">
          <p className="text-xs font-semibold text-[var(--amber)]">Emergency cutover</p>
          <p className="mt-1 max-w-[78ch] text-xs leading-5 text-[var(--muted-strong)]">
            Restores the old GCE screening path immediately after the existing audited apply; no
            deploy. Targon rentals stop being claimed; GCE workers remain the authority.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={readOnly || loading}
              onClick={() => draftAllLanes(GCE_ONLY_PRIORITY)}
              className="inline-flex min-h-11 items-center justify-center rounded-lg border border-[var(--amber)] px-3 text-xs font-semibold text-[var(--amber)] disabled:opacity-40"
            >
              Cut over to GCE only
            </button>
            <button
              type="button"
              disabled={readOnly || loading}
              onClick={() => draftAllLanes(TARGON_FIRST_PRIORITY)}
              className="inline-flex min-h-11 items-center justify-center rounded-lg border border-[var(--line)] px-3 text-xs font-semibold text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
            >
              Restore Targon-first
            </button>
          </div>
        </div>
        <div className="grid gap-4 lg:grid-cols-3">
          {([
            ['build', 'Remote image builders', settings.build_provider_priority],
            ['runtime', 'Runtime smoke checks', settings.runtime_provider_priority],
            ['source-review', 'Source review', settings.source_review_provider_priority],
          ] as const).map(([lane, label, priority]) => (
            <fieldset key={lane} disabled={readOnly || loading} className="rounded-lg border border-[var(--line)] p-4">
              <legend className="px-1 text-xs font-semibold">{label}</legend>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                {([
                  ['targon-first', 'Targon first'],
                  ['gcp-only', 'Targon off (GCE only)'],
                ] as const).map(([mode, modeLabel]) => (
                  <button
                    key={mode}
                    type="button"
                    aria-pressed={providerMode(priority) === mode}
                    onClick={() => setMode(lane, mode)}
                    className={`min-h-11 rounded-lg border px-3 py-2 text-left text-xs disabled:opacity-40 ${providerMode(priority) === mode ? 'border-[var(--acid)] bg-[var(--acid-dim)] text-[var(--acid)]' : 'border-[var(--line)] text-[var(--muted-strong)] hover:bg-white/5'}`}
                  >
                    {modeLabel}
                  </button>
                ))}
              </div>
            </fieldset>
          ))}
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-[var(--muted)]">
            Audit reason
            <textarea
              rows={3}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              disabled={readOnly || loading}
              className="mt-1.5 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 py-2 text-sm text-white disabled:opacity-50"
            />
          </label>
          <label className="text-xs text-[var(--muted)]">
            Type to confirm
            <input
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              disabled={readOnly || loading}
              placeholder={expectedConfirmation}
              className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white disabled:opacity-50"
            />
            <span className="mt-2 block break-all font-mono text-[10px]">{expectedConfirmation}</span>
          </label>
        </div>
        {error ? <p role="alert" className="text-xs text-[var(--red)]">{error}</p> : null}
        <button
          type="button"
          onClick={() => void submit()}
          disabled={!ready || loading || readOnly}
          className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-[var(--ink)] disabled:opacity-35"
        >
          <CheckCircle2 className="h-3.5 w-3.5" />
          Append provider revision
        </button>
      </div>
    </section>
  )
}

export function ScreenerCapacityPanel({
  initialState,
  readOnly,
}: {
  initialState: ScreenerCapacityView
  readOnly: boolean
}) {
  const fetchCapacity = useServerFn(getScreenerCapacity)
  const [state, setState] = useState(initialState)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const snapshot = state.snapshot
  const leaseFresh = snapshot
    ? new Date(snapshot.controller_lease_expires_at).getTime() > Date.now()
    : false
  const cleanupEvents = state.events.filter(
    (event) => event.provider === 'targon' && event.event_type === 'provider_cleanup_required',
  )
  const latestCleanup = cleanupEvents[0]
  const visibleProviderJobs = state.provider_jobs.filter(
    (job) => !(job.lane === 'runtime' && job.status === 'skipped'),
  )
  const runtimeMode = providerMode(
    state.provider_control.current.settings.runtime_provider_priority,
  )
  const buildMode = providerMode(
    state.provider_control.current.settings.build_provider_priority,
  )
  const sourceReviewMode = providerMode(
    state.provider_control.current.settings.source_review_provider_priority,
  )
  const targonWorkerBlocked =
    snapshot?.targon_capability !== 'go'
    || runtimeMode !== 'targon-first'
    || sourceReviewMode !== 'targon-first'

  async function refresh() {
    setLoading(true)
    setError('')
    try {
      setState(await fetchCapacity())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh screener capacity')
    } finally {
      setLoading(false)
    }
  }

  if (!snapshot) {
    return (
      <div className="mt-6 space-y-5">
        <ProviderRoutingControl
          control={state.provider_control}
          appliedRevision={null}
          readOnly={readOnly}
          onApplied={(provider_control) => setState((current) => ({ ...current, provider_control }))}
        />
        <section className="rounded-xl border border-[var(--amber)]/30 bg-[var(--amber-dim)] p-6">
          <AlertTriangle className="h-5 w-5 text-[var(--amber)]" />
          <h2 className="mt-3 text-base font-semibold">Capacity controller has not checked in</h2>
          <p className="mt-2 max-w-[70ch] text-sm leading-6 text-[var(--muted-strong)]">
            The independent GCE watchdog is eligible to scale out when queue depth is nonzero.
            No Targon workload will be started without a valid capability attestation.
          </p>
        </section>
      </div>
    )
  }

  return (
    <div className="mt-6 space-y-5">
      <ProviderRoutingControl
        control={state.provider_control}
        appliedRevision={snapshot.provider_settings_revision}
        readOnly={readOnly}
        onApplied={(provider_control) => setState((current) => ({ ...current, provider_control }))}
      />
      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
              <ServerCog className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-semibold">Controller authority</h2>
              <p className="mt-1 max-w-[72ch] text-xs leading-5 text-[var(--muted)]">
                One fenced writer applies provider revision {snapshot.provider_settings_revision}
                {' '}and sends only residual demand to the lower-priority lane. The GCP watchdog
                may scale out only after this lease expires.
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
            Refresh capacity
          </button>
        </div>

        <div className="p-4 sm:p-5">
          <div
            className={`flex items-start gap-3 rounded-lg border p-4 ${
              leaseFresh
                ? 'border-[#46552f] bg-[var(--acid-dim)]'
                : 'border-[var(--red)]/30 bg-[var(--red-dim)]'
            }`}
          >
            {leaseFresh ? (
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-[var(--acid)]" />
            ) : (
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--red)]" />
            )}
            <div>
              <p className="text-sm font-medium">
                {leaseFresh ? 'Normal writer lease is healthy' : 'Normal writer lease is stale'}
              </p>
              <p className="mt-1 text-xs leading-5 text-[var(--muted-strong)]">
                Epoch {shortIdentity(snapshot.controller_epoch)} · heartbeat{' '}
                {formatWhen(snapshot.controller_heartbeat_at)} · lease expires{' '}
                {formatWhen(snapshot.controller_lease_expires_at)}
              </p>
            </div>
          </div>

          {targonWorkerBlocked ? (
            <div className="mt-4 flex items-start gap-3 rounded-lg border border-[var(--amber)]/30 bg-[var(--amber-dim)] p-4">
              <Container className="mt-0.5 h-4 w-4 shrink-0 text-[var(--amber)]" />
              <div>
                <p className="text-sm font-medium">Full Targon worker lane is blocked</p>
                <p className="mt-1 break-words text-xs leading-5 text-[var(--muted-strong)]">
                  Capability is {snapshot.targon_capability.toUpperCase()} and the configured lane
                  is runtime {runtimeMode.replaceAll('-', ' ')} / source review{' '}
                  {sourceReviewMode.replaceAll('-', ' ')}. This blocks full Targon screener
                  workers, not the independently controlled credential-minimal Kaniko build lane.
                  GCE receives all screening-worker demand and completes the health, source, and
                  policy gates. Reason:{' '}
                  <span className="break-all">
                    {snapshot.fallback_reason ?? 'No current capability attestation'}
                  </span>
                  .
                </p>
                {snapshot.targon_capability !== 'go' ? (
                  <p className="mt-2 text-xs leading-5 text-[var(--muted-strong)]">
                    {capabilityGuidance(snapshot.fallback_reason)}
                  </p>
                ) : null}
              </div>
            </div>
          ) : null}

          <dl className="mt-5 grid gap-px overflow-hidden rounded-lg border border-[var(--line)] bg-[var(--line)] sm:grid-cols-3 lg:grid-cols-6">
            {[
              ['Runnable', snapshot.runnable_backlog],
              ['Active leases', snapshot.active_leases],
              ['Desired slots', snapshot.desired_slots],
              ['Global cap', snapshot.global_cap],
              ['Targon ready', snapshot.targon_healthy],
              ['GCE target', snapshot.gce_target],
            ].map(([label, value]) => (
              <div key={label} className="bg-[var(--panel-soft)] px-4 py-3">
                <dt className="text-[11px] text-[var(--muted)]">{label}</dt>
                <dd className="mt-1 text-lg font-semibold tabular-nums">{value}</dd>
              </div>
            ))}
          </dl>
        </div>
      </section>

      {latestCleanup ? (
        <section className="rounded-xl border border-[var(--amber)]/30 bg-[var(--amber-dim)] p-4 sm:p-5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--amber)]" />
            <div>
              <h2 className="text-sm font-semibold">Targon provider cleanup is incomplete</h2>
              <p className="mt-1 max-w-[72ch] text-xs leading-5 text-[var(--muted-strong)]">
                {cleanupEvents.length} cleanup-required {cleanupEvents.length === 1 ? 'event' : 'events'}
                {' '}appear in the latest {state.events.length}-event audit window. The affected
                one-shot Targon rentals were suspended at zero replicas, but provider deletion
                still needs retry. This may affect build, runtime, or source-review jobs. Latest
                event {formatWhen(latestCleanup.created_at)}.
              </p>
            </div>
          </div>
        </section>
      ) : null}

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-4 py-4 sm:px-5">
          <h2 className="text-sm font-semibold">Recent provider jobs</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Redacted build, direct-image runtime, and source-review lifecycle state. A remote
            runtime result remains advisory until the GCE isolated smoke also passes.
          </p>
        </div>
        {visibleProviderJobs.length === 0 ? (
          <p className="p-5 text-sm text-[var(--muted)]">No one-shot provider job has been recorded.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="bg-[var(--panel-soft)] text-[var(--muted)]">
                <tr>
                  <th className="px-4 py-3 font-medium sm:px-5">Lane</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Rental</th>
                  <th className="px-4 py-3 font-medium">Provenance</th>
                  <th className="px-4 py-3 font-medium">Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--line)]">
                {visibleProviderJobs.slice(0, 30).map((job) => (
                  <tr key={`${job.lane}-${job.job_id}`}>
                    <td className="px-4 py-3.5 font-medium capitalize sm:px-5">
                      {job.lane.replaceAll('_', ' ')}
                    </td>
                    <td className="px-4 py-3.5">
                      <span className={`rounded-full px-2 py-1 ${providerJobTone(job.status)}`}>
                        {job.status.replaceAll('_', ' ')}
                      </span>
                      {job.error_code ? <p className="mt-1 break-all text-[10px] text-[var(--muted)]">{job.error_code}</p> : null}
                    </td>
                    <td className="px-4 py-3.5 font-mono text-[var(--muted-strong)]">
                      {job.provider_resource_id ? shortIdentity(job.provider_resource_id) : '—'}
                    </td>
                    <td className="px-4 py-3.5 font-mono text-[var(--muted-strong)]">
                      {job.image_reference ? shortIdentity(job.image_reference) : shortIdentity(job.job_id)}
                    </td>
                    <td className="px-4 py-3.5 text-[var(--muted-strong)]">{formatWhen(job.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex items-start gap-3 border-b border-[var(--line)] px-4 py-4 sm:px-5">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
            <Hammer className="h-4 w-4" />
          </span>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-semibold">Trusted image builds</h2>
              <span className="rounded-full bg-[var(--cyan-dim)] px-2 py-0.5 text-[10px] font-medium text-[var(--cyan)]">
                {buildMode.replaceAll('-', ' ')}
              </span>
            </div>
            <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
              Release and miner-image builds follow the independent builder priority above.
              Targon uses a dedicated credential-minimal Kaniko rental; GCP runs the allowlisted
              fallback. Hostile miner runtimes never enter this trusted lane.
            </p>
          </div>
        </div>
        {state.builds.length === 0 ? (
          <p className="p-5 text-sm text-[var(--muted)]">No trusted screener image build has been queued.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[840px] text-left text-xs">
              <thead className="bg-[var(--panel-soft)] text-[var(--muted)]">
                <tr>
                  <th className="px-4 py-3 font-medium sm:px-5">Revision</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Provider</th>
                  <th className="px-4 py-3 font-medium">Attempts</th>
                  <th className="px-4 py-3 font-medium">Image</th>
                  <th className="px-4 py-3 font-medium">Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--line)]">
                {state.builds.map((build) => (
                  <tr key={build.build_id}>
                    <td className="px-4 py-3.5 font-mono sm:px-5" title={build.source_sha}>
                      {build.source_sha.slice(0, 12)}
                    </td>
                    <td className="px-4 py-3.5">
                      <span className={`rounded-full px-2 py-1 font-medium ${buildTone(build.status)}`}>
                        {build.status.replaceAll('_', ' ')}
                      </span>
                      {build.error_code ? (
                        <p className="mt-1 max-w-60 break-all text-[10px] text-[var(--muted)]">
                          {build.error_code}
                        </p>
                      ) : null}
                    </td>
                    <td className="px-4 py-3.5 capitalize text-[var(--muted-strong)]">
                      {build.provider ?? 'Waiting'}
                    </td>
                    <td className="px-4 py-3.5 tabular-nums text-[var(--muted-strong)]">
                      {build.attempt_count}
                    </td>
                    <td className="px-4 py-3.5 font-mono text-[var(--muted-strong)]" title={build.image_digest ?? build.destination}>
                      {build.image_digest ? shortIdentity(build.image_digest) : shortIdentity(build.destination)}
                    </td>
                    <td className="px-4 py-3.5 text-[var(--muted-strong)]">
                      {formatWhen(build.updated_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-4 py-4 sm:px-5">
          <h2 className="text-sm font-semibold">Provider allocation</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Pending capacity is counted before fallback so Targon and GCE never both scale to
            the full queue. Advertised inventory is available supply, not an active worker count;
            zero healthy workers is expected when desired slots are zero.
          </p>
        </div>
        <div className="divide-y divide-[var(--line)]">
          <ProviderRow
            icon={<Container className="h-4 w-4" />}
            name="Targon"
            detail={`${snapshot.targon_available} CPU rentals advertised; full workers require a GO capability`}
            values={{
              healthy: snapshot.targon_healthy,
              pending: snapshot.targon_pending,
              draining: snapshot.targon_draining,
            }}
          />
          <ProviderRow
            icon={<Cloud className="h-4 w-4" />}
            name="Google Compute Engine"
            detail="Regional managed instance group; zero is the steady idle target"
            values={{
              healthy: snapshot.gce_healthy,
              pending: snapshot.gce_pending,
              draining: snapshot.gce_draining,
            }}
          />
        </div>
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-4 py-4 sm:px-5">
          <h2 className="text-sm font-semibold">Enrolled workers</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Each worker has a unique hotkey and rotating bearer authority. Draining and revoked
            nodes cannot claim new jobs.
          </p>
        </div>
        {state.nodes.length === 0 ? (
          <p className="p-5 text-sm text-[var(--muted)]">
            No federated worker has enrolled yet. Legacy GCE workers remain visible on the
            public screener heartbeat surface during migration.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="bg-[var(--panel-soft)] text-[var(--muted)]">
                <tr>
                  <th className="px-4 py-3 font-medium sm:px-5">Node</th>
                  <th className="px-4 py-3 font-medium">Provider</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Version</th>
                  <th className="px-4 py-3 font-medium">Current phase</th>
                  <th className="px-4 py-3 font-medium">Last heartbeat</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--line)]">
                {state.nodes.map((node) => (
                  <tr key={node.node_id}>
                    <td className="px-4 py-3.5 font-medium sm:px-5">
                      {shortIdentity(node.node_id)}
                    </td>
                    <td className="px-4 py-3.5 capitalize text-[var(--muted-strong)]">
                      {node.provider}
                    </td>
                    <td className="px-4 py-3.5">
                      <span className={`rounded-full px-2 py-1 font-medium ${nodeTone(node.status)}`}>
                        {node.status}
                      </span>
                    </td>
                    <td className="px-4 py-3.5 text-[var(--muted-strong)]">
                      {node.software_version ?? '—'}
                    </td>
                    <td className="px-4 py-3.5 text-[var(--muted-strong)]">
                      {node.current_phase ?? 'No heartbeat'}
                    </td>
                    <td className="px-4 py-3.5 text-[var(--muted-strong)]">
                      {formatWhen(node.heartbeat_seen_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-4 py-4 sm:px-5">
          <h2 className="text-sm font-semibold">Recent capacity events</h2>
        </div>
        {state.events.length === 0 ? (
          <p className="p-5 text-sm text-[var(--muted)]">No capacity changes recorded.</p>
        ) : (
          <ol className="divide-y divide-[var(--line)]">
            {state.events.map((event) => (
              <li key={event.event_id} className="flex gap-4 px-4 py-3.5 sm:px-5">
                <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-[var(--cyan)]" />
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-medium">{event.detail}</p>
                  <p className="mt-1 text-[11px] text-[var(--muted)]">
                    {event.event_type} · {event.provider ?? 'controller'} ·{' '}
                    {formatWhen(event.created_at)}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      {error ? (
        <p role="alert" className="rounded-lg border border-[var(--red)]/30 bg-[var(--red-dim)] p-4 text-sm text-[var(--red)]">
          {error}
        </p>
      ) : null}
    </div>
  )
}

function ProviderRow({
  icon,
  name,
  detail,
  values,
}: {
  icon: ReactNode
  name: string
  detail: string
  values: { healthy: number; pending: number; draining: number }
}) {
  return (
    <div className="flex flex-col gap-4 px-4 py-4 sm:flex-row sm:items-center sm:px-5">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-white/[0.05] text-[var(--muted-strong)]">
          {icon}
        </span>
        <div>
          <p className="text-sm font-medium">{name}</p>
          <p className="mt-0.5 text-xs text-[var(--muted)]">{detail}</p>
        </div>
      </div>
      <dl className="flex gap-6 text-xs sm:text-right">
        <div>
          <dt className="text-[var(--muted)]">Healthy</dt>
          <dd className="mt-1 font-semibold tabular-nums">{values.healthy}</dd>
        </div>
        <div>
          <dt className="text-[var(--muted)]">Pending</dt>
          <dd className="mt-1 font-semibold tabular-nums">{values.pending}</dd>
        </div>
        <div>
          <dt className="text-[var(--muted)]">Draining</dt>
          <dd className="mt-1 font-semibold tabular-nums">{values.draining}</dd>
        </div>
      </dl>
    </div>
  )
}
