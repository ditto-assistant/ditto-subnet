import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, Copy } from 'lucide-react'
import { PageHeader } from '../../components/PageHeader'
import { CopyReviewPanel } from '../../components/CopyReviewPanel'
import { listCopyReviews } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/copy-review')({
  loader: async () => {
    const reviews = await listCopyReviews({ data: { generation: 'active' } })
    return { reviews }
  },
  pendingComponent: CopyReviewPending,
  errorComponent: CopyReviewError,
  component: CopyReviewPage,
})

function CopyReviewPage() {
  const { reviews } = Route.useLoaderData()
  const { user } = Route.useRouteContext()

  return (
    <div>
      <PageHeader
        label="SN118 scoring"
        title="Operator reviews"
        description="Compare each immutable original hold with the current calibrated evidence, then record an individual, miner-visible clear or reject decision. Automated scoring is paused while a review is pending."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--amber)]/25 bg-[var(--amber-dim)] px-3 py-2 text-xs text-[var(--amber)]">
            <Copy className="h-3.5 w-3.5" />
            {reviews.items.length} awaiting review
          </div>
        }
      />
      <CopyReviewPanel
        initialItems={reviews.items}
        initialBulkEligibleCount={reviews.bulk_eligible_count}
        initialGeneration={reviews.generation}
        initialActiveBenchVersion={reviews.active_bench_version}
        readOnly={user.accessLevel === 'read'}
      />
    </div>
  )
}

function CopyReviewPending() {
  return (
    <div className="space-y-5" aria-label="Loading operator reviews">
      <div className="h-24 animate-pulse rounded-xl bg-white/[0.035]" />
      <div className="h-72 animate-pulse rounded-xl bg-white/[0.035]" />
    </div>
  )
}

function CopyReviewError({ error }: { error: Error }) {
  return (
    <div className="mx-auto max-w-2xl rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6">
      <AlertTriangle className="h-6 w-6 text-[var(--red)]" />
      <h2 className="mt-4 text-lg font-semibold">Operator reviews are unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
      <p className="mt-3 text-xs text-[var(--muted)]">
        The copy-review endpoints require a platform deploy that includes the
        admin copy-review API and the shared admin token.
      </p>
    </div>
  )
}
