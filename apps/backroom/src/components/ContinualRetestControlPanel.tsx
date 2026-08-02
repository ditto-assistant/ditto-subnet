import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, PauseCircle, RefreshCw, RotateCw } from 'lucide-react'
import {
  CONTINUAL_RETEST_CONFIRMATION,
  type ContinualRetestSettingsControl,
} from '../lib/admin.schemas'
import {
  getContinualRetestSettings,
  updateContinualRetestSettings,
} from '../server/admin.functions'

type AggregateMode = 'disabled' | 'fleet_ready' | 'enabled'
type RolloutStanddown = 'off' | 'capable_validators' | 'all'
type WaveMembership = 'strict' | 'participants' | 'per_agent'
type EligibilityMode = 'fixed' | 'statistical'

const modes: Array<{ value: AggregateMode; label: string; detail: string }> = [
  {
    value: 'fleet_ready',
    label: 'Fleet ready',
    detail: 'Fold completed waves only when the live fleet satisfies the protocol gate.',
  },
  {
    value: 'enabled',
    label: 'Force enabled',
    detail: 'Immediately fold completed cohort waves into the authoritative score.',
  },
  {
    value: 'disabled',
    label: 'Disabled',
    detail: 'Keep initial quorum scores authoritative while preserving retest audit rows.',
  },
]

const standdownModes: Array<{
  value: RolloutStanddown
  label: string
  detail: string
}> = [
  {
    value: 'capable_validators',
    label: 'Yield capable slots',
    detail:
      'Validators that can score the incoming version stop retesting the previous one; the rest keep confirming.',
  },
  {
    value: 'all',
    label: 'Yield every slot',
    detail: 'Pause the whole retest lane for the duration of an open rollout.',
  },
  {
    value: 'off',
    label: 'Never yield',
    detail: 'Keep retesting the active version even while a rollout collects scores.',
  },
]

const membershipModes: Array<{
  value: WaveMembership
  label: string
  detail: string
}> = [
  {
    value: 'participants',
    label: 'Participants',
    detail:
      'Intersect over emission-set members that hold at least one confirmation. The shipped fold.',
  },
  {
    value: 'strict',
    label: 'Strict (rollback)',
    detail:
      'Intersect over every current member. Historical behaviour: one member at depth zero empties the wave.',
  },
  {
    value: 'per_agent',
    label: 'Per agent',
    detail:
      'No intersection — each agent aggregates its own seeds. Most responsive, least comparable.',
  },
]

const eligibilityModes: Array<{
  value: EligibilityMode
  label: string
  detail: string
}> = [
  {
    value: 'fixed',
    label: 'Fixed rank',
    detail: 'Cut at exactly the cohort size. A tie at the boundary is split by first seen.',
  },
  {
    value: 'statistical',
    label: 'Tie tolerant',
    detail:
      'Also admit anyone below the cutoff who is not statistically distinguishable from it.',
  },
]

// The read schema defaults this to the platform's own default, but a build old
// enough to omit the field predates it and is folding `strict`. Showing
// `participants` selected there would contradict the warning right below it.
function foldInEffect(state: ContinualRetestSettingsControl): WaveMembership {
  return state.field_support.wave_membership
    ? state.effective.settings.wave_membership
    : 'strict'
}

export function ContinualRetestControlPanel({
  initialState,
  readOnly,
}: {
  initialState: ContinualRetestSettingsControl
  readOnly: boolean
}) {
  const refreshSettings = useServerFn(getContinualRetestSettings)
  const applySettings = useServerFn(updateContinualRetestSettings)
  const [state, setState] = useState(initialState)
  const [mode, setMode] = useState<AggregateMode>(
    initialState.effective.settings.aggregate_mode,
  )
  const [idleRetests, setIdleRetests] = useState(
    initialState.effective.settings.idle_retests_enabled,
  )
  const [standdown, setStanddown] = useState<RolloutStanddown>(
    initialState.effective.settings.rollout_standdown,
  )
  const [cohortSize, setCohortSize] = useState(
    String(initialState.effective.settings.retest_cohort_size),
  )
  const [membership, setMembership] = useState<WaveMembership>(foldInEffect(initialState))
  const [eligibilityMode, setEligibilityMode] = useState<EligibilityMode>(
    initialState.effective.settings.retest_eligibility_mode,
  )
  const [eligibilityZ, setEligibilityZ] = useState(
    String(initialState.effective.settings.retest_eligibility_z),
  )
  const [cohortMaxSize, setCohortMaxSize] = useState(
    String(initialState.effective.settings.retest_cohort_max_size),
  )
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState<{ kind: 'error' | 'success'; text: string } | null>(null)

  const effective = state.effective
  const floor = effective.emission_set_size
  const ceiling = effective.max_retest_cohort_size
  // Backroom and the platform deploy separately. Against a build that predates
  // cohort sizing the dial is inert, so it is disabled rather than left live to
  // collect a number the platform will reject along with the rest of the form.
  const cohortSizingSupported = state.field_support.retest_cohort_size
  const membershipSupported = state.field_support.wave_membership
  const eligibilitySupported =
    state.field_support.retest_eligibility_mode &&
    state.field_support.retest_eligibility_z &&
    state.field_support.retest_cohort_max_size
  const zCeiling = effective.max_retest_eligibility_z
  const parsedCohortSize = Number(cohortSize)
  const parsedCohortMaxSize = Number(cohortMaxSize)
  const parsedEligibilityZ = Number(eligibilityZ)
  const cohortSizeValid =
    Number.isInteger(parsedCohortSize) &&
    parsedCohortSize >= floor &&
    parsedCohortSize <= ceiling
  const cohortMaxSizeValid =
    Number.isInteger(parsedCohortMaxSize) &&
    parsedCohortMaxSize >= floor &&
    parsedCohortMaxSize <= ceiling
  const eligibilityZValid =
    eligibilityZ.trim() !== '' &&
    Number.isFinite(parsedEligibilityZ) &&
    parsedEligibilityZ >= 0 &&
    parsedEligibilityZ <= zCeiling
  // The platform refuses a ceiling that cuts into the cohort the rank cutoff
  // already admitted, so say which of the two numbers is wrong here rather than
  // spending a round trip to be told.
  const ceilingBelowCohort =
    cohortSizeValid && cohortMaxSizeValid && parsedCohortMaxSize < parsedCohortSize
  // The cohort is the smaller of the dial and the field, so a number the
  // ranking cannot fill is worth saying out loud rather than leaving the
  // operator to wonder why nothing changed.
  const exceedsField =
    cohortSizeValid &&
    effective.eligible_agent_count !== null &&
    parsedCohortSize > effective.eligible_agent_count
  const changed =
    mode !== effective.settings.aggregate_mode ||
    idleRetests !== effective.settings.idle_retests_enabled ||
    standdown !== effective.settings.rollout_standdown ||
    (cohortSizingSupported &&
      cohortSizeValid &&
      parsedCohortSize !== effective.settings.retest_cohort_size) ||
    (membershipSupported && membership !== effective.settings.wave_membership) ||
    (eligibilitySupported &&
      (eligibilityMode !== effective.settings.retest_eligibility_mode ||
        (eligibilityZValid &&
          parsedEligibilityZ !== effective.settings.retest_eligibility_z) ||
        (cohortMaxSizeValid &&
          parsedCohortMaxSize !== effective.settings.retest_cohort_max_size)))
  const ready =
    changed &&
    (cohortSizeValid || !cohortSizingSupported) &&
    (!eligibilitySupported || (eligibilityZValid && cohortMaxSizeValid)) &&
    !ceilingBelowCohort &&
    reason.trim().length >= 8 &&
    confirmation === CONTINUAL_RETEST_CONFIRMATION

  function reset(next = state) {
    setMode(next.effective.settings.aggregate_mode)
    setIdleRetests(next.effective.settings.idle_retests_enabled)
    setStanddown(next.effective.settings.rollout_standdown)
    setCohortSize(String(next.effective.settings.retest_cohort_size))
    setMembership(foldInEffect(next))
    setEligibilityMode(next.effective.settings.retest_eligibility_mode)
    setEligibilityZ(String(next.effective.settings.retest_eligibility_z))
    setCohortMaxSize(String(next.effective.settings.retest_cohort_max_size))
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
        text: cause instanceof Error ? cause.message : 'Unable to refresh retest policy',
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
      const next = await applySettings({
        data: {
          expectedRevision: effective.revision,
          // A revision stores the whole policy, so every field goes on the wire
          // every time. Anything left out here is written as the platform's
          // default, not left alone.
          settings: {
            aggregate_mode: mode,
            idle_retests_enabled: idleRetests,
            rollout_standdown: standdown,
            retest_cohort_size: cohortSizingSupported ? parsedCohortSize : floor,
            wave_membership: membershipSupported ? membership : 'strict',
            retest_eligibility_mode: eligibilitySupported ? eligibilityMode : 'fixed',
            retest_eligibility_z: eligibilitySupported
              ? parsedEligibilityZ
              : effective.settings.retest_eligibility_z,
            retest_cohort_max_size: eligibilitySupported ? parsedCohortMaxSize : ceiling,
          },
          reason,
          confirmation,
        },
      })
      setState(next)
      reset(next)
      setMessage({ kind: 'success', text: 'Continual retest policy updated.' })
    } catch (cause) {
      setMessage({
        kind: 'error',
        text: cause instanceof Error ? cause.message : 'Unable to update retest policy',
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
            <RotateCw className="h-4 w-4" />
          </div>
          <div>
            <h2 className="text-sm font-semibold">Current scoring policy</h2>
            <p className="mt-1 max-w-[76ch] text-xs leading-5 text-[var(--muted)]">
              Controls how deep the retest lane reaches down the ranking, when completed waves
              affect rankings, and whether spare validator capacity may advance the next bounded
              wave.
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
        <dl className="grid gap-3 text-xs sm:grid-cols-3 lg:grid-cols-6">
          <div><dt className="text-[var(--muted)]">Revision</dt><dd className="mt-1 font-semibold">{effective.revision}</dd></div>
          <div><dt className="text-[var(--muted)]">Fleet gate</dt><dd className="mt-1 font-semibold">{effective.fleet_protocol_ready ? 'Ready' : 'Not ready'}</dd></div>
          <div><dt className="text-[var(--muted)]">Aggregate fold</dt><dd className="mt-1 font-semibold">{effective.aggregate_active ? 'Active' : 'Inactive'}</dd></div>
          <div>
            <dt className="text-[var(--muted)]">Retest lane</dt>
            <dd
              className={`mt-1 font-semibold ${effective.rollout_standdown_active ? 'text-[var(--amber)]' : ''}`}
            >
              {effective.rollout_standdown_active ? 'Standing down' : 'Running'}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--muted)]">Retest cohort</dt>
            <dd className="mt-1 font-semibold">
              Top {effective.settings.retest_cohort_size}
              {/* What the operator asked for against what the ranking actually
                  admitted. They diverge only in tie-tolerant mode, and that
                  divergence is the reason the mode exists. */}
              {effective.resolved_cohort_size !== null &&
              effective.resolved_cohort_size !== effective.settings.retest_cohort_size ? (
                <span className="ml-1 font-normal text-[var(--amber)]">
                  → {effective.resolved_cohort_size} admitted
                </span>
              ) : null}
              {effective.eligible_agent_count !== null ? (
                <span className="ml-1 font-normal text-[var(--muted)]">
                  of {effective.eligible_agent_count} ranked
                </span>
              ) : null}
            </dd>
          </div>
          <div><dt className="text-[var(--muted)]">Propagation</dt><dd className="mt-1 font-semibold">Within {effective.max_age_seconds}s</dd></div>
        </dl>

        {effective.rollout_standdown_active ? (
          <p className="mt-4 flex gap-3 rounded-lg border border-[var(--amber)]/30 bg-[var(--amber-dim)] p-4 text-xs leading-5 text-[var(--muted-strong)]">
            <PauseCircle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--amber)]" />
            <span>
              Retests are paused, not broken. Benchmark version{' '}
              {effective.open_rollout_desired_version} is collecting cohort scores, so
              rollout-capable validators are holding their slots for it instead of rescoring
              the active version. Leases already issued still run and report. This lifts on
              its own when the rollout activates or is superseded.
            </span>
          </p>
        ) : null}

        <div className="mt-5 grid gap-2 sm:grid-cols-3">
          {modes.map((item) => (
            <button
              key={item.value}
              type="button"
              disabled={readOnly || loading}
              onClick={() => setMode(item.value)}
              className={`min-h-24 rounded-lg border p-4 text-left disabled:opacity-45 ${
                mode === item.value
                  ? 'border-[var(--amber)]/40 bg-[var(--amber-dim)]'
                  : 'border-[var(--line)] bg-[var(--panel-soft)] hover:border-[var(--line-strong)]'
              }`}
            >
              <span className="block text-sm font-semibold">{item.label}</span>
              <span className="mt-1 block text-[11px] leading-4 text-[var(--muted)]">
                {item.detail}
              </span>
            </button>
          ))}
        </div>

        <div className="mt-5 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4">
          <h3 className="text-xs font-semibold text-[var(--muted-strong)]">
            Which retests count toward a seed
          </h3>
          <p className="mt-1 max-w-[76ch] text-[11px] leading-4 text-[var(--muted)]">
            This changes what validators weight. The continual mean behind{' '}
            <code>official_composite</code> is taken over the seeds this rule admits, so widening
            it widens the estimator, re-orders the tail, and moves emission shares. Strict is the
            historical fold and the rollback path — one audited revision, no redeploy.
          </p>
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            {membershipModes.map((item) => (
              <button
                key={item.value}
                type="button"
                disabled={readOnly || loading || !membershipSupported}
                onClick={() => setMembership(item.value)}
                className={`min-h-24 rounded-lg border p-4 text-left disabled:opacity-45 ${
                  membership === item.value
                    ? 'border-[var(--amber)]/40 bg-[var(--amber-dim)]'
                    : 'border-[var(--line)] bg-[var(--panel)] hover:border-[var(--line-strong)]'
                }`}
              >
                <span className="block text-sm font-semibold">{item.label}</span>
                <span className="mt-1 block text-[11px] leading-4 text-[var(--muted)]">
                  {item.detail}
                </span>
              </button>
            ))}
          </div>
          {!membershipSupported ? (
            <p className="mt-3 flex gap-2 text-xs leading-5 text-[var(--amber)]">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                The platform serving this page has no wave-membership field yet, so the fold is
                strict and these buttons are inert. Deploy a platform build that carries{' '}
                <code>wave_membership</code> to change it.
              </span>
            </p>
          ) : null}
          {membershipSupported && membership === 'per_agent' ? (
            <p className="mt-3 flex gap-2 text-xs leading-5 text-[var(--red)]">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                Per agent drops the shared-seed intersection, so two agents' means are taken over
                different seeds and their difference carries a seed-composition term. At a KOTH
                margin of 0.007 that added noise is the size of the decision it feeds.
              </span>
            </p>
          ) : null}
        </div>

        <label className="mt-5 flex items-start gap-3 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4">
          <input
            type="checkbox"
            checked={idleRetests}
            disabled={readOnly || loading}
            onChange={(event) => setIdleRetests(event.target.checked)}
            className="mt-0.5 h-4 w-4 accent-[var(--acid)]"
          />
          <span>
            <span className="block text-sm font-semibold">Use idle capacity for retests</span>
            <span className="mt-1 block text-xs leading-5 text-[var(--muted)]">
              Ordinary scoring is polled first. Membership, coverage, authentication,
              one-score-per-validator, and seed-cap guards remain enforced.
            </span>
          </span>
        </label>

        <div className="mt-5 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4">
          <h3 className="text-xs font-semibold text-[var(--muted-strong)]">How deep to retest</h3>
          <p className="mt-1 max-w-[76ch] text-[11px] leading-4 text-[var(--muted)]">
            {floor} is the emission set — the agents that earn. Going deeper rescores ranked
            challengers on the same wave seeds, so one arrives at the top {floor} with
            confirmation depth already banked instead of starting a fresh sweep. Emissions, the
            weight fold, and wave completion stay on the top {floor} at every setting; the extra
            members ride each seed only once the emission set is claimed.
          </p>
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Cohort size ({floor}–{ceiling})
              <input
                type="number"
                inputMode="numeric"
                min={floor}
                max={ceiling}
                step={1}
                value={cohortSize}
                disabled={readOnly || loading || !cohortSizingSupported}
                onChange={(event) => setCohortSize(event.target.value)}
                className="mt-2 block min-h-11 w-32 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm outline-none focus:border-[var(--cyan)] disabled:opacity-45"
              />
            </label>
            <div className="flex gap-2 pb-1">
              {[floor, 10, ceiling].map((preset) => (
                <button
                  key={preset}
                  type="button"
                  disabled={readOnly || loading || !cohortSizingSupported}
                  onClick={() => setCohortSize(String(preset))}
                  className={`min-h-11 rounded-lg border px-3 text-xs font-medium disabled:opacity-45 ${
                    parsedCohortSize === preset
                      ? 'border-[var(--amber)]/40 bg-[var(--amber-dim)]'
                      : 'border-[var(--line)] hover:border-[var(--line-strong)]'
                  }`}
                >
                  Top {preset}
                </button>
              ))}
            </div>
          </div>
          {!cohortSizingSupported ? (
            <p className="mt-3 flex gap-2 text-xs leading-5 text-[var(--amber)]">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                The platform serving this page has no cohort-size field yet, so the lane rescores
                the emission set (top {floor}) and this dial is inert. Everything else on this
                page still applies. Deploy a platform build that carries{' '}
                <code>retest_cohort_size</code> to reach further down the ranking.
              </span>
            </p>
          ) : null}
          {cohortSizingSupported && !cohortSizeValid ? (
            <p className="mt-3 text-xs text-[var(--red)]">
              Cohort size must be a whole number between {floor} and {ceiling}.
            </p>
          ) : null}
          {cohortSizingSupported && exceedsField ? (
            <p className="mt-3 text-xs text-[var(--muted-strong)]">
              Only {effective.eligible_agent_count} ranked agents exist on the active benchmark
              right now, so the cohort is all of them until the field grows.
            </p>
          ) : null}

          <h4 className="mt-5 text-xs font-semibold text-[var(--muted-strong)]">
            Where to draw the bottom edge
          </h4>
          <p className="mt-1 max-w-[76ch] text-[11px] leading-4 text-[var(--muted)]">
            A rank cutoff cannot express a tie: rank {'n'} is retested and rank {'n'} + 1 is not,
            even when the two hold the same composite and only first-seen separates them. Tie
            tolerant keeps the same cutoff and then admits anyone below it whose composite is
            within the band of it, so the band narrows on its own as retests accumulate.
          </p>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            {eligibilityModes.map((item) => (
              <button
                key={item.value}
                type="button"
                disabled={readOnly || loading || !eligibilitySupported}
                onClick={() => setEligibilityMode(item.value)}
                className={`min-h-20 rounded-lg border p-4 text-left disabled:opacity-45 ${
                  eligibilityMode === item.value
                    ? 'border-[var(--amber)]/40 bg-[var(--amber-dim)]'
                    : 'border-[var(--line)] bg-[var(--panel)] hover:border-[var(--line-strong)]'
                }`}
              >
                <span className="block text-sm font-semibold">{item.label}</span>
                <span className="mt-1 block text-[11px] leading-4 text-[var(--muted)]">
                  {item.detail}
                </span>
              </button>
            ))}
          </div>
          <div className="mt-3 flex flex-wrap items-end gap-4">
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Band width in standard errors (0–{zCeiling})
              <input
                type="number"
                inputMode="decimal"
                min={0}
                max={zCeiling}
                step={0.01}
                value={eligibilityZ}
                disabled={
                  readOnly || loading || !eligibilitySupported || eligibilityMode === 'fixed'
                }
                onChange={(event) => setEligibilityZ(event.target.value)}
                className="mt-2 block min-h-11 w-32 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm outline-none focus:border-[var(--cyan)] disabled:opacity-45"
              />
            </label>
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Cohort ceiling ({floor}–{ceiling})
              <input
                type="number"
                inputMode="numeric"
                min={floor}
                max={ceiling}
                step={1}
                value={cohortMaxSize}
                disabled={readOnly || loading || !eligibilitySupported}
                onChange={(event) => setCohortMaxSize(event.target.value)}
                className="mt-2 block min-h-11 w-32 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-sm outline-none focus:border-[var(--cyan)] disabled:opacity-45"
              />
            </label>
          </div>
          {!eligibilitySupported ? (
            <p className="mt-3 flex gap-2 text-xs leading-5 text-[var(--amber)]">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                The platform serving this page has no tie-tolerance fields yet, so the cohort cuts
                at exactly the rank above and these controls are inert. Deploy a platform build
                that carries <code>retest_eligibility_mode</code> to change it.
              </span>
            </p>
          ) : null}
          {eligibilitySupported && !eligibilityZValid ? (
            <p className="mt-3 text-xs text-[var(--red)]">
              Band width must be a number between 0 and {zCeiling}. Zero is a real setting: it
              admits exact ties and nothing else.
            </p>
          ) : null}
          {eligibilitySupported && !cohortMaxSizeValid ? (
            <p className="mt-3 text-xs text-[var(--red)]">
              Cohort ceiling must be a whole number between {floor} and {ceiling}.
            </p>
          ) : null}
          {ceilingBelowCohort ? (
            <p className="mt-3 text-xs text-[var(--red)]">
              The ceiling ({parsedCohortMaxSize}) cannot sit below the cohort size (
              {parsedCohortSize}); it would cut into the agents the rank cutoff already admitted.
            </p>
          ) : null}
          {eligibilitySupported && eligibilityMode === 'statistical' ? (
            <p className="mt-3 text-xs leading-5 text-[var(--muted-strong)]">
              The ceiling is a stop, not a target. With no ties near the cutoff the cohort stays
              at top {cohortSizeValid ? parsedCohortSize : effective.settings.retest_cohort_size}{' '}
              and the ceiling never binds.
            </p>
          ) : null}
        </div>

        <div className="mt-5">
          <h3 className="text-xs font-semibold text-[var(--muted-strong)]">
            During a benchmark rollout
          </h3>
          <p className="mt-1 text-[11px] leading-4 text-[var(--muted)]">
            Retests rescore the active version. While a rollout collects, they compete for the
            same validator slots the incoming cohort needs to reach quorum.
          </p>
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            {standdownModes.map((item) => (
              <button
                key={item.value}
                type="button"
                disabled={readOnly || loading}
                onClick={() => setStanddown(item.value)}
                className={`min-h-24 rounded-lg border p-4 text-left disabled:opacity-45 ${
                  standdown === item.value
                    ? 'border-[var(--amber)]/40 bg-[var(--amber-dim)]'
                    : 'border-[var(--line)] bg-[var(--panel-soft)] hover:border-[var(--line-strong)]'
                }`}
              >
                <span className="block text-sm font-semibold">{item.label}</span>
                <span className="mt-1 block text-[11px] leading-4 text-[var(--muted)]">
                  {item.detail}
                </span>
              </button>
            ))}
          </div>
        </div>

        {standdown === 'off' && effective.open_rollout_desired_version !== null ? (
          <p className="mt-4 flex gap-3 rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] p-4 text-xs leading-5 text-[var(--red)]">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            A rollout to benchmark version {effective.open_rollout_desired_version} is open right
            now. Never yield spends scarce validator slots on previous-generation rescores and
            will slow it down.
          </p>
        ) : null}

        {mode === 'enabled' && !effective.fleet_protocol_ready ? (
          <p className="mt-4 flex gap-3 rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] p-4 text-xs leading-5 text-[var(--red)]">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            Force enabled bypasses the live fleet readiness signal. Use only for an intentional
            asynchronous rollout decision.
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
              {CONTINUAL_RETEST_CONFIRMATION}
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
          <p className={`mt-4 flex items-center gap-2 text-xs ${message.kind === 'error' ? 'text-[var(--red)]' : 'text-[var(--acid)]'}`}>
            {message.kind === 'success' ? <CheckCircle2 className="h-4 w-4" /> : null}
            {message.text}
          </p>
        ) : null}

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={() => reset()} disabled={loading || !changed} className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-medium disabled:opacity-40">Cancel</button>
          <button type="button" onClick={() => void submit()} disabled={readOnly || loading || !ready} className="min-h-11 rounded-lg bg-[var(--acid)] px-4 text-xs font-semibold text-black disabled:opacity-35">{loading ? 'Applying…' : 'Apply policy'}</button>
        </div>
      </div>
    </section>
  )
}
