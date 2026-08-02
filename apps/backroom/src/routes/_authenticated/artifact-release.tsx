import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, TimerReset } from 'lucide-react'
import { ArtifactReleaseControlPanel } from '../../components/ArtifactReleaseControlPanel'
import { PageHeader } from '../../components/PageHeader'
import { getArtifactReleaseControl } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/artifact-release')({
  loader: () => getArtifactReleaseControl(),
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: ArtifactReleasePage,
})

function ArtifactReleasePage() {
  const initialState = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 transparency"
        title="Public source release"
        description="Stage the privacy window for the leaderboard king. Only the top agent's source is ever released — no other submissions are affected — and the window is measured from when it first takes the throne."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <TimerReset className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Platform managed
          </div>
        }
      />
      <ArtifactReleaseControlPanel
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
  return (
    <div className="mx-auto mt-6 max-w-2xl rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6">
      <AlertTriangle className="h-6 w-6 text-[var(--red)]" />
      <h2 className="mt-4 text-lg font-semibold">Source-release controls unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
