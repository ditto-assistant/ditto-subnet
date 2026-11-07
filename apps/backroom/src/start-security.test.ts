import { describe, expect, it } from 'vitest'
import { createServerEntry, getResponseHeaders } from '@tanstack/react-start/server'
import { redirect } from '@tanstack/react-router'
import { startInstance } from './start'
import { clearOAuthAttemptCookie, clearSessionCookie } from './server/session.server'

describe('actual Start request state', () => {
  it('sets security headers and rejects cross-origin mutations', async () => {
    const options = await startInstance.getOptions()
    const handler = options.requestMiddleware![0].options.server!
    let reached = false
    const entry = createServerEntry({
      fetch: async (request) => {
        await handler({ request, context: {}, next: async () => { reached = true; return {} } } as never)
        return new Response('handler reached', { headers: new Headers(Array.from(getResponseHeaders().entries())) })
      },
    })
    const get = await entry.fetch(new Request('https://backroom.example.test/'))
    expect(get.status).toBe(200)
    expect(get.headers.get('content-security-policy')).toContain("frame-ancestors 'none'")
    expect(get.headers.get('strict-transport-security')).toContain('max-age=')
    expect(get.headers.get('x-frame-options')).toBe('DENY')
    expect(get.headers.get('x-content-type-options')).toBe('nosniff')
    expect(get.headers.get('referrer-policy')).toBe('no-referrer')
    expect(get.headers.get('permissions-policy')).toContain('camera=()')
    reached = false
    const post = await entry.fetch(new Request('https://backroom.example.test/', { method: 'POST', headers: { origin: 'https://evil.example.test' } }))
    expect(post.status).toBe(403)
    expect(reached).toBe(false)
    const missingOrigin = await entry.fetch(new Request('https://backroom.example.test/', { method: 'POST' }))
    expect(missingOrigin.status).toBe(403)
    expect(reached).toBe(false)
    const sameOrigin = await entry.fetch(new Request('https://backroom.example.test/', { method: 'POST', headers: { origin: 'https://backroom.example.test' } }))
    expect(sameOrigin.status).toBe(200)
    expect(reached).toBe(true)
  })

  it('retains both Set-Cookie values on a thrown redirect', async () => {
    const entry = createServerEntry({
      fetch: async () => {
        clearSessionCookie()
        clearOAuthAttemptCookie()
        throw redirect({ href: '/login' })
      },
    })
    const response = await entry.fetch(new Request('https://backroom.example.test/'))
    expect(response.status).toBeGreaterThanOrEqual(300)
    expect(response.status).toBeLessThan(400)
    const cookies = response.headers.getSetCookie()
    expect(cookies).toHaveLength(2)
    expect(cookies[0]).toContain('__Host-backroom_session=')
    expect(cookies[1]).toContain('__Host-backroom_oauth=')
  })
})
