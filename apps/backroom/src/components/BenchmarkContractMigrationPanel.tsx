import { useServerFn } from '@tanstack/react-start'
import { ArrowRight, ShieldAlert } from 'lucide-react'
import { useState } from 'react'
import type { BenchmarkContractMigrationDetail } from '../lib/admin.schemas'
import {
  getBenchmarkContractMigration,
  migrateZeroScoreBenchmarkContract,
} from '../server/admin.functions'

export function BenchmarkContractMigrationPanel({
  readOnly,
}: {
  readOnly: boolean
}) {
  const inspectMigration = useServerFn(getBenchmarkContractMigration)
  const migrate = useServerFn(migrateZeroScoreBenchmarkContract)
  const [agentId, setAgentId] = useState('')
  const [detail, setDetail] = useState<BenchmarkContractMigrationDetail | null>(
    null,
  )
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const inspect = async () => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      setDetail(await inspectMigration({ data: { agentId: agentId.trim() } }))
      setReason('')
      setConfirmed(false)
    } catch (cause) {
      setDetail(null)
      setError(
        cause instanceof Error ? cause.message : 'Unable to inspect migration',
      )
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (
      !detail?.source_dataset_sha256 ||
      !confirmed ||
      reason.trim().length < 8
    )
      return
    setLoading(true)
    setError('')
    try {
      const result = await migrate({
        data: {
          agentId: detail.agent_id,
          expectedSha256: detail.artifact_sha256,
          expectedSourceDatasetSha256: detail.source_dataset_sha256,
          reason: reason.trim(),
        },
      })
      setMessage(
        `Migrated v${result.source_bench_version} → v${result.target_bench_version}; expired ${result.expired_ticket_count} stale ticket${result.expired_ticket_count === 1 ? '' : 's'} and queued rescreening.`,
      )
      setDetail(null)
      setReason('')
      setConfirmed(false)
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : 'Unable to migrate submission',
      )
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="mt-6 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="border-b border-[var(--line)] px-4 py-4">
        <div className="flex items-center gap-2">
          <ArrowRight className="h-4 w-4 text-[var(--amber)]" />
          <h2 className="text-sm font-semibold">
            Migrate zero-score v2 submission to v3
          </h2>
        </div>
        <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
          Preserves the submitted artifact and history, expires unscored v2
          tickets, pins a v3 dataset, clears stale screened-image metadata, and
          sends the same artifact through screening before fresh v3 tickets are
          issued.
        </p>
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <input
            aria-label="Agent ID for v2 to v3 migration"
            value={agentId}
            onChange={(event) => setAgentId(event.target.value)}
            placeholder="Agent UUID"
            className="min-h-11 flex-1 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 font-mono text-xs"
          />
          <button
            type="button"
            onClick={() => void inspect()}
            disabled={loading || !agentId.trim()}
            className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-semibold disabled:opacity-40"
          >
            Inspect migration
          </button>
        </div>
      </div>
      {error ? (
        <p role="alert" className="m-4 text-xs text-[var(--red)]">
          {error}
        </p>
      ) : null}
      {message ? (
        <p role="status" className="m-4 text-xs text-[var(--green)]">
          {message}
        </p>
      ) : null}
      {detail ? (
        <div className="space-y-4 p-4">
          <div>
            <p className="text-sm font-semibold">{detail.agent_name}</p>
            <p className="font-mono text-[10px] text-[var(--muted)]">
              {detail.agent_id}
            </p>
            <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-3">
              <div>
                <dt className="text-[var(--muted)]">Contract</dt>
                <dd>v2 → v{detail.target_bench_version ?? '?'}</dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Accepted scores</dt>
                <dd>
                  v2: {detail.source_score_count} · v3:{' '}
                  {detail.target_score_count}
                </dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Active work</dt>
                <dd>
                  {detail.screening_attempt_active ||
                  detail.validator_run_active
                    ? 'Yes'
                    : 'No'}
                </dd>
              </div>
            </dl>
            {detail.blocking_reason ? (
              <p className="mt-3 text-xs text-[var(--amber)]">
                Blocked: {detail.blocking_reason}
              </p>
            ) : null}
          </div>
          <div className="rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] p-4">
            <div className="flex gap-2">
              <ShieldAlert className="h-4 w-4 text-[var(--red)]" />
              <p className="text-xs font-semibold text-[var(--red)]">
                Guarded contract migration
              </p>
            </div>
            <textarea
              aria-label="v2 to v3 migration audit reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
              placeholder="Why this zero-score legacy submission requires migration"
              className="mt-3 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs"
            />
            <label className="mt-3 flex gap-2 text-xs text-[var(--muted-strong)]">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              I verified this submission has zero accepted v2 and v3 scores and
              no active work.
            </label>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={
                readOnly ||
                loading ||
                !detail.migration_allowed ||
                !detail.source_dataset_sha256 ||
                !confirmed ||
                reason.trim().length < 8
              }
              className="mt-3 min-h-11 rounded-lg bg-[var(--red)] px-4 text-xs font-semibold text-white disabled:opacity-40"
            >
              Migrate and rescreen submission
            </button>
          </div>
        </div>
      ) : null}
    </section>
  )
}
