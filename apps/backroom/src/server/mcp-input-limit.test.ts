import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import type { BackroomSession } from '../lib/auth.types'
import { BACKROOM_READ_SCOPE, createBackroomMcpServer } from './mcp.server'
import { BACKROOM_TREASURY_OBSERVE_SCOPE } from './treasury-observer-access.server'
import { createTreasuryObserverServer } from './treasury-observer-mcp.server'

const session: BackroomSession = {
  version: 2,
  uid: 'fixture',
  email: 'preview@localhost.invalid',
  name: 'Fixture',
  picture: '',
  accessLevel: 'write',
  issuedAt: Date.now(),
  expiresAt: Date.now() + 60_000,
}

describe.each([
  ['ordinary', () => createBackroomMcpServer({ session, scopes: [BACKROOM_READ_SCOPE], clientName: 'fixture' })],
  ['observer', () => createTreasuryObserverServer({ session, scopes: [BACKROOM_TREASURY_OBSERVE_SCOPE], clientName: 'fixture' })],
])('%s MCP input limit', (_name, createServer) => {
  it('accepts exactly 10,000 combined members and rejects 10,001', async () => {
    const server = createServer()
    const handler = vi.fn(async () => ({ content: [{ type: 'text' as const, text: 'accepted' }] }))
    server.registerTool('input_limit_fixture', { inputSchema: { values: z.array(z.number()) } }, handler)
    const client = new Client({ name: 'fixture', version: '1.0.0' })
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
    try {
      await Promise.all([server.connect(serverTransport), client.connect(clientTransport)])
      const atLimit = await client.callTool({ name: 'input_limit_fixture', arguments: { values: Array(9_999).fill(0) } })
      expect(atLimit.isError).not.toBe(true)
      expect(handler).toHaveBeenCalledTimes(1)
      const overLimit = await client.callTool({ name: 'input_limit_fixture', arguments: { values: Array(10_000).fill(0) } })
      expect(overLimit.isError).toBe(true)
      expect(JSON.stringify(overLimit)).toContain('maximum of 10000 elements')
      expect(handler).toHaveBeenCalledTimes(1)
    } finally {
      await client.close()
      await server.close()
    }
  })
})
