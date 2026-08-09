import { createFileRoute } from '@tanstack/react-router'
import { AlertTriangle, FlaskConical } from 'lucide-react'
import { CONFIRMATION_BUNDLE_PAGE_SIZE, ConfirmationBundleControlPanel } from '../../components/ConfirmationBundleControlPanel'
import { PageHeader } from '../../components/PageHeader'
import {
  getConfirmationBundleSettings,
  listConfirmationBundles,
} from '../../server/admin.functions'

export async function loadConfirmationBundlesPage() {
  const [settings, bundles] = await Promise.all([
    getConfirmationBundleSettings(),
    listConfirmationBundles({ data: { limit: CONFIRMATION_BUNDLE_PAGE_SIZE } }),
  ])
  return { settings, bundles }
}

export const Route = createFileRoute('/_authenticated/confirmation-bundles')({
  loader: loadConfirmationBundlesPage,
  pendingComponent: Pending,
  errorComponent: ErrorState,
  component: ConfirmationBundlesPage,
})

function ConfirmationBundlesPage() {
  const { settings, bundles } = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  return (
    <div>
      <PageHeader
        label="Bench v9 qualification"
        title="Confirmation bundles"
        description="Audit the shared LongMem and binary-ablation evidence that qualifies Bench v9 candidates, control bounded issuance through append-only settings, and explicitly authorize evidence retests. This surface cannot submit evidence, change canonical scores, or activate rewards."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <FlaskConical className="h-3.5 w-3.5 text-[var(--cyan)]" />
            Isolated qualification lane
          </div>
        }
      />
      <ConfirmationBundleControlPanel
        initialSettings={settings}
        initialBundles={bundles}
        readOnly={user.accessLevel === 'read'}
      />
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
      <h2 className="mt-4 text-lg font-semibold">Confirmation controls unavailable</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted-strong)]">{error.message}</p>
    </div>
  )
}
