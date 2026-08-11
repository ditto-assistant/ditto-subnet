import { useServerFn } from '@tanstack/react-start'
import {
  AlertTriangle,
  CheckCircle2,
  Gauge,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import {
  benchmarkRolloutConfirmation,
  type BenchmarkRolloutControl,
} from '../lib/admin.schemas'
import {
  getBenchmarkRolloutControl,
  selectActiveBenchmark,
  startBenchmarkRollout,
  supersedeBenchmarkRollout,
} from '../server/admin.functions'

const quorum = 3

function shortId(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-4)}`
}

function initialTarget(state: BenchmarkRolloutControl) {
  return state.available_target_versions.at(-1) ?? null
}

function statusCopy(state: BenchmarkRolloutControl) {
  if (state.status === 'activated') {
    return {
      label: `Benchmark v${state.active_version} active`,
      detail: 'Qualification completed and the versioned contract is authoritative.',
      tone: 'acid',
    } as const
  }
  if (state.status === 'blocked_ineligible') {
    return {
      label: `Benchmark v${state.desired_version} blocked`,
      detail:
        state.blocked_reason ??
        `The v${state.active_version} top five is not currently eligible for v${state.desired_version}.`,
      tone: 'red',
    } as const
  }
  if (state.status === 'collecting') {
    if (state.active_version === state.desired_version) {
      return {
        label: `Benchmark v${state.desired_version} authority active`,
        detail: `Benchmark v${state.desired_version} already drives weights while the remaining qualified cohort finishes scoring. This rollout cannot be superseded.`,
        tone: 'acid',
      } as const
    }
    return {
      label: `Benchmark v${state.desired_version} collecting`,
      detail: `Eligible top-five agents are gathering v${state.desired_version} scores. Benchmark v${state.active_version} remains authoritative until activation.`,
      tone: 'amber',
    } as const
  }
  if (state.status === 'superseded') {
    return {
      label: `Benchmark v${state.desired_version} superseded`,
      detail: 'That transition is closed. The active contract did not change.',
      tone: 'muted',
    } as const
  }
  return {
    label: 'No benchmark transition open',
    detail: 'The active benchmark remains unchanged until an operator starts a target.',
    tone: 'muted',
  } as const
}

export function BenchmarkRolloutPanel({
  initialState,
  readOnly,
}: {
  initialState: BenchmarkRolloutControl
  readOnly: boolean
}) {
  const fetchState = useServerFn(getBenchmarkRolloutControl)
  const startRollout = useServerFn(startBenchmarkRollout)
  const supersedeRollout = useServerFn(supersedeBenchmarkRollout)
  const activateContract = useServerFn(selectActiveBenchmark)
  const [state, setState] = useState(initialState)
  const [selectedTarget, setSelectedTarget] = useState<number | null>(
    initialTarget(initialState),
  )
  const [reviewing, setReviewing] = useState<
    'start' | 'supersede' | 'activate' | null
  >(null)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const status = statusCopy(state)
  const selectedContract = useMemo(
    () => state.contracts.find((contract) => contract.version === selectedTarget) ?? null,
    [selectedTarget, state.contracts],
  )
  const open = state.status === 'collecting' || state.status === 'blocked_ineligible'
  const priorityReadyCount = state.members.filter(
    (member) =>
      member.position <= state.priority_cohort_size && member.score_count >= 3,
  ).length
  const canSupersede = open && state.active_version !== state.desired_version
  const candidatesDegraded = state.degraded_sections.includes(
    'active_contract_candidates',
  )
  const authorityCandidate =
    [...state.active_contract_candidates]
      .filter((candidate) => candidate.version > state.active_version)
      .sort((left, right) => right.version - left.version)
      .find((candidate) => candidate.ready) ?? null
  const canStart = !open && selectedContract !== null
  const capacityReady = (selectedContract?.capable_validator_count ?? 0) >= 1
  const startReady = capacityReady && (selectedContract?.start_ready ?? false)
  const actionVersion =
    reviewing === 'supersede'
      ? state.desired_version
      : reviewing === 'activate'
        ? authorityCandidate?.version ?? null
        : selectedTarget
  const expectedConfirmation =
    actionVersion === null
      ? ''
      : benchmarkRolloutConfirmation(
          reviewing === 'supersede'
            ? 'SUPERSEDE'
            : reviewing === 'activate'
              ? 'ACTIVATE'
              : 'START',
          actionVersion,
        )
  const actionReady = reason.trim().length >= 8 && confirmation === expectedConfirmation

  const resetReview = () => {
    setReviewing(null)
    setReason('')
    setConfirmation('')
  }

  const acceptState = (next: BenchmarkRolloutControl) => {
    setState(next)
    setSelectedTarget(initialTarget(next))
    resetReview()
  }

  const refresh = async () => {
    setLoading(true)
    setError('')
    try {
      acceptState(await fetchState())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh rollout state')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!actionReady || actionVersion === null || reviewing === null) return
    setLoading(true)
    setError('')
    try {
      const next = reviewing === 'start'
        ? await startRollout({
              data: {
                desiredVersion: actionVersion,
                expectedActiveVersion: state.active_version,
                reason,
                confirmation,
              },
            })
        : reviewing === 'activate'
          ? await activateContract({
              data: {
                desiredVersion: actionVersion,
                expectedActiveVersion: state.active_version,
                reason,
                confirmation,
              },
            })
          : await supersedeRollout({
              data: {
                desiredVersion: actionVersion,
                reason,
                confirmation,
              },
            })
      acceptState(next)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to change rollout state')
    } finally {
      setLoading(false)
    }
  }

  const toneClasses = {
    acid: 'border-[var(--acid)]/25 bg-[var(--acid-dim)] text-[var(--acid)]',
    amber: 'border-[var(--amber)]/25 bg-[var(--amber-dim)] text-[var(--amber)]',
    red: 'border-[var(--red)]/25 bg-[var(--red-dim)] text-[var(--red)]',
    muted: 'border-[var(--line)] bg-[var(--panel-soft)] text-[var(--muted-strong)]',
  }[status.tone]

  return (
    <div className="mt-6 space-y-5">
      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
              <Gauge className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-semibold">Benchmark contract state</h2>
              <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted)]">
                Shipping a contract only makes it available. Reading this page never opens,
                supersedes, or advances a rollout.
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
            Refresh state
          </button>
        </div>

        <div className="p-4 sm:p-5">
          <div className={`rounded-lg border px-4 py-3 ${toneClasses}`}>
            <div className="flex items-start gap-3">
              {state.status === 'activated' ? (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
              ) : state.status === 'blocked_ineligible' ? (
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              ) : (
                <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-current" />
              )}
              <div>
                <p className="text-xs font-semibold">{status.label}</p>
                <p className="mt-1 text-xs leading-5 opacity-90">{status.detail}</p>
              </div>
            </div>
          </div>

          <dl className="mt-5 grid gap-x-6 gap-y-4 text-xs sm:grid-cols-2 lg:grid-cols-5">
            <div>
              <dt className="text-[var(--muted)]">Active contract</dt>
              <dd className="mt-1 text-sm font-semibold">v{state.active_version}</dd>
            </div>
            <div>
              <dt className="text-[var(--muted)]">Rollout target</dt>
              <dd className="mt-1 text-sm font-semibold">{open ? `v${state.desired_version}` : 'None'}</dd>
            </div>
            <div>
              <dt className="text-[var(--muted)]">Target-capable validators</dt>
              <dd className="mt-1 text-sm font-semibold">
                {open ? state.canary_capable_validator_count : selectedContract?.capable_validator_count ?? '—'}
                <span className="ml-1 text-xs font-normal text-[var(--muted)]">· minimum 1</span>
              </dd>
            </div>
            <div>
              <dt className="text-[var(--muted)]">Top-five membership</dt>
              <dd className="mt-1 text-sm font-semibold">
                {state.qualification_converged ? 'Stable' : 'Updating'}
              </dd>
            </div>
            <div>
              <dt className="text-[var(--muted)]">Priority scoring gate</dt>
              <dd className="mt-1 text-sm font-semibold">
                {open ? (
                  <>
                    {priorityReadyCount}/{state.priority_cohort_size} at 3/3
                    <span className="ml-1 text-xs font-normal text-[var(--muted)]">
                      · {state.priority_complete ? 'complete' : 'pending'}
                    </span>
                  </>
                ) : (
                  'Not rolling out'
                )}
              </dd>
            </div>
          </dl>

          <div className="mt-5 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {state.contracts.map((contract) => (
              <div
                key={contract.version}
                className={`rounded-lg border px-3 py-3 text-xs ${
                  contract.version === state.active_version
                    ? 'border-[var(--acid)]/25 bg-[var(--acid-dim)]'
                    : 'border-[var(--line)] bg-[var(--panel-soft)]'
                }`}
              >
                <div className="flex items-center justify-between gap-3">
                  <span className="font-semibold">Benchmark v{contract.version}</span>
                  <span className="text-[var(--muted)]">
                    {contract.capable_validator_count} capable
                  </span>
                </div>
                <p className="mt-1 text-[var(--muted)]">
                  Policy {contract.minimum_screening_policy_version}+
                  {contract.requires_screened_image ? ' · screened image required' : ' · source fallback allowed'}
                </p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {candidatesDegraded ? (
        <section
          role="status"
          className="rounded-xl border border-[var(--amber)]/25 bg-[var(--amber-dim)] p-4 sm:p-5"
        >
          <h2 className="text-sm font-semibold text-[var(--amber)]">
            Activation readiness not loaded
          </h2>
          <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted-strong)]">
            The platform bounded this section out of a slow read, so no contract is
            offered for activation here. That is an unknown, not a &ldquo;nothing is
            ready&rdquo; &mdash; refresh to try again.
          </p>
        </section>
      ) : null}

      {authorityCandidate ? (
        <section className="rounded-xl border border-[var(--acid)]/25 bg-[var(--acid-dim)] p-4 sm:p-5">
          <h2 className="text-sm font-semibold text-[var(--acid)]">
            Active contract · v{authorityCandidate.version} ready
          </h2>
          <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted-strong)]">
            This contract has {authorityCandidate.ranked_quorum_agents} ranked quorums and
            can safely become weight authority. Rollout target control remains separate.
          </p>
          {open ? (
            <p role="status" className="mt-3 text-xs font-medium text-[var(--amber)]">
              Supersede the open v{state.desired_version} rollout before changing active
              authority, then restart that target from the newly active contract.
            </p>
          ) : readOnly ? (
            <ReadOnlyNotice />
          ) : reviewing === 'activate' ? (
            <ConfirmationForm
              action="Activate contract"
              tone="acid"
              expectedConfirmation={expectedConfirmation}
              reason={reason}
              confirmation={confirmation}
              loading={loading}
              ready={actionReady}
              onReason={setReason}
              onConfirmation={setConfirmation}
              onSubmit={() => void submit()}
              onCancel={resetReview}
            />
          ) : (
            <button
              type="button"
              onClick={() => setReviewing('activate')}
              className="mt-4 min-h-11 rounded-lg border border-[var(--acid)]/40 px-4 text-xs font-semibold text-[var(--acid)] hover:bg-[var(--acid)]/10"
            >
              Review activation of v{authorityCandidate.version}
            </button>
          )}
        </section>
      ) : null}

      {state.members.length > 0 ? (
        <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
          <div className="border-b border-[var(--line)] px-4 py-4 sm:px-5">
            <h2 className="text-sm font-semibold">Qualified agents · v{state.desired_version}</h2>
            <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
              Membership is append-only; top-five movement can qualify additional agents.
            </p>
          </div>
          <div className="divide-y divide-[var(--line)]">
            {state.members.map((member) => (
              <div
                key={member.agent_id}
                className="grid gap-3 px-4 py-3 text-xs sm:grid-cols-[4rem_minmax(12rem,1fr)_8rem_8rem] sm:items-center sm:px-5"
              >
                <span className="text-[var(--muted)]">Position {member.position}</span>
                <code className="text-[var(--muted-strong)]" title={member.agent_id}>
                  {shortId(member.agent_id)}
                </code>
                <span className="text-[var(--muted-strong)]">
                  {member.score_count}/{quorum} scores
                </span>
                <span className={member.currently_top_five ? 'text-[var(--acid)]' : 'text-[var(--muted)]'}>
                  {member.currently_top_five ? 'Current top five' : 'Previously qualified'}
                </span>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {canStart ? (
        <section className="rounded-xl border border-[var(--amber)]/30 bg-[var(--amber-dim)] p-4 sm:p-5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--amber)]" />
            <div className="min-w-0 flex-1">
              <h2 className="text-sm font-semibold text-[var(--amber)]">
                Start benchmark v{state.active_version} → v{selectedTarget} rollout
              </h2>
              <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[#e8cfaa]">
                This freezes the current v{state.active_version} eligible top five and renders
                append-only v{selectedTarget} dataset pins. Benchmark v{state.active_version}
                remains active until the cohort reaches quorum.
              </p>

              <label className="mt-4 block max-w-sm text-xs font-medium text-[#f4dfbf]">
                Target contract
                <select
                  value={selectedTarget ?? ''}
                  onChange={(event) => {
                    setSelectedTarget(Number(event.target.value))
                    resetReview()
                  }}
                  disabled={loading || reviewing !== null}
                  className="mt-2 h-11 w-full rounded-lg border border-[var(--amber)]/35 bg-[var(--panel)] px-3 text-sm text-white"
                >
                  {state.available_target_versions.map((version) => (
                    <option key={version} value={version}>Benchmark v{version}</option>
                  ))}
                </select>
              </label>

              {!capacityReady ? (
                <p role="status" className="mt-3 text-xs font-medium text-[var(--red)]">
                  Start is unavailable until one fresh identity-matched v{selectedTarget} validator is online.
                </p>
              ) : null}

              {capacityReady && !startReady ? (
                <div role="status" className="mt-3 text-xs font-medium text-[var(--red)]">
                  {(selectedContract?.start_blockers ?? []).length > 0 ? (
                    <ul className="space-y-1">
                      {selectedContract?.start_blockers.map((blocker) => (
                        <li key={blocker}>{blocker}</li>
                      ))}
                    </ul>
                  ) : (
                    'Start preflight is unavailable; refresh after Platform deployment.'
                  )}
                </div>
              ) : null}

              {readOnly ? (
                <ReadOnlyNotice />
              ) : reviewing === 'start' ? (
                <ConfirmationForm
                  action="Start rollout"
                  tone="amber"
                  expectedConfirmation={expectedConfirmation}
                  reason={reason}
                  confirmation={confirmation}
                  loading={loading}
                  ready={actionReady}
                  onReason={setReason}
                  onConfirmation={setConfirmation}
                  onSubmit={() => void submit()}
                  onCancel={resetReview}
                />
              ) : (
                <button
                  type="button"
                  onClick={() => setReviewing('start')}
                  disabled={!startReady}
                  className="mt-4 min-h-11 rounded-lg border border-[var(--amber)]/45 px-4 text-xs font-semibold text-[var(--amber)] transition-colors hover:bg-[var(--amber)]/10 disabled:opacity-40"
                >
                  Review v{selectedTarget} rollout
                </button>
              )}
            </div>
          </div>
        </section>
      ) : null}

      {canSupersede ? (
        <section className="rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-4 sm:p-5">
          <h2 className="text-sm font-semibold text-[var(--red)]">
            Supersede benchmark v{state.desired_version}
          </h2>
          <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[#e9b7b7]">
            This terminally closes the unactivated transition and preserves every score and audit row.
            It does not change the active benchmark.
          </p>
          {readOnly ? (
            <ReadOnlyNotice />
          ) : reviewing === 'supersede' ? (
            <ConfirmationForm
              action="Supersede rollout"
              tone="red"
              expectedConfirmation={expectedConfirmation}
              reason={reason}
              confirmation={confirmation}
              loading={loading}
              ready={actionReady}
              onReason={setReason}
              onConfirmation={setConfirmation}
              onSubmit={() => void submit()}
              onCancel={resetReview}
            />
          ) : (
            <button
              type="button"
              onClick={() => setReviewing('supersede')}
              className="mt-4 min-h-11 rounded-lg border border-[var(--red)]/40 px-4 text-xs font-semibold text-[var(--red)] hover:bg-[var(--red)]/10"
            >
              Review supersede
            </button>
          )}
        </section>
      ) : open ? (
        <section className="rounded-xl border border-[var(--acid)]/25 bg-[var(--acid-dim)] p-4 sm:p-5">
          <h2 className="text-sm font-semibold text-[var(--acid)]">
            Benchmark v{state.desired_version} owns active authority
          </h2>
          <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted-strong)]">
            Superseding this rollout would roll weight authority backward, so the action is unavailable.
            Finish the remaining cohort or select a newer qualified contract through the audited authority control.
          </p>
        </section>
      ) : null}

      {error ? (
        <div role="alert" className="rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] px-4 py-3 text-xs text-[var(--red)]">
          {error}
        </div>
      ) : null}
    </div>
  )
}

function ReadOnlyNotice() {
  return (
    <div className="mt-4 flex gap-2 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-3 text-xs text-[var(--muted-strong)]">
      <ShieldCheck className="h-4 w-4 shrink-0 text-[var(--muted)]" />
      Your Backroom account is read only. An editor must change rollout state.
    </div>
  )
}

function ConfirmationForm({
  action,
  tone,
  expectedConfirmation,
  reason,
  confirmation,
  loading,
  ready,
  onReason,
  onConfirmation,
  onSubmit,
  onCancel,
}: {
  action: string
  tone: 'acid' | 'amber' | 'red'
  expectedConfirmation: string
  reason: string
  confirmation: string
  loading: boolean
  ready: boolean
  onReason: (value: string) => void
  onConfirmation: (value: string) => void
  onSubmit: () => void
  onCancel: () => void
}) {
  const actionClasses =
    tone === 'red'
      ? 'bg-[var(--red)] text-[#21100e] hover:bg-[#ff958d]'
      : tone === 'acid'
        ? 'bg-[var(--acid)] text-[#111607] hover:bg-[#d4ff63]'
      : 'bg-[var(--amber)] text-[#211708] hover:bg-[#ffd080]'

  return (
    <div className="mt-4 space-y-3 border-t border-current/15 pt-4">
      <label className="block text-xs font-medium">
        Operator reason
        <textarea
          value={reason}
          onChange={(event) => onReason(event.target.value)}
          rows={3}
          className="mt-2 w-full rounded-lg border border-current/25 bg-[var(--panel)] px-3 py-2 text-xs text-white placeholder:text-[var(--muted)]"
          placeholder="Why is this benchmark transition appropriate now?"
        />
      </label>
      <label className="block text-xs font-medium">
        Type <code>{expectedConfirmation}</code> to confirm
        <input
          value={confirmation}
          onChange={(event) => onConfirmation(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          className="mt-2 h-10 w-full rounded-lg border border-current/25 bg-[var(--panel)] px-3 font-mono text-xs text-white placeholder:text-[var(--muted)]"
          placeholder={expectedConfirmation}
        />
      </label>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onSubmit}
          disabled={loading || !ready}
          className={`min-h-11 rounded-lg px-4 text-xs font-semibold transition-colors disabled:opacity-40 ${actionClasses}`}
        >
          {loading ? 'Applying…' : action}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={loading}
          className="min-h-11 rounded-lg border border-[var(--line-strong)] px-4 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}
