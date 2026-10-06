import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle } from 'lucide-react'
import { PageHeader } from '../../components/PageHeader'
import { TreasuryControlPanel } from '../../components/TreasuryControlPanel'
import { TreasuryManualTransferPanel } from '../../components/TreasuryManualTransferPanel'
import { getTreasurySettings } from '../../server/treasury.functions'
import { getManualTransfers } from '../../server/treasury-manual.functions'

export const Route = createFileRoute('/_authenticated/gamma')({
  loader: async () => {
    const [treasury, manual] = await Promise.all([
      getTreasurySettings().then(
        (state) => ({ state, error: null }),
        () => ({ state: null, error: 'Treasury policy is unavailable. Refresh to try again.' }),
      ),
      getManualTransfers().then(
        (state) => ({ state, error: null }),
        () => ({ state: null, error: 'Manual transfer controls are unavailable. Refresh to try again.' }),
      ),
    ])
    return { treasury, manual }
  },
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: GammaPage,
})

function GammaPage() {
  const { treasury, manual } = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  const readOnly = user.accessLevel === 'read'
  return (
    <div>
      <PageHeader
        label="SN118 service funding"
        title="Gamma & transfers"
        description="Transfer collector alpha to approved service wallets, track transfer requests, and manage service wallet policy. A finalized wallet transfer does not confirm purchased GM credits."
      />
      <div className="mt-6 space-y-6">
        {manual.state ? (
          <TreasuryManualTransferPanel initialState={manual.state} readOnly={readOnly} />
        ) : (
          <p className="text-sm text-[var(--red)]" role="alert">{manual.error}</p>
        )}
        {treasury.state ? (
          <TreasuryControlPanel initialState={treasury.state} readOnly={readOnly} />
        ) : (
          <p className="text-sm text-[var(--red)]" role="alert">{treasury.error}</p>
        )}
      </div>
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
      <h2 className="mt-4 text-lg font-semibold">Gamma controls unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
