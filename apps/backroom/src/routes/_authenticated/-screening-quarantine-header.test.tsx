// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import type { ComponentType } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// ditto-subnet#2038: an active quarantine behind an already banned agent is
// reconciliation work. The route header must count only actionable reviews,
// the same number the queue tab shows, never the raw pagination total.
const loaderData = vi.hoisted(() => ({ current: {} as Record<string, unknown> }))
const panelProps = vi.hoisted(() => ({ current: {} as Record<string, unknown> }))

vi.mock('@tanstack/react-router', () => ({
  createFileRoute: () => (options: Record<string, unknown>) => ({
    ...options,
    useLoaderData: () => loaderData.current,
    useRouteContext: () => ({ user: { accessLevel: 'read' } }),
  }),
}))

vi.mock('../../server/admin.functions', () => ({}))

vi.mock('../../components/ScreeningQuarantinePanel', () => ({
  ScreeningQuarantinePanel: (props: Record<string, unknown>) => {
    panelProps.current = props
    return null
  },
}))

vi.mock('../../components/BenchmarkContractMigrationPanel', () => ({
  BenchmarkContractMigrationPanel: () => null,
}))
vi.mock('../../components/BenchmarkContractRefreshPanel', () => ({
  BenchmarkContractRefreshPanel: () => null,
}))
vi.mock('../../components/ScreenedImageRebuildPanel', () => ({
  ScreenedImageRebuildPanel: () => null,
}))
vi.mock('../../components/ValidatorAssignmentPanel', () => ({
  ValidatorAssignmentPanel: () => null,
}))
vi.mock('../../components/ValidatorRetryPanel', () => ({
  ValidatorRetryPanel: () => null,
}))
vi.mock('../../components/StuckSubmissionFleetPanel', () => ({
  StuckSubmissionFleetPanel: () => null,
}))

import { Route as DisputesRoute } from './screening-quarantine.disputes'
import { Route as HistoryRoute } from './screening-quarantine.history'
import { Route as QueueRoute } from './screening-quarantine.index'

afterEach(() => {
  cleanup()
  panelProps.current = {}
})

const quarantines = {
  items: [],
  // Three active rows, two of them terminal ghosts behind banned agents.
  count: 3,
  terminal_ghost_count: 2,
  actionable_count: 1,
  oldest_actionable_created_at: null,
}
const page = { items: [], count: 4 }

describe.each([
  ['queue', QueueRoute],
  ['disputes', DisputesRoute],
  ['history', HistoryRoute],
])('screening %s route header', (_name, route) => {
  it('counts actionable quarantines, not terminal ghosts', () => {
    loaderData.current = {
      assignments: { items: [] },
      quarantines,
      disputes: page,
      submissions: page,
      stuck: { items: [], count: 0 },
      page: 1,
    }
    const Page = (route as unknown as { component: ComponentType }).component

    render(<Page />)

    expect(screen.getByText('1 reviews · 4 disputes')).toBeTruthy()
    expect(screen.queryByText('3 reviews · 4 disputes')).toBeNull()
    expect(panelProps.current.quarantineCount).toBe(1)
  })

  it('falls back to the total against a platform without actionable_count', () => {
    loaderData.current = {
      assignments: { items: [] },
      quarantines: { ...quarantines, actionable_count: null },
      disputes: page,
      submissions: page,
      stuck: { items: [], count: 0 },
      page: 1,
    }
    const Page = (route as unknown as { component: ComponentType }).component

    render(<Page />)

    expect(screen.getByText('3 reviews · 4 disputes')).toBeTruthy()
    expect(panelProps.current.quarantineCount).toBe(3)
  })
})
