import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle } from 'lucide-react'
import { PageHeader } from '../../components/PageHeader'
import { TreasuryControlPanel } from '../../components/TreasuryControlPanel'
import { TreasuryManualTransferPanel } from '../../components/TreasuryManualTransferPanel'
import { getTreasuryRuntime, getTreasurySettings } from '../../server/treasury.functions'
import { getManualTransfers } from '../../server/treasury-manual.functions'

export const Route = createFileRoute('/_authenticated/gamma')({
  loader: async () => {
    const [treasury, manual, runtime] = await Promise.all([
      getTreasurySettings().then(
        (state) => ({ state, error: null }),
        () => ({ state: null, error: 'Treasury policy is unavailable. Refresh to try again.' }),
      ),
      getManualTransfers().then(
        (state) => ({ state, error: null }),
        () => ({ state: null, error: 'Manual transfer controls are unavailable. Refresh to try again.' }),
      ),
      getTreasuryRuntime().then(
        (state) => ({ state, error: null }),
        () => ({ state: null, error: 'Signed emission policy is unavailable. Refresh to try again.' }),
      ),
    ])
    return { treasury, manual, runtime }
  },
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: GammaPage,
})

function GammaPage() {
  const { treasury, manual, runtime } = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  const readOnly = user.accessLevel === 'read'
  const signed = runtime.state?.latest?.settings
  const signedGammaPercent = (signed?.approval.policy.buckets.reduce((sum, bucket) => sum + bucket.allocation_bps, 0) ?? 0) / 100
  return (
    <div>
      <PageHeader
        label="SN118 service funding"
        title="Gamma & transfers"
        description="Set the proposed Gamma and miner split, review the signed emission policy, and transfer collector alpha to approved service wallets."
      />
      <div className="mt-6 space-y-6">
        {runtime.state ? (
          <section aria-label="Signed Gamma emission policy" className="space-y-2">
            <h2 className="text-lg font-semibold">Signed emission policy</h2>
            {signed ? <>
                <p>Gamma {signedGammaPercent}% · Miners {100 - signedGammaPercent}% before burn · {signed.mode}</p>
                <p className="text-sm text-[var(--muted-strong)]">
                  Signed policy revision {signed.approval.policy.revision}. This is the recorded runtime
                  configuration; finalized weights must be verified separately. Saving a proposal below
                  does not replace this signed policy.
                </p>
              </> : <p>No signed emission policy is recorded.</p>}
          </section>
        ) : <p role="alert" className="text-sm text-[var(--red)]">{runtime.error}</p>}
        {treasury.state ? (
          <TreasuryControlPanel initialState={treasury.state} readOnly={readOnly} />
        ) : (
          <p className="text-sm text-[var(--red)]" role="alert">{treasury.error}</p>
        )}
        {manual.state ? (
          <TreasuryManualTransferPanel initialState={manual.state} readOnly={readOnly} />
        ) : (
          <p className="text-sm text-[var(--red)]" role="alert">{manual.error}</p>
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
