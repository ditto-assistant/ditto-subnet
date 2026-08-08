import '@tanstack/react-start/server-only'

import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import { WorkerEntrypoint } from 'cloudflare:workers'
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
