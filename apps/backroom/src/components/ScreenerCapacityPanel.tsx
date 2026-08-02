import { useServerFn } from '@tanstack/react-start'
import {
  AlertTriangle,
  CheckCircle2,
  Cloud,
  Container,
  RefreshCw,
  ServerCog,
} from 'lucide-react'
import { useState, type ReactNode } from 'react'
import type { ScreenerCapacityNode, ScreenerCapacityView } from '../lib/admin.schemas'
import { getScreenerCapacity } from '../server/admin.functions'

function formatWhen(value: string | null) {
  if (!value) return 'Never'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed)
}

function shortIdentity(value: string) {
  return value.length > 24 ? `${value.slice(0, 14)}…${value.slice(-6)}` : value
}

function nodeTone(status: ScreenerCapacityNode['status']) {
  if (status === 'active') return 'bg-[var(--acid-dim)] text-[var(--acid)]'
  if (status === 'draining') return 'bg-[var(--amber-dim)] text-[var(--amber)]'
  return 'bg-[var(--red-dim)] text-[var(--red)]'
}

export function ScreenerCapacityPanel({ initialState }: { initialState: ScreenerCapacityView }) {
  const fetchCapacity = useServerFn(getScreenerCapacity)
  const [state, setState] = useState(initialState)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const snapshot = state.snapshot
  const leaseFresh = snapshot
    ? new Date(snapshot.controller_lease_expires_at).getTime() > Date.now()
    : false

  async function refresh() {
    setLoading(true)
    setError('')
    try {
      setState(await fetchCapacity())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh screener capacity')
    } finally {
      setLoading(false)
    }
  }

  if (!snapshot) {
    return (
      <section className="mt-6 rounded-xl border border-[var(--amber)]/30 bg-[var(--amber-dim)] p-6">
        <AlertTriangle className="h-5 w-5 text-[var(--amber)]" />
        <h2 className="mt-3 text-base font-semibold">Capacity controller has not checked in</h2>
        <p className="mt-2 max-w-[70ch] text-sm leading-6 text-[var(--muted-strong)]">
          The independent GCE watchdog is eligible to scale out when queue depth is nonzero.
          No Targon workload will be started without a valid capability attestation.
        </p>
      </section>
    )
  }

  return (
    <div className="mt-6 space-y-5">
      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
              <ServerCog className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-semibold">Controller authority</h2>
              <p className="mt-1 max-w-[72ch] text-xs leading-5 text-[var(--muted)]">
                One fenced writer allocates Targon first and sends only residual demand to
                GCE. The GCP watchdog may scale out only after this lease expires.
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
            Refresh capacity
          </button>
        </div>

        <div className="p-4 sm:p-5">
          <div
            className={`flex items-start gap-3 rounded-lg border p-4 ${
              leaseFresh
                ? 'border-[#46552f] bg-[var(--acid-dim)]'
                : 'border-[var(--red)]/30 bg-[var(--red-dim)]'
            }`}
          >
            {leaseFresh ? (
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-[var(--acid)]" />
            ) : (
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--red)]" />
            )}
            <div>
              <p className="text-sm font-medium">
                {leaseFresh ? 'Normal writer lease is healthy' : 'Normal writer lease is stale'}
              </p>
              <p className="mt-1 text-xs leading-5 text-[var(--muted-strong)]">
                Epoch {shortIdentity(snapshot.controller_epoch)} · heartbeat{' '}
                {formatWhen(snapshot.controller_heartbeat_at)} · lease expires{' '}
                {formatWhen(snapshot.controller_lease_expires_at)}
              </p>
            </div>
          </div>

          {snapshot.targon_capability !== 'go' ? (
            <div className="mt-4 flex items-start gap-3 rounded-lg border border-[var(--amber)]/30 bg-[var(--amber-dim)] p-4">
              <Container className="mt-0.5 h-4 w-4 shrink-0 text-[var(--amber)]" />
              <div>
                <p className="text-sm font-medium">Targon execution boundary is blocked</p>
                <p className="mt-1 break-words text-xs leading-5 text-[var(--muted-strong)]">
                  Capability is {snapshot.targon_capability.toUpperCase()}. No miner artifact is
                  sent to Targon; GCE receives the entire residual target. Reason:{' '}
                  <span className="break-all">
                    {snapshot.fallback_reason ?? 'No current capability attestation'}
                  </span>
                  .
                </p>
              </div>
            </div>
          ) : null}

          <dl className="mt-5 grid gap-px overflow-hidden rounded-lg border border-[var(--line)] bg-[var(--line)] sm:grid-cols-3 lg:grid-cols-6">
            {[
              ['Runnable', snapshot.runnable_backlog],
              ['Active leases', snapshot.active_leases],
              ['Desired slots', snapshot.desired_slots],
              ['Global cap', snapshot.global_cap],
              ['Targon ready', snapshot.targon_healthy],
              ['GCE target', snapshot.gce_target],
            ].map(([label, value]) => (
              <div key={label} className="bg-[var(--panel-soft)] px-4 py-3">
                <dt className="text-[11px] text-[var(--muted)]">{label}</dt>
                <dd className="mt-1 text-lg font-semibold tabular-nums">{value}</dd>
              </div>
            ))}
          </dl>
        </div>
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-4 py-4 sm:px-5">
          <h2 className="text-sm font-semibold">Provider allocation</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Pending capacity is counted before fallback so Targon and GCE never both scale to
            the full queue.
          </p>
        </div>
        <div className="divide-y divide-[var(--line)]">
          <ProviderRow
            icon={<Container className="h-4 w-4" />}
            name="Targon"
            detail={`${snapshot.targon_available} CPU rentals currently advertised`}
            values={{
              healthy: snapshot.targon_healthy,
              pending: snapshot.targon_pending,
              draining: snapshot.targon_draining,
            }}
          />
          <ProviderRow
            icon={<Cloud className="h-4 w-4" />}
            name="Google Compute Engine"
            detail="Regional managed instance group; zero is the steady idle target"
            values={{
              healthy: snapshot.gce_healthy,
              pending: snapshot.gce_pending,
              draining: snapshot.gce_draining,
            }}
          />
        </div>
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-4 py-4 sm:px-5">
          <h2 className="text-sm font-semibold">Enrolled workers</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Each worker has a unique hotkey and rotating bearer authority. Draining and revoked
            nodes cannot claim new jobs.
          </p>
        </div>
        {state.nodes.length === 0 ? (
          <p className="p-5 text-sm text-[var(--muted)]">
            No federated worker has enrolled yet. Legacy GCE workers remain visible on the
            public screener heartbeat surface during migration.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="bg-[var(--panel-soft)] text-[var(--muted)]">
                <tr>
                  <th className="px-4 py-3 font-medium sm:px-5">Node</th>
                  <th className="px-4 py-3 font-medium">Provider</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Version</th>
                  <th className="px-4 py-3 font-medium">Current phase</th>
                  <th className="px-4 py-3 font-medium">Last heartbeat</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--line)]">
                {state.nodes.map((node) => (
                  <tr key={node.node_id}>
                    <td className="px-4 py-3.5 font-medium sm:px-5">
                      {shortIdentity(node.node_id)}
                    </td>
                    <td className="px-4 py-3.5 capitalize text-[var(--muted-strong)]">
                      {node.provider}
                    </td>
                    <td className="px-4 py-3.5">
                      <span className={`rounded-full px-2 py-1 font-medium ${nodeTone(node.status)}`}>
                        {node.status}
                      </span>
                    </td>
                    <td className="px-4 py-3.5 text-[var(--muted-strong)]">
                      {node.software_version ?? '—'}
                    </td>
                    <td className="px-4 py-3.5 text-[var(--muted-strong)]">
                      {node.current_phase ?? 'No heartbeat'}
                    </td>
                    <td className="px-4 py-3.5 text-[var(--muted-strong)]">
                      {formatWhen(node.heartbeat_seen_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-4 py-4 sm:px-5">
          <h2 className="text-sm font-semibold">Recent capacity events</h2>
        </div>
        {state.events.length === 0 ? (
          <p className="p-5 text-sm text-[var(--muted)]">No capacity changes recorded.</p>
        ) : (
          <ol className="divide-y divide-[var(--line)]">
            {state.events.map((event) => (
              <li key={event.event_id} className="flex gap-4 px-4 py-3.5 sm:px-5">
                <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-[var(--cyan)]" />
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-medium">{event.detail}</p>
                  <p className="mt-1 text-[11px] text-[var(--muted)]">
                    {event.event_type} · {event.provider ?? 'controller'} ·{' '}
                    {formatWhen(event.created_at)}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      {error ? (
        <p role="alert" className="rounded-lg border border-[var(--red)]/30 bg-[var(--red-dim)] p-4 text-sm text-[var(--red)]">
          {error}
        </p>
      ) : null}
    </div>
  )
}

function ProviderRow({
  icon,
  name,
  detail,
  values,
}: {
  icon: ReactNode
  name: string
  detail: string
  values: { healthy: number; pending: number; draining: number }
}) {
  return (
    <div className="flex flex-col gap-4 px-4 py-4 sm:flex-row sm:items-center sm:px-5">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-white/[0.05] text-[var(--muted-strong)]">
          {icon}
        </span>
        <div>
          <p className="text-sm font-medium">{name}</p>
          <p className="mt-0.5 text-xs text-[var(--muted)]">{detail}</p>
        </div>
      </div>
      <dl className="flex gap-6 text-xs sm:text-right">
        <div>
          <dt className="text-[var(--muted)]">Healthy</dt>
          <dd className="mt-1 font-semibold tabular-nums">{values.healthy}</dd>
        </div>
        <div>
          <dt className="text-[var(--muted)]">Pending</dt>
          <dd className="mt-1 font-semibold tabular-nums">{values.pending}</dd>
        </div>
        <div>
          <dt className="text-[var(--muted)]">Draining</dt>
          <dd className="mt-1 font-semibold tabular-nums">{values.draining}</dd>
        </div>
      </dl>
    </div>
  )
}
