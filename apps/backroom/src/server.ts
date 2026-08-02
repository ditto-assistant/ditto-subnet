import handler from '@tanstack/react-start/server-entry'

function applySecurityHeaders(response: Response, request: Request) {
  const secured = new Response(response.body, response)
  secured.headers.set('X-Content-Type-Options', 'nosniff')
  secured.headers.set('Referrer-Policy', 'no-referrer')
  if (new URL(request.url).protocol === 'https:') {
    secured.headers.set(
      'Strict-Transport-Security',
      'max-age=31536000; includeSubDomains',
    )
  }
  return secured
}

export default {
  async fetch(request: Request) {
    return applySecurityHeaders(await handler.fetch(request), request)
  },
} satisfies ExportedHandler
