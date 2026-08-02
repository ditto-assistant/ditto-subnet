import { createFileRoute } from '@tanstack/react-router'
import { z } from 'zod'
import { ScreeningQuarantinePanel } from '../../components/ScreeningQuarantinePanel'
import {
  ScreeningError,
  ScreeningPending,
  ScreeningRouteFrame,
} from '../../components/ScreeningRouteFrame'
import {
  listScreeningDisputes,
  listScreeningQuarantines,
  listScreeningSubmissions,
} from '../../server/admin.functions'

const PAGE_SIZE = 50
const searchSchema = z.object({ page: z.coerce.number().int().min(1).catch(1) })

export const Route = createFileRoute('/_authenticated/screening-quarantine/disputes')({
  validateSearch: searchSchema,
  loaderDeps: ({ search }) => ({ page: search.page }),
  loader: async ({ deps: { page } }) => {
    const offset = (page - 1) * PAGE_SIZE
    const [quarantines, disputes, submissions] = await Promise.all([
      listScreeningQuarantines({ data: { status: 'active', sort: 'oldest' } }),
      listScreeningDisputes({ data: { status: 'pending', limit: PAGE_SIZE, offset } }),
      listScreeningSubmissions({ data: { limit: 1, offset: 0 } }),
    ])
    return { quarantines, disputes, submissions, page }
  },
  pendingComponent: ScreeningPending,
  errorComponent: ScreeningError,
  component: ScreeningDisputesPage,
})

function ScreeningDisputesPage() {
  const { quarantines, disputes, submissions, page } = Route.useLoaderData()
  const { user } = Route.useRouteContext()

  return (
    <ScreeningRouteFrame reviewCount={quarantines.count} disputeCount={disputes.count}>
      <ScreeningQuarantinePanel
        view="disputes"
        initialItems={[]}
        initialDisputes={disputes.items}
        initialSubmissions={[]}
        quarantineCount={quarantines.count}
        disputeCount={disputes.count}
        submissionCount={submissions.count}
        page={page}
        pageSize={PAGE_SIZE}
        readOnly={user.accessLevel === 'read'}
      />
    </ScreeningRouteFrame>
  )
}
