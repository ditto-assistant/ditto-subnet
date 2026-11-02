import { describe, expect, it } from 'vitest'
import { boundMcpRequest } from './mcp-request-limits.server'

function request(chunks: string[]) {
  const encoder = new TextEncoder()
  return new Request('https://backroom.example/mcp', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer fixture' },
    body: new ReadableStream({ start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    } }),
    duplex: 'half',
  } as RequestInit)
}

describe('bounded MCP pre-parser', () => {
  it('preserves request headers and bytes for scope and transport readers', async () => {
    const bounded = await boundMcpRequest(request(['{"ok":', 'true}']), 11)
    expect(bounded).toBeInstanceOf(Request)
    expect(bounded.headers.get('Authorization')).toBe('Bearer fixture')
    expect(await bounded.clone().json()).toEqual({ ok: true })
    expect(await bounded.text()).toBe('{"ok":true}')
  })
  it('bounds observed UTF-8 bytes without a Content-Length header', async () => {
    const bounded = await boundMcpRequest(request(['{"s":"', 'éé', '"}']), 10)
    expect(bounded).toBeInstanceOf(Response)
    expect((bounded as Response).status).toBe(413)
    expect(bounded.headers.get('Cache-Control')).toBe('no-store')
  })
  it('preserves bodyless transport requests', async () => {
    const original = new Request('https://backroom.example/mcp')
    expect(await boundMcpRequest(original)).toBe(original)
  })
})
