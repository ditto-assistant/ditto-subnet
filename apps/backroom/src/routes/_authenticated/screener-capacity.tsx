import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, ServerCog } from 'lucide-react'
import { PageHeader } from '../../components/PageHeader'
import { ScreenerCapacityPanel } from '../../components/ScreenerCapacityPanel'
import { ScreeningInfraRetryPanel } from '../../components/ScreeningInfraRetryPanel'
import { SubnetLivenessPanel } from '../../components/SubnetLivenessPanel'
import {
  getScreenerCapacity,
  getScreeningInfraRetries,
  getSubnetLiveness,
} from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/screener-capacity')({
  // The retry and liveness reads report their own failures (neither reader
  // throws) so they cannot take the capacity page down or hide why they are
  // missing. This is the signed-in landing page, so liveness is seen first,
  // and a failed capacity read must not discard a liveness verdict either:
  // capacity failing is exactly when the verdict matters most.
  loader: async () => {
    const [capacity, infraRetries, liveness] = await Promise.all([
      getScreenerCapacity().then(
        (view) => ({ ok: true as const, view }),
        (error: unknown) => ({
          ok: false as const,
          message: error instanceof Error ? error.message : 'Unknown error reading screener capacity.',
        }),
      ),
      getScreeningInfraRetries(),
      getSubnetLiveness(),
    ])
    return { capacity, infraRetries, liveness }
  },
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: ScreenerCapacityPage,
})

function ScreenerCapacityPage() {
  const { capacity, infraRetries, liveness } = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 screening"
        title="Screener capacity"
        description="Audited single-shot screening providers, trusted builds, and parked failures with manual retry control."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <ServerCog className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Provider controls
          </div>
        }
      />
      <SubnetLivenessPanel initialState={liveness} />
      {capacity.ok ? (
        <ScreenerCapacityPanel
          initialState={capacity.view}
          readOnly={user.accessLevel === 'read'}
        />
      ) : (
        <ErrorState error={new Error(capacity.message)} />
      )}
      <ScreeningInfraRetryPanel initialState={infraRetries} />
    </div>
  )
}

function Pending() {
  return <div className="mt-6 h-96 animate-pulse rounded-xl bg-white/[0.035]" />
}

function ErrorState({ error }: { error: Error }) {
  return (
    <div className="mt-6 rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6">
      <AlertTriangle className="h-6 w-6 text-[var(--red)]" />
      <h2 className="mt-4 text-lg font-semibold">Screener capacity unavailable</h2>
      <p className="mt-2 text-sm text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
