import { describe, expect, it, vi } from 'vitest'
import { BackroomMcpHandler } from './mcp-handler.server'
import {
  BACKROOM_READ_SCOPE,
  type BackroomEnv,
  type McpGrantProps,
} from './mcp-contract.server'
import { BACKROOM_TREASURY_OBSERVE_SCOPE } from './treasury-observer-access.server'

const loaded = vi.hoisted(() => ({ registry: vi.fn(), transport: vi.fn() }))
vi.mock('./mcp.server', () => {
  loaded.registry()
  return { createBackroomMcpServer: () => ({ connect: async () => {} }) }
})
vi.mock('@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js', () => {
  loaded.transport()
  return {
    WebStandardStreamableHTTPServerTransport: class {
      async handleRequest() {
        return new Response('authorized')
      }
    },
  }
})

const props: McpGrantProps = {
  session: {
    version: 2,
    uid: 'staff',
    email: 'staff@omniaura.ai',
    name: 'Staff',
    picture: '',
    accessLevel: 'write',
    issuedAt: Date.now(),
    expiresAt: Date.now() + 3_600_000,
  },
  scopes: [BACKROOM_READ_SCOPE],
  clientName: 'test',
}
const env = { BACKROOM_ADMIN_EMAILS: props.session.email } as BackroomEnv
function handler(grant = props, bindings = env) {
  return new BackroomMcpHandler(
    { props: grant } as unknown as ExecutionContext & { props: McpGrantProps },
    bindings,
  )
}
function request(name: string) {
  return new Request('https://backroom.dittobench.ai/mcp', {
    method: 'POST',
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/call',
      params: { name, arguments: {} },
    }),
  })
}

describe('MCP startup loading', () => {
  it('keeps the registry and transport unloaded until all authorization checks pass', async () => {
    expect(loaded.registry).not.toHaveBeenCalled()
    expect(loaded.transport).not.toHaveBeenCalled()
    const rejected = [
      handler({ ...props, session: { ...props.session, expiresAt: 0 } }),
      handler(props, { ...env, BACKROOM_BLOCKED_EMAILS: props.session.email }),
      handler({ ...props, scopes: [] }),
      handler({ ...props, scopes: [BACKROOM_TREASURY_OBSERVE_SCOPE] }),
      handler(),
    ]
    for (const instance of rejected) {
      const response = await instance.fetch(request('resolve_screening_quarantine'))
      expect([401, 403]).toContain(response.status)
      expect(response.headers.get('Cache-Control')).toBe('no-store')
      expect(loaded.registry).not.toHaveBeenCalled()
      expect(loaded.transport).not.toHaveBeenCalled()
    }
    const authorized = await handler().fetch(request('get_backroom_access'))
    expect(await authorized.text()).toBe('authorized')
    expect(loaded.registry).toHaveBeenCalledOnce()
    expect(loaded.transport).toHaveBeenCalledOnce()
    await handler().fetch(request('get_backroom_access'))
    expect(loaded.registry).toHaveBeenCalledOnce()
    expect(loaded.transport).toHaveBeenCalledOnce()
  })
})
