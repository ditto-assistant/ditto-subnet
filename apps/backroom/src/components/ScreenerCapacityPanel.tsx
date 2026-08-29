import { useServerFn } from '@tanstack/react-start'
import {
  AlertTriangle,
  CheckCircle2,
  Hammer,
  Route,
  RefreshCw,
  RotateCcw,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import type {
  ScreenerCapacityNode,
  ScreenerCapacityView,
  ScreenerHostSpecs,
  ScreenerNodeChannelSettings,
  ScreenerNodeChannelSettingsControl,
  ScreenerProviderSettings,
  ScreenerProviderSettingsControl,
  TrustedImageBuild,
} from '../lib/admin.schemas'
import {
  screenerNodeChannelSettingsConfirmation,
  screenerProviderSettingsConfirmation,
} from '../lib/admin.schemas'
import {
  getScreenerCapacity,
  retryTrustedImageBuild,
  updateScreenerProviderSettings,
  updateScreenerNodeChannelSettings,
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

/** The worker's own account of its hardware. Read against `capacity` and the
 * channel limits below it, this is what separates "this node is oversubscribed"
 * from "this node is small" — two states that look identical in a backlog. */
function hostSpecsLine(specs: ScreenerHostSpecs) {
  const memoryGib = Math.round(specs.memory_total_mib / 1024)
  return `${specs.cpu_count} vCPU · ${memoryGib} GiB · ${specs.disk_total_gib} GiB disk`
}

function hostSpecsTitle(specs: ScreenerHostSpecs) {
  const cores =
    specs.cpu_physical_cores === null ? '' : `${specs.cpu_physical_cores} physical cores, `
  const memoryGib = Math.round(specs.memory_total_mib / 1024)
  return (
    `Announced by the worker's own signed heartbeat · ${specs.cpu_count} logical CPUs ` +
    `(${cores}${specs.architecture}) · ${memoryGib} GiB RAM · ${specs.disk_total_gib} GiB disk`
  )
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

function retryableBuildStatus(
  status: TrustedImageBuild['status'],
): status is 'failed' | 'fallback_required' | 'canceled' {
  return status === 'failed' || status === 'fallback_required' || status === 'canceled'
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

type ProviderMode = 'hetzner-overflow' | 'targon-only' | 'gcp-only'
type RoutingPosture = 'hetzner-primary' | 'targon-only' | 'gce-only' | 'mixed'

const HETZNER_PRIORITY = ['hetzner', 'gcp'] as const
const TARGON_PRIORITY = ['targon', 'gcp'] as const
const GCE_PRIORITY = ['gcp'] as const

function providerMode(priority: ScreenerProviderSettings['runtime_provider_priority']): ProviderMode {
  if (priority[0] === 'hetzner') return 'hetzner-overflow'
  return priority[0] === 'targon' ? 'targon-only' : 'gcp-only'
}

function priorityForMode(mode: ProviderMode): ('hetzner' | 'targon' | 'gcp')[] {
  if (mode === 'hetzner-overflow') return [...HETZNER_PRIORITY]
  return mode === 'targon-only' ? [...TARGON_PRIORITY] : [...GCE_PRIORITY]
}

function routingPosture(settings: ScreenerProviderSettings): RoutingPosture {
  const modes = [
    providerMode(settings.build_provider_priority),
    providerMode(settings.runtime_provider_priority),
    providerMode(settings.source_review_provider_priority),
  ]
  if (modes.every((mode) => mode === 'hetzner-overflow')) return 'hetzner-primary'
  if (modes.every((mode) => mode === 'targon-only')) return 'targon-only'
  if (modes.every((mode) => mode === 'gcp-only')) return 'gce-only'
  return 'mixed'
}

function postureChip(posture: RoutingPosture) {
  if (posture === 'hetzner-primary') {
    return {
      label: 'Hetzner base load',
      className: 'bg-[var(--acid-dim)] text-[var(--acid)]',
    }
  }
  if (posture === 'targon-only') {
    return {
      label: 'All lanes Targon-first',
      className: 'bg-[var(--cyan-dim)] text-[var(--cyan)]',
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
  const overflowValid = !settings.gce_overflow_enabled || (
    settings.primary_node_id !== null
    && settings.build_provider_priority[0] === 'hetzner'
    && settings.runtime_provider_priority[0] === 'hetzner'
    && settings.source_review_provider_priority[0] === 'hetzner'
  )
  const ready = overflowValid && reason.trim().length >= 8 && confirmation === expectedConfirmation
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
            Hetzner handles normal work. GCE may claim separate, still-unclaimed submissions only
            during a primary-node outage or sustained backlog overflow. A failed lane parks its
            own attempt and never moves to the next provider as an automatic retry.
          </p>
        </div>
      </div>
      <div className="space-y-5 p-4 sm:p-5">
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
                  ['hetzner-overflow', 'Hetzner + GCE overflow'],
                  ['gcp-only', 'GCE only'],
                  ['targon-only', 'Targon-first + GCE'],
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
        <fieldset disabled={readOnly || loading} className="rounded-lg border border-[var(--line)] p-4">
          <legend className="px-1 text-xs font-semibold">GCE overflow policy</legend>
          <label className="mt-2 flex min-h-11 items-center gap-3 text-sm">
            <input
              type="checkbox"
              checked={settings.gce_overflow_enabled}
              onChange={(event) => setSettings((current) => ({
                ...current,
                gce_overflow_enabled: event.target.checked,
              }))}
              className="h-4 w-4 accent-[var(--acid)]"
            />
            Activate GCE for primary outage or excess unclaimed backlog
          </label>
          <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <label className="text-xs text-[var(--muted)] lg:col-span-2">
              Primary node
              <input
                value={settings.primary_node_id ?? ''}
                onChange={(event) => setSettings((current) => ({
                  ...current,
                  primary_node_id: event.target.value || null,
                }))}
                placeholder="subnet-screener-1"
                className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-base text-white sm:text-sm"
              />
            </label>
            {([
              ['gce_overflow_backlog_multiplier', 'Backlog multiple'],
              ['gce_overflow_min_backlog', 'Minimum backlog'],
              ['gce_overflow_max_instances', 'Maximum GCE'],
            ] as const).map(([field, label]) => (
              <label key={field} className="text-xs text-[var(--muted)]">
                {label}
                <input
                  type="number"
                  min={field === 'gce_overflow_backlog_multiplier' ? 2 : field === 'gce_overflow_min_backlog' ? 1 : 0}
                  max={field === 'gce_overflow_backlog_multiplier' ? 20 : field === 'gce_overflow_min_backlog' ? 1000 : 32}
                  value={settings[field]}
                  onChange={(event) => setSettings((current) => ({
                    ...current,
                    [field]: Number(event.target.value),
                  }))}
                  className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-base tabular-nums text-white sm:text-sm"
                />
              </label>
            ))}
          </div>
          {!overflowValid ? (
            <p role="alert" className="mt-3 text-xs text-[var(--amber)]">
              Enabled overflow requires all three lanes to use Hetzner + GCE overflow and a primary node ID.
            </p>
          ) : null}
        </fieldset>
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

function NodeChannelControl({
  control,
  readOnly,
  onApplied,
}: {
  control: ScreenerNodeChannelSettingsControl
  readOnly: boolean
  onApplied: (control: ScreenerNodeChannelSettingsControl) => void
}) {
  const apply = useServerFn(updateScreenerNodeChannelSettings)
  const [settings, setSettings] = useState(control.current.settings)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const nodeId = control.current.node_id
  const expected = screenerNodeChannelSettingsConfirmation(nodeId, settings)
  const sandboxValid = settings.build_concurrency <= settings.sandbox_slots
    && settings.runtime_concurrency <= settings.sandbox_slots
  const ready = sandboxValid && reason.trim().length >= 8 && confirmation === expected

  const fields: { key: keyof ScreenerNodeChannelSettings; label: string; max: number }[] = [
    { key: 'screening_concurrency', label: 'Full screens', max: 32 },
    { key: 'sandbox_slots', label: 'KVM sandboxes', max: 16 },
    { key: 'build_concurrency', label: 'Builds', max: 16 },
    { key: 'runtime_concurrency', label: 'Runtime smoke', max: 16 },
    { key: 'source_review_concurrency', label: 'Source review', max: 32 },
  ]

  async function submit() {
    if (!ready || readOnly) return
    setLoading(true)
    setError('')
    try {
      onApplied(await apply({
        data: {
          nodeId,
          expectedRevision: control.current.revision,
          settings,
          reason,
          confirmation,
        },
      }))
      setReason('')
      setConfirmation('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to apply node capacity')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4 sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">{nodeId}</h3>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Platform-enforced limits, revision {control.current.revision}. Build and smoke share the sandbox ceiling.
          </p>
        </div>
        {control.usage ? (
          <span className="text-xs tabular-nums text-[var(--muted-strong)]">
            {control.usage.screening_active}/{settings.screening_concurrency} screens · {control.usage.sandbox_active}/{settings.sandbox_slots} sandboxes
          </span>
        ) : null}
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {fields.map(({ key, label, max }) => (
          <label key={key} className="text-xs text-[var(--muted)]">
            {label}
            <input
              type="number"
              min={0}
              max={max}
              value={settings[key]}
              disabled={readOnly || loading}
              onChange={(event) => setSettings((current) => ({
                ...current,
                [key]: Number(event.target.value),
              }))}
              className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-base tabular-nums text-white disabled:opacity-50 sm:text-sm"
            />
          </label>
        ))}
      </div>
      {!sandboxValid ? (
        <p role="alert" className="mt-3 text-xs text-[var(--amber)]">
          Build and runtime limits cannot exceed the shared KVM sandbox ceiling.
        </p>
      ) : null}
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <label className="text-xs text-[var(--muted)]">
          Audit reason
          <input
            value={reason}
            disabled={readOnly || loading}
            onChange={(event) => setReason(event.target.value)}
            className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-base text-white disabled:opacity-50 sm:text-sm"
          />
        </label>
        <label className="text-xs text-[var(--muted)]">
          Type to confirm
          <input
            value={confirmation}
            disabled={readOnly || loading}
            onChange={(event) => setConfirmation(event.target.value)}
            placeholder={expected}
            className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-base text-white disabled:opacity-50 sm:text-sm"
          />
          <span className="mt-2 block break-all font-mono text-[10px]">{expected}</span>
        </label>
      </div>
      {error ? <p role="alert" className="mt-3 text-xs text-[var(--red)]">{error}</p> : null}
      <button
        type="button"
        onClick={() => void submit()}
        disabled={!ready || loading || readOnly}
        className="mt-4 inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-[var(--ink)] disabled:opacity-35"
      >
        <CheckCircle2 className="h-3.5 w-3.5" />
        Append node capacity revision
      </button>
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
  const retryBuild = useServerFn(retryTrustedImageBuild)
  const [state, setState] = useState(initialState)
  const [loading, setLoading] = useState(false)
  const [retryingBuildId, setRetryingBuildId] = useState<string | null>(null)
  const [retryReasons, setRetryReasons] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const snapshot = state.snapshot
  const cleanupEvents = state.events.filter(
    (event) => event.provider === 'targon' && event.event_type === 'provider_cleanup_required',
  )
  const latestCleanup = cleanupEvents[0]
  const visibleProviderJobs = state.provider_jobs.filter(
    (job) => !(job.lane === 'runtime' && job.status === 'skipped'),
  )
  const buildMode = providerMode(
    state.provider_control.current.settings.build_provider_priority,
  )

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

  async function retryTrustedBuild(build: TrustedImageBuild) {
    if (readOnly || !retryableBuildStatus(build.status)) return
    const reason = (retryReasons[build.build_id] ?? '').trim()
    if (reason.length < 8) return
    setRetryingBuildId(build.build_id)
    setError('')
    try {
      const updated = await retryBuild({
        data: {
          buildId: build.build_id,
          expectedStatus: build.status,
          expectedAttemptCount: build.attempt_count,
          reason,
        },
      })
      setState((current) => ({
        ...current,
        builds: current.builds.map((candidate) =>
          candidate.build_id === updated.build_id ? updated : candidate,
        ),
      }))
      setRetryReasons((current) => ({ ...current, [build.build_id]: '' }))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to retry trusted image build')
    } finally {
      setRetryingBuildId(null)
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
            The independent GCE watchdog may conservatively scale out while the controller is
            stale and queued work exists. Hetzner node controls and the audited overflow threshold
            will appear again when the controller heartbeat recovers.
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
      <section className="space-y-3">
        <div>
          <h2 className="text-sm font-semibold">Per-node concurrency</h2>
          <p className="mt-1 max-w-[72ch] text-xs leading-5 text-[var(--muted)]">
            Full screening workers may outnumber KVM slots. Platform enforces both ceilings so the 64 GB host can keep submissions moving without overcommitting build memory.
          </p>
        </div>
        {state.node_controls.length === 0 ? (
          <p className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-5 text-sm text-[var(--muted)]">
            No enrolled node exposes channel controls yet.
          </p>
        ) : state.node_controls.map((control) => (
          <NodeChannelControl
            key={control.current.node_id}
            control={control}
            readOnly={readOnly}
            onApplied={(updated) => setState((current) => ({
              ...current,
              node_controls: current.node_controls.map((candidate) => (
                candidate.current.node_id === updated.current.node_id ? updated : candidate
              )),
            }))}
          />
        ))}
      </section>
      <div className="flex justify-end">
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
            provider result is authoritative for its lane; no secondary provider runs on failure.
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
                  <th className="px-4 py-3 font-medium">Node / resource</th>
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
                    <td
                      className="px-4 py-3.5 font-mono text-[var(--muted-strong)]"
                      title={job.node_id ?? job.provider_resource_id ?? undefined}
                    >
                      {job.node_id
                        ? shortIdentity(job.node_id)
                        : job.provider_resource_id
                          ? shortIdentity(job.provider_resource_id)
                          : '—'}
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
              Trusted screener release-image builds remain separate from the miner submission
              route above. A retry is always an explicit, guarded operator action. Hostile miner
              runtimes never enter this trusted lane.
            </p>
          </div>
        </div>
        {state.builds.length === 0 ? (
          <p className="p-5 text-sm text-[var(--muted)]">No trusted screener image build has been queued.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1060px] text-left text-xs">
              <thead className="bg-[var(--panel-soft)] text-[var(--muted)]">
                <tr>
                  <th className="px-4 py-3 font-medium sm:px-5">Revision</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Provider</th>
                  <th className="px-4 py-3 font-medium">Attempts</th>
                  <th className="px-4 py-3 font-medium">Image</th>
                  <th className="px-4 py-3 font-medium">Updated</th>
                  <th className="px-4 py-3 font-medium">Manual retry</th>
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
                    <td className="px-4 py-3.5">
                      {retryableBuildStatus(build.status) ? (
                        <div className="flex min-w-64 items-center gap-2">
                          <input
                            aria-label={`Retry reason for ${build.source_sha.slice(0, 12)}`}
                            value={retryReasons[build.build_id] ?? ''}
                            onChange={(event) =>
                              setRetryReasons((current) => ({
                                ...current,
                                [build.build_id]: event.target.value,
                              }))
                            }
                            disabled={readOnly || retryingBuildId === build.build_id}
                            placeholder="Audit reason"
                            className="min-h-10 min-w-0 flex-1 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-xs text-white disabled:opacity-40"
                          />
                          <button
                            type="button"
                            onClick={() => void retryTrustedBuild(build)}
                            disabled={
                              readOnly ||
                              retryingBuildId !== null ||
                              (retryReasons[build.build_id] ?? '').trim().length < 8
                            }
                            className="inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-[var(--line)] px-3 font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-35"
                          >
                            <RotateCcw className="h-3.5 w-3.5" />
                            Retry
                          </button>
                        </div>
                      ) : (
                        <span className="text-[var(--muted)]">—</span>
                      )}
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
          <h2 className="text-sm font-semibold">Enrolled workers</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Each Hetzner node has one identity and revisioned concurrency limits. GCE overflow
            workers keep separate identities and claim only new, unowned submissions. Leftover
            nested-Docker Targon slots are drained and cannot claim new jobs.
          </p>
        </div>
        {state.nodes.length === 0 ? (
          <p className="p-5 text-sm text-[var(--muted)]">
            No dedicated node has enrolled yet. GCE overflow workers remain visible through their
            heartbeat-derived identities when the MIG is active.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-left text-xs">
              <thead className="bg-[var(--panel-soft)] text-[var(--muted)]">
                <tr>
                  <th className="px-4 py-3 font-medium sm:px-5">Node</th>
                  <th className="px-4 py-3 font-medium">Provider</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Announced host</th>
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
                      {node.host_specs ? (
                        <span title={hostSpecsTitle(node.host_specs)}>
                          {hostSpecsLine(node.host_specs)}
                        </span>
                      ) : (
                        <span className="text-[var(--muted)]">Not announced</span>
                      )}
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
