import '@tanstack/react-start/server-only'

import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import { WorkerEntrypoint } from 'cloudflare:workers'
import {
  createBackroomMcpServer,
  type BackroomEnv,
  type McpGrantProps,
} from './mcp.server'
import { currentMcpGrant } from './mcp-scope.server'

export class BackroomMcpHandler extends WorkerEntrypoint<
  BackroomEnv,
  McpGrantProps
> {
  async fetch(request: Request) {
    const props = await currentMcpGrant(request, this.ctx.props, this.env)
    if (props instanceof Response) return props

    const server = createBackroomMcpServer(props)
    const transport = new WebStandardStreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    })
    await server.connect(transport)
    return transport.handleRequest(request)
  }
}
