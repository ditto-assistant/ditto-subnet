import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, Clock3, RefreshCw, Unplug } from 'lucide-react'
import { useState } from 'react'
import type { ValidatorAssignment } from '../lib/admin.schemas'
import {
  listValidatorAssignments,
  releaseActiveValidatorAssignment,
} from '../server/admin.functions'

function short(value: string, length = 14) {
  return value.length > length ? `${value.slice(0, length)}…` : value
}

function leaseKey(item: ValidatorAssignment) {
  return `${short(item.agent_id, 8)} · ${short(item.validator_hotkey, 8)}`
}

function formatDate(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Unknown time'
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(date)
}

export function ValidatorAssignmentPanel({
  initialItems,
  readOnly,
}: {
  initialItems: Array<ValidatorAssignment>
  readOnly: boolean
}) {
  const listAssignments = useServerFn(listValidatorAssignments)
  const releaseAssignment = useServerFn(releaseActiveValidatorAssignment)
  const [items, setItems] = useState(initialItems)
  const [selected, setSelected] = useState<ValidatorAssignment | null>(null)
  const [reason, setReason] = useState('')
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const refresh = async () => {
    setLoading(true)
    setError('')
    try {
      const result = await listAssignments({ data: { generation: 'active' } })
      setItems(result.items)
      setSelected(
        (current) =>
          result.items.find(
            (item) =>
              item.agent_id === current?.agent_id &&
              item.validator_hotkey === current.validator_hotkey,
          ) ?? null,
      )
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : 'Unable to load validator assignments',
      )
    } finally {
      setLoading(false)
    }
  }

  const release = async () => {
    if (!selected || reason.trim().length < 8) return
    setSubmitting(true)
    setError('')
    try {
      await releaseAssignment({
        data: {
          agentId: selected.agent_id,
          validatorHotkey: selected.validator_hotkey,
          expectedDeadline: selected.deadline,
          reason: reason.trim(),
        },
      })
      setItems((current) =>
        current.filter(
          (item) =>
            item.agent_id !== selected.agent_id ||
            item.validator_hotkey !== selected.validator_hotkey,
        ),
      )
      setSelected(null)
      setReason('')
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : 'Unable to release this validator assignment',
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="mt-6 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="flex flex-col gap-3 border-b border-[var(--line)] px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Unplug className="h-4 w-4 text-[var(--amber)]" />
            <h2 className="text-sm font-semibold">
              Live validator assignments
            </h2>
            <span className="rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] text-[var(--muted-strong)]">
              {items.length}
            </span>
          </div>
          <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
            Current-benchmark leases only. Release only when a validator has
            stopped or cannot finish; existing scores and the miner submission
            are preserved.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 disabled:opacity-40"
        >
          <RefreshCw
            className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`}
          />
          Refresh assignments
        </button>
      </div>

      {error ? (
        <div
          role="alert"
          className="m-4 rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] px-4 py-3 text-xs text-[var(--red)]"
        >
          {error}
        </div>
      ) : null}

      {items.length === 0 ? (
        <div className="px-5 py-8 text-center">
          <Clock3 className="mx-auto h-6 w-6 text-[var(--muted)]" />
          <p className="mt-2 text-sm font-medium">No active assignments</p>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Validators have no live scoring leases right now.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-[var(--line)]">
          {items.map((item) => {
            const isSelected =
              selected?.agent_id === item.agent_id &&
              selected.validator_hotkey === item.validator_hotkey
            const releaseControlId = `release-${item.agent_id}-${item.validator_hotkey}`
            return (
              <article
                key={`${item.agent_id}:${item.validator_hotkey}`}
                className="px-4 py-4"
              >
                <div className="grid gap-4 lg:grid-cols-[minmax(12rem,0.9fr)_minmax(26rem,2fr)_auto] lg:items-center">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="truncate text-sm font-semibold">
                        {item.agent_name}
                      </p>
                      <span className="rounded-full border border-[var(--acid)]/25 bg-[var(--acid)]/10 px-2 py-0.5 text-[10px] font-semibold text-[var(--acid)]">
                        Active lease
                      </span>
                      <span className="rounded-full border border-[var(--amber)]/25 bg-[var(--amber-dim)] px-2 py-0.5 font-mono text-[10px] font-semibold text-[var(--amber)]">
                        Benchmark v{item.bench_version}
                      </span>
                    </div>
                    <p className="mt-1 truncate font-mono text-[10px] text-[var(--muted)]">
                      {item.agent_id}
                    </p>
                  </div>
                  <dl className="grid grid-cols-2 gap-x-5 gap-y-3 text-xs sm:grid-cols-3">
                    <div>
                      <dt className="text-[var(--muted)]">Validator</dt>
                      <dd
                        className="mt-1 font-mono text-[var(--muted-strong)]"
                        title={item.validator_hotkey}
                      >
                        {short(item.validator_hotkey)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[var(--muted)]">Lease key</dt>
                      <dd
                        className="mt-1 font-mono text-[var(--muted-strong)]"
                        title={`${item.agent_id}:${item.validator_hotkey}`}
                      >
                        {leaseKey(item)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[var(--muted)]">Attempt</dt>
                      <dd className="mt-1 text-[var(--muted-strong)]">
                        {item.attempt_count}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[var(--muted)]">Issued</dt>
                      <dd className="mt-1 text-[var(--muted-strong)]">
                        {formatDate(item.issued_at)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[var(--muted)]">Deadline</dt>
                      <dd className="mt-1 text-[var(--muted-strong)]">
                        {formatDate(item.deadline)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[var(--muted)]">Score progress</dt>
                      <dd className="mt-1 text-[var(--muted-strong)]">
                        {item.score_count} of 3 accepted
                        {item.provisional_composite === null
                          ? ''
                          : ` · provisional ${item.provisional_composite.toFixed(3)}`}
                      </dd>
                    </div>
                  </dl>
                  <button
                    type="button"
                    onClick={() => {
                      setSelected(isSelected ? null : item)
                      setReason('')
                      setError('')
                    }}
                    disabled={readOnly}
                    className="inline-flex min-h-11 items-center justify-center rounded-lg border border-[var(--amber)]/35 px-3 text-xs font-semibold text-[var(--amber)] hover:bg-[var(--amber-dim)] disabled:opacity-40"
                  >
                    {isSelected ? 'Cancel' : 'Release assignment'}
                  </button>
                </div>

                {isSelected ? (
                  <div className="mt-4 rounded-lg border border-[var(--amber)]/25 bg-[var(--amber-dim)] p-4">
                    <div className="flex gap-3">
                      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--amber)]" />
                      <div>
                        <p className="text-xs font-semibold text-[var(--amber)]">
                          Release this exact lease?
                        </p>
                        <p className="mt-1 text-xs leading-5 text-[var(--muted-strong)]">
                          The slot reopens for another validator. This validator
                          enters a six-hour cooldown for this submission;
                          accepted scores are not changed.
                        </p>
                      </div>
                    </div>
                    <label
                      className="mt-4 block text-xs font-medium"
                      htmlFor={releaseControlId}
                    >
                      Audit reason
                    </label>
                    <textarea
                      id={releaseControlId}
                      value={reason}
                      onChange={(event) => setReason(event.target.value)}
                      rows={2}
                      placeholder="Why this validator cannot finish the assignment"
                      className="mt-2 w-full resize-y rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs leading-5 placeholder:text-[var(--muted)] focus:border-[var(--amber)]/60"
                    />
                    <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                      <span className="text-[10px] text-[var(--muted)]">
                        {reason.trim().length} characters · minimum 8
                      </span>
                      <button
                        type="button"
                        onClick={() => void release()}
                        disabled={submitting || reason.trim().length < 8}
                        className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-[var(--amber)] px-4 text-xs font-semibold text-[#1c1407] disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        {submitting ? (
                          <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                        ) : null}
                        Confirm release
                      </button>
                    </div>
                  </div>
                ) : null}
              </article>
            )
          })}
        </div>
      )}
    </section>
  )
}
