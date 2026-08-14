import { createFileRoute } from '@tanstack/react-router'
import { BenchmarkContractMigrationPanel } from '../../components/BenchmarkContractMigrationPanel'
import { BenchmarkContractRefreshPanel } from '../../components/BenchmarkContractRefreshPanel'
import { ScreenedImageRebuildPanel } from '../../components/ScreenedImageRebuildPanel'
import { ScreeningQuarantinePanel } from '../../components/ScreeningQuarantinePanel'
import {
  ScreeningError,
  ScreeningPending,
  ScreeningRouteFrame,
} from '../../components/ScreeningRouteFrame'
import { ValidatorAssignmentPanel } from '../../components/ValidatorAssignmentPanel'
import { ValidatorRetryPanel } from '../../components/ValidatorRetryPanel'
import { StuckSubmissionFleetPanel } from '../../components/StuckSubmissionFleetPanel'
import {
  listScreeningDisputes,
  listScreeningQuarantines,
  listScreeningSubmissions,
  listValidatorAssignments,
  listStuckSubmissions,
} from '../../server/admin.functions'

export const Route = createFileRoute('/_authenticated/screening-quarantine/')({
  loader: async () => {
    const [assignments, quarantines, disputes, submissions, stuck] = await Promise.all([
      listValidatorAssignments(),
      listScreeningQuarantines({ data: { status: 'active', sort: 'oldest' } }),
      listScreeningDisputes({ data: { status: 'pending', limit: 1, offset: 0 } }),
      listScreeningSubmissions({ data: { limit: 1, offset: 0 } }),
      listStuckSubmissions({
        data: { state: ['exhausted'], limit: 200, offset: 0 },
      }),
    ])
    return { assignments, quarantines, disputes, submissions, stuck }
  },
  pendingComponent: ScreeningPending,
  errorComponent: ScreeningError,
  component: ScreeningQueuePage,
})

function ScreeningQueuePage() {
  const { assignments, quarantines, disputes, submissions, stuck } = Route.useLoaderData()
  const { user } = Route.useRouteContext()

  return (
    <ScreeningRouteFrame reviewCount={quarantines.count} disputeCount={disputes.count}>
      <ValidatorAssignmentPanel
        initialItems={assignments.items}
        readOnly={user.accessLevel === 'read'}
      />
      <ValidatorRetryPanel readOnly={user.accessLevel === 'read'} />
      <StuckSubmissionFleetPanel initial={stuck} readOnly={user.accessLevel === 'read'} />
      <BenchmarkContractRefreshPanel readOnly={user.accessLevel === 'read'} />
      <ScreenedImageRebuildPanel readOnly={user.accessLevel === 'read'} />
      <BenchmarkContractMigrationPanel readOnly={user.accessLevel === 'read'} />
      <ScreeningQuarantinePanel
        view="queue"
        initialItems={quarantines.items}
        initialSubmissions={[]}
        quarantineCount={quarantines.count}
        disputeCount={disputes.count}
        submissionCount={submissions.count}
        readOnly={user.accessLevel === 'read'}
      />
    </ScreeningRouteFrame>
  )
}
