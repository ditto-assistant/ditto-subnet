import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { describe, expect, it } from 'vitest'
import type { BackroomSession } from '../lib/auth.types'
import { BACKROOM_READ_SCOPE, createBackroomMcpServer } from './mcp.server'

const session: BackroomSession = {
  version: 2,
  uid: 'pagination-test',
  email: 'pagination@omniaura.ai',
  name: 'Pagination test',
  picture: '',
  accessLevel: 'read',
  issuedAt: Date.now(),
  expiresAt: Date.now() + 60_000,
}

type NumberSchema = {
  type?: string
  minimum?: number
  maximum?: number
  default?: number
}

describe('Backroom MCP collection pagination', () => {
  it('publishes bounded limit/offset defaults for every top-level collection tool', async () => {
    const server = createBackroomMcpServer({
      session,
      scopes: [BACKROOM_READ_SCOPE],
      clientName: 'Pagination test',
    })
    const client = new Client({ name: 'pagination-test', version: '1.0.0' })
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
    await Promise.all([server.connect(serverTransport), client.connect(clientTransport)])

    try {
      const { tools } = await client.listTools()
      const aliases = new Set(['get_screening_review_queue', 'get_leaderboard'])
      const collections = tools.filter(
        ({ name }) => name.startsWith('list_') || aliases.has(name),
      )

      expect(collections.length).toBeGreaterThan(aliases.size)
      expect(collections.map(({ name }) => name)).toEqual(
        expect.arrayContaining([...aliases]),
      )
      for (const tool of collections) {
        const properties = tool.inputSchema.properties as
          | Record<string, NumberSchema>
          | undefined
        const limit = properties?.limit
        const offset = properties?.offset

        expect.soft(limit, `${tool.name}.limit`).toMatchObject({
          type: 'integer',
          minimum: 1,
          maximum: expect.any(Number),
          default: expect.any(Number),
        })
        expect.soft(limit?.default, `${tool.name}.limit default`).toBeGreaterThanOrEqual(1)
        expect.soft(limit?.default, `${tool.name}.limit default`).toBeLessThanOrEqual(
          limit?.maximum ?? -1,
        )
        expect.soft(offset, `${tool.name}.offset`).toMatchObject({
          type: 'integer',
          minimum: 0,
          default: 0,
        })
      }
    } finally {
      await client.close()
      await server.close()
    }
  })
})
