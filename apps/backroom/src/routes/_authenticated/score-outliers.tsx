import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, Activity } from 'lucide-react'
import { z } from 'zod'
import { PageHeader } from '../../components/PageHeader'
import { ScoreOutlierPanel } from '../../components/ScoreOutlierPanel'
import { listScoreOutliers } from '../../server/admin.functions'

export const SCORE_OUTLIER_PAGE_SIZE = 50

const searchSchema = z.object({ page: z.coerce.number().int().min(1).catch(1) })

export const Route = createFileRoute('/_authenticated/score-outliers')({
  validateSearch: searchSchema,
  loaderDeps: ({ search }) => ({ page: search.page }),
  loader: ({ deps: { page } }) =>
    listScoreOutliers({
      data: {
        limit: SCORE_OUTLIER_PAGE_SIZE,
        offset: (page - 1) * SCORE_OUTLIER_PAGE_SIZE,
      },
    }),
  pendingComponent: ScoreOutliersPending,
  errorComponent: ScoreOutliersError,
  component: ScoreOutliersPage,
})

function ScoreOutliersPage() {
  const initial = Route.useLoaderData()
  const { page } = Route.useSearch()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 scoring"
        title="Score outliers"
        description="Review finalized three-validator score sets with one result far outside its peers. Re-tests preserve the public finalized score until the same validator submits a replacement."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <Activity className="h-3.5 w-3.5 text-[var(--amber)]" />
            Platform detected
          </div>
        }
      />
      {/* Keyed by page: the panel holds the page's rows and their unsent audit
          reasons in local state, so a page change has to be a fresh mount
          rather than new loader data arriving behind stale state. */}
      <ScoreOutlierPanel
        key={page}
        initialItems={initial.items}
        initialCount={initial.count}
        initialBenchVersion={initial.bench_version}
        page={page}
        pageSize={SCORE_OUTLIER_PAGE_SIZE}
        readOnly={user.accessLevel === 'read'}
      />
    </div>
  )
}

function ScoreOutliersPending() {
  return <div className="mt-6 h-64 animate-pulse rounded-xl bg-white/[0.035]" aria-label="Loading score outliers" />
}

function ScoreOutliersError({ error }: { error: Error }) {
  return (
    <div className="mx-auto mt-6 max-w-2xl rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6">
      <AlertTriangle className="h-6 w-6 text-[var(--red)]" />
      <h2 className="mt-4 text-lg font-semibold">Score outliers are unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
