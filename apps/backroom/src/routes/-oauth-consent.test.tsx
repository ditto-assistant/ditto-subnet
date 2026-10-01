// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import type { ComponentType } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const data = vi.hoisted(() => ({ observerOnly: true, accessLevel: 'write' }))
vi.mock('@tanstack/react-router', () => ({
  redirect: vi.fn(),
  createFileRoute: () => (options: Record<string, unknown>) => ({ ...options,
    useLoaderData: () => ({ clientName: 'Collector observer', observerOnly: data.observerOnly,
      requestedScopes: data.observerOnly ? ['backroom:treasury:observe'] : ['backroom:read'],
      canRequestArtifact: false, canRequestWrite: false, csrf: 'csrf' }),
    useRouteContext: () => ({ user: { name: 'Staff', email: 'staff@omniaura.ai', accessLevel: data.accessLevel } }),
    useSearch: () => ({ request: 'sealed-request' }),
  }),
}))
vi.mock('../server/auth.functions', () => ({ getCurrentUser: vi.fn() }))
vi.mock('../server/mcp-oauth.functions', () => ({ readMcpConsentRequest: vi.fn() }))
vi.mock('../components/AppShell', () => ({ BackroomMark: () => null }))
import { Route } from './oauth.consent'
const Page = (Route as unknown as { component: ComponentType }).component
afterEach(() => { cleanup(); data.observerOnly = true; data.accessLevel = 'write' })

describe('Dedicated observer consent', () => {
  it('offers only receipt observation and describes its authority boundary', () => {
    render(<Page />)
    expect(screen.getByRole('button', { name: /Allow receipt observations/ }).hasAttribute('disabled')).toBe(false)
    for (const name of ['Read only', 'Read & download source', 'Read & write', 'Full requested access']) {
      expect(screen.getByRole('button', { name: new RegExp(name) }).hasAttribute('disabled')).toBe(true)
    }
    expect(screen.getByText(/Cannot sign, spend, change policy/)).toBeTruthy()
  })
  it('allows decline but never authorizes a read-level staff account', () => {
    data.accessLevel = 'read'
    render(<Page />)
    expect(screen.getByRole('button', { name: /Allow receipt observations/ }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: /Decline/ }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByText(/cannot authorize a treasury observer/)).toBeTruthy()
  })
  it('preserves the ordinary read-only consent default', () => {
    data.observerOnly = false
    render(<Page />)
    expect(screen.getByRole('button', { name: /Allow read only/ }).hasAttribute('disabled')).toBe(false)
    expect(screen.queryByText('Observe treasury receipts')).toBeNull()
  })
})
