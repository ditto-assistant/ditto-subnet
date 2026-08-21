import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, Gauge, RefreshCw, Zap } from 'lucide-react'
import {
  INFERENCE_CONCURRENCY_CONFIRMATION,
  MAX_BENCHMARK_CASE_CONCURRENCY,
  MAX_CHAT_REQUEST_BUDGET,
  MAX_CHAT_TOKEN_BUDGET,
  MAX_CHAT_CONCURRENCY,
  MAX_EMBEDDING_CONCURRENCY,
  MAX_RELAY_DELAY_FINGERPRINT_MS,
  type InferenceConcurrencySettingsControl,
} from '../lib/admin.schemas'
import {
  getInferenceConcurrencySettings,
  updateInferenceConcurrencySettings,
} from '../server/admin.functions'

// The eight hosted-inference fields, in the order an operator reaches for them
// during an incident: the allowance that actually binds first, then the brake.
const fields = [
  {
    key: 'chat_token_budget',
    label: 'Chat token budget',
    max: MAX_CHAT_TOKEN_BUDGET,
    detail:
      'Chat tokens (prompt + completion) one scoring ticket may spend. This is the allowance that binds in practice — raising the request budget alone left the heaviest strategies failing in exactly the same place. A cap, not a spend: raising it changes only which runs are permitted to finish.',
  },
  {
    key: 'chat_request_budget',
    label: 'Chat request budget',
    max: MAX_CHAT_REQUEST_BUDGET,
    detail:
      'Chat completions one ticket may spend in total. The bound that survives a pathological loop of tiny requests, which the token budget would absorb slowly.',
  },
  {
    key: 'chat_per_ticket_concurrency',
    label: 'Chat · per ticket',
    max: MAX_CHAT_CONCURRENCY,
    detail: 'Concurrent hosted chat requests one scoring ticket may hold.',
  },
  {
    key: 'chat_per_validator_concurrency',
    label: 'Chat · per validator',
    max: MAX_CHAT_CONCURRENCY,
    detail: 'Concurrent hosted chat requests summed over one validator’s grants.',
  },
  {
    key: 'chat_global_concurrency',
    label: 'Chat · fleet',
    max: MAX_CHAT_CONCURRENCY,
    detail:
      'Concurrent hosted chat requests across the fleet. Live within five seconds without a relay restart.',
  },
  {
    key: 'embedding_per_ticket_concurrency',
    label: 'Embedding · per ticket',
    max: MAX_EMBEDDING_CONCURRENCY,
    detail:
      'Concurrent hosted embedding requests one ticket may hold. The emergency brake: lowering it takes effect fleet-wide on the next admission.',
  },
  {
    key: 'embedding_per_validator_concurrency',
    label: 'Embedding · per validator',
    max: MAX_EMBEDDING_CONCURRENCY,
    detail: 'Summed over one validator’s grants.',
  },
  {
    key: 'embedding_global_concurrency',
    label: 'Embedding · fleet',
    max: MAX_EMBEDDING_CONCURRENCY,
    detail:
      'Across the whole fleet. Enforced by a cross-grant aggregate, so it is best-effort under a simultaneous burst — size it as a load-shedding backstop, not an exact valve.',
  },
] as const

type FieldKey = (typeof fields)[number]['key']
type Draft = Record<FieldKey, string>

function draftFrom(control: InferenceConcurrencySettingsControl): Draft {
  const settings = control.effective.settings
  return {
    chat_token_budget: String(settings.chat_token_budget),
    chat_request_budget: String(settings.chat_request_budget),
    chat_per_ticket_concurrency: String(settings.chat_per_ticket_concurrency),
    chat_per_validator_concurrency: String(settings.chat_per_validator_concurrency),
    chat_global_concurrency: String(settings.chat_global_concurrency),
    embedding_per_ticket_concurrency: String(settings.embedding_per_ticket_concurrency),
    embedding_per_validator_concurrency: String(settings.embedding_per_validator_concurrency),
    embedding_global_concurrency: String(settings.embedding_global_concurrency),
  }
}

export function InferenceConcurrencyControlPanel({
  initialState,
  readOnly,
}: {
  initialState: InferenceConcurrencySettingsControl
  readOnly: boolean
}) {
  const refreshSettings = useServerFn(getInferenceConcurrencySettings)
  const applySettings = useServerFn(updateInferenceConcurrencySettings)
  const [state, setState] = useState(initialState)
  const [draft, setDraft] = useState<Draft>(() => draftFrom(initialState))
  const [caseConcurrency, setCaseConcurrency] = useState(
    String(initialState.effective.settings.benchmark_runtime.case_concurrency),
  )
  const [delayMode, setDelayMode] = useState<'off' | 'shadow'>(
    initialState.effective.settings.benchmark_runtime.relay_delay_fingerprint_mode,
  )
  const [delayMinMs, setDelayMinMs] = useState(
    String(initialState.effective.settings.benchmark_runtime.relay_delay_fingerprint_min_ms),
  )
  const [delayMaxMs, setDelayMaxMs] = useState(
    String(initialState.effective.settings.benchmark_runtime.relay_delay_fingerprint_max_ms),
  )
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState<{ kind: 'error' | 'success'; text: string } | null>(null)

  const effective = state.effective
  const parsed = Object.fromEntries(
    fields.map((field) => [field.key, Number(draft[field.key])]),
  ) as Record<FieldKey, number>

  const invalid = fields.filter((field) => {
    const value = parsed[field.key]
    return !Number.isInteger(value) || value < 1 || value > field.max
  })
  // The platform enforces this ordering and 422s a violation. Surface it here so
  // the operator sees which field is wrong without spending a round trip.
  const hierarchyBroken =
    invalid.length === 0 &&
    (parsed.chat_per_ticket_concurrency > parsed.chat_per_validator_concurrency ||
      parsed.chat_per_validator_concurrency > parsed.chat_global_concurrency ||
      parsed.embedding_per_ticket_concurrency > parsed.embedding_per_validator_concurrency ||
      parsed.embedding_per_validator_concurrency > parsed.embedding_global_concurrency)

  const parsedCaseConcurrency = Number(caseConcurrency)
  const parsedDelayMinMs = Number(delayMinMs)
  const parsedDelayMaxMs = Number(delayMaxMs)
  const runtimeInvalid =
    !Number.isInteger(parsedCaseConcurrency) ||
    parsedCaseConcurrency < 1 ||
    parsedCaseConcurrency > MAX_BENCHMARK_CASE_CONCURRENCY ||
    !Number.isInteger(parsedDelayMinMs) ||
    parsedDelayMinMs < 0 ||
    parsedDelayMinMs > MAX_RELAY_DELAY_FINGERPRINT_MS ||
    !Number.isInteger(parsedDelayMaxMs) ||
    parsedDelayMaxMs < parsedDelayMinMs ||
    parsedDelayMaxMs > MAX_RELAY_DELAY_FINGERPRINT_MS

  const changed =
    fields.some((field) => parsed[field.key] !== effective.settings[field.key]) ||
    parsedCaseConcurrency !== effective.settings.benchmark_runtime.case_concurrency ||
    delayMode !== effective.settings.benchmark_runtime.relay_delay_fingerprint_mode ||
    parsedDelayMinMs !==
      effective.settings.benchmark_runtime.relay_delay_fingerprint_min_ms ||
    parsedDelayMaxMs !== effective.settings.benchmark_runtime.relay_delay_fingerprint_max_ms
  const ready =
    changed &&
    invalid.length === 0 &&
    !hierarchyBroken &&
    !runtimeInvalid &&
    reason.trim().length >= 8 &&
    confirmation === INFERENCE_CONCURRENCY_CONFIRMATION

  // Only the embedding limits are enforced at admission. Lowering one is the
  // live brake; the budgets are stamped at mint and cannot touch a running lease.
  const loweringBrake =
    invalid.length === 0 &&
    parsed.embedding_per_ticket_concurrency < effective.settings.embedding_per_ticket_concurrency

  function reset(next = state) {
    setDraft(draftFrom(next))
    setCaseConcurrency(String(next.effective.settings.benchmark_runtime.case_concurrency))
    setDelayMode(next.effective.settings.benchmark_runtime.relay_delay_fingerprint_mode)
    setDelayMinMs(
      String(next.effective.settings.benchmark_runtime.relay_delay_fingerprint_min_ms),
    )
    setDelayMaxMs(
      String(next.effective.settings.benchmark_runtime.relay_delay_fingerprint_max_ms),
    )
    setReason('')
    setConfirmation('')
  }

  async function refresh() {
    setLoading(true)
    setMessage(null)
    try {
      const next = await refreshSettings()
      setState(next)
      reset(next)
    } catch (cause) {
      setMessage({
        kind: 'error',
        text: cause instanceof Error ? cause.message : 'Unable to refresh inference policy',
      })
    } finally {
      setLoading(false)
    }
  }

  async function submit() {
    if (!ready) return
    setLoading(true)
    setMessage(null)
    try {
      // The whole object, every time: a revision is never a diff.
      const next = await applySettings({
        data: {
          expectedRevision: effective.revision,
          settings: {
            chat_request_budget: parsed.chat_request_budget,
            chat_token_budget: parsed.chat_token_budget,
            chat_per_ticket_concurrency: parsed.chat_per_ticket_concurrency,
            chat_per_validator_concurrency: parsed.chat_per_validator_concurrency,
            chat_global_concurrency: parsed.chat_global_concurrency,
            embedding_per_ticket_concurrency: parsed.embedding_per_ticket_concurrency,
            embedding_per_validator_concurrency: parsed.embedding_per_validator_concurrency,
            embedding_global_concurrency: parsed.embedding_global_concurrency,
            benchmark_runtime: {
              case_concurrency: parsedCaseConcurrency,
              relay_delay_fingerprint_mode: delayMode,
              relay_delay_fingerprint_min_ms: parsedDelayMinMs,
              relay_delay_fingerprint_max_ms: parsedDelayMaxMs,
            },
          },
          reason,
          confirmation,
        },
      })
      setState(next)
      reset(next)
      setMessage({ kind: 'success', text: 'Inference and benchmark runtime policy updated.' })
    } catch (cause) {
      setMessage({
        kind: 'error',
        text: cause instanceof Error ? cause.message : 'Unable to update inference policy',
      })
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="mt-6 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
            <Gauge className="h-4 w-4" />
          </div>
          <div>
            <h2 className="text-sm font-semibold">Current inference and runtime policy</h2>
            <p className="mt-1 max-w-[76ch] text-xs leading-5 text-[var(--muted)]">
              What one scoring ticket may spend, how parallel inference traffic may be, and
              how a newly issued v10 lease schedules and fingerprints its cases.
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
          Refresh policy
        </button>
      </div>

      <div className="p-4 sm:p-5">
        <dl className="grid gap-3 text-xs sm:grid-cols-3">
          <div>
            <dt className="text-[var(--muted)]">Revision</dt>
            <dd className="mt-1 font-semibold">{effective.revision}</dd>
          </div>
          <div>
            <dt className="text-[var(--muted)]">Source</dt>
            <dd className="mt-1 font-semibold">
              {effective.source === 'revision' ? 'Operator revision' : 'Shipped default'}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--muted)]">Propagation</dt>
            <dd className="mt-1 font-semibold">Admission ≤5s · runtime next lease</dd>
          </div>
        </dl>

        <p className="mt-4 flex gap-3 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4 text-xs leading-5 text-[var(--muted-strong)]">
          <Zap className="mt-0.5 h-4 w-4 shrink-0 text-[var(--cyan)]" />
          <span>
            The two budgets are stamped onto a grant when it is minted, so a change here
            governs the <strong>next</strong> lease and can never retroactively exhaust a run
            already in flight. Both chat and embedding concurrency limits are enforced at
            admission instead — which is what makes lowering a per-ticket limit safe to pull
            mid-run: a concurrency decline is answered with 503 and Retry-After, so a validator
            backs off and continues rather than discarding the run.
          </span>
        </p>

        <div className="mt-5 grid gap-3 lg:grid-cols-2">
          {fields.map((field) => {
            const value = parsed[field.key]
            const bad = !Number.isInteger(value) || value < 1 || value > field.max
            const current = effective.settings[field.key]
            return (
              <div
                key={field.key}
                className="rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4"
              >
                <label className="block text-xs font-medium text-[var(--muted-strong)]">
                  {field.label}{' '}
                  <span className="font-normal text-[var(--muted)]">
                    (1–{field.max.toLocaleString()})
                  </span>
                  <input
                    type="number"
                    inputMode="numeric"
                    min={1}
                    max={field.max}
                    step={1}
                    value={draft[field.key]}
                    disabled={readOnly || loading}
                    onChange={(event) =>
                      setDraft((previous) => ({ ...previous, [field.key]: event.target.value }))
                    }
                    className="mt-2 block min-h-11 w-44 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm outline-none focus:border-[var(--cyan)] disabled:opacity-45"
                  />
                </label>
                <p className="mt-2 text-[11px] leading-4 text-[var(--muted)]">{field.detail}</p>
                {value !== current && !bad ? (
                  <p className="mt-2 text-[11px] font-medium text-[var(--amber)]">
                    In force: {current.toLocaleString()}
                  </p>
                ) : null}
                {bad ? (
                  <p className="mt-2 text-[11px] text-[var(--red)]">
                    Must be a whole number between 1 and {field.max.toLocaleString()}.
                  </p>
                ) : null}
              </div>
            )
          })}
        </div>

        {hierarchyBroken ? (
          <p className="mt-4 flex gap-3 rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] p-4 text-xs leading-5 text-[var(--red)]">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            Both chat and embedding limits must satisfy per-ticket ≤ per-validator ≤ fleet.
            A ticket cannot be allowed more concurrency than the validator hosting it, and no
            validator more than the fleet.
          </p>
        ) : null}

        <fieldset className="mt-6 border-t border-[var(--line)] pt-5">
          <legend className="pr-3 text-xs font-semibold text-[var(--muted-strong)]">
            V10 benchmark runtime
          </legend>
          <p className="mb-4 max-w-[76ch] text-[11px] leading-4 text-[var(--muted)]">
            Stamped onto newly issued v10+ leases. In-flight leases keep their stamp. v9 has
            no stamp and uses the scorer default (4). A stored revision of 1 stays live until
            you write a new whole-object policy. Values above 16 need every validator on a
            release that accepts 1–64; older validators reject such leases outright.
          </p>
          <div className="grid gap-4 lg:grid-cols-2">
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              V10 case concurrency{' '}
              <span className="font-normal text-[var(--muted)]">
                (1–{MAX_BENCHMARK_CASE_CONCURRENCY})
              </span>
              <input
                type="number"
                min={1}
                max={MAX_BENCHMARK_CASE_CONCURRENCY}
                step={1}
                value={caseConcurrency}
                disabled={readOnly || loading}
                onChange={(event) => setCaseConcurrency(event.target.value)}
                className="mt-2 block min-h-11 w-44 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm outline-none focus:border-[var(--cyan)] disabled:opacity-45"
              />
              <span className="mt-2 block text-[11px] font-normal leading-4 text-[var(--muted)]">
                How many /run calls one ticket may overlap. Default 4. Miners keep one
                process-wide inference URL.
              </span>
            </label>

            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Relay delay fingerprint
              <select
                value={delayMode}
                disabled={readOnly || loading}
                onChange={(event) => setDelayMode(event.target.value as 'off' | 'shadow')}
                className="mt-2 block min-h-11 w-44 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm outline-none focus:border-[var(--cyan)] disabled:opacity-45"
              >
                <option value="off">Off</option>
                <option value="shadow">Shadow</option>
              </select>
              <span className="mt-2 block text-[11px] font-normal leading-4 text-[var(--muted)]">
                Shadow injects a secret deterministic hold and records evidence only inside
                confirmation case windows; ordinary scored runs no longer open case windows, so
                it is a no-op there. It never changes scores.
              </span>
            </label>

            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Minimum delay (ms)
              <input
                type="number"
                min={0}
                max={MAX_RELAY_DELAY_FINGERPRINT_MS}
                step={1}
                value={delayMinMs}
                disabled={readOnly || loading}
                onChange={(event) => setDelayMinMs(event.target.value)}
                className="mt-2 block min-h-11 w-44 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm outline-none focus:border-[var(--cyan)] disabled:opacity-45"
              />
            </label>

            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Maximum delay (ms)
              <input
                type="number"
                min={0}
                max={MAX_RELAY_DELAY_FINGERPRINT_MS}
                step={1}
                value={delayMaxMs}
                disabled={readOnly || loading}
                onChange={(event) => setDelayMaxMs(event.target.value)}
                className="mt-2 block min-h-11 w-44 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm outline-none focus:border-[var(--cyan)] disabled:opacity-45"
              />
            </label>
          </div>
          {runtimeInvalid ? (
            <p className="mt-3 text-[11px] text-[var(--red)]">
              Case concurrency must be 1–{MAX_BENCHMARK_CASE_CONCURRENCY}; delays must satisfy
              0 ≤ minimum ≤ maximum ≤ {MAX_RELAY_DELAY_FINGERPRINT_MS.toLocaleString()} ms.
            </p>
          ) : null}
        </fieldset>

        {loweringBrake ? (
          <p className="mt-4 flex gap-3 rounded-lg border border-[var(--amber)]/30 bg-[var(--amber-dim)] p-4 text-xs leading-5 text-[var(--muted-strong)]">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--amber)]" />
            <span>
              This lowers the per-ticket embedding brake from{' '}
              {effective.settings.embedding_per_ticket_concurrency} to{' '}
              {parsed.embedding_per_ticket_concurrency}. That takes effect on the next
              admission fleet-wide. Runs in flight keep going — they will simply embed more
              slowly.
            </span>
          </p>
        ) : null}

        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <label className="text-xs font-medium text-[var(--muted-strong)]">
            Operator reason
            <input
              value={reason}
              disabled={readOnly || loading || !changed}
              onChange={(event) => setReason(event.target.value)}
              className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none focus:border-[var(--cyan)] disabled:opacity-45"
            />
          </label>
          <label className="text-xs font-medium text-[var(--muted-strong)]">
            Type to confirm
            <code className="ml-2 text-[11px] text-[var(--cyan)]">
              {INFERENCE_CONCURRENCY_CONFIRMATION}
            </code>
            <input
              value={confirmation}
              disabled={readOnly || loading || !changed}
              onChange={(event) => setConfirmation(event.target.value)}
              className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 font-mono text-xs outline-none focus:border-[var(--cyan)] disabled:opacity-45"
            />
          </label>
        </div>

        {message ? (
          <p
            className={`mt-4 flex items-center gap-2 text-xs ${
              message.kind === 'error' ? 'text-[var(--red)]' : 'text-[var(--acid)]'
            }`}
          >
            {message.kind === 'success' ? <CheckCircle2 className="h-4 w-4" /> : null}
            {message.text}
          </p>
        ) : null}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => reset()}
            disabled={loading || !changed}
            className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-medium disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={readOnly || loading || !ready}
            className="min-h-11 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-black disabled:opacity-35"
          >
            {loading ? 'Applying…' : 'Apply policy'}
          </button>
        </div>
      </div>
    </section>
  )
}
