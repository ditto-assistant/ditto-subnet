import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, RotateCw } from 'lucide-react'
import { ContinualRetestControlPanel } from '../../components/ContinualRetestControlPanel'
import { PageHeader } from '../../components/PageHeader'
import { getContinualRetestSettings } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/continual-retests')({
  loader: () => getContinualRetestSettings(),
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: ContinualRetestsPage,
})

function ContinualRetestsPage() {
  const initialState = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 scoring"
        title="Continual retests"
        description="Hot-swap which retests count toward the fold, how deep the lane reaches down the ranking and where it draws the bottom edge, completed-wave aggregation, spare-capacity retests, and how the lane yields to an open benchmark rollout — no platform redeploy. The fold rule changes what validators weight; strict is the audited rollback path. Every change is an append-only revision that stores the whole policy."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <RotateCw className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Platform managed
          </div>
        }
      />
      <ContinualRetestControlPanel initialState={initialState} readOnly={user.accessLevel === 'read'} />
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
      <h2 className="mt-4 text-lg font-semibold">Continual retest policy unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
