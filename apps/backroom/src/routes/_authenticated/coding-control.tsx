import { createFileRoute, useRouter } from '@tanstack/react-router'
import { AlertTriangle, Code2 } from 'lucide-react'
import { CodingControlPlane } from '../../components/CodingControlPlane'
import { PageHeader } from '../../components/PageHeader'
import { getCodingControlPlane } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/coding-control')({
  loader: () => getCodingControlPlane({ data: { limit: 50 } }),
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: CodingControlPage,
})

function CodingControlPage() {
  const initialState = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 shadow operations"
        title="Coding Bench control plane"
        description="Register externally signed private releases, prepare exact artifact-bound runs, issue fixed k=3 validator tickets, and inspect the permanent weight-zero ledger."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <Code2 className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Shadow only
          </div>
        }
      />
      <CodingControlPlane
        initialState={initialState}
        readOnly={user.accessLevel === 'read'}
      />
    </div>
  )
}

function Pending() {
  return <div className="mt-6 h-80 animate-pulse rounded-xl bg-white/[0.035]" />
}

function ErrorState({ error }: { error: Error }) {
  const router = useRouter()
  return (
    <div className="mx-auto mt-6 max-w-2xl rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6">
      <AlertTriangle className="h-6 w-6 text-[var(--red)]" />
      <h2 className="mt-4 text-lg font-semibold">Coding controls unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
      <button type="button" onClick={() => void router.invalidate()}
        className="mt-4 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs font-medium text-[var(--muted-strong)] hover:text-[var(--fg)]">
        Retry
      </button>
    </div>
  )
}
