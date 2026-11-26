import { beforeEach, describe, expect, it, vi } from 'vitest'

const response = vi.hoisted(() => ({ headers: new Headers(), url: 'https://backroom.example.test/' }))

vi.mock('@tanstack/react-start/server', () => ({
  getRequest: () => new Request(response.url),
  getRequestHeader: () => undefined,
  appendResponseHeader: (name: string, value: string) => response.headers.append(name, value),
}))

import { clearOAuthAttemptCookie, clearSessionCookie } from './session.server'

describe('session cookie response headers', () => {
  beforeEach(() => {
    response.headers = new Headers()
    response.url = 'https://backroom.example.test/'
  })

  it('appends distinct secure session and OAuth cookies to the response', () => {
    clearSessionCookie()
    clearOAuthAttemptCookie()
    const cookies = response.headers.getSetCookie()
    expect(cookies).toHaveLength(2)
    expect(cookies[0]).toContain('__Host-backroom_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0; Secure')
    expect(cookies[1]).toContain('__Host-backroom_oauth=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0; Secure')
  })

  it('uses non-prefixed cookies for local HTTP', () => {
    response.url = 'http://localhost:3000/'
    clearSessionCookie()
    clearOAuthAttemptCookie()
    const cookies = response.headers.getSetCookie()
    expect(cookies).toHaveLength(2)
    expect(cookies[0]).toContain('backroom_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0')
    expect(cookies[1]).toContain('backroom_oauth=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0')
    expect(cookies.join('; ')).not.toContain('__Host-')
    expect(cookies.join('; ')).not.toContain('Secure')
  })
})
