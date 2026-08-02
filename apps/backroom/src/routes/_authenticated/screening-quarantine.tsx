import { createFileRoute, Outlet } from '@tanstack/react-router'

export const Route = createFileRoute('/_authenticated/screening-quarantine')({
  component: ScreeningQuarantineLayout,
})

function ScreeningQuarantineLayout() {
  return <Outlet />
}
