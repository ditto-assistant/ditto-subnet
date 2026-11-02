import { describe, expect, it } from 'vitest'
import { expiredSessionResponse } from './mcp-handler.server'
import { BACKROOM_CHALLENGE_SCOPE } from './mcp.server'

describe('MCP 401 challenge scope', () => {
  it('advertises every Backroom scope so consent can offer every level', () => {
    expect(BACKROOM_CHALLENGE_SCOPE).toBe(
      'backroom:read backroom:artifact:read backroom:write',
    )
    const response = expiredSessionResponse(new Request('https://backroom.test/mcp'))
    expect(response.headers.get('WWW-Authenticate')).toContain(
      'scope="backroom:read backroom:artifact:read backroom:write"',
    )
  })
})
