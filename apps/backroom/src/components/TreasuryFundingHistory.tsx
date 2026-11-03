import { useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { fundingAmount } from '../lib/treasury-funding.schemas'
import type { FundingHistory, FundingMovement } from '../lib/treasury-funding.schemas'
import { getTreasuryFundingHistory } from '../server/treasury-funding.functions'

const dollars = (cents: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100)
const assetName = (asset: string) => asset === 'SN118_ALPHA' ? 'DITTO' : asset === 'SN28_ALPHA' ? 'SN28 alpha' : 'TAO'
function Movement({ movement }: { movement: FundingMovement }) {
  return <div>
    <p className="tabular-nums">{fundingAmount(movement.input_amount_atomic)} {assetName(movement.input_asset)}
      {movement.output_amount_atomic && movement.output_asset ? <> → {fundingAmount(movement.output_amount_atomic)} {assetName(movement.output_asset)}</> : null}</p>
    <a className="text-xs text-[var(--muted-strong)] underline underline-offset-2" href={`https://bittensor.ai/explorer/extrinsic/${movement.reference}`} target="_blank" rel="noreferrer">{movement.reference} ↗</a>
  </div>
}
export function TreasuryFundingHistory({ initialState }: { initialState: FundingHistory }) {
  const [state, setState] = useState(initialState)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function refresh() {
    setBusy(true); setError(null)
    try { setState(await getTreasuryFundingHistory()) }
    catch { setError('Funding history could not be refreshed. Try again.') }
    finally { setBusy(false) }
  }
  const saleUses = new Map<string, number>()
  for (const item of state.items) {
    const ref = item.record.preceding_sale?.reference
    if (ref) saleUses.set(ref, (saleUses.get(ref) ?? 0) + 1)
  }
  return <section aria-label="GM funding history" className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-5 sm:p-6">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div><h2 className="text-lg font-semibold">DITTO → TAO → GM credits</h2>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-[var(--muted-strong)]">Completed manual top-ups, reconciled by an operator from chain activity and GM purchase records. Payments are matched by time and recipient; GM has not supplied a transaction attestation.</p></div>
      <button type="button" onClick={() => void refresh()} disabled={busy} className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-[var(--line)] px-3 text-sm disabled:opacity-50"><RefreshCw className={`h-4 w-4 ${busy ? 'animate-spin' : ''}`} />{busy ? 'Refreshing…' : 'Refresh history'}</button>
    </div>
    {error ? <p role="alert" className="mt-3 text-sm text-[var(--red)]">{error}</p> : null}
    {state.items.length === 0 ? <p className="mt-5 text-sm text-[var(--muted-strong)]">No reconciled purchases yet. A collector transfer only moves DITTO to the holding wallet. Sales, TAO payments and GM credits appear here after their records are reconciled.</p> : <>
      <dl className="my-5 grid gap-4 sm:grid-cols-3">
        <div><dt className="text-xs text-[var(--muted-strong)]">GM credits added</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{dollars(state.credited_usd_cents)}</dd></div>
        <div><dt className="text-xs text-[var(--muted-strong)]">TAO paid to GM</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{fundingAmount(state.tao_paid_rao)}</dd></div>
        <div><dt className="text-xs text-[var(--muted-strong)]">Completed purchases</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{state.items.length}</dd></div>
      </dl>
      {state.truncated ? <p className="mb-3 text-sm">Showing the latest 500 records. Totals cover these records only.</p> : null}
      <p id="funding-scroll-help" className="mb-2 text-xs text-[var(--muted-strong)] sm:hidden">Scroll horizontally to view payments and credits.</p>
      <div role="region" aria-label="GM purchase and payment records" aria-describedby="funding-scroll-help" tabIndex={0} className="overflow-x-auto rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--acid)]"><table className="w-full min-w-[760px] text-left text-sm">
        <caption className="sr-only">Operator-reconciled DITTO sales, TAO payments, and completed GM purchases</caption>
        <thead className="border-b border-[var(--line)] text-xs text-[var(--muted-strong)]"><tr><th scope="col" className="py-3 pr-4">GM purchase (UTC)</th><th scope="col" className="py-3 pr-4">Sale / conversion</th><th scope="col" className="py-3 pr-4">Payment to GM</th><th scope="col" className="py-3">Credits added</th></tr></thead>
        <tbody>{state.items.map(item => {
          const record = item.record
          return <tr key={record.purchase.purchase_id} className="border-b border-[var(--line)] align-top last:border-0">
            <td className="py-4 pr-5"><p>{record.purchase.occurred_at.slice(0, 19).replace('T', ' ')}</p><details className="mt-2 text-xs text-[var(--muted-strong)]"><summary className="cursor-pointer">Reconciliation details</summary><div className="mt-2 max-w-64 space-y-2 break-words"><p>GM purchase {record.purchase.purchase_id}</p><p>Operator: {item.actor}</p><p>Recorded: {item.recorded_at}</p><p>Matched by time and recipient. This purchase does not prove that its full payment came from the preceding sale; existing TAO and fees can affect the balance.</p></div></details></td>
            <td className="py-4 pr-5">{record.earlier_conversions.map((m, index) => <div className="mb-2" key={`${m.reference}-${index}`}><Movement movement={m} /></div>)}{record.preceding_sale ? <><Movement movement={record.preceding_sale} />{(saleUses.get(record.preceding_sale.reference) ?? 0) > 1 ? <p className="mt-1 text-xs text-[var(--muted-strong)]">Shared sale · counted once</p> : null}</> : <p className="text-[var(--muted-strong)]">Existing TAO balance<br /><span className="text-xs">No matched successful sale</span></p>}</td>
            <td className="py-4 pr-5"><Movement movement={record.payment} /></td>
            <td className="py-4 font-semibold tabular-nums">{dollars(record.purchase.credited_usd_cents)}<span className="mt-1 block text-xs font-normal text-[var(--muted-strong)]">Completed · operator reconciled</span></td>
          </tr>
        })}</tbody>
      </table></div>
    </>}
    <p className="mt-4 text-xs leading-5 text-[var(--muted-strong)]">Historical credits added are separate from the current spendable GM balance. Automatic top-ups are off.</p>
  </section>
}
