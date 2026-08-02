import { createMiddleware, createStart } from '@tanstack/react-start'
import { getRequest, setResponseHeader } from '@tanstack/react-start/server'

const csrfMiddleware = createMiddleware().server(async ({ next }) => {
  const request = getRequest()
  if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method)) {
    const origin = request.headers.get('origin')
    const expectedOrigin = new URL(request.url).origin
    let originMatches = false
    try {
      originMatches = Boolean(origin && new URL(origin).origin === expectedOrigin)
    } catch {
      originMatches = false
    }
    if (!originMatches) {
      throw new Response('Origin check failed', { status: 403 })
    }
  }

  setResponseHeader('X-Content-Type-Options', 'nosniff')
  setResponseHeader('X-Frame-Options', 'DENY')
  setResponseHeader('Referrer-Policy', 'no-referrer')
  setResponseHeader(
    'Permissions-Policy',
    'camera=(), microphone=(), geolocation=(), payment=()',
  )
  if (new URL(request.url).protocol === 'https:') {
    setResponseHeader(
      'Content-Security-Policy',
      "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' https://lh3.googleusercontent.com data:; connect-src 'self'",
    )
    setResponseHeader(
      'Strict-Transport-Security',
      'max-age=31536000; includeSubDomains',
    )
  }
  return next()
})

export const startInstance = createStart(() => ({
  requestMiddleware: [csrfMiddleware],
}))
