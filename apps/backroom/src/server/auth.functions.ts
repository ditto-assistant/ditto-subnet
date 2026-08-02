import { redirect } from '@tanstack/react-router'
import { createMiddleware, createServerFn } from '@tanstack/react-start'
import { getRequest, setResponseHeader } from '@tanstack/react-start/server'
import { z } from 'zod'
import { clearSessionCookie, publicUser, readSession } from './session.server'
import { completeGoogleOAuth, createGoogleAuthorizationUrl } from './oauth.server'
import { isSameOriginRequest } from '../lib/auth.policy'

export const authMiddleware = createMiddleware({ type: 'function' }).server(async ({ next }) => {
  const session = await readSession()
  if (!session) throw new Error('Your Backroom session has expired')
  return next({ context: { session } })
})

export const writeAuthMiddleware = createMiddleware({
  type: 'function',
}).server(async ({ next }) => {
  const session = await readSession()
  if (!session) throw new Error('Your Backroom session has expired')
  if (session.accessLevel !== 'write') {
    throw new Error('This Backroom account has read-only access')
  }
  return next({ context: { session } })
})

export const sameOriginMiddleware = createMiddleware({
  type: 'function',
}).server(async ({ next }) => {
  if (!isSameOriginRequest(getRequest())) throw new Error('Origin check failed')
  return next()
})

export const getCurrentUser = createServerFn({ method: 'GET' }).handler(async () => {
  setResponseHeader('Cache-Control', 'no-store')
  setResponseHeader('Vary', 'Cookie, Authorization')
  const session = await readSession()
  return session ? publicUser(session) : null
})

export const beginGoogleOAuth = createServerFn({ method: 'GET' })
  .validator(z.object({ returnTo: z.string().max(8_192).optional() }))
  .handler(async ({ data }) => {
    throw redirect({ href: await createGoogleAuthorizationUrl(data.returnTo) })
  })

export const finishGoogleOAuth = createServerFn({ method: 'GET' })
  .validator(z.object({ code: z.string().min(1), state: z.string().min(1) }))
  .handler(async ({ data }) => {
    const returnTo = await completeGoogleOAuth(data.code, data.state)
    return { ok: true, returnTo }
  })

export const logout = createServerFn({ method: 'POST' }).handler(async () => {
  clearSessionCookie()
  return { ok: true }
})
