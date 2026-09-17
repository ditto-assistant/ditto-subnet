import '@tanstack/react-start/server-only'

import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import { WorkerEntrypoint } from 'cloudflare:workers'
import { accessLevelForEmail } from '../lib/auth.policy'
import {
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

export class BackroomMcpHandler extends WorkerEntrypoint<
  BackroomEnv,
  McpGrantProps
> {
  async fetch(request: Request) {
    const props = this.ctx.props
    try {
      accessLevelForEmail(
        props.session.email,
        this.env.BACKROOM_ADMIN_EMAILS,
        this.env.BACKROOM_BLOCKED_EMAILS,
      )
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
