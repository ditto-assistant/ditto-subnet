import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import {
  AlertTriangle,
  CheckCircle2,
  HardDrive,
  Layers,
  PauseCircle,
  PlayCircle,
  RefreshCw,
} from 'lucide-react'
import {
  CEILING_DISABLED,
  DISK_PERCENT_QUANTUM,
  MIN_ENABLED_CEILING,
  VALIDATOR_HARD_SLOT_CEILING,
  validatorIssuanceConfirmation,
  validatorSlotConfirmation,
  validatorSlotSettingsSchema,
  type ValidatorFleet,
  type ValidatorSlotSettingsControl,
} from '../lib/admin.schemas'
import {
  getValidatorFleet,
  getValidatorSlotSettings,
  updateValidatorIssuancePause,
  updateValidatorSlotSettings,
} from '../server/admin.functions'

function formatWhen(value: string) {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed)
}

function formatEpoch(value: number | null | undefined) {
  return value == null ? null : formatWhen(new Date(value * 1000).toISOString())
}

function updaterLabel(updater: ValidatorFleet['validators'][number]['updater_status']) {
  if (updater === null) return 'unreported'
  if (updater.state === 'not_managed') return 'self-managed'
  if (updater.state === 'disabled') return 'disabled'
  if (updater.state === 'unavailable') return 'unavailable'
  if (updater.self_refresh_installed === false) return 'updater refresh missing'
  const version = updater.candidate_version ?? updater.current_version
  return `${updater.state}${version ? ` · v${version}` : ''}`
}

function updaterDetail(updater: ValidatorFleet['validators'][number]['updater_status']) {
  if (updater === null) return 'Requires heartbeat protocol v23 or newer.'
  const details = [
    `state: ${updater.state}`,
    updater.transaction_phase ? `phase: ${updater.transaction_phase}` : null,
    updater.current_version ? `current version: ${updater.current_version}` : null,
    updater.current_descriptor ? `current descriptor: ${updater.current_descriptor}` : null,
    updater.candidate_version ? `candidate version: ${updater.candidate_version}` : null,
    updater.candidate_descriptor ? `candidate descriptor: ${updater.candidate_descriptor}` : null,
    updater.failed_candidate_count > 0
      ? `failures: ${updater.failed_candidate_count}`
      : null,
    updater.last_failure_reason ? `last failure: ${updater.last_failure_reason}` : null,
    updater.retry_after ? `retry after: ${formatEpoch(updater.retry_after)}` : null,
    updater.last_success_at ? `last success: ${formatEpoch(updater.last_success_at)}` : null,
    updater.self_refresh_installed == null
      ? 'updater self-refresh: unreported (heartbeat protocol before v26)'
      : `updater self-refresh: ${updater.self_refresh_installed ? 'installed' : 'missing'}`,
    updater.self_refresh_revision
      ? `updater revision: ${updater.self_refresh_revision}`
      : null,
    updater.self_refresh_last_success_at
      ? `updater last refreshed: ${formatEpoch(updater.self_refresh_last_success_at)}`
      : null,
  ]
  return details.filter(Boolean).join('\n')
}

function shortHotkey(hotkey: string) {
  return hotkey.length > 14 ? `${hotkey.slice(0, 8)}…${hotkey.slice(-4)}` : hotkey
}

/** A ceiling of zero is off, not "gate at 0%"; never render it as a percentage. */
function describeCeiling(value: number) {
  return value === CEILING_DISABLED ? 'off' : `${value}%`
}

/** Whole numbers only; a blank or malformed box must not read as zero. */
function toWholeNumber(value: string) {
  const trimmed = value.trim()
  if (!/^-?\d+$/.test(trimmed)) return Number.NaN
  return Number(trimmed)
}

export function ValidatorSlotControlPanel({
  initialState,
  initialFleet,
  readOnly,
}: {
  initialState: ValidatorSlotSettingsControl
  initialFleet: ValidatorFleet | null
  readOnly: boolean
}) {
  const refreshSettings = useServerFn(getValidatorSlotSettings)
  const refreshFleet = useServerFn(getValidatorFleet)
  const applySettings = useServerFn(updateValidatorSlotSettings)
  const applyIssuancePause = useServerFn(updateValidatorIssuancePause)

  const [state, setState] = useState(initialState)
  const [fleet, setFleet] = useState(initialFleet)
  const [cap, setCap] = useState(String(initialState.effective.settings.max_concurrent_slots))
  const [diskCeiling, setDiskCeiling] = useState(
    String(initialState.effective.settings.disk_percent_ceiling),
  )
  const [memoryCeiling, setMemoryCeiling] = useState(
    String(initialState.effective.settings.memory_percent_ceiling),
  )
  const [cpuCeiling, setCpuCeiling] = useState(
    String(initialState.effective.settings.cpu_percent_ceiling),
  )
  const [blockCeiling, setBlockCeiling] = useState(
    String(initialState.effective.settings.resource_block_percent_ceiling),
  )
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [pauseTarget, setPauseTarget] = useState<{
    hotkey: string
    paused: boolean
  } | null>(null)
  const [pauseReason, setPauseReason] = useState('')
  const [pauseConfirmation, setPauseConfirmation] = useState('')

  const effective = state.effective
  const inForce = effective.settings
  // The platform reports the protocol maximum with the policy; the shipped
  // constant is only the fallback when an older platform omits it.
  const hardCeiling = Math.min(
    effective.hard_slot_ceiling || VALIDATOR_HARD_SLOT_CEILING,
    VALIDATOR_HARD_SLOT_CEILING,
  )

  const proposed = {
    max_concurrent_slots: toWholeNumber(cap),
    disk_percent_ceiling: toWholeNumber(diskCeiling),
    memory_percent_ceiling: toWholeNumber(memoryCeiling),
    cpu_percent_ceiling: toWholeNumber(cpuCeiling),
    resource_block_percent_ceiling: toWholeNumber(blockCeiling),
    paused_validator_hotkeys: inForce.paused_validator_hotkeys,
  }
  // The same schema the MCP tool validates against, so the console cannot send a
  // policy the platform would reject and cannot invent a bound of its own.
  const parsed = validatorSlotSettingsSchema.safeParse(proposed)
  const overHardCeiling =
    Number.isInteger(proposed.max_concurrent_slots) &&
    proposed.max_concurrent_slots > hardCeiling
  const boundsProblems = [
    ...(parsed.success ? [] : parsed.error.issues.map((issue) => issue.message)),
    ...(overHardCeiling
      ? [
          `max_concurrent_slots cannot exceed hard_slot_ceiling (${hardCeiling}), the protocol maximum a validator can advertise`,
        ]
      : []),
  ]
  const valid = parsed.success && !overHardCeiling
  const changed =
    valid &&
    (proposed.max_concurrent_slots !== inForce.max_concurrent_slots ||
      proposed.disk_percent_ceiling !== inForce.disk_percent_ceiling ||
      proposed.memory_percent_ceiling !== inForce.memory_percent_ceiling ||
      proposed.cpu_percent_ceiling !== inForce.cpu_percent_ceiling ||
      proposed.resource_block_percent_ceiling !== inForce.resource_block_percent_ceiling)
  const reasonReady = reason.trim().length >= 8
  // Deliberately NOT gated on the confirmation. It is checked on submit instead,
  // so a mistyped cap earns a refusal that says why rather than a button that is
  // silently dead.
  const canSubmit = !readOnly && !loading && changed && reasonReady

  const activeCeiling = valid ? proposed.disk_percent_ceiling : inForce.disk_percent_ceiling
  const pendingCap = valid ? proposed.max_concurrent_slots : inForce.max_concurrent_slots

  function ticketedSlots(advertised: number, disk: number | null, capValue: number) {
    const restricted = disk !== null && disk >= activeCeiling
    return Math.min(advertised, restricted ? effective.disk_restricted_slots : capValue)
  }

  function resetForm(next = state) {
    setCap(String(next.effective.settings.max_concurrent_slots))
    setDiskCeiling(String(next.effective.settings.disk_percent_ceiling))
    setMemoryCeiling(String(next.effective.settings.memory_percent_ceiling))
    setCpuCeiling(String(next.effective.settings.cpu_percent_ceiling))
    setBlockCeiling(String(next.effective.settings.resource_block_percent_ceiling))
    setReason('')
    setConfirmation('')
    setError('')
  }

  async function refresh() {
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const [nextState, nextFleet] = await Promise.all([refreshSettings(), refreshFleet()])
      setState(nextState)
      setFleet(nextFleet)
      resetForm(nextState)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh the slot policy')
    } finally {
      setLoading(false)
    }
  }

  async function submit() {
    if (!canSubmit || !parsed.success) return
    // Both halves of the double statement are checked against each other here,
    // before anything leaves the browser. The expected phrase is never written
    // into the box for the operator: typing the resulting cap a second time is
    // the whole safeguard, and pre-filling it would confirm nothing.
    if (confirmation !== validatorSlotConfirmation(proposed.max_concurrent_slots)) {
      setSuccess('')
      setError(
        `Nothing was sent. The confirmation must read APPLY VALIDATOR SLOT CAP followed by the cap this revision applies (${proposed.max_concurrent_slots}), typed out.`,
      )
      return
    }
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await applySettings({
        data: {
          expectedRevision: effective.revision,
          settings: parsed.data,
          reason,
          confirmation,
        },
      })
      setState(next)
      resetForm(next)
      setSuccess(
        `Slot policy applied as revision ${next.effective.revision}: cap ${next.effective.settings.max_concurrent_slots}, disk ${describeCeiling(next.effective.settings.disk_percent_ceiling)}, memory ${describeCeiling(next.effective.settings.memory_percent_ceiling)}, CPU ${describeCeiling(next.effective.settings.cpu_percent_ceiling)}, hard stop ${describeCeiling(next.effective.settings.resource_block_percent_ceiling)}.`,
      )
      void refreshFleet().then(setFleet).catch(() => {})
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to apply the slot policy')
    } finally {
      setLoading(false)
    }
  }

  async function submitIssuancePause() {
    if (
      readOnly ||
      loading ||
      pauseTarget === null ||
      pauseReason.trim().length < 8
    ) {
      return
    }
    const expected = validatorIssuanceConfirmation(
      pauseTarget.hotkey,
      pauseTarget.paused,
    )
    if (pauseConfirmation !== expected) {
      setSuccess('')
      setError(`Nothing was sent. The confirmation must be exactly ${expected}.`)
      return
    }
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await applyIssuancePause({
        data: {
          validatorHotkey: pauseTarget.hotkey,
          paused: pauseTarget.paused,
          expectedRevision: effective.revision,
          reason: pauseReason,
          confirmation: pauseConfirmation,
        },
      })
      setState(next)
      setPauseTarget(null)
      setPauseReason('')
      setPauseConfirmation('')
      setSuccess(
        `${pauseTarget.paused ? 'Paused' : 'Resumed'} new issuance for ${shortHotkey(pauseTarget.hotkey)} at revision ${next.effective.revision}. Live leases were not revoked.`,
      )
      void refreshFleet().then(setFleet).catch(() => {})
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to change validator issuance')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mt-6 space-y-5">
      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
              <Layers className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-semibold">Slot policy in force</h2>
              <p className="mt-1 max-w-[76ch] text-xs leading-5 text-[var(--muted)]">
                How many advertised benchmark slots receive tickets on any one validator, plus
                the disk circuit breaker. Both are evaluated at ticket issue time only: a change
                never revokes a live lease, so an in-flight benchmark always runs to completion
                and a ramp down drains rather than aborts.
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
            Refresh policy
          </button>
        </div>

        <div className="p-4 sm:p-5">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="text-xs text-[var(--muted)]">Concurrent slot cap</p>
              <p className="mt-2 text-3xl font-semibold tracking-tight">
                {inForce.max_concurrent_slots}
                <span className="ml-2 text-sm font-normal text-[var(--muted)]">
                  of {hardCeiling} advertisable
                </span>
              </p>
              <p className="mt-2 text-xs text-[var(--muted-strong)]">
                {effective.source === 'revision' ? (
                  <>
                    Operator revision {effective.revision}. Somebody chose this.
                  </>
                ) : (
                  <>
                    Shipped default (revision 0). No operator revision has ever been written.
                  </>
                )}
              </p>
            </div>
            <dl className="grid gap-3 text-xs sm:grid-cols-2 sm:text-right lg:grid-cols-4">
              <div>
                <dt className="text-[var(--muted)]">Ceilings (disk / mem / cpu)</dt>
                <dd className="mt-1 font-medium">
                  {describeCeiling(inForce.disk_percent_ceiling)} /{' '}
                  {describeCeiling(inForce.memory_percent_ceiling)} /{' '}
                  {describeCeiling(inForce.cpu_percent_ceiling)}
                </dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Hard stop</dt>
                <dd className="mt-1 font-medium">
                  {describeCeiling(inForce.resource_block_percent_ceiling)}
                </dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Restricted to</dt>
                <dd className="mt-1 font-medium">
                  {effective.disk_restricted_slots}{' '}
                  {effective.disk_restricted_slots === 1 ? 'slot' : 'slots'}
                </dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Propagation</dt>
                <dd className="mt-1 font-medium">Within {effective.max_age_seconds}s</dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Shipped default</dt>
                <dd className="mt-1 font-medium">
                  {state.default.max_concurrent_slots} /{' '}
                  {describeCeiling(state.default.disk_percent_ceiling)}
                </dd>
              </div>
            </dl>
          </div>

          <p className="mt-5 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4 text-xs leading-5 text-[var(--muted-strong)]">
            A validator advertises its own capacity in the heartbeat; this cap decides how many
            of those advertised slots the platform fills with live tickets, so it can narrow the
            fleet but never widen it past what a validator can serve. A cap of 1 is the kill
            switch: dispatch returns to strictly serial. A validator whose latest heartbeat
            reports disk, memory or CPU at or above its ceiling is held to{' '}
            {effective.disk_restricted_slots}{' '}
            {effective.disk_restricted_slots === 1 ? 'slot' : 'slots'}; at or above the hard
            stop it is issued nothing at all until it recovers. A ceiling of {CEILING_DISABLED}{' '}
            turns that resource off in both tiers, which is why CPU ships off — a pinned CPU is
            a working benchmark host, not a failing one. All of it is evaluated at issue time
            only: live leases keep running, and the restriction lifts on its own when a fresh
            heartbeat reports headroom. Changes reach the dispatch path within{' '}
            {effective.max_age_seconds}s with no platform restart.
          </p>
        </div>
      </section>

      <FleetContext
        fleet={fleet}
        ceiling={activeCeiling}
        currentCeiling={inForce.disk_percent_ceiling}
        currentCap={inForce.max_concurrent_slots}
        pendingCap={pendingCap}
        restrictedSlots={effective.disk_restricted_slots}
        ticketedSlots={ticketedSlots}
        pausedHotkeys={new Set(inForce.paused_validator_hotkeys)}
        readOnly={readOnly}
        loading={loading}
        onToggleIssuance={(hotkey, paused) => {
          setPauseTarget({ hotkey, paused })
          setPauseReason('')
          setPauseConfirmation('')
          setError('')
          setSuccess('')
        }}
      />

      {pauseTarget !== null ? (
        <section className="overflow-hidden rounded-xl border border-[var(--amber)]/40 bg-[var(--panel)]">
          <div className="border-b border-[var(--line)] bg-[var(--panel-raised)] px-4 py-3">
            <h2 className="text-sm font-semibold">
              {pauseTarget.paused ? 'Pause' : 'Resume'} validator issuance
            </h2>
            <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
              This changes new lease issuance only. Any live benchmark, continual retest, or
              LongMem confirmation lease keeps running and may report normally.
            </p>
          </div>
          <div className="p-4 sm:p-5">
            <p className="break-all font-mono text-xs text-[var(--muted-strong)]">
              {pauseTarget.hotkey}
            </p>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <label className="text-xs font-medium text-[var(--muted-strong)]">
                Operator reason (at least 8 characters)
                <input
                  type="text"
                  value={pauseReason}
                  disabled={readOnly || loading}
                  onChange={(event) => setPauseReason(event.target.value)}
                  placeholder="Why this validator should drain"
                  className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none focus:border-[var(--cyan)] disabled:opacity-45"
                />
              </label>
              <label className="text-xs font-medium text-[var(--muted-strong)]">
                Type to confirm
                <code className="ml-2 break-all text-[11px] text-[var(--cyan)]">
                  {validatorIssuanceConfirmation(pauseTarget.hotkey, pauseTarget.paused)}
                </code>
                <input
                  type="text"
                  value={pauseConfirmation}
                  disabled={readOnly || loading}
                  onChange={(event) => setPauseConfirmation(event.target.value)}
                  placeholder="Type the exact validator action"
                  className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 font-mono text-xs outline-none focus:border-[var(--cyan)] disabled:opacity-45"
                />
              </label>
            </div>
            <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <button
                type="button"
                disabled={loading}
                onClick={() => setPauseTarget(null)}
                className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-medium text-[var(--muted-strong)] disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={readOnly || loading || pauseReason.trim().length < 8}
                onClick={() => void submitIssuancePause()}
                className="min-h-11 rounded-lg bg-[var(--amber)] px-4 text-xs font-semibold text-black disabled:opacity-35"
              >
                {loading
                  ? 'Applying…'
                  : `${pauseTarget.paused ? 'Pause' : 'Resume'} new issuance`}
              </button>
            </div>
          </div>
        </section>
      ) : null}

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] bg-[var(--panel-raised)] px-4 py-3">
          <h2 className="text-sm font-semibold">Apply a new revision</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">
            A revision stores the whole policy, never a diff. Every knob is written every time,
            against revision {effective.revision}; a concurrent write is refused rather than
            overwritten.
          </p>
        </div>

        <div className="p-4 sm:p-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Concurrent slot cap (1–{hardCeiling})
              <input
                type="number"
                inputMode="numeric"
                min={1}
                max={hardCeiling}
                step={1}
                value={cap}
                disabled={readOnly || loading}
                onChange={(event) => {
                  setCap(event.target.value)
                  setConfirmation('')
                  setError('')
                  setSuccess('')
                }}
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
              />
            </label>
            {(
              [
                ['Disk percent ceiling', diskCeiling, setDiskCeiling],
                ['Memory percent ceiling', memoryCeiling, setMemoryCeiling],
                ['CPU percent ceiling', cpuCeiling, setCpuCeiling],
                ['Hard stop percent ceiling', blockCeiling, setBlockCeiling],
              ] as const
            ).map(([label, value, setValue]) => (
              <label key={label} className="text-xs font-medium text-[var(--muted-strong)]">
                {label} ({CEILING_DISABLED} = off, or {MIN_ENABLED_CEILING}–100 in steps of{' '}
                {DISK_PERCENT_QUANTUM})
                <input
                  type="number"
                  inputMode="numeric"
                  min={CEILING_DISABLED}
                  max={100}
                  step={DISK_PERCENT_QUANTUM}
                  value={value}
                  disabled={readOnly || loading}
                  onChange={(event) => {
                    setValue(event.target.value)
                    setError('')
                    setSuccess('')
                  }}
                  className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
                />
              </label>
            ))}
          </div>

          {boundsProblems.length > 0 ? (
            <ul className="mt-3 space-y-1 text-[11px] leading-5 text-[var(--red)]">
              {boundsProblems.map((problem) => (
                <li key={problem} className="flex gap-2">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  {problem}
                </li>
              ))}
            </ul>
          ) : null}

          {valid && proposed.max_concurrent_slots === 1 ? (
            <p className="mt-3 flex gap-3 rounded-lg border border-[var(--amber)]/30 bg-[var(--amber-dim)] p-4 text-xs leading-5 text-[var(--muted-strong)]">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--amber)]" />
              <span>
                A cap of 1 is the kill switch: dispatch returns to one ticket at a time across
                the whole fleet. Leases already issued still run to completion.
              </span>
            </p>
          ) : null}

          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Operator reason (at least 8 characters)
              <input
                type="text"
                value={reason}
                disabled={readOnly || loading || !changed}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Why the fleet is moving to this cap"
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 text-sm outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
              />
            </label>
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Type to confirm
              <code className="ml-2 break-all text-[11px] text-[var(--cyan)]">
                APPLY VALIDATOR SLOT CAP &lt;cap&gt;
              </code>
              <input
                type="text"
                value={confirmation}
                disabled={readOnly || loading || !changed}
                onChange={(event) => setConfirmation(event.target.value)}
                placeholder="Type the phrase, naming the cap you are applying"
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 font-mono text-xs outline-none transition-colors focus:border-[var(--cyan)] disabled:opacity-45"
              />
              <span className="mt-1 block text-[11px] leading-4 text-[var(--muted)]">
                Write the cap out yourself. The phrase is never filled in for you: stating the
                resulting cap a second time is what stops a mistyped ramp from landing silently.
              </span>
            </label>
          </div>

          {error ? <p className="mt-4 text-xs leading-5 text-[var(--red)]">{error}</p> : null}
          {success ? (
            <p className="mt-4 flex items-center gap-2 text-xs text-[var(--acid)]">
              <CheckCircle2 className="h-4 w-4" />
              {success}
            </p>
          ) : null}

          <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={() => resetForm()}
              disabled={loading || !changed}
              className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-medium text-[var(--muted-strong)] transition-colors hover:bg-white/5 disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!canSubmit}
              className="min-h-11 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-black transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-35"
            >
              {loading ? 'Applying…' : 'Apply slot policy'}
            </button>
          </div>
        </div>
      </section>

      <RevisionHistory state={state} />
    </div>
  )
}

function FleetContext({
  fleet,
  ceiling,
  currentCeiling,
  currentCap,
  pendingCap,
  restrictedSlots,
  ticketedSlots,
  pausedHotkeys,
  readOnly,
  loading,
  onToggleIssuance,
}: {
  fleet: ValidatorFleet | null
  ceiling: number
  currentCeiling: number
  currentCap: number
  pendingCap: number
  restrictedSlots: number
  ticketedSlots: (advertised: number, disk: number | null, capValue: number) => number
  pausedHotkeys: Set<string>
  readOnly: boolean
  loading: boolean
  onToggleIssuance: (hotkey: string, paused: boolean) => void
}) {
  return (
    <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="border-b border-[var(--line)] bg-[var(--panel-raised)] px-4 py-3">
        <div className="flex items-center gap-2">
          <HardDrive className="h-3.5 w-3.5 text-[var(--cyan)]" />
          <h2 className="text-sm font-semibold">Fleet right now</h2>
          {fleet?.active_bench_version != null ? (
            <span className="rounded-full border border-[var(--line)] px-2 py-0.5 text-[10px] text-[var(--muted-strong)]">
              Scoring v{fleet.active_bench_version}
            </span>
          ) : null}
        </div>
        <p className="mt-1 text-xs text-[var(--muted)]">
          Advertised capacity and reported disk from the latest signed heartbeats. Slots filled
          is what the cap would issue at the next ticket, not a live reassignment.
        </p>
        {ceiling !== currentCeiling ? (
          <p className="mt-1 text-xs text-[var(--amber)]">
            Previewing the {ceiling}% ceiling you typed. The {currentCeiling}% ceiling is still
            the one in force.
          </p>
        ) : null}
      </div>
      {fleet === null || fleet.validators.length === 0 ? (
        <p className="px-4 py-6 text-xs text-[var(--muted)]">
          Fleet telemetry is unavailable. The slot policy above is unaffected.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[44rem] text-left text-xs">
            <thead className="text-[var(--muted)]">
              <tr className="border-b border-[var(--line)]">
                <th scope="col" className="px-4 py-2 font-medium">Validator</th>
                <th scope="col" className="px-4 py-2 font-medium">Advertised</th>
                <th scope="col" className="px-4 py-2 font-medium">In flight</th>
                <th scope="col" className="px-4 py-2 font-medium">Disk</th>
                <th scope="col" className="px-4 py-2 font-medium">Updater</th>
                <th scope="col" className="px-4 py-2 font-medium">Slots filled</th>
                <th scope="col" className="px-4 py-2 font-medium">Issuance</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--line)]">
              {fleet.validators.map((validator) => {
                const disk = validator.disk_percent
                const restricted = disk !== null && disk >= ceiling
                const now = ticketedSlots(validator.configured_slots, disk, currentCap)
                const next = ticketedSlots(validator.configured_slots, disk, pendingCap)
                const notServing = validator.bench_serviceability !== 'serving'
                const orphanedCount = validator.orphaned_slots.length
                const issuancePaused = pausedHotkeys.has(validator.validator_hotkey)
                return (
                  <tr key={validator.validator_hotkey}>
                    <td className="px-4 py-2.5">
                      <span className="font-mono text-[11px]">
                        {shortHotkey(validator.validator_hotkey)}
                      </span>
                      <span className="mt-0.5 block text-[10px] text-[var(--muted)]">
                        {validator.online ? validator.admission : 'offline'}
                      </span>
                      {notServing ? (
                        <span
                          className={`mt-0.5 block text-[10px] ${validator.bench_serviceability === 'software_obsolete' ? 'text-[var(--red)]' : 'text-[var(--amber)]'}`}
                          title={
                            validator.bench_serviceability === 'software_obsolete'
                              ? 'Heartbeat protocol too old to describe the active benchmark — only an upgrade clears this'
                              : 'Current-enough software whose scorer is not advertising the active benchmark — a fix can clear this'
                          }
                        >
                          {validator.bench_serviceability === 'software_obsolete'
                            ? 'software obsolete'
                            : 'scorer unverified'}
                        </span>
                      ) : null}
                    </td>
                    <td className="px-4 py-2.5 font-medium">
                      {validator.configured_slots}
                      <span className="ml-1 text-[10px] text-[var(--muted)]">
                        ({validator.healthy_slot_count} healthy)
                      </span>
                    </td>
                    <td className="px-4 py-2.5">
                      {validator.active_benchmark_count}
                      {orphanedCount > 0 ? (
                        <span
                          className="ml-1 text-[var(--amber)]"
                          title="Slots whose lease an operator evicted while the benchmark container may still be executing — NOT free capacity"
                        >
                          · {orphanedCount} orphaned
                        </span>
                      ) : null}
                    </td>
                    <td
                      className={`px-4 py-2.5 font-medium ${restricted ? 'text-[var(--amber)]' : ''}`}
                    >
                      {disk === null ? '—' : `${disk}%`}
                      {restricted ? (
                        <span className="mt-0.5 block text-[10px]">
                          at or above the {ceiling}% ceiling, held to {restrictedSlots}
                        </span>
                      ) : null}
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`font-medium ${
                          validator.updater_status?.state === 'suppressed' ||
                          validator.updater_status?.state === 'rollback'
                            ? 'text-[var(--red)]'
                            : validator.updater_status?.state === 'backoff' ||
                                validator.updater_status?.state === 'unavailable'
                              ? 'text-[var(--amber)]'
                              : ''
                        }`}
                        title={updaterDetail(validator.updater_status)}
                      >
                        {updaterLabel(validator.updater_status)}
                      </span>
                      {validator.updater_status?.retry_after ? (
                        <span className="mt-0.5 block text-[10px] text-[var(--muted)]">
                          retry {formatEpoch(validator.updater_status.retry_after)}
                        </span>
                      ) : null}
                    </td>
                    <td className="px-4 py-2.5 font-medium">
                      {issuancePaused ? 0 : now}
                      {!issuancePaused && next !== now ? (
                        <span className="ml-1 text-[var(--acid)]">→ {next}</span>
                      ) : null}
                    </td>
                    <td className="px-4 py-2.5">
                      <button
                        type="button"
                        disabled={readOnly || loading}
                        onClick={() =>
                          onToggleIssuance(validator.validator_hotkey, !issuancePaused)
                        }
                        className={`inline-flex min-h-9 items-center gap-1.5 rounded-lg border px-2.5 text-[11px] font-medium disabled:opacity-40 ${
                          issuancePaused
                            ? 'border-[var(--acid)]/40 text-[var(--acid)]'
                            : 'border-[var(--line)] text-[var(--muted-strong)]'
                        }`}
                      >
                        {issuancePaused ? (
                          <PlayCircle className="h-3.5 w-3.5" />
                        ) : (
                          <PauseCircle className="h-3.5 w-3.5" />
                        )}
                        {issuancePaused ? 'Resume' : 'Pause'}
                      </button>
                      <span className="mt-1 block text-[10px] text-[var(--muted)]">
                        {issuancePaused ? 'new leases paused' : 'accepting policy permits'}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function RevisionHistory({ state }: { state: ValidatorSlotSettingsControl }) {
  const revisions = state.history
  return (
    <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="border-b border-[var(--line)] bg-[var(--panel-raised)] px-4 py-3">
        <h2 className="text-sm font-semibold">Revision history</h2>
        <p className="mt-1 text-xs text-[var(--muted)]">
          Append-only, newest first. Nothing here is ever edited or removed; a change is a new
          revision recorded against the operator who applied it.
        </p>
      </div>
      {revisions.length === 0 ? (
        <p className="px-4 py-6 text-xs text-[var(--muted)]">
          No revision has ever been written. The fleet is running the shipped default of{' '}
          {state.default.max_concurrent_slots} concurrent{' '}
          {state.default.max_concurrent_slots === 1 ? 'slot' : 'slots'} at a{' '}
          {state.default.disk_percent_ceiling}% disk ceiling.
        </p>
      ) : (
        <ol className="divide-y divide-[var(--line)]">
          {revisions.map((revision) => (
            <li key={`${revision.scope}-${revision.revision}`} className="px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-semibold">Revision {revision.revision}</span>
                {revision.revision === state.effective.revision &&
                state.effective.source === 'revision' ? (
                  <span className="rounded-full border border-[#46552f] bg-[var(--acid-dim)] px-2 py-0.5 text-[10px] font-medium text-[var(--acid)]">
                    In force
                  </span>
                ) : null}
                <span className="text-[11px] text-[var(--muted-strong)]">
                  cap {revision.settings.max_concurrent_slots} · disk ceiling{' '}
                  {revision.settings.disk_percent_ceiling}%
                </span>
              </div>
              <p className="mt-1 text-[11px] leading-5 text-[var(--muted-strong)]">
                {revision.reason}
              </p>
              <p className="mt-1 text-[10px] text-[var(--muted)]">
                {revision.actor} · {formatWhen(revision.created_at)} · parent revision{' '}
                {revision.parent_revision}
              </p>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
