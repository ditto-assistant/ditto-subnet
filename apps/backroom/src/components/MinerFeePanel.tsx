import { useState } from 'react'
import { CalendarDays, CircleDollarSign, Coins, Users, Download } from 'lucide-react'
import type { MinerFeeSummary } from '../lib/miner-fees'
import { MetricCard } from './MetricCard'

const RAO_PER_TAO = 1_000_000_000
const tao = (rao: number, digits = 6) =>
  new Intl.NumberFormat('en-US', { maximumFractionDigits: digits }).format(
    rao / RAO_PER_TAO,
  )
const usd = (value: number) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value)
const dateTime = (value: string | null) =>
  value
    ? `${new Intl.DateTimeFormat('en-US', {
        dateStyle: 'medium',
        timeStyle: 'short',
        timeZone: 'UTC',
      }).format(new Date(value))} UTC`
    : 'No payments yet'

export function MinerFeePanel({ summary, onExport }: {
  summary: MinerFeeSummary
  onExport?: () => Promise<string>
}) {
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState('')
  async function downloadCsv() {
    if (!onExport || exporting) return
    setExporting(true)
    setExportError('')
    try {
      const csv = await onExport()
      const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'miner-submission-fees.csv'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) {
      setExportError(error instanceof Error ? error.message : 'Export failed. Try again.')
    } finally {
      setExporting(false)
    }
  }
  const averageRao = summary.paid_submissions
    ? summary.gross_amount_rao / summary.paid_submissions
    : 0
  const coverage = summary.paid_submissions
    ? (summary.priced_submissions / summary.paid_submissions) * 100
    : 100

  return (
    <div className="space-y-6">
      {onExport ? <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-[var(--muted)]">Export every recorded payment, across all dates and collection addresses.</p>
        <button type="button" onClick={() => void downloadCsv()} disabled={exporting} className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-[var(--line)] px-4 text-sm disabled:opacity-50"><Download className="h-4 w-4" />{exporting ? 'Exporting…' : 'Export all payments CSV'}</button>
      </div> : null}
      {exportError ? <p role="alert" className="text-sm text-[var(--red)]">{exportError}</p> : null}
      <section className="summary-strip overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <MetricCard label="Gross submission fees" value={`${tao(summary.gross_amount_rao, 9)} TAO`} note="Canonical total from accepted payment records" icon={Coins} tone="acid" />
        <MetricCard label="Paid submissions" value={summary.paid_submissions.toLocaleString()} note={`${tao(averageRao)} TAO average accepted payment`} icon={CircleDollarSign} tone="cyan" />
        <MetricCard label="Historical USD value" value={usd(summary.gross_value_usd)} note={`${summary.priced_submissions.toLocaleString()} payments have a captured rate`} icon={CalendarDays} tone={summary.unpriced_submissions ? 'amber' : 'neutral'} />
        <MetricCard label="Unique paying wallets" value={summary.unique_paying_coldkeys.toLocaleString()} note="Distinct payment-time miner coldkeys" icon={Users} />
      </section>

      {summary.unpriced_submissions > 0 ? (
        <section className="rounded-xl border border-[var(--amber)]/30 bg-[var(--amber-dim)] px-4 py-3 text-sm text-[var(--muted-strong)]">
          <span className="font-medium text-[var(--amber)]">{summary.unpriced_submissions.toLocaleString()} legacy payments are unpriced.</span>{' '}
          Their TAO is included in gross fees, but they are excluded from historical USD value until an audited price backfill is available.
        </section>
      ) : null}

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-col gap-4 border-b border-[var(--line)] px-5 py-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 className="text-sm font-semibold">Accounting coverage</h2>
            <p className="mt-1 text-xs leading-5 text-[var(--muted)]">TAO comes from the immutable payment ledger. USD uses the rate captured during payment verification.</p>
          </div>
          <p className="text-xs text-[var(--muted-strong)]">{coverage.toFixed(1)}% price coverage</p>
        </div>
        <dl className="grid gap-px bg-[var(--line)] sm:grid-cols-2 lg:grid-cols-4">
          {[
            ['First payment', dateTime(summary.first_payment_at)],
            ['Latest payment', dateTime(summary.last_payment_at)],
            ['Receive address', summary.payment_address],
            ['Snapshot', dateTime(summary.generated_at)],
          ].map(([label, value]) => (
            <div key={label} className="min-w-0 bg-[var(--panel)] px-5 py-4">
              <dt className="text-[11px] text-[var(--muted)]">{label}</dt>
              <dd className="mt-1 truncate font-mono text-xs text-[var(--muted-strong)]" title={value}>{value}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-5 py-4">
          <h2 className="text-sm font-semibold">Collection address history</h2>
          <p className="mt-1 text-xs leading-5 text-[var(--muted)]">These fees are already included in gross submission fees above. They are not current wallet balances. First and latest payments are observed ledger dates, not address activation dates.</p>
        </div>
        {summary.address_history ? <div className="overflow-x-auto"><table className="w-full min-w-[52rem] text-left text-xs">
          <thead className="bg-[var(--panel-soft)] text-[var(--muted)]"><tr>
            <th className="px-5 py-3">Collection address</th><th className="px-5 py-3">Status</th><th className="px-5 py-3">Payments</th><th className="px-5 py-3">Gross TAO</th><th className="px-5 py-3">First payment</th><th className="px-5 py-3">Latest payment</th>
          </tr></thead>
          <tbody className="divide-y divide-[var(--line)]">{summary.address_history.map(address => <tr key={address.payment_address}>
            <td className="max-w-sm break-all px-5 py-3 font-mono select-all">{address.payment_address}</td>
            <td className="px-5 py-3">{address.is_current ? 'Current' : 'Previous / configured'}</td>
            <td className="px-5 py-3 tabular-nums">{address.paid_submissions.toLocaleString()}</td>
            <td className="px-5 py-3 font-mono tabular-nums">{tao(address.gross_amount_rao, 9)}</td>
            <td className="px-5 py-3">{dateTime(address.first_payment_at)}</td>
            <td className="px-5 py-3">{dateTime(address.last_payment_at)}</td>
          </tr>)}</tbody>
        </table></div> : <p className="px-5 py-4 text-sm text-[var(--amber)]">Address history is unavailable from this Platform version. The gross total still includes all recorded destinations.</p>}
        <p className="px-5 py-4 text-xs text-[var(--muted)]">Includes ledger destinations and saved configuration. Unrecorded on-chain transfers and older configuration without payment records cannot be reconstructed here.</p>
      </section>

      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="border-b border-[var(--line)] px-5 py-4">
          <h2 className="text-sm font-semibold">Recent daily fees</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">Database-recorded payments from the trailing 30 days.</p>
        </div>
        {summary.recent_days.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[42rem] text-left text-xs">
              <thead className="bg-[var(--panel-soft)] text-[var(--muted)]">
                <tr>
                  <th className="px-5 py-3 font-medium">UTC date</th><th className="px-5 py-3 text-right font-medium">Submissions</th><th className="px-5 py-3 text-right font-medium">Gross TAO</th><th className="px-5 py-3 text-right font-medium">Historical USD</th><th className="px-5 py-3 text-right font-medium">Price coverage</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--line)]">
                {[...summary.recent_days].reverse().map((day) => (
                  <tr key={day.date} className="text-[var(--muted-strong)]">
                    <td className="px-5 py-3 font-medium text-white">{day.date}</td><td className="px-5 py-3 text-right tabular-nums">{day.paid_submissions}</td><td className="px-5 py-3 text-right font-mono tabular-nums">{tao(day.gross_amount_rao, 9)}</td><td className="px-5 py-3 text-right tabular-nums">{usd(day.gross_value_usd)}</td><td className="px-5 py-3 text-right tabular-nums">{day.priced_submissions}/{day.paid_submissions}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="px-5 py-8 text-sm text-[var(--muted)]">No paid submissions have been recorded.</p>}
      </section>
    </div>
  )
}
