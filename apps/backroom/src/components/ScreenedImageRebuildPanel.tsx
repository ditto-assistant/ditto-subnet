import { useServerFn } from '@tanstack/react-start'
import { Hammer, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import type { ScreenedImageRebuildDetail } from '../lib/admin.schemas'
import {
  getScreenedImageRebuild,
  rebuildScreenedImage,
} from '../server/admin.functions'

function short(value: string) {
  return value.length > 20 ? `${value.slice(0, 20)}…` : value
}

export function ScreenedImageRebuildPanel({ readOnly }: { readOnly: boolean }) {
  const inspectRebuild = useServerFn(getScreenedImageRebuild)
  const rebuild = useServerFn(rebuildScreenedImage)
  const [agentId, setAgentId] = useState('')
  const [detail, setDetail] = useState<ScreenedImageRebuildDetail | null>(null)
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
      const next = await inspectRebuild({ data: { agentId: agentId.trim() } })
      setDetail(next)
      setReason('')
      setConfirmed(false)
    } catch (cause) {
      setDetail(null)
      setError(cause instanceof Error ? cause.message : 'Unable to inspect screened image')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (
      !detail ||
      !detail.screened_image_sha256 ||
      !detail.screened_image_upload_id ||
      !confirmed ||
      reason.trim().length < 8
    ) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await rebuild({
        data: {
          agentId: detail.agent_id,
          expectedSha256: detail.artifact_sha256,
          expectedBenchVersion: detail.bench_version,
          expectedScoreCount: 0,
          expectedImageSha256: detail.screened_image_sha256,
          expectedImageUploadId: detail.screened_image_upload_id,
          reason: reason.trim(),
        },
      })
      setMessage(
        `Queued a build-only image replacement for benchmark v${result.bench_version}; expired ${result.expired_ticket_count} unscored ticket${result.expired_ticket_count === 1 ? '' : 's'}.`,
      )
      setDetail(null)
      setReason('')
      setConfirmed(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to rebuild screened image')
    } finally {
      setLoading(false)
    }
  }

  const actionable = Boolean(
    detail?.rebuild_allowed &&
      detail.score_count === 0 &&
      detail.screened_image_sha256 &&
      detail.screened_image_upload_id,
  )

  return (
    <section className="mt-6 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
      <div className="border-b border-[var(--line)] px-4 py-4">
        <div className="flex items-center gap-2">
          <Hammer className="h-4 w-4 text-[var(--amber)]" />
          <h2 className="text-sm font-semibold">Rebuild stale screened image</h2>
        </div>
        <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
          Replace an incompatible image archive without repeating source review. The dataset,
          accepted screening verdict, submission history, and ownership stay unchanged.
        </p>
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <input
            aria-label="Agent ID for screened image rebuild"
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
            Inspect image
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
            <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-4">
              <div><dt className="text-[var(--muted)]">Benchmark</dt><dd>v{detail.bench_version}</dd></div>
              <div><dt className="text-[var(--muted)]">Accepted scores</dt><dd>{detail.score_count}</dd></div>
              <div><dt className="text-[var(--muted)]">Artifact</dt><dd className="font-mono" title={detail.artifact_sha256}>{short(detail.artifact_sha256)}</dd></div>
              <div><dt className="text-[var(--muted)]">Current image</dt><dd className="font-mono" title={detail.screened_image_sha256 ?? ''}>{detail.screened_image_sha256 ? short(detail.screened_image_sha256) : 'Missing'}</dd></div>
            </dl>
            {detail.validator_ticket_active ? (
              <p className="mt-3 text-xs text-[var(--amber)]">
                An unscored validator ticket is active; the rebuild will expire it before replacing the image.
              </p>
            ) : null}
            {detail.blocking_reason ? (
              <p className="mt-3 text-xs text-[var(--amber)]">Blocked: {detail.blocking_reason}</p>
            ) : null}
          </div>

          <div className="rounded-lg border border-[var(--amber)]/30 bg-[var(--amber-dim)] p-4">
            <div className="flex gap-2">
              <ShieldCheck className="h-4 w-4 text-[var(--amber)]" />
              <p className="text-xs font-semibold text-[var(--amber)]">Guarded build-only repair</p>
            </div>
            <p className="mt-2 text-xs leading-5 text-[var(--muted-strong)]">
              The platform rechecks the artifact, benchmark, zero-score count, and exact current
              image identity atomically. The screener rebuilds and verifies the image without
              rerunning the accepted source-review decision.
            </p>
            <textarea
              aria-label="Screened image rebuild audit reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
              placeholder="Evidence that the current image archive is incompatible with healthy validators"
              className="mt-3 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs"
            />
            <label className="mt-3 flex gap-2 text-xs text-[var(--muted-strong)]">
              <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
              I verified this zero-score submission needs only a screened-image rebuild.
            </label>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={readOnly || loading || !actionable || !confirmed || reason.trim().length < 8}
              className="mt-3 min-h-11 rounded-lg bg-[var(--amber)] px-4 text-xs font-semibold text-black disabled:opacity-40"
            >
              Queue build-only image replacement
            </button>
          </div>
        </div>
      ) : null}
    </section>
  )
}
