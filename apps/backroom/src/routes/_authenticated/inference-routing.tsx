import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, Network } from 'lucide-react'
import { InferenceRoutingPanel } from '../../components/InferenceRoutingPanel'
import { PageHeader } from '../../components/PageHeader'
import { listInferenceRoutes } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/inference-routing')({
  loader: () => listInferenceRoutes(),
  pendingComponent: InferenceRoutingPending,
  errorComponent: InferenceRoutingError,
  component: InferenceRoutingPage,
})

function InferenceRoutingPage() {
  const initialInventory = Route.useLoaderData()
  const { user } = Route.useRouteContext()

  return (
    <div>
      <PageHeader
        label="Platform inference"
        title="Inference routing"
        description="Inspect every discovered OpenRouter profile and deliberately admit reviewed calibration evidence. Routing remains platform-owned and ticket-bound."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <Network className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Dynamic routing controls
          </div>
        }
      />
      <InferenceRoutingPanel
        initialInventory={initialInventory}
        readOnly={user.accessLevel === 'read'}
      />
    </div>
  )
}

function InferenceRoutingPending() {
  return (
    <div className="mt-6 space-y-4" aria-label="Loading inference routes">
      <div className="h-24 animate-pulse rounded-xl bg-white/[0.035]" />
      <div className="h-80 animate-pulse rounded-xl bg-white/[0.035]" />
    </div>
  )
}

function InferenceRoutingError({ error }: { error: Error }) {
  return (
    <div className="mx-auto mt-6 max-w-2xl rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6">
      <AlertTriangle className="h-6 w-6 text-[var(--red)]" />
      <h2 className="mt-4 text-lg font-semibold">Inference routing is unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
      <p className="mt-3 text-xs text-[var(--muted)]">
        Confirm platform #298 is deployed dark and Backroom has the platform admin token.
      </p>
    </div>
  )
}
