import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, Layers } from 'lucide-react'
import { PageHeader } from '../../components/PageHeader'
import { ScoringLeaseControlPanel } from '../../components/ScoringLeaseControlPanel'
import { ValidatorSlotControlPanel } from '../../components/ValidatorSlotControlPanel'
import {
  getScoringLeaseSettings,
  getValidatorFleet,
  getValidatorSlotSettings,
} from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/validator-slots')({
  // The fleet read resolves to null rather than throwing, so heartbeat trouble
  // can never keep the slot cap itself off the screen. The scoring lease board
  // is independent for the same reason.
  loader: async () => {
    const [control, fleet, scoringLease] = await Promise.all([
      getValidatorSlotSettings(),
      getValidatorFleet(),
      getScoringLeaseSettings().catch(() => null),
    ])
    return { control, fleet, scoringLease }
  },
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: ValidatorSlotsPage,
})

function ValidatorSlotsPage() {
  const { control, fleet, scoringLease } = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 dispatch"
        title="Validator slot cap"
        description="How many advertised benchmark slots receive live tickets on any one validator, the disk, memory and CPU ceilings that narrow an overloaded host, the hard stop above which it receives nothing at all, and how long a new scoring lease lasts. Applied at the next ticket issue, live within seconds, and recorded as an append-only audited revision."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <Layers className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Platform managed
          </div>
        }
      />
      <ValidatorSlotControlPanel
        initialState={control}
        initialFleet={fleet}
        readOnly={user.accessLevel === 'read'}
      />
      {scoringLease ? (
        <ScoringLeaseControlPanel
          initialState={scoringLease}
          readOnly={user.accessLevel === 'read'}
        />
      ) : (
        <p className="mt-6 text-xs text-[var(--muted)]">
          Scoring lease settings are unavailable right now. Use get_scoring_lease_settings to read
          them.
        </p>
      )}
    </div>
  )
}

function Pending() {
  return <div className="mt-6 h-96 animate-pulse rounded-xl bg-white/[0.035]" />
}

function ErrorState({ error }: { error: Error }) {
  return (
    <div className="mx-auto mt-6 max-w-2xl rounded-xl border border-[var(--red)]/25 bg-[var(--red-dim)] p-6">
      <AlertTriangle className="h-6 w-6 text-[var(--red)]" />
      <h2 className="mt-4 text-lg font-semibold">Validator slot policy unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
