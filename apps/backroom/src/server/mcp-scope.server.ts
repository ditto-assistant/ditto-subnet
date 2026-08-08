import '@tanstack/react-start/server-only'

import {
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  TOOL_SCOPE_REQUIREMENTS,
} from './mcp.server'

export function insufficientScopeResponse(request: Request, scope: string) {
  const origin = new URL(request.url).origin
  const resourceMetadata = `${origin}/.well-known/oauth-protected-resource/mcp`
  return Response.json(
    {
      error: 'insufficient_scope',
      error_description: `This operation requires ${scope}`,
    },
    {
      status: 403,
      headers: {
        'Cache-Control': 'no-store',
        'WWW-Authenticate': `Bearer error="insufficient_scope", scope="${BACKROOM_READ_SCOPE} ${scope}", resource_metadata="${resourceMetadata}"`,
      },
    },
  )
}

export async function callsWriteTool(request: Request) {
  return (await requiredScopesForRequest(request)).includes(BACKROOM_WRITE_SCOPE)
}

export async function requiredScopesForRequest(request: Request) {
  if (request.method !== 'POST') return []
  try {
    const payload = (await request.clone().json()) as unknown
    const messages = Array.isArray(payload) ? payload : [payload]
    const scopes = messages.flatMap((message) => {
      if (!message || typeof message !== 'object') return []
      const record = message as Record<string, unknown>
      if (record.method !== 'tools/call') return []
      const params = record.params
      if (!params || typeof params !== 'object') return []
      const name = (params as Record<string, unknown>).name
      if (typeof name !== 'string') return []
      const scope = TOOL_SCOPE_REQUIREMENTS.get(name)
      return scope ? [scope] : []
    })
    return [...new Set(scopes)]
  } catch {
    return []
  }
}
