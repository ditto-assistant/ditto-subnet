import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, ServerCog } from 'lucide-react'
import { PageHeader } from '../../components/PageHeader'
import { ScreenerCapacityPanel } from '../../components/ScreenerCapacityPanel'
import { getScreenerCapacity } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/screener-capacity')({
  loader: () => getScreenerCapacity(),
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: ScreenerCapacityPage,
})

function ScreenerCapacityPage() {
  const initialState = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 screening"
        title="Screener capacity"
        description="Audited one-shot Targon routing, trusted builds, leftover worker drain, and GCE fallback for screening."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <ServerCog className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Provider controls
          </div>
        }
      />
      <ScreenerCapacityPanel
        initialState={initialState}
        readOnly={user.accessLevel === 'read'}
      />
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
