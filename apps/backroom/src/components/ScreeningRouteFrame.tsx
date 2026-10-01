import { AlertTriangle, Clock3 } from 'lucide-react'
import type { ReactNode } from 'react'
import { PageHeader } from './PageHeader'

/**
 * Active quarantines that still need a screening ruling. An active row behind
 * an already banned agent is reconciliation work (ditto-subnet#2038), so the
 * header and the queue tab count only actionable rows. Falls back to the raw
 * total against a platform that predates `actionable_count`.
 */
export function actionableQuarantineCount(quarantines: {
  count: number
  actionable_count?: number | null
}) {
  return quarantines.actionable_count ?? quarantines.count
}

export function ScreeningRouteFrame({
  reviewCount,
  disputeCount,
  children,
}: {
  reviewCount: number
  disputeCount: number
  children: ReactNode
}) {
  return (
    <div>
      <PageHeader
        label="SN118 screening"
        title="Screening review"
        description="Review submissions held by the anti-cheat screener and miners' one-time disputes. Every decision is recorded with the operator and reason."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--amber)]/25 bg-[var(--amber-dim)] px-3 py-2 text-xs text-[var(--amber)]">
            <Clock3 className="h-3.5 w-3.5" />
            {reviewCount} reviews · {disputeCount} disputes
          </div>
        }
      />
      {children}
    </div>
  )
}

export function ScreeningPending() {
  return (
    <div className="space-y-5" aria-label="Loading screening review">
      <div className="h-24 animate-pulse rounded-xl bg-white/[0.035]" />
      <div className="h-72 animate-pulse rounded-xl bg-white/[0.035]" />
    </div>
  )
}

export function ScreeningError({ error }: { error: Error }) {
  return (
    <div className="mx-auto max-w-2xl rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6">
      <AlertTriangle className="h-6 w-6 text-[var(--red)]" />
      <h2 className="mt-4 text-lg font-semibold">Screening controls are unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
      <p className="mt-3 text-xs text-[var(--muted)]">
        Confirm the Backroom and platform services share the configured admin token.
      </p>
    </div>
  )
}
