import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, Bot } from 'lucide-react'
import { PageHeader } from '../../components/PageHeader'
import { ScreenerReviewControlPanel } from '../../components/ScreenerReviewControlPanel'
import { getQueuePolicyControl, getScreenerReviewControl } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/screener-review-settings')({
  loader: async () => {
    const [reviewControl, queuePolicy] = await Promise.all([
      getScreenerReviewControl(),
      getQueuePolicyControl(),
    ])
    return { reviewControl, queuePolicy }
  },
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: ScreenerReviewSettingsPage,
})

function ScreenerReviewSettingsPage() {
  const { reviewControl, queuePolicy } = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 screening"
        title="Agentic review controls"
        description="Control deferred source-review admission, L2/L3 routing, budgets, and one-worker shadow mode through versioned settings. Reading this page never changes a worker."
        aside={<div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]"><Bot className="h-3.5 w-3.5 text-[var(--cyan)]" />Platform managed</div>}
      />
      <ScreenerReviewControlPanel
        initialState={reviewControl}
        initialQueuePolicy={queuePolicy}
        readOnly={user.accessLevel === 'read'}
      />
    </div>
  )
}

function Pending() {
  return <div className="mt-6 h-96 animate-pulse rounded-xl bg-white/[0.035]" />
}

function ErrorState({ error }: { error: Error }) {
  return <div className="mt-6 rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6"><AlertTriangle className="h-6 w-6 text-[var(--red)]" /><h2 className="mt-4 text-lg font-semibold">Reviewer controls unavailable</h2><p className="mt-2 text-sm text-[var(--muted-strong)]">{error.message}</p></div>
}
