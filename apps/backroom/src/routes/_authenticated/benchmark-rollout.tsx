import { createFileRoute, useRouter } from '@tanstack/react-router'
import { AlertTriangle, Gauge } from 'lucide-react'
import { BenchmarkRolloutPanel } from '../../components/BenchmarkRolloutPanel'
import { PageHeader } from '../../components/PageHeader'
import { getBenchmarkRolloutControl } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/benchmark-rollout')({
  loader: () => getBenchmarkRolloutControl(),
  pendingComponent: BenchmarkRolloutPending,
  errorComponent: BenchmarkRolloutError,
  component: BenchmarkRolloutPage,
})

function BenchmarkRolloutPage() {
  const initialState = Route.useLoaderData()
  const { user } = Route.useRouteContext()

  return (
    <div>
      <PageHeader
        label="SN118 benchmark"
        title="Benchmark rollout"
        description="Inspect the versioned scoring transition and deliberately seed qualification. Rollouts never start from page load, validator heartbeat, or scheduled work."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <Gauge className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Operator controlled
          </div>
        }
      />
      <BenchmarkRolloutPanel
        initialState={initialState}
        readOnly={user.accessLevel === 'read'}
      />
    </div>
  )
}

function BenchmarkRolloutPending() {
  return (
    <div className="mt-6 space-y-5" aria-label="Loading benchmark rollout">
      <div className="h-56 animate-pulse rounded-xl bg-white/[0.035]" />
      <div className="h-40 animate-pulse rounded-xl bg-white/[0.035]" />
    </div>
  )
}

function BenchmarkRolloutError({ error }: { error: Error }) {
  const router = useRouter()
  return (
    <div className="mx-auto mt-6 max-w-2xl rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6">
      <AlertTriangle className="h-6 w-6 text-[var(--red)]" />
      <h2 className="mt-4 text-lg font-semibold">Benchmark rollout is unavailable</h2>
      {/*
        The message is written where the failure is actually known -- it names
        latency, the admin token, or a platform fault specifically. A fixed
        footer guessing at the cause used to contradict it, and cost an
        operator an hour chasing an endpoint and a token that were both fine.
      */}
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
      <button
        type="button"
        onClick={() => void router.invalidate()}
        className="mt-4 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs font-medium text-[var(--muted-strong)] hover:text-[var(--fg)]"
      >
        Retry
      </button>
    </div>
  )
}
