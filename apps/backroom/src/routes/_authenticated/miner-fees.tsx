import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, Database } from 'lucide-react'
import { MinerFeePanel } from '../../components/MinerFeePanel'
import { PageHeader } from '../../components/PageHeader'
import { getMinerFeeSummary } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/miner-fees')({
  loader: () => getMinerFeeSummary(),
  pendingComponent: MinerFeesPending,
  errorComponent: MinerFeesError,
  component: MinerFeesPage,
})

function MinerFeesPage() {
  const summary = Route.useLoaderData()
  return <div><PageHeader label="SN118 accounting" title="Miner submission fees" description="Track gross TAO received for accepted submissions and historical USD value captured when each payment was verified. Validator stake and unrelated wallet activity are excluded." aside={<div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]"><Database className="h-3.5 w-3.5 text-[var(--acid)]" />Database backed</div>} /><MinerFeePanel summary={summary} /></div>
}

function MinerFeesPending() {
  return <div className="mt-6 space-y-5" aria-label="Loading miner fee accounting"><div className="h-36 animate-pulse rounded-xl bg-white/[0.035]" /><div className="h-52 animate-pulse rounded-xl bg-white/[0.035]" /></div>
}

function MinerFeesError({ error }: { error: Error }) {
  return <div className="mx-auto mt-6 max-w-2xl rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6"><AlertTriangle className="h-6 w-6 text-[var(--red)]" /><h2 className="mt-4 text-lg font-semibold">Miner fee accounting is unavailable</h2><p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p><p className="mt-3 text-xs text-[var(--muted)]">Confirm the platform miner-fee admin endpoint is deployed and Backroom has its admin token.</p></div>
}
