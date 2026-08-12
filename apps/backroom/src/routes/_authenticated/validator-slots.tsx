import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, Layers } from 'lucide-react'
import { PageHeader } from '../../components/PageHeader'
import { ValidatorFleetUpdatePanel } from '../../components/ValidatorFleetUpdatePanel'
import { ValidatorSlotControlPanel } from '../../components/ValidatorSlotControlPanel'
import {
  getValidatorFleet,
  getValidatorFleetUpdate,
  getValidatorSlotSettings,
} from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/validator-slots')({
  // The fleet read resolves to null rather than throwing, so heartbeat trouble
  // can never keep the slot cap itself off the screen.
  loader: async () => {
    const [control, fleet, fleetUpdate] = await Promise.all([
      getValidatorSlotSettings(),
      getValidatorFleet(),
      getValidatorFleetUpdate(),
    ])
    return { control, fleet, fleetUpdate }
  },
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: ValidatorSlotsPage,
})

function ValidatorSlotsPage() {
  const { control, fleet, fleetUpdate } = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="SN118 dispatch"
        title="Validator fleet control"
        description="Shape ordinary benchmark dispatch with the slot and resource policy, or use the separately guarded emergency control to revoke active work and request a managed-stack update. Every mutation is audited; command receipt and deployed runtime revision remain distinct evidence."
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
      <div className="mt-5">
        <ValidatorFleetUpdatePanel
          initialPreview={fleetUpdate}
          readOnly={user.accessLevel === 'read'}
        />
      </div>
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
      <h2 className="mt-4 text-lg font-semibold">Validator fleet controls unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
