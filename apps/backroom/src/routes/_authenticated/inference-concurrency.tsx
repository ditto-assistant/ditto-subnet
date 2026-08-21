import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, Gauge } from 'lucide-react'
import { PageHeader } from '../../components/PageHeader'
import { InferenceConcurrencyControlPanel } from '../../components/InferenceConcurrencyControlPanel'
import { getInferenceConcurrencySettings } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/inference-concurrency')({
  loader: () => getInferenceConcurrencySettings(),
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: InferenceConcurrencyPage,
})

function InferenceConcurrencyPage() {
  const control = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 inference"
        title="Inference & benchmark runtime"
        description="Control hosted inference budgets, chat and embedding admission, v10 case concurrency, and relay delay fingerprints. Changes are lease-stamped from an append-only audited revision. Case concurrency overlaps /run against the process-wide inference URL."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <Gauge className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Platform managed
          </div>
        }
      />
      <InferenceConcurrencyControlPanel
        initialState={control}
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
    <div className="mx-auto mt-6 max-w-2xl rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6">
      <AlertTriangle className="h-6 w-6 text-[var(--red)]" />
      <h2 className="mt-4 text-lg font-semibold">Hosted inference policy unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
