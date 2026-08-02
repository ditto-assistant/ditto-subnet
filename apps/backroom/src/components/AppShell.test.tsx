// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AppShell } from './AppShell'

vi.mock('@tanstack/react-router', () => ({
  Link: ({ children }: { children: ReactNode }) => <a href="#">{children}</a>,
  Outlet: () => <div>Route content</div>,
  useRouter: () => ({ navigate: vi.fn(), invalidate: vi.fn() }),
  useRouterState: ({ select }: { select: (state: unknown) => unknown }) =>
    select({ location: { pathname: '/benchmark-rollout' } }),
}))

vi.mock('@tanstack/react-start', () => ({ useServerFn: () => vi.fn() }))
vi.mock('../server/auth.functions', () => ({ logout: vi.fn() }))

describe('AppShell navigation', () => {
  afterEach(cleanup)

  it('keeps route actions above the compact mobile task switcher and safe area', () => {
    render(
      <AppShell
        user={{
          uid: 'operator-1',
          email: 'operator@omniaura.ai',
          name: 'Operator',
          picture: '',
          accessLevel: 'write',
        }}
      />,
    )

    expect(screen.getByRole('main').className).toContain(
      'pb-[calc(5.75rem+env(safe-area-inset-bottom))]',
    )
    expect(screen.getByRole('navigation', { name: 'Mobile task switcher' }).className).toContain('bottom-0')
    expect(screen.getByRole('button', { name: 'All tasks' })).toBeTruthy()
  })

  it('opens search with Command-K and includes tasks and visible page sections', () => {
    render(
      <AppShell
        user={{ uid: 'operator-1', email: 'operator@omniaura.ai', name: 'Operator', picture: '', accessLevel: 'write' }}
      />,
    )

    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    expect(screen.getByRole('dialog', { name: 'Search Backroom' })).toBeTruthy()
    expect(screen.getByRole('dialog', { name: 'Search Backroom' }).textContent).toContain('Bench rollout')
    expect(document.activeElement).toBe(screen.getByLabelText('Search tasks, tabs, and sections'))
  })
})
