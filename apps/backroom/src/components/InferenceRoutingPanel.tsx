import { useServerFn } from '@tanstack/react-start'
import { ArrowDown, ArrowUp, Check, RefreshCw, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import {
  inferencePolicyConfirmation,
  inferenceRouteConfirmation,
  type InferenceRoute,
  type InferenceRouteCalibrationAction,
  type InferenceRouteIdentity,
  type InferenceRoutingAudit,
  type InferenceRoutingInventory,
  type InferenceRoutingPolicy,
  type InferenceProviderTelemetry,
} from '../lib/admin.schemas'
import {
  calibrateInferenceRoute,
  listInferenceRoutes,
  updateInferenceRoutingPolicy,
} from '../server/admin.functions'

const actions: InferenceRouteCalibrationAction[] = ['eligible', 'shadow', 'disabled']

function statusClass(status: string) {
  if (
    status === 'healthy' ||
    status === 'eligible' ||
    status === 'enabled' ||
    status === 'adaptive'
  ) {
    return 'border-[#4b602d] bg-[var(--acid-dim)] text-[var(--acid)]'
  }
  if (status === 'degraded' || status === 'shadow' || status === 'discovered') {
    return 'border-[#654e2b] bg-[var(--amber-dim)] text-[var(--amber)]'
  }
  if (status === 'aggregate') {
    return 'border-[var(--cyan)]/35 bg-[var(--cyan-dim)] text-[var(--cyan)]'
  }
  return 'border-[#63302d] bg-[var(--red-dim)] text-[var(--red)]'
}

function Status({ value }: { value: string }) {
  return (
    <span
      className={`inline-flex rounded-full border px-2 py-1 text-[10px] font-semibold capitalize ${statusClass(value)}`}
    >
      {value}
    </span>
  )
}

function metric(value: number | null, digits = 1) {
  return value === null ? '—' : value.toFixed(digits)
}

function percent(value: number | null) {
  return value === null ? '—' : `${(value * 100).toFixed(1)}%`
}

function perMillion(value: number | null) {
  return value === null ? '—' : `$${(value * 1_000_000).toFixed(3)}`
}

const utcDateTime = new Intl.DateTimeFormat('en-US', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  second: '2-digit',
  timeZone: 'UTC',
  timeZoneName: 'short',
})

function formatUtc(value: string) {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : utcDateTime.format(parsed)
}

function RouteActionEditor({
  route,
  action,
  readOnly,
  onClose,
  onUpdated,
}: {
  route: InferenceRoute
  action: InferenceRouteCalibrationAction
  readOnly: boolean
  onClose: () => void
  onUpdated: (inventory: InferenceRoutingInventory) => void
}) {
  const submitCalibration = useServerFn(calibrateInferenceRoute)
  const [manifest, setManifest] = useState(route.calibration_manifest_sha256 ?? '')
  const [samples, setSamples] = useState(
    route.calibration_sample_count > 0 ? String(route.calibration_sample_count) : '',
  )
  const [toolAccuracy, setToolAccuracy] = useState(
    route.calibration_tool_accuracy === null ? '' : String(route.calibration_tool_accuracy),
  )
  const [composite, setComposite] = useState(
    route.calibration_composite === null ? '' : String(route.calibration_composite),
  )
  const [confirmation, setConfirmation] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const expected = inferenceRouteConfirmation(action, route.profile_revision)
  const sampleCount = Number(samples)
  const tool = Number(toolAccuracy)
  const compositeScore = Number(composite)
  const ready =
    !readOnly &&
    /^[0-9a-f]{64}$/.test(manifest) &&
    Number.isInteger(sampleCount) &&
    sampleCount >= 1 &&
    Number.isFinite(tool) &&
    tool >= 0 &&
    tool <= 1 &&
    Number.isFinite(compositeScore) &&
    compositeScore >= 0 &&
    compositeScore <= 1 &&
    confirmation === expected

  const submit = async () => {
    if (!ready) return
    setPending(true)
    setError('')
    try {
      const routes = await submitCalibration({
        data: {
          profileRevision: route.profile_revision,
          model: route.model,
          provider: route.provider,
          expectedRevision: route.calibration_revision,
          action,
          manifestSha256: manifest,
          toolAccuracy: tool,
          composite: compositeScore,
          sampleCount,
          confirmation,
        },
      })
      onUpdated(routes)
      onClose()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not update the inference route')
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="border-t border-[var(--line)] bg-[var(--panel-soft)] px-4 py-4">
      <div className="flex flex-col justify-between gap-3 lg:flex-row lg:items-start">
        <div>
          <p className="text-sm font-semibold">
            Review <span className="capitalize">{action}</span> admission
          </p>
          <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted)]">
            This records the reviewed calibration evidence for this exact immutable profile. It does
            not change any sibling profile.
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="inline-flex min-h-9 items-center justify-center gap-1.5 rounded-lg border border-[var(--line)] px-3 text-xs text-[var(--muted-strong)] hover:bg-white/[0.04]"
        >
          <X className="h-3.5 w-3.5" />
          Close
        </button>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <label className="text-xs text-[var(--muted-strong)] md:col-span-2">
          Calibration manifest SHA-256
          <input
            value={manifest}
            onChange={(event) => setManifest(event.target.value.trim().toLowerCase())}
            disabled={readOnly || pending}
            spellCheck={false}
            autoComplete="off"
            className="mt-1.5 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 font-mono text-xs text-white placeholder:text-[var(--muted)] disabled:opacity-45"
            placeholder="64 lowercase hexadecimal characters"
          />
        </label>
        <label className="text-xs text-[var(--muted-strong)]">
          Reviewed samples
          <input
            type="number"
            min="1"
            step="1"
            value={samples}
            onChange={(event) => setSamples(event.target.value)}
            disabled={readOnly || pending}
            className="mt-1.5 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm text-white placeholder:text-[var(--muted)] disabled:opacity-45"
            placeholder="60"
          />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="text-xs text-[var(--muted-strong)]">
            Tool accuracy
            <input
              type="number"
              min="0"
              max="1"
              step="0.0001"
              value={toolAccuracy}
              onChange={(event) => setToolAccuracy(event.target.value)}
              disabled={readOnly || pending}
              className="mt-1.5 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm text-white placeholder:text-[var(--muted)] disabled:opacity-45"
              placeholder="0.9"
            />
          </label>
          <label className="text-xs text-[var(--muted-strong)]">
            Composite
            <input
              type="number"
              min="0"
              max="1"
              step="0.0001"
              value={composite}
              onChange={(event) => setComposite(event.target.value)}
              disabled={readOnly || pending}
              className="mt-1.5 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm text-white placeholder:text-[var(--muted)] disabled:opacity-45"
              placeholder="0.8"
            />
          </label>
        </div>
      </div>

      <label className="mt-4 block text-xs text-[var(--muted-strong)]">
        Type <code className="break-all text-white">{expected}</code> exactly to confirm
        <input
          aria-label={`Type ${expected} exactly`}
          value={confirmation}
          onChange={(event) => setConfirmation(event.target.value)}
          disabled={readOnly || pending}
          spellCheck={false}
          autoComplete="off"
          className="mt-1.5 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 font-mono text-xs text-white placeholder:text-[var(--muted)] disabled:opacity-45"
        />
      </label>

      {error ? (
        <p role="alert" className="mt-3 text-xs text-[var(--red)]">
          {error}
        </p>
      ) : null}

      <div className="mt-4 flex justify-end">
        <button
          type="button"
          onClick={submit}
          disabled={!ready || pending}
          className={`inline-flex min-h-10 items-center justify-center gap-2 rounded-lg px-4 text-xs font-semibold disabled:opacity-35 ${
            action === 'eligible'
              ? 'bg-[var(--acid)] text-[#11150d] hover:bg-[var(--acid-hover)]'
              : action === 'shadow'
                ? 'bg-[var(--amber)] text-[#211708] hover:brightness-110'
                : 'bg-[var(--red)] text-white hover:brightness-110'
          }`}
        >
          <Check className="h-3.5 w-3.5" />
          {pending ? 'Applying…' : `Set ${action}`}
        </button>
      </div>
    </div>
  )
}

type PolicyDraft = {
  speedWeight: string
  costWeight: string
  explorationWeight: string
  explorationTicketBudget: string
  minToolAccuracy: string
  minComposite: string
  minCalibrationSamples: string
  maxErrorRate: string
  maxTimeoutRate: string
  cooldownSeconds: string
  ewmaAlpha: string
}

const policyFields: Array<{
  key: keyof PolicyDraft
  label: string
  min: number
  max: number
  step: string
}> = [
  { key: 'speedWeight', label: 'Speed weight', min: 0, max: 1, step: '0.01' },
  { key: 'costWeight', label: 'Cost weight', min: 0, max: 1, step: '0.01' },
  {
    key: 'explorationWeight',
    label: 'Exploration weight',
    min: 0,
    max: 1,
    step: '0.01',
  },
  {
    key: 'explorationTicketBudget',
    label: 'Exploration tickets',
    min: 0,
    max: 100,
    step: '1',
  },
  {
    key: 'minToolAccuracy',
    label: 'Minimum tool accuracy',
    min: 0,
    max: 1,
    step: '0.01',
  },
  {
    key: 'minComposite',
    label: 'Minimum composite',
    min: 0,
    max: 1,
    step: '0.01',
  },
  {
    key: 'minCalibrationSamples',
    label: 'Minimum calibration samples',
    min: 1,
    max: 10_000,
    step: '1',
  },
  {
    key: 'maxErrorRate',
    label: 'Maximum error rate',
    min: 0,
    max: 1,
    step: '0.01',
  },
  {
    key: 'maxTimeoutRate',
    label: 'Maximum timeout rate',
    min: 0,
    max: 1,
    step: '0.01',
  },
  {
    key: 'cooldownSeconds',
    label: 'Cooldown seconds',
    min: 1,
    max: 3_600,
    step: '1',
  },
  { key: 'ewmaAlpha', label: 'EWMA alpha', min: 0.0001, max: 1, step: '0.01' },
]

function PolicyEditor({
  policy,
  readOnly,
  aggregateMode,
  onUpdated,
}: {
  policy: InferenceRoutingPolicy
  readOnly: boolean
  aggregateMode: boolean
  onUpdated: (inventory: InferenceRoutingInventory) => void
}) {
  const updatePolicy = useServerFn(updateInferenceRoutingPolicy)
  const [enabled, setEnabled] = useState(policy.enabled)
  const [draft, setDraft] = useState<PolicyDraft>({
    speedWeight: String(policy.speed_weight),
    costWeight: String(policy.cost_weight),
    explorationWeight: String(policy.exploration_weight),
    explorationTicketBudget: String(policy.exploration_ticket_budget),
    minToolAccuracy: String(policy.min_tool_accuracy),
    minComposite: String(policy.min_composite),
    minCalibrationSamples: String(policy.min_calibration_samples),
    maxErrorRate: String(policy.max_error_rate),
    maxTimeoutRate: String(policy.max_timeout_rate),
    cooldownSeconds: String(policy.cooldown_seconds),
    ewmaAlpha: String(policy.ewma_alpha),
  })
  const [confirmation, setConfirmation] = useState('')
  const [expanded, setExpanded] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const expected = inferencePolicyConfirmation(policy.model)
  const numbers = Object.fromEntries(
    Object.entries(draft).map(([key, value]) => [key, Number(value)]),
  ) as Record<keyof PolicyDraft, number>
  const weights = numbers.speedWeight + numbers.costWeight + numbers.explorationWeight
  const fieldsValid = policyFields.every(({ key, min, max, step }) => {
    const value = numbers[key]
    return (
      draft[key] !== '' &&
      Number.isFinite(value) &&
      value >= min &&
      value <= max &&
      (step !== '1' || Number.isInteger(value))
    )
  })
  const ready =
    !readOnly && !aggregateMode && fieldsValid && weights > 0 && confirmation === expected

  const submit = async () => {
    if (!ready) return
    setPending(true)
    setError('')
    try {
      const inventory = await updatePolicy({
        data: {
          model: policy.model,
          expectedRevision: policy.revision,
          enabled,
          speedWeight: numbers.speedWeight,
          costWeight: numbers.costWeight,
          explorationWeight: numbers.explorationWeight,
          explorationTicketBudget: numbers.explorationTicketBudget,
          minToolAccuracy: numbers.minToolAccuracy,
          minComposite: numbers.minComposite,
          minCalibrationSamples: numbers.minCalibrationSamples,
          maxErrorRate: numbers.maxErrorRate,
          maxTimeoutRate: numbers.maxTimeoutRate,
          cooldownSeconds: numbers.cooldownSeconds,
          ewmaAlpha: numbers.ewmaAlpha,
          confirmation,
        },
      })
      onUpdated(inventory)
      setExpanded(false)
      setConfirmation('')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not update routing policy')
    } finally {
      setPending(false)
    }
  }

  return (
    <section className="border-b border-[var(--line)] bg-[var(--panel)]">
      <div className="flex flex-col justify-between gap-4 px-4 py-4 lg:flex-row lg:items-center">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold">{policy.model}</h3>
            <Status value={policy.enabled ? 'enabled' : 'disabled'} />
          </div>
          <p className="mt-2 text-xs text-[var(--muted)]">
            Speed {policy.speed_weight.toFixed(2)} · Cost {policy.cost_weight.toFixed(2)} ·
            Exploration {policy.exploration_weight.toFixed(2)} · {policy.exploration_ticket_budget}{' '}
            exploration tickets
          </p>
          <p className="mt-1 text-[10px] text-[var(--muted)]">
            Quality floors: {percent(policy.min_tool_accuracy)} tool /{' '}
            {percent(policy.min_composite)} composite · Reliability ceilings:{' '}
            {percent(policy.max_error_rate)} error / {percent(policy.max_timeout_rate)} timeout
          </p>
          <p className="mt-1 text-[10px] text-[var(--muted)]">Policy revision {policy.revision}</p>
        </div>
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          disabled={readOnly || aggregateMode}
          aria-expanded={expanded}
          className="inline-flex min-h-10 shrink-0 items-center justify-center rounded-lg border border-[var(--line)] px-3 text-xs font-semibold text-[var(--muted-strong)] hover:bg-white/[0.04] disabled:opacity-35"
        >
          {aggregateMode
            ? 'Locked in aggregate mode'
            : expanded
              ? 'Close policy editor'
              : 'Review policy'}
        </button>
      </div>
      {expanded && !aggregateMode ? (
        <div className="border-t border-[var(--line)] bg-[var(--panel-soft)] px-4 py-4">
          <label className="inline-flex min-h-10 items-center gap-2 text-xs font-medium text-[var(--muted-strong)]">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => setEnabled(event.target.checked)}
              disabled={pending}
              className="h-4 w-4 accent-[var(--acid)]"
            />
            Dynamic routing enabled for this model
          </label>

          <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
            {policyFields.map((field) => (
              <label key={field.key} className="text-xs text-[var(--muted-strong)]">
                {field.label}
                <input
                  type="number"
                  min={field.min}
                  max={field.max}
                  step={field.step}
                  value={draft[field.key]}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      [field.key]: event.target.value,
                    }))
                  }
                  disabled={pending}
                  className="mt-1.5 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm text-white disabled:opacity-45"
                />
              </label>
            ))}
          </div>

          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs">
            <p className={weights > 0 ? 'text-[var(--muted)]' : 'text-[var(--red)]'}>
              Weight total {Number.isFinite(weights) ? weights.toFixed(2) : '—'}; weights need not
              sum to 1, but cannot all be zero.
            </p>
            <p className="text-[var(--muted)]">
              EWMA {draft.ewmaAlpha} · cooldown {draft.cooldownSeconds}s · minimum{' '}
              {draft.minCalibrationSamples} calibration samples
            </p>
          </div>

          <label className="mt-4 block text-xs text-[var(--muted-strong)]">
            Type <code className="break-all text-white">{expected}</code> exactly to confirm
            <input
              aria-label={`Type ${expected} exactly`}
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              disabled={pending}
              spellCheck={false}
              autoComplete="off"
              className="mt-1.5 min-h-10 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 font-mono text-xs text-white disabled:opacity-45"
            />
          </label>
          {error ? (
            <p role="alert" className="mt-3 text-xs text-[var(--red)]">
              {error}
            </p>
          ) : null}
          <div className="mt-4 flex justify-end">
            <button
              type="button"
              onClick={submit}
              disabled={!ready || pending}
              className="inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-[#11150d] hover:bg-[var(--acid-hover)] disabled:opacity-35"
            >
              <Check className="h-3.5 w-3.5" />
              {pending ? 'Applying…' : 'Update routing policy'}
            </button>
          </div>
        </div>
      ) : null}
    </section>
  )
}

export function InferenceRoutingPanel({
  initialInventory,
  readOnly,
}: {
  initialInventory: InferenceRoutingInventory
  readOnly: boolean
}) {
  const refreshRoutes = useServerFn(listInferenceRoutes)
  const [inventory, setInventory] = useState(initialInventory)
  const routes = inventory.routes
  const [editing, setEditing] = useState<{
    profile: string
    action: InferenceRouteCalibrationAction
  } | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const aggregateMode = inventory.routing_mode === 'aggregate_throughput'
  const counts = useMemo(
    () => ({
      total: routes.length,
      healthy: routes.filter((route) => route.status === 'healthy').length,
      eligible: routes.filter((route) => route.calibration_status === 'eligible').length,
      attention: routes.filter((route) => route.status === 'degraded' || route.status === 'offline')
        .length,
    }),
    [routes],
  )

  const refresh = async () => {
    setRefreshing(true)
    setError('')
    try {
      setInventory(await refreshRoutes())
      setEditing(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not refresh inference routes')
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <div className="mt-6">
      {readOnly ? (
        <div className="mb-4 rounded-xl border border-[var(--amber)]/30 bg-[var(--amber-dim)] px-4 py-3 text-sm text-[var(--amber)]">
          You have read-only access. Route admission controls are disabled.
        </div>
      ) : null}

      <div
        className={`mb-4 rounded-xl border px-4 py-3 ${
          aggregateMode
            ? 'border-[var(--cyan)]/30 bg-[var(--cyan-dim)]'
            : 'border-[#4b602d] bg-[var(--acid-dim)]'
        }`}
      >
        <div className="flex flex-col justify-between gap-2 sm:flex-row sm:items-center">
          <div>
            <p className="text-sm font-semibold">
              {aggregateMode ? 'Throughput-first aggregate route' : 'Adaptive provider routing'}
            </p>
            <p className="mt-1 text-xs leading-5 text-[var(--muted-strong)]">
              {aggregateMode
                ? 'OpenRouter starts with its fastest-throughput eligible provider, then the platform retries through a bounded reliability route. Individual provider admission controls stay locked in aggregate mode.'
                : 'The platform selects among individually calibrated provider profiles using the active model policy.'}
            </p>
            {aggregateMode && inventory.aggregate_route ? (
              <p className="mt-1 text-xs text-[var(--muted-strong)]">
                Primary: {inventory.aggregate_route.provider_sort === 'throughput'
                  ? 'fastest throughput'
                  : inventory.aggregate_route.provider_order.join(' → ')} · recovery:{' '}
                {inventory.aggregate_route.reliability_provider_order.join(' → ') || 'none'} · excluded:{' '}
                {inventory.aggregate_route.ignored_providers.join(', ') || 'none'} · fallback{' '}
                {inventory.aggregate_route.allow_fallbacks ? 'enabled' : 'disabled'}
              </p>
            ) : null}
          </div>
          <Status value={aggregateMode ? 'aggregate' : 'adaptive'} />
        </div>
      </div>

      <div className="summary-strip overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        {[
          ['Discovered profiles', counts.total],
          ['Healthy', counts.healthy],
          ['Eligible', counts.eligible],
          ['Needs attention', counts.attention],
        ].map(([label, value]) => (
          <div key={label} className="px-4 py-3.5">
            <p className="text-[11px] text-[var(--muted)]">{label}</p>
            <p className="mt-1 text-xl font-semibold tabular-nums">{value}</p>
          </div>
        ))}
      </div>

      <div className="mt-4 grid overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)] sm:grid-cols-2 sm:divide-x sm:divide-[var(--line)]">
        <div className="px-4 py-3.5">
          <p className="text-[11px] text-[var(--muted)]">Benchmark relay abort tickets</p>
          <p className="mt-1 text-xl font-semibold tabular-nums">
            {inventory.relay_recovery_telemetry.benchmark_relay_abort_ticket_count.toLocaleString()}
          </p>
        </div>
        <div className="border-t border-[var(--line)] px-4 py-3.5 sm:border-t-0">
          <p className="text-[11px] text-[var(--muted)]">Broker recovery exhausted tickets</p>
          <p className="mt-1 text-xl font-semibold tabular-nums text-[var(--red)]">
            {inventory.relay_recovery_telemetry.broker_recovery_exhausted_ticket_count.toLocaleString()}
          </p>
        </div>
      </div>

      <ProviderTelemetryPanel rows={inventory.provider_telemetry} />

      <div className="mt-4 overflow-hidden rounded-xl border border-[var(--line)]">
        <div className="bg-[var(--panel-raised)] px-4 py-3">
          <h3 className="text-sm font-semibold">Per-model routing policy</h3>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Tune speed, cost, exploration, quality, reliability, cooldown, and telemetry response.
            Changes replace one model policy atomically.
          </p>
        </div>
        {inventory.policies.length === 0 ? (
          <p className="bg-[var(--panel)] px-4 py-6 text-xs text-[var(--muted)]">
            No model routing policy is available. Dynamic routing remains disabled.
          </p>
        ) : (
          inventory.policies.map((policy) => (
            <PolicyEditor
              key={`${policy.model}:${policy.updated_at}`}
              policy={policy}
              readOnly={readOnly}
              aggregateMode={aggregateMode}
              onUpdated={setInventory}
            />
          ))
        )}
      </div>

      <div className="mt-4 flex items-center justify-between gap-4">
        <p className="max-w-[72ch] text-xs leading-5 text-[var(--muted)]">
          Provider observations contain aggregate routing telemetry only. Prompts, responses,
          credentials, and ticket capabilities are never displayed here.
        </p>
        <button
          type="button"
          onClick={refresh}
          disabled={refreshing}
          className="inline-flex min-h-10 shrink-0 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/[0.04] disabled:opacity-40"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {error ? (
        <p role="alert" className="mt-3 text-xs text-[var(--red)]">
          {error}
        </p>
      ) : null}

      {routes.length === 0 ? (
        <div className="mt-4 rounded-xl border border-dashed border-[var(--line-strong)] px-6 py-12 text-center">
          <p className="text-sm font-semibold">No provider profiles discovered</p>
          <p className="mx-auto mt-2 max-w-[60ch] text-xs leading-5 text-[var(--muted)]">
            Keep routing dark until platform discovery publishes immutable profiles and reviewed
            calibration manifests are available.
          </p>
        </div>
      ) : (
        <div className="scrollbar-thin mt-4 overflow-x-auto rounded-xl border border-[var(--line)]">
          <table className="min-w-[86rem] w-full border-collapse text-left">
            <thead className="bg-[var(--panel-raised)] text-[10px] font-semibold text-[var(--muted-strong)]">
              <tr>
                <th className="px-4 py-3">Provider profile</th>
                <th className="px-3 py-3">Discovery / health</th>
                <th className="px-3 py-3">Calibration</th>
                <th className="px-3 py-3">Quality</th>
                <th className="px-3 py-3">Speed / latency</th>
                <th className="px-3 py-3">Reliability</th>
                <th className="px-3 py-3">Cost / 1M tokens</th>
                <th className="px-4 py-3 text-right">Admission</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--line)] bg-[var(--panel)]">
              {routes.map((route) => {
                const expanded = editing?.profile === route.profile_revision
                return (
                  <RouteRows
                    key={route.profile_revision}
                    route={route}
                    readOnly={readOnly}
                    aggregateMode={aggregateMode}
                    aggregateRoute={inventory.aggregate_route}
                    editing={expanded ? editing.action : null}
                    onEdit={(action) => setEditing({ profile: route.profile_revision, action })}
                    onClose={() => setEditing(null)}
                    onUpdated={(updated) => {
                      setInventory(updated)
                      setEditing(null)
                    }}
                  />
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <AuditHistory audits={inventory.audits} />
    </div>
  )
}

function providerCost(value: number) {
  return `$${(value / 1_000_000).toFixed(4)}`
}

type ProviderTokenSort = 'prompt_tokens' | 'completion_tokens'

function ProviderTelemetryPanel({ rows }: { rows: InferenceProviderTelemetry[] }) {
  const [tokenSort, setTokenSort] = useState<ProviderTokenSort>('prompt_tokens')
  const [sortDirection, setSortDirection] = useState<'ascending' | 'descending'>('descending')
  const totals = useMemo(
    () =>
      rows.reduce(
        (sum, row) => ({
          prompt: sum.prompt + row.prompt_tokens,
          completion: sum.completion + row.completion_tokens,
        }),
        { prompt: 0, completion: 0 },
      ),
    [rows],
  )
  const sortedRows = useMemo(
    () =>
      [...rows].sort((left, right) => {
        const difference = left[tokenSort] - right[tokenSort]
        if (difference !== 0) return sortDirection === 'ascending' ? difference : -difference
        return left.provider.localeCompare(right.provider)
      }),
    [rows, sortDirection, tokenSort],
  )

  const sortBy = (column: ProviderTokenSort) => {
    if (column === tokenSort) {
      setSortDirection((current) => (current === 'descending' ? 'ascending' : 'descending'))
      return
    }
    setTokenSort(column)
    setSortDirection('descending')
  }

  const sortableTokenHeader = (label: string, column: ProviderTokenSort) => {
    const active = tokenSort === column
    const SortIcon = active && sortDirection === 'ascending' ? ArrowUp : ArrowDown
    return (
      <button
        type="button"
        onClick={() => sortBy(column)}
        className="inline-flex min-h-8 items-center gap-1.5 rounded-md px-1.5 text-left hover:bg-white/[0.04] hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--acid)]/70"
        aria-label={`Sort by ${label.toLowerCase()} ${active && sortDirection === 'descending' ? 'ascending' : 'descending'}`}
      >
        {label}
        <SortIcon className={`h-3 w-3 ${active ? 'text-[var(--acid)]' : 'text-[var(--muted)]'}`} />
      </button>
    )
  }

  return (
    <section className="mt-4 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="border-b border-[var(--line)] bg-[var(--panel-raised)] px-4 py-3">
        <h3 className="text-sm font-semibold">Actual upstream providers</h3>
        <p className="mt-1 text-xs text-[var(--muted)]">
          Trusted aggregate accounting from every attributed proxy request, including terminal
          failures and recoveries. Failed means one Platform request exhausted its configured
          provider phases; OpenRouter attempts come from opted-in router metadata when available.
          Observed output TPS is completion tokens divided by end-to-end provider latency. No
          prompts, responses, or request bodies are collected here.
        </p>
      </div>
      {rows.length === 0 ? (
        <p className="px-4 py-6 text-xs text-[var(--muted)]">
          No upstream-provider telemetry recorded yet.
        </p>
      ) : (
        <>
          <div className="grid grid-cols-2 divide-x divide-[var(--line)] border-b border-[var(--line)] bg-[var(--panel-soft)]">
            <div className="px-4 py-3">
              <p className="text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--muted)]">
                Total input tokens
              </p>
              <p className="mt-1 text-base font-semibold tabular-nums text-white">
                {totals.prompt.toLocaleString()}
              </p>
            </div>
            <div className="px-4 py-3">
              <p className="text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--muted)]">
                Total output tokens
              </p>
              <p className="mt-1 text-base font-semibold tabular-nums text-white">
                {totals.completion.toLocaleString()}
              </p>
            </div>
          </div>
          <div className="scrollbar-thin overflow-x-auto">
            <table className="min-w-[92rem] w-full border-collapse text-left">
              <thead className="text-[10px] font-semibold text-[var(--muted-strong)]">
                <tr>
                  <th className="px-4 py-3">Provider</th>
                  <th className="px-3 py-3">Requests</th>
                  <th className="px-3 py-3">Completed</th>
                  <th className="px-3 py-3">Failed</th>
                  <th className="px-3 py-3">In flight</th>
                  <th className="px-3 py-3">Timeouts</th>
                  <th className="px-3 py-3">Upstream attempts</th>
                  <th className="px-3 py-3">OpenRouter attempts</th>
                  <th className="px-3 py-3">Recovered</th>
                  <th className="px-3 py-3">Terminal</th>
                  <th
                    className="px-1.5 py-1.5"
                    aria-sort={tokenSort === 'prompt_tokens' ? sortDirection : 'none'}
                  >
                    {sortableTokenHeader('Input tokens', 'prompt_tokens')}
                  </th>
                  <th
                    className="px-1.5 py-1.5"
                    aria-sort={tokenSort === 'completion_tokens' ? sortDirection : 'none'}
                  >
                    {sortableTokenHeader('Output tokens', 'completion_tokens')}
                  </th>
                  <th className="px-3 py-3">Average latency</th>
                  <th className="px-3 py-3">Observed output TPS</th>
                  <th className="px-4 py-3 text-right">Observed cost</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--line)]">
                {sortedRows.map((row) => (
                  <tr key={row.provider} className="text-xs tabular-nums">
                    <td className="px-4 py-3 font-semibold text-white">{row.provider}</td>
                    <td className="px-3 py-3">{row.request_count.toLocaleString()}</td>
                    <td className="px-3 py-3">
                      {row.completed_count.toLocaleString()}
                      <span className="ml-1 text-[10px] text-[var(--muted)]">
                        (
                        {row.request_count === 0
                          ? '—'
                          : percent(row.completed_count / row.request_count)}
                        )
                      </span>
                    </td>
                    <td
                      className={`px-3 py-3 ${row.failed_count > 0 ? 'font-semibold text-[var(--red)]' : ''}`}
                    >
                      {row.failed_count.toLocaleString()}
                      <span className="ml-1 text-[10px] text-[var(--muted)]">
                        (
                        {row.request_count === 0
                          ? '—'
                          : percent(row.failed_count / row.request_count)}
                        )
                      </span>
                    </td>
                    <td className="px-3 py-3">{row.inflight_count.toLocaleString()}</td>
                    <td className="px-3 py-3">{row.timeout_count.toLocaleString()}</td>
                    <td className="px-3 py-3">{row.upstream_attempt_count.toLocaleString()}</td>
                    <td className="px-3 py-3">{row.openrouter_attempt_count.toLocaleString()}</td>
                    <td className="px-3 py-3">{row.recovered_after_fallback_count.toLocaleString()}</td>
                    <td
                      className={`px-3 py-3 ${row.terminal_failure_count > 0 ? 'font-semibold text-[var(--red)]' : ''}`}
                    >
                      {row.terminal_failure_count.toLocaleString()}
                    </td>
                    <td className="px-3 py-3">{row.prompt_tokens.toLocaleString()}</td>
                    <td className="px-3 py-3 text-[var(--muted-strong)]">
                      {row.completion_tokens.toLocaleString()}
                    </td>
                    <td className="px-3 py-3">
                      {row.average_latency_ms === null
                        ? '—'
                        : `${row.average_latency_ms.toFixed(0)} ms`}
                    </td>
                    <td className="px-3 py-3 font-medium text-white">
                      {row.observed_output_tps === null
                        ? '—'
                        : `${row.observed_output_tps.toFixed(1)} tok/s`}
                    </td>
                    <td className="px-4 py-3 text-right font-medium text-white">
                      {providerCost(row.cost_microusd)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}

const visibleAuditFields = new Set([
  'enabled',
  'expected_revision',
  'speed_weight',
  'cost_weight',
  'exploration_weight',
  'exploration_ticket_budget',
  'min_tool_accuracy',
  'min_composite',
  'min_calibration_samples',
  'max_error_rate',
  'max_timeout_rate',
  'cooldown_seconds',
  'ewma_alpha',
  'action',
  'manifest_sha256',
  'tool_accuracy',
  'composite',
  'sample_count',
])

function auditValue(key: string, value: unknown) {
  if (key === 'manifest_sha256' && typeof value === 'string') {
    return `${value.slice(0, 12)}…${value.slice(-8)}`
  }
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return null
}

function AuditHistory({ audits }: { audits: InferenceRoutingAudit[] }) {
  return (
    <section className="mt-4 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="border-b border-[var(--line)] bg-[var(--panel-raised)] px-4 py-3">
        <h3 className="text-sm font-semibold">Routing audit history</h3>
        <p className="mt-1 text-xs text-[var(--muted)]">
          Latest {audits.length} append-only operator decisions. Only allowlisted aggregate control
          fields are rendered.
        </p>
      </div>
      {audits.length === 0 ? (
        <p className="px-4 py-6 text-xs text-[var(--muted)]">No routing decisions recorded.</p>
      ) : (
        <ol className="divide-y divide-[var(--line)]">
          {audits.map((audit) => {
            const details = Object.entries(audit.payload)
              .filter(([key]) => visibleAuditFields.has(key))
              .map(([key, value]) => [key, auditValue(key, value)] as const)
              .filter((entry): entry is readonly [string, string] => entry[1] !== null)
            return (
              <li key={audit.audit_id} className="px-4 py-3">
                <div className="flex flex-col justify-between gap-1 sm:flex-row sm:items-start">
                  <div>
                    <p className="text-xs font-semibold">
                      {audit.action.replaceAll('_', ' ')} · {audit.model}
                    </p>
                    <p className="mt-1 text-[10px] text-[var(--muted)]">
                      {audit.actor} · {formatUtc(audit.recorded_at)}
                      {audit.profile_revision ? ` · ${audit.profile_revision}` : ''}
                    </p>
                  </div>
                </div>
                {details.length > 0 ? (
                  <dl className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-[var(--muted-strong)]">
                    {details.map(([key, value]) => (
                      <div key={key} className="flex gap-1">
                        <dt>{key.replaceAll('_', ' ')}</dt>
                        <dd className="font-mono text-white">{value}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}

function RouteRows({
  route,
  readOnly,
  aggregateMode,
  aggregateRoute,
  editing,
  onEdit,
  onClose,
  onUpdated,
}: {
  route: InferenceRoute
  readOnly: boolean
  aggregateMode: boolean
  aggregateRoute: InferenceRouteIdentity | null
  editing: InferenceRouteCalibrationAction | null
  onEdit: (action: InferenceRouteCalibrationAction) => void
  onClose: () => void
  onUpdated: (inventory: InferenceRoutingInventory) => void
}) {
  const isAggregateRoute =
    aggregateRoute !== null &&
    route.model === aggregateRoute.model &&
    route.provider === aggregateRoute.provider &&
    route.profile_revision === aggregateRoute.profile_revision
  const controlsDisabled = readOnly || (aggregateMode && !isAggregateRoute)

  return (
    <>
      <tr className="align-top">
        <td className="px-4 py-4">
          <p className="text-sm font-semibold text-white">{route.provider}</p>
          <p className="mt-1 text-[11px] text-[var(--muted-strong)]">{route.model}</p>
          <p className="mt-1 text-[10px] text-[var(--muted)]">
            Quantization: {route.quantization ?? 'unspecified'}
          </p>
          <code className="mt-2 block max-w-[18rem] break-all text-[9px] leading-4 text-[var(--muted)]">
            {route.profile_revision}
          </code>
        </td>
        <td className="px-3 py-4">
          <Status value={route.status} />
          <p className="mt-2 text-[10px] leading-4 text-[var(--muted)]">
            Updated {formatUtc(route.updated_at)}
          </p>
          <p className="mt-1 text-[10px] text-[var(--muted)]">
            {route.sample_count.toLocaleString()} observations
          </p>
          <p className="mt-1 text-[10px] text-[var(--muted)]">
            {route.selected_ticket_count.toLocaleString()} selected ·{' '}
            {route.exploration_ticket_count.toLocaleString()} explored
          </p>
          <p className="mt-1 text-[10px] leading-4 text-[var(--muted)]">
            Last selected{' '}
            {route.last_selected_at ? formatUtc(route.last_selected_at) : 'never'}
          </p>
        </td>
        <td className="px-3 py-4">
          <Status value={route.calibration_status} />
          <p className="mt-2 text-[10px] text-[var(--muted)]">
            {route.calibration_sample_count.toLocaleString()} reviewed samples
          </p>
          <p className="mt-1 text-[10px] text-[var(--muted)]">
            Calibration revision {route.calibration_revision}
          </p>
          <code className="mt-1 block max-w-[11rem] break-all text-[9px] leading-4 text-[var(--muted)]">
            {route.calibration_manifest_sha256 ?? 'No calibration manifest'}
          </code>
        </td>
        <td className="px-3 py-4 text-xs tabular-nums">
          <p>Tool {percent(route.calibration_tool_accuracy)}</p>
          <p className="mt-1 text-[var(--muted-strong)]">
            Composite {percent(route.calibration_composite)}
          </p>
        </td>
        <td className="px-3 py-4 text-xs tabular-nums">
          <p>{metric(route.ewma_tokens_per_second)} tok/s</p>
          <p className="mt-1 text-[var(--muted-strong)]">{metric(route.ewma_latency_ms, 0)} ms</p>
        </td>
        <td className="px-3 py-4 text-xs tabular-nums">
          <p>Error {percent(route.ewma_error_rate)}</p>
          <p className="mt-1 text-[var(--muted-strong)]">
            Timeout {percent(route.ewma_timeout_rate)}
          </p>
        </td>
        <td className="px-3 py-4 text-xs tabular-nums">
          <p>Input {perMillion(route.prompt_price_per_token)}</p>
          <p className="mt-1 text-[var(--muted-strong)]">
            Output {perMillion(route.completion_price_per_token)}
          </p>
        </td>
        <td className="px-4 py-4">
          <div className="flex justify-end gap-1.5">
            {actions.map((action) => (
              <button
                key={action}
                type="button"
                onClick={() => onEdit(action)}
                disabled={controlsDisabled}
                aria-pressed={editing === action}
                className={`min-h-9 rounded-lg border px-2.5 text-[10px] font-semibold capitalize disabled:opacity-35 ${
                  editing === action
                    ? statusClass(action)
                    : 'border-[var(--line)] text-[var(--muted-strong)] hover:bg-white/[0.04]'
                }`}
              >
                {action}
              </button>
            ))}
          </div>
          {aggregateMode && !isAggregateRoute ? (
            <p className="mt-2 text-right text-[9px] leading-4 text-[var(--muted)]">
              Individual admission locked
            </p>
          ) : null}
        </td>
      </tr>
      {editing ? (
        <tr>
          <td colSpan={8} className="p-0">
            <RouteActionEditor
              key={`${route.profile_revision}:${editing}`}
              route={route}
              action={editing}
              readOnly={readOnly}
              onClose={onClose}
              onUpdated={onUpdated}
            />
          </td>
        </tr>
      ) : null}
    </>
  )
}
