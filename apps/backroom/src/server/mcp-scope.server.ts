import '@tanstack/react-start/server-only'

import { accessLevelForEmail } from '../lib/auth.policy'
import {
  BACKROOM_ARTIFACT_SCOPE,
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  TOOL_SCOPE_REQUIREMENTS,
  type BackroomEnv,
  type McpGrantProps,
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

/** OAuth grant scopes are a ceiling; the live staff allowlist is the authority. */
export async function currentMcpGrant(
  request: Request,
  props: McpGrantProps,
  env: Pick<BackroomEnv, 'BACKROOM_ADMIN_EMAILS' | 'BACKROOM_BLOCKED_EMAILS'>,
): Promise<McpGrantProps | Response> {
  // An OAuth grant minted near staff-session expiry can outlive the session
  // briefly. Never let its cached scopes extend the underlying identity.
  if (props.session.expiresAt <= Date.now()) {
    return Response.json(
      { error: 'access_denied', error_description: 'This session has expired' },
      { status: 403, headers: { 'Cache-Control': 'no-store' } },
    )
  }
  let accessLevel: McpGrantProps['session']['accessLevel']
  try {
    accessLevel = accessLevelForEmail(
      props.session.email,
      env.BACKROOM_ADMIN_EMAILS,
      env.BACKROOM_BLOCKED_EMAILS,
    )
  } catch {
    return Response.json(
      { error: 'access_denied', error_description: 'This account is not authorized' },
      { status: 403, headers: { 'Cache-Control': 'no-store' } },
    )
  }
  if (!props.scopes.includes(BACKROOM_READ_SCOPE)) {
    return insufficientScopeResponse(request, BACKROOM_READ_SCOPE)
  }
  for (const scope of await requiredScopesForRequest(request)) {
    if (
      !props.scopes.includes(scope) ||
      (accessLevel !== 'write' &&
        (scope === BACKROOM_WRITE_SCOPE || scope === BACKROOM_ARTIFACT_SCOPE))
    ) {
      return insufficientScopeResponse(request, scope)
    }
  }
  return { ...props, session: { ...props.session, accessLevel } }
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
