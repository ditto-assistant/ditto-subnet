import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, Timer } from 'lucide-react'
import { PageHeader } from '../../components/PageHeader'
import { SubmissionCooldownControlPanel } from '../../components/SubmissionCooldownControlPanel'
import { getSubmissionSettingsControl } from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/submission-settings')({
  loader: () => getSubmissionSettingsControl(),
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: SubmissionSettingsPage,
})

function SubmissionSettingsPage() {
  const initialState = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 admission"
        title="Miner submission settings"
        description="Control the TAO-denominated fee and the minimum interval between accepted uploads from the same owner coldkey. The platform is authoritative; compatible clients reserve admission before payment."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <Timer className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Platform managed
          </div>
        }
      />
      <SubmissionCooldownControlPanel
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
      <h2 className="mt-4 text-lg font-semibold">Upload cooldown unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
