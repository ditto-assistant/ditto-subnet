import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, Bot, CheckCircle2, Clock3, LockKeyhole, RefreshCw } from 'lucide-react'
import { useMemo, useState } from 'react'
import type {
  QueuePolicySettingsControl,
  ScreenerReviewControl,
  ScreenerReviewSettings,
} from '../lib/admin.schemas'
import { QUEUE_POLICY_CONFIRMATION } from '../lib/admin.schemas'
import {
  getScreenerReviewControl,
  getQueuePolicyControl,
  updateQueuePolicySettings,
  updateScreenerReviewSettings,
} from '../server/admin.functions'

const defaults: ScreenerReviewSettings = {
  mode: 'off',
  l2_model: 'moonshotai/kimi-k3',
  l2_fallback_models: ['z-ai/glm-5.2', 'openai/gpt-5.6-sol'],
  l3_enabled: true,
  l3_model: 'openai/gpt-5.6-sol',
  timeout_seconds: 900,
  max_steps: 18,
  source_review_max_steps: 200,
  source_review_max_read_bytes: 8_000_000,
  source_review_max_completion_tokens: 8_000,
  concern_hold_count: 3,
  clear_min_notes: 3,
  adjudicator_mode: 'off' as const,
  adjudicator_model: 'z-ai/glm-5.3-flash' as const,
  adjudicator_max_steps: 128,
  adjudicator_timeout_seconds: 600,
  source_review_reasoning_effort: 'high',
  source_review_model: 'openai/gpt-5.6-luna',
  source_review_timeout_seconds: 1_800,
  max_input_tokens: 425_000,
  max_output_tokens: 20_000,
  max_completion_tokens: 2_400,
  max_cost_usd: 2,
  critic_reasoning_effort: 'medium',
  cache_ttl_seconds: 604_800,
  audit_retention_days: 30,
  policy_manifest_profile: 'l1',
  policy_manifest_rotation_id: 'v8-luna-source-review-behavioral-oracle',
}

function withDefaults(settings?: Partial<ScreenerReviewSettings>): ScreenerReviewSettings {
  return { ...defaults, ...settings }
}

function shortDigest(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-4)}`
}

function NumericField({
  label,
  value,
  onChange,
  step = 1,
  min,
  max,
  hint,
}: {
  label: string
  value: number
  onChange: (value: number) => void
  step?: number
  min?: number
  max?: number
  hint?: string
}) {
  return (
    <label className="block text-xs text-[var(--muted)]">
      {label}
      <input
        type="number"
        aria-label={label}
        value={value}
        step={step}
        min={min}
        max={max}
        onChange={(event) => onChange(Number(event.target.value))}
        className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white focus:border-[var(--acid)]"
      />
      {hint ? <span className="mt-1 block text-[10px] leading-4">{hint}</span> : null}
    </label>
  )
}

const DEFERRED_MODE_DESCRIPTION = {
  off: 'Legacy full source review runs before scoring.',
  observe:
    'Full source review still runs before scoring while the platform records which submissions would qualify for deferred review.',
  enforce:
    'Cheap admission and prescoring run first; top-five entrants and configured score anomalies then receive deep source review.',
  bypass:
    'No source review at all: cheap admission only, no post-score qualification, no holds. Copy/plagiarism enforcement is a separate path and stays armed.',
} as const

function DeferredSourceReviewPolicy({
  initialState,
  readOnly,
}: {
  initialState: QueuePolicySettingsControl
  readOnly: boolean
}) {
  const fetchPolicy = useServerFn(getQueuePolicyControl)
  const applyPolicy = useServerFn(updateQueuePolicySettings)
  const [state, setState] = useState(initialState)
  const [settings, setSettings] = useState(initialState.effective.settings)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const deferred = settings.deferred_source_review
  const deferredValid = Number.isInteger(deferred.min_cohort_size)
    && deferred.min_cohort_size >= 5
    && deferred.min_cohort_size <= 100
    && deferred.composite_mad_multiplier >= 1
    && deferred.composite_mad_multiplier <= 20
    && deferred.axis_mad_multiplier >= 1
    && deferred.axis_mad_multiplier <= 20
    && deferred.min_composite_delta >= 0
    && deferred.min_composite_delta <= 1
    && deferred.min_axis_delta >= 0
    && deferred.min_axis_delta <= 1
  const ready = deferredValid
    && reason.trim().length >= 8
    && confirmation === QUEUE_POLICY_CONFIRMATION

  const changeDeferred = (
    patch: Partial<typeof settings.deferred_source_review>,
  ) => {
    setSettings((current) => ({
      ...current,
      deferred_source_review: { ...current.deferred_source_review, ...patch },
    }))
  }

  const refresh = async () => {
    setLoading(true)
    setError('')
    try {
      const next = await fetchPolicy()
      setState(next)
      setSettings(next.effective.settings)
      setReason('')
      setConfirmation('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh deferred review policy')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!ready || readOnly) return
    setLoading(true)
    setError('')
    try {
      const next = await applyPolicy({
        data: {
          expectedRevision: state.effective.revision,
          settings,
          reason,
          confirmation,
        },
      })
      setState(next)
      setSettings(next.effective.settings)
      setReason('')
      setConfirmation('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to apply deferred review policy')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-lg bg-[var(--amber-dim)] text-[var(--amber)]">
            <LockKeyhole className="h-4 w-4" />
          </div>
          <div>
            <h2 className="text-sm font-semibold">Deferred deep source review</h2>
            <p className="mt-1 max-w-[78ch] text-xs leading-5 text-[var(--muted)]">
              Choose when expensive source review runs. The policy is stored with the complete
              validator queue policy, so this control preserves every unrelated queue setting.
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      <div className="space-y-5 p-4 sm:p-5">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" aria-label="Deferred review mode">
          {(['off', 'observe', 'enforce', 'bypass'] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              aria-label={mode}
              disabled={readOnly || loading}
              aria-pressed={deferred.mode === mode}
              onClick={() => changeDeferred({ mode })}
              className={`min-h-11 rounded-lg border p-3 text-left text-xs disabled:cursor-not-allowed disabled:opacity-40 ${
                deferred.mode === mode
                  ? 'border-[var(--acid)] bg-[var(--acid-dim)] text-[var(--acid)]'
                  : 'border-[var(--line)] text-[var(--muted-strong)] hover:bg-white/5'
              }`}
            >
              <span className="block font-semibold capitalize">{mode}</span>
              <span className="mt-1 block font-normal leading-4 text-[var(--muted)]">
                {DEFERRED_MODE_DESCRIPTION[mode]}
              </span>
            </button>
          ))}
        </div>

        <div className="flex items-start gap-3 rounded-lg border border-[var(--amber)]/25 bg-[var(--amber-dim)]/35 p-4">
          <LockKeyhole className="mt-0.5 h-4 w-4 shrink-0 text-[var(--amber)]" />
          <div>
            <p className="text-xs font-semibold text-[var(--amber)]">Top-five integrity rail</p>
            <p className="mt-1 text-xs leading-5 text-[var(--muted-strong)]">
              In enforce mode, every top-five entrant receives deferred deep review. This rail has
              no operator switch; the thresholds below tune only the additional anomaly trigger.
            </p>
          </div>
        </div>

        <fieldset className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" disabled={readOnly || loading}>
          <NumericField
            label="Minimum scored cohort"
            value={deferred.min_cohort_size}
            min={5}
            max={100}
            hint="Whole number from 5 to 100."
            onChange={(value) => changeDeferred({ min_cohort_size: value })}
          />
          <NumericField
            label="Composite MAD multiplier"
            value={deferred.composite_mad_multiplier}
            step={0.1}
            min={1}
            max={20}
            hint="Number from 1 to 20."
            onChange={(value) => changeDeferred({ composite_mad_multiplier: value })}
          />
          <NumericField
            label="Axis MAD multiplier"
            value={deferred.axis_mad_multiplier}
            step={0.1}
            min={1}
            max={20}
            hint="Number from 1 to 20."
            onChange={(value) => changeDeferred({ axis_mad_multiplier: value })}
          />
          <NumericField
            label="Minimum composite delta"
            value={deferred.min_composite_delta}
            step={0.01}
            min={0}
            max={1}
            hint="Score delta from 0 to 1."
            onChange={(value) => changeDeferred({ min_composite_delta: value })}
          />
          <NumericField
            label="Minimum axis delta"
            value={deferred.min_axis_delta}
            step={0.01}
            min={0}
            max={1}
            hint="Score delta from 0 to 1."
            onChange={(value) => changeDeferred({ min_axis_delta: value })}
          />
        </fieldset>
        <p className="text-xs leading-5 text-[var(--muted)]">
          MAD thresholds compare the submission with the scored cohort robustly. Minimum deltas
          prevent a tiny but statistically narrow spread from escalating ordinary variance.
        </p>
        {!deferredValid ? (
          <p className="flex items-start gap-2 text-xs text-[var(--red)]" role="alert">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            Use a whole-number cohort from 5 to 100, MAD multipliers from 1 to 20,
            and score deltas from 0 to 1.
          </p>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block text-xs text-[var(--muted)]">
            Audit reason
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              disabled={readOnly || loading}
              rows={3}
              className="mt-1.5 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 py-2 text-sm text-white disabled:opacity-50"
            />
          </label>
          <label className="block text-xs text-[var(--muted)]">
            Type to confirm
            <input
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              disabled={readOnly || loading}
              placeholder={QUEUE_POLICY_CONFIRMATION}
              className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white disabled:opacity-50"
            />
            <span className="mt-2 block font-mono text-[10px] text-[var(--muted)]">
              {QUEUE_POLICY_CONFIRMATION}
            </span>
          </label>
        </div>
        {error ? (
          <p className="flex items-start gap-2 text-xs text-[var(--red)]">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {error}
          </p>
        ) : null}
        <button
          type="button"
          onClick={() => void submit()}
          disabled={!ready || loading || readOnly}
          className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-[var(--ink)] hover:bg-[var(--acid-hover)] disabled:opacity-35"
        >
          <CheckCircle2 className="h-3.5 w-3.5" />
          Append queue-policy revision
        </button>

        <div className="border-t border-[var(--line)] pt-5">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
            <span className="text-[var(--muted)]">Effective revision <strong className="text-white">{state.effective.revision}</strong></span>
            <span className="text-[var(--muted)]">Source <strong className="capitalize text-white">{state.effective.source}</strong></span>
            <span className="text-[var(--muted)]">Mode <strong className="capitalize text-white">{state.effective.settings.deferred_source_review.mode}</strong></span>
          </div>
          {state.history.length > 0 ? (
            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[42rem] text-left text-xs">
                <thead className="text-[var(--muted)]">
                  <tr className="border-b border-[var(--line)]">
                    <th className="py-2 pr-3 font-medium">Revision</th>
                    <th className="px-3 py-2 font-medium">Mode</th>
                    <th className="px-3 py-2 font-medium">Actor</th>
                    <th className="px-3 py-2 font-medium">Reason</th>
                    <th className="py-2 pl-3 font-medium">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {state.history.slice(0, 8).map((revision) => (
                    <tr key={`${revision.scope}:${revision.revision}`} className="border-b border-[var(--line)] last:border-0">
                      <td className="py-2 pr-3 font-mono">{revision.revision}</td>
                      <td className="px-3 py-2 capitalize">{revision.settings.deferred_source_review.mode}</td>
                      <td className="px-3 py-2 text-[var(--muted-strong)]">{revision.actor}</td>
                      <td className="max-w-[24rem] px-3 py-2 text-[var(--muted-strong)]">{revision.reason}</td>
                      <td className="py-2 pl-3 text-[var(--muted)]">{new Date(revision.created_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="mt-3 text-xs text-[var(--muted)]">No queue-policy revision has been written yet.</p>
          )}
        </div>
      </div>
    </section>
  )
}

export function ScreenerReviewControlPanel({
  initialState,
  initialQueuePolicy,
  readOnly,
}: {
  initialState: ScreenerReviewControl
  initialQueuePolicy: QueuePolicySettingsControl
  readOnly: boolean
}) {
  const fetchControl = useServerFn(getScreenerReviewControl)
  const applySettings = useServerFn(updateScreenerReviewSettings)
  const [state, setState] = useState(initialState)
  const [scope, setScope] = useState('*')
  const selected = useMemo(
    () => state.current.find((revision) => revision.scope === scope) ?? null,
    [scope, state.current],
  )
  const [settings, setSettings] = useState<ScreenerReviewSettings>(
    withDefaults(selected?.settings),
  )
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const expectedConfirmation = `APPLY SCREENER REVIEW ${scope} ${settings.mode.toUpperCase()}`
  const ready = reason.trim().length >= 8 && confirmation === expectedConfirmation
  const scopes = ['*', ...new Set(state.known_instances)]
  const appliedWorkers = state.applied_instances.filter((item) => {
    const selectedWorker = scope === '*' ? item.expected_scope === '*' : item.instance_id === scope
    return selectedWorker && item.fresh && item.matches_effective
  })
  const availableModes = scope === '*'
    ? (['off', 'shadow', 'enforce'] as const)
    : (['off', 'shadow', 'inherit', 'enforce'] as const)

  const chooseScope = (next: string) => {
    setScope(next)
    setSettings(withDefaults(state.current.find((revision) => revision.scope === next)?.settings))
    setReason('')
    setConfirmation('')
  }

  const refresh = async () => {
    setLoading(true)
    setError('')
    try {
      const next = await fetchControl()
      setState(next)
      setSettings(withDefaults(next.current.find((revision) => revision.scope === scope)?.settings))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh reviewer settings')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!ready || readOnly) return
    setLoading(true)
    setError('')
    try {
      await applySettings({
        data: {
          scope,
          expectedRevision: selected?.revision ?? 0,
          settings,
          reason,
          confirmation,
        },
      })
      const next = await fetchControl()
      setState(next)
      setReason('')
      setConfirmation('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to apply reviewer settings')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mt-6 space-y-5">
      <DeferredSourceReviewPolicy initialState={initialQueuePolicy} readOnly={readOnly} />
      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
              <Bot className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-semibold">Effective reviewer policy</h2>
              <p className="mt-1 max-w-[72ch] text-xs leading-5 text-[var(--muted)]">
                Changes are append-only. Workers fetch them between leases; shadow records a
                private recommendation without changing a miner outcome.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>

        <div className="grid gap-5 p-4 sm:p-5 lg:grid-cols-[16rem_1fr]">
          <div>
            <label className="block text-xs text-[var(--muted)]">
              Scope
              <select
                value={scope}
                onChange={(event) => chooseScope(event.target.value)}
                className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white"
              >
                {scopes.map((item) => (
                  <option key={item} value={item}>
                    {item === '*' ? 'Global default' : item}
                  </option>
                ))}
              </select>
            </label>
            <dl className="mt-4 space-y-3 text-xs">
              <div>
                <dt className="text-[var(--muted)]">Current revision</dt>
                <dd className="mt-1 font-medium">{selected?.revision ?? 'Built-in defaults'}</dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Applied workers</dt>
                <dd className="mt-1 font-medium">
                  {appliedWorkers.length}
                </dd>
              </div>
            </dl>
          </div>

          <div className="space-y-5">
            <div
              className={`grid gap-3 ${scope === '*' ? 'sm:grid-cols-3' : 'sm:grid-cols-4'}`}
              aria-label="Agentic review mode"
            >
              {availableModes.map((mode) => (
                <button
                  key={mode}
                  type="button"
                  onClick={() => setSettings((current) => ({ ...current, mode }))}
                  className={`min-h-11 rounded-lg border px-3 text-xs font-semibold capitalize disabled:cursor-not-allowed disabled:opacity-40 ${
                    settings.mode === mode
                      ? 'border-[var(--acid)] bg-[var(--acid-dim)] text-[var(--acid)]'
                      : 'border-[var(--line)] text-[var(--muted-strong)] hover:bg-white/5'
                  }`}
                >
                  {mode}
                </button>
              ))}
            </div>
            <p className="flex items-start gap-2 text-xs leading-5 text-[var(--muted)]">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--amber)]" />
              Enforce makes the versioned L2/L3 review authoritative for screening.
              Each signed verdict is bound to this worker, revision, scope, and checksum;
              workers fail closed if enforced settings become unavailable.
              {scope !== '*' ? ' Inherit removes this worker override and follows the global policy.' : ''}
            </p>

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <label className="block text-xs text-[var(--muted)]">
                L2 primary
                <select
                  value={settings.l2_model}
                  onChange={(event) =>
                    setSettings((current) => ({
                      ...current,
                      l2_model: event.target.value as ScreenerReviewSettings['l2_model'],
                    }))
                  }
                  className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white"
                >
                  <option value="moonshotai/kimi-k3">Kimi K3</option>
                  <option value="z-ai/glm-5.2">GLM 5.2</option>
                  <option value="openai/gpt-5.6-sol">GPT-5.6 SOL</option>
                </select>
              </label>
              {[0, 1].map((index) => (
                <label key={index} className="block text-xs text-[var(--muted)]">
                  L2 fallback {index + 1}
                  <select
                    value={settings.l2_fallback_models[index] ?? ''}
                    onChange={(event) => {
                      const next = [...settings.l2_fallback_models]
                      next[index] = event.target.value as ScreenerReviewSettings['l2_model']
                      setSettings((current) => ({ ...current, l2_fallback_models: next }))
                    }}
                    className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white"
                  >
                    <option value="moonshotai/kimi-k3">Kimi K3</option>
                    <option value="z-ai/glm-5.2">GLM 5.2</option>
                    <option value="openai/gpt-5.6-sol">GPT-5.6 SOL</option>
                  </select>
                </label>
              ))}
              <NumericField label="Timeout seconds" value={settings.timeout_seconds} onChange={(value) => setSettings((current) => ({ ...current, timeout_seconds: value }))} />
              <NumericField label="L2 max agent steps" value={settings.max_steps} onChange={(value) => setSettings((current) => ({ ...current, max_steps: value }))} />
              <label className="block text-xs text-[var(--muted)]">
                L1 model
                <select
                  value={settings.source_review_model}
                  onChange={(event) =>
                    setSettings((current) => ({
                      ...current,
                      source_review_model: event.target.value as ScreenerReviewSettings['source_review_model'],
                    }))
                  }
                  className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white"
                >
                  <option value="openai/gpt-5.6-luna">GPT-5.6 Luna</option>
                </select>
              </label>
              <NumericField label="L1 timeout seconds" value={settings.source_review_timeout_seconds} onChange={(value) => setSettings((current) => ({ ...current, source_review_timeout_seconds: value }))} />
              <NumericField label="L1 Luna max steps" value={settings.source_review_max_steps} onChange={(value) => setSettings((current) => ({ ...current, source_review_max_steps: value }))} />
              <NumericField label="L1 Luna read bytes" value={settings.source_review_max_read_bytes} onChange={(value) => setSettings((current) => ({ ...current, source_review_max_read_bytes: value }))} />
              <NumericField label="L1 verdict completion tokens" value={settings.source_review_max_completion_tokens} onChange={(value) => setSettings((current) => ({ ...current, source_review_max_completion_tokens: value }))} />
              <NumericField label="Substantiated concerns that hold" value={settings.concern_hold_count} onChange={(value) => setSettings((current) => ({ ...current, concern_hold_count: value }))} />
              <NumericField label="Cleared notes that admit" value={settings.clear_min_notes} onChange={(value) => setSettings((current) => ({ ...current, clear_min_notes: value }))} />
              <label className="block text-xs text-[var(--muted)]">
                Adjudicator (clears and rejects holds)
                <select
                  value={settings.adjudicator_mode}
                  onChange={(event) =>
                    setSettings((current) => ({
                      ...current,
                      adjudicator_mode: event.target.value as ScreenerReviewSettings['adjudicator_mode'],
                    }))
                  }
                  className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white"
                >
                  <option value="off">off</option>
                  <option value="shadow">shadow (record only)</option>
                  <option value="enforce">enforce</option>
                </select>
              </label>
              <NumericField label="Adjudicator steps" value={settings.adjudicator_max_steps} onChange={(value) => setSettings((current) => ({ ...current, adjudicator_max_steps: value }))} />
              <NumericField label="Adjudicator timeout (s)" value={settings.adjudicator_timeout_seconds} onChange={(value) => setSettings((current) => ({ ...current, adjudicator_timeout_seconds: value }))} />
              <label className="block text-xs text-[var(--muted)]">
                L1 Luna reasoning
                <select
                  value={settings.source_review_reasoning_effort}
                  onChange={(event) => setSettings((current) => ({ ...current, source_review_reasoning_effort: event.target.value as 'low' | 'medium' | 'high' }))}
                  className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white"
                >
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </select>
              </label>
              <NumericField label="Max input tokens" value={settings.max_input_tokens} onChange={(value) => setSettings((current) => ({ ...current, max_input_tokens: value }))} />
              <NumericField label="Max output tokens" value={settings.max_output_tokens} onChange={(value) => setSettings((current) => ({ ...current, max_output_tokens: value }))} />
              <NumericField label="Completion cap" value={settings.max_completion_tokens} onChange={(value) => setSettings((current) => ({ ...current, max_completion_tokens: value }))} />
              <NumericField label="Max cost (USD)" value={settings.max_cost_usd} step={0.05} onChange={(value) => setSettings((current) => ({ ...current, max_cost_usd: value }))} />
              <NumericField label="Result cache seconds" value={settings.cache_ttl_seconds} onChange={(value) => setSettings((current) => ({ ...current, cache_ttl_seconds: value }))} />
              <NumericField label="Audit retention days" value={settings.audit_retention_days} onChange={(value) => setSettings((current) => ({ ...current, audit_retention_days: value }))} />
              <label className="block text-xs text-[var(--muted)]">
                SOL critic reasoning
                <select
                  value={settings.critic_reasoning_effort}
                  onChange={(event) => setSettings((current) => ({ ...current, critic_reasoning_effort: event.target.value as 'low' | 'medium' | 'high' }))}
                  className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white"
                >
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </select>
              </label>
            </div>

            <div className="flex flex-col gap-4 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <p className="text-xs font-semibold">Independent L3 verification</p>
                <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted)]">
                  {settings.l3_enabled
                    ? 'GPT-5.6 SOL independently critiques or adjudicates the Kimi result. This adds paid model calls when L2 escalates.'
                    : 'L3 is disabled. The L2 analyst becomes the final paid reviewer; L1 routing, L2 budgets, caching, and audit evidence stay active.'}
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={settings.l3_enabled}
                aria-label="Enable L3 verification"
                disabled={readOnly || loading || settings.mode === 'inherit'}
                onClick={() => setSettings((current) => ({ ...current, l3_enabled: !current.l3_enabled }))}
                className={`inline-flex min-h-11 shrink-0 items-center rounded-lg border px-4 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-40 ${
                  settings.l3_enabled
                    ? 'border-[var(--acid)] bg-[var(--acid-dim)] text-[var(--acid)]'
                    : 'border-[var(--line)] text-[var(--muted-strong)] hover:bg-white/5'
                }`}
              >
                L3 {settings.l3_enabled ? 'enabled' : 'disabled'}
              </button>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-xs text-[var(--muted)]">
                Audit reason
                <textarea
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  rows={3}
                  className="mt-1.5 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 py-2 text-sm text-white"
                />
              </label>
              <label className="block text-xs text-[var(--muted)]">
                Type to confirm
                <input
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  placeholder={expectedConfirmation}
                  className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm text-white"
                />
                <span className="mt-2 block font-mono text-[10px] text-[var(--muted)]">
                  {expectedConfirmation}
                </span>
              </label>
            </div>
            {error ? (
              <p className="flex items-start gap-2 text-xs text-[var(--red)]">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {error}
              </p>
            ) : null}
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!ready || loading || readOnly}
              className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-[var(--ink)] hover:bg-[var(--acid-hover)] disabled:opacity-35"
            >
              <CheckCircle2 className="h-3.5 w-3.5" />
              Append settings revision
            </button>
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-4 py-4 sm:px-5">
          <h2 className="text-sm font-semibold">Worker application status</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">A worker counts as applied only when its signed heartbeat is under five minutes old and matches the effective revision and checksum.</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[48rem] text-left text-xs">
            <thead className="text-[var(--muted)]">
              <tr className="border-b border-[var(--line)]">
                <th className="px-5 py-3 font-medium">Instance</th><th className="px-3 py-3 font-medium">Status</th><th className="px-3 py-3 font-medium">Mode</th><th className="px-3 py-3 font-medium">Applied / expected</th><th className="px-3 py-3 font-medium">Source</th><th className="px-3 py-3 font-medium">Checksum</th><th className="px-5 py-3 font-medium">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {state.applied_instances.map((item) => (
                <tr key={item.instance_id} className="border-b border-[var(--line)] last:border-0">
                  <td className="px-5 py-3 font-medium">{item.instance_id}</td>
                  <td className="px-3 py-3">
                    <span className={`inline-flex items-center gap-1.5 font-medium ${item.fresh && item.matches_effective ? 'text-[var(--acid)]' : 'text-[var(--amber)]'}`}>
                      {item.fresh && item.matches_effective ? <CheckCircle2 className="h-3.5 w-3.5" /> : <Clock3 className="h-3.5 w-3.5" />}
                      {!item.fresh ? 'Stale' : item.matches_effective ? 'Applied' : 'Mismatch'}
                    </span>
                  </td>
                  <td className="px-3 py-3 capitalize">{item.mode}</td>
                  <td className="px-3 py-3">{item.revision} / {item.expected_revision}</td>
                  <td className="px-3 py-3 capitalize">{item.source}</td>
                  <td className="px-3 py-3 font-mono text-[var(--muted)]">{shortDigest(item.checksum)}</td>
                  <td className="px-5 py-3 text-[var(--muted)]">{new Date(item.seen_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {state.applied_instances.length === 0 ? <p className="px-5 py-8 text-xs text-[var(--muted)]">No v4 worker heartbeat has reported an applied revision yet.</p> : null}
        </div>
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-4 py-4 sm:px-5">
          <h2 className="text-sm font-semibold">Recent shadow observations</h2>
          <p className="mt-1 max-w-[72ch] text-xs leading-5 text-[var(--muted)]">
            Attempt-bound L2/L3 recommendations for calibration. These records never change a submission outcome.
          </p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[58rem] text-left text-xs">
            <thead className="text-[var(--muted)]">
              <tr className="border-b border-[var(--line)]">
                <th className="px-5 py-3 font-medium">Observed</th><th className="px-3 py-3 font-medium">Agent</th><th className="px-3 py-3 font-medium">Disposition</th><th className="px-3 py-3 font-medium">Review path</th><th className="px-3 py-3 font-medium">Models</th><th className="px-5 py-3 font-medium">Cost / tokens</th>
              </tr>
            </thead>
            <tbody>
              {state.shadow_observations.map((item) => {
                const tokens = Number(item.usage.input_tokens ?? 0) + Number(item.usage.output_tokens ?? 0)
                const cost = Number(item.usage.reported_cost_usd ?? item.usage.estimated_cost_usd ?? 0)
                return (
                  <tr key={item.attempt_id} className="border-b border-[var(--line)] align-top last:border-0">
                    <td className="px-5 py-3 text-[var(--muted)]">{new Date(item.created_at).toLocaleString()}</td>
                    <td className="px-3 py-3 font-mono" title={item.agent_id}>{item.agent_id.slice(0, 8)}…</td>
                    <td className="px-3 py-3 font-medium capitalize">{item.disposition.replace('_', ' ')}</td>
                    <td className="px-3 py-3 text-[var(--muted-strong)]">{item.clearance_path ?? item.resolution_basis ?? 'No terminal path'}</td>
                    <td className="px-3 py-3 text-[var(--muted)]">{item.response_models.join(' → ') || 'No response'}</td>
                    <td className="px-5 py-3 text-[var(--muted)]">${cost.toFixed(3)} · {tokens.toLocaleString()}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {state.shadow_observations.length === 0 ? <p className="px-5 py-8 text-xs text-[var(--muted)]">No shadow observations yet. Enable shadow on one exact worker to begin calibration.</p> : null}
        </div>
      </section>
    </div>
  )
}
