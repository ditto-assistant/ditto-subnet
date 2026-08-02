import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import type { BenchmarkContractRefreshDetail } from '../lib/admin.schemas'
import {
  getBenchmarkContractRefresh,
  rebuildBenchmarkContract,
} from '../server/admin.functions'

function short(value: string) {
  return value.length > 20 ? `${value.slice(0, 20)}…` : value
}

export function BenchmarkContractRefreshPanel({ readOnly }: { readOnly: boolean }) {
  const inspectRefresh = useServerFn(getBenchmarkContractRefresh)
  const rebuild = useServerFn(rebuildBenchmarkContract)
  const [agentId, setAgentId] = useState('')
  const [detail, setDetail] = useState<BenchmarkContractRefreshDetail | null>(null)
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
      const next = await inspectRefresh({ data: { agentId: agentId.trim() } })
      setDetail(next)
      setReason('')
      setConfirmed(false)
    } catch (cause) {
      setDetail(null)
      setError(cause instanceof Error ? cause.message : 'Unable to inspect benchmark contract')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!detail || !detail.dataset_sha256 || !confirmed || reason.trim().length < 8) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await rebuild({
        data: {
          agentId: detail.agent_id,
          expectedSha256: detail.artifact_sha256,
          expectedBenchVersion: detail.bench_version,
          expectedDatasetSha256: detail.dataset_sha256,
          expectedScoreCount: detail.score_count,
          reason: reason.trim(),
        },
      })
      setMessage(
        `Queued policy rescreen for benchmark v${result.bench_version}; expired ${result.expired_ticket_count} stale ticket${result.expired_ticket_count === 1 ? '' : 's'}.`,
      )
      setDetail(null)
      setReason('')
      setConfirmed(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to rebuild benchmark contract')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="mt-6 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="border-b border-[var(--line)] px-4 py-4">
        <div className="flex items-center gap-2">
          <RefreshCw className="h-4 w-4 text-[var(--amber)]" />
          <h2 className="text-sm font-semibold">Rebuild stale benchmark contract</h2>
        </div>
        <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
          For a zero-score v3+ submission whose stored dataset no longer matches validators.
          This expires stale tickets, removes the stale screened image and sends the same source
          artifact through screening again. Submission and attempt history are preserved.
        </p>
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <input
            aria-label="Agent ID for benchmark contract rebuild"
            value={agentId}
            onChange={(event) => setAgentId(event.target.value)}
            placeholder="Agent UUID"
            className="min-h-11 flex-1 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 font-mono text-xs"
          />
          <button
            type="button"
            onClick={() => void inspect()}
            disabled={loading || agentId.trim().length === 0}
            className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-semibold disabled:opacity-40"
          >
            Inspect contract
          </button>
        </div>
      </div>

      {error ? <p role="alert" className="m-4 text-xs text-[var(--red)]">{error}</p> : null}
      {message ? <p role="status" className="m-4 text-xs text-[var(--green)]">{message}</p> : null}
      {detail ? (
        <div className="space-y-4 p-4">
          <div>
            <p className="text-sm font-semibold">{detail.agent_name}</p>
            <p className="font-mono text-[10px] text-[var(--muted)]">{detail.agent_id}</p>
            <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-3">
              <div><dt className="text-[var(--muted)]">Benchmark</dt><dd>v{detail.bench_version}</dd></div>
              <div><dt className="text-[var(--muted)]">Accepted scores</dt><dd>{detail.score_count}</dd></div>
              <div><dt className="text-[var(--muted)]">Artifact</dt><dd className="font-mono" title={detail.artifact_sha256}>{short(detail.artifact_sha256)}</dd></div>
            </dl>
            {detail.blocking_reason ? (
              <p className="mt-3 text-xs text-[var(--amber)]">Blocked: {detail.blocking_reason}</p>
            ) : null}
          </div>

          <div className="rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] p-4">
            <div className="flex gap-2">
              <AlertTriangle className="h-4 w-4 text-[var(--red)]" />
              <p className="text-xs font-semibold text-[var(--red)]">Destructive contract repair</p>
            </div>
            <p className="mt-2 text-xs leading-5 text-[var(--muted-strong)]">
              The exact artifact stays stored, but its current dataset, screened-image metadata and
              unscored tickets are replaced. The platform rechecks every value before committing.
            </p>
            <textarea
              aria-label="Benchmark contract rebuild audit reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
              placeholder="Evidence that the pinned dataset differs from the active validator generator"
              className="mt-3 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs"
            />
            <label className="mt-3 flex gap-2 text-xs text-[var(--muted-strong)]">
              <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
              I verified this submission has zero accepted v{detail.bench_version} scores and needs a fresh screening image and dataset.
            </label>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={readOnly || loading || !detail.refresh_allowed || !detail.dataset_sha256 || !confirmed || reason.trim().length < 8}
              className="mt-3 min-h-11 rounded-lg bg-[var(--red)] px-4 text-xs font-semibold text-white disabled:opacity-40"
            >
              Rebuild and rescreen submission
            </button>
          </div>
        </div>
      ) : null}
    </section>
  )
}
