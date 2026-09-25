import '@tanstack/react-start/server-only'

import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import { WorkerEntrypoint } from 'cloudflare:workers'
import { accessLevelForEmail } from '../lib/auth.policy'
import {
  BACKROOM_CHALLENGE_SCOPE,
  BACKROOM_READ_SCOPE,
  createBackroomMcpServer,
  type BackroomEnv,
  type McpGrantProps,
} from './mcp.server'
import {
  insufficientScopeResponse,
  requiredScopesForRequest,
} from './mcp-scope.server'

function hasReadAccess(props: McpGrantProps) {
  return props.scopes.includes(BACKROOM_READ_SCOPE)
}

/**
 * The authorizing staff session is re-checked on every MCP request, exactly as
 * the live `BACKROOM_ADMIN_EMAILS` level is. Token TTLs are clamped to the
 * session at issuance, but expiry is enforced here too so no rounding, clock
 * skew, or future issuance path can let a token outlive its session.
 */
export function expiredSessionResponse(request: Request) {
  const origin = new URL(request.url).origin
  const resourceMetadata = `${origin}/.well-known/oauth-protected-resource/mcp`
  return Response.json(
    {
      error: 'invalid_token',
      error_description: 'The Backroom staff session expired; authorize again',
    },
    {
      status: 401,
      headers: {
        'Cache-Control': 'no-store',
        'WWW-Authenticate': `Bearer error="invalid_token", error_description="The Backroom staff session expired", scope="${BACKROOM_CHALLENGE_SCOPE}", resource_metadata="${resourceMetadata}"`,
      },
    },
  )
}

export class BackroomMcpHandler extends WorkerEntrypoint<
  BackroomEnv,
  McpGrantProps
> {
  async fetch(request: Request) {
    const grantProps = this.ctx.props
    if (!grantProps?.session || grantProps.session.expiresAt <= Date.now()) {
      return expiredSessionResponse(request)
    }
    let props: McpGrantProps
    try {
      // The live binding, not the level sealed into the grant, decides what
      // this connection may exercise: removing an address from
      // BACKROOM_ADMIN_EMAILS drops artifact and write access on the next
      // request (the tools' own level checks refuse them), and
      // get_backroom_access reports that effective level.
      const accessLevel = accessLevelForEmail(
        grantProps.session.email,
        this.env.BACKROOM_ADMIN_EMAILS,
        this.env.BACKROOM_BLOCKED_EMAILS,
      )
      props = { ...grantProps, session: { ...grantProps.session, accessLevel } }
    } catch {
      return Response.json(
        { error: 'access_denied', error_description: 'This account is not authorized' },
        { status: 403, headers: { 'Cache-Control': 'no-store' } },
      )
    }
    if (!hasReadAccess(props)) {
      return insufficientScopeResponse(request, BACKROOM_READ_SCOPE)
    }
    const requiredScopes = await requiredScopesForRequest(request)
    for (const scope of requiredScopes) {
      if (!props.scopes.includes(scope)) {
        return insufficientScopeResponse(request, scope)
      }
    }

    const server = createBackroomMcpServer(props)
    const transport = new WebStandardStreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    })
    await server.connect(transport)
    return transport.handleRequest(request)
  }
}
