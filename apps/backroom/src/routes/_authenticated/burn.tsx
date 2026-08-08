import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, Flame } from 'lucide-react'
import { BurnControlPanel } from '../../components/BurnControlPanel'
import { PageHeader } from '../../components/PageHeader'
import { getBurnSettings } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/burn')({
  loader: () => getBurnSettings(),
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: BurnPage,
})

function BurnPage() {
  const initialState = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 emissions"
        title="Emission burn"
        description="Set the share of miner emission validators route to the subnet owner's burn hotkey — no validator release. The remainder is normalized across the eligible miner weights, so the burn scales the competitive vector without re-ordering it. Every change is an append-only revision recording who set it and why."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <Flame className="h-3.5 w-3.5 text-[var(--amber)]" />
            Moves TAO
          </div>
        }
      />
      <BurnControlPanel initialState={initialState} readOnly={user.accessLevel === 'read'} />
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
      <h2 className="mt-4 text-lg font-semibold">Burn policy unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
