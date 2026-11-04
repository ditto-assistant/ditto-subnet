import { describe, expect, it } from 'vitest'
import { boundMcpRequest } from './mcp-request-limits.server'
import { observerRequestAllowed } from './treasury-observer-access.server'

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
  it('replays empty and highly fragmented bodies without changing bytes', async () => {
    const empty = await boundMcpRequest(request(['']))
    expect(empty).toBeInstanceOf(Request)
    expect(await (empty as Request).text()).toBe('')

    const text = 'x'.repeat(1100)
    const bounded = await boundMcpRequest(request(Array.from(text)), text.length)
    expect(bounded).toBeInstanceOf(Request)
    expect(await (bounded as Request).clone().text()).toBe(text)
    expect(await (bounded as Request).text()).toBe(text)

    const belowLimit = await boundMcpRequest(request(Array.from(text)))
    expect(belowLimit).toBeInstanceOf(Request)
    const first = await (belowLimit as Request).body!.getReader().read()
    expect(first.value?.byteLength).toBe(text.length)
    expect(first.value?.buffer.byteLength).toBe(text.length)
  })
  it('preserves UTF-8 split across chunks for both readers', async () => {
    const bytes = new TextEncoder().encode('{"s":"é"}')
    const incoming = new Request('https://backroom.example/mcp', {
      method: 'POST',
      body: new ReadableStream({ start(controller) {
        controller.enqueue(bytes.subarray(0, 7))
        controller.enqueue(bytes.subarray(7, 8))
        controller.enqueue(bytes.subarray(8))
        controller.close()
      } }),
      duplex: 'half',
    } as RequestInit)
    const bounded = await boundMcpRequest(incoming, bytes.byteLength)
    expect(bounded).toBeInstanceOf(Request)
    expect(await (bounded as Request).clone().json()).toEqual({ s: 'é' })
    expect(await (bounded as Request).text()).toBe('{"s":"é"}')
  })
  it('does not retain a large backing buffer for a small chunk', async () => {
    const backing = new Uint8Array(1024 * 1024)
    backing[0] = 120
    const incoming = new Request('https://backroom.example/mcp', {
      method: 'POST', body: new ReadableStream({ start(controller) {
        controller.enqueue(backing.subarray(0, 1))
        controller.close()
      } }), duplex: 'half',
    } as RequestInit)
    const bounded = await boundMcpRequest(incoming)
    const first = await (bounded as Request).body!.getReader().read()
    expect(first.value?.buffer.byteLength).toBe(1)
    expect(first.value?.[0]).toBe(120)
  })
  it('cancels a stream that repeatedly produces no bytes', async () => {
    let cancelled = false
    const incoming = new Request('https://backroom.example/mcp', {
      method: 'POST', body: new ReadableStream({
        pull(controller) { controller.enqueue(new Uint8Array()) },
        cancel() { cancelled = true },
      }), duplex: 'half',
    } as RequestInit)
    const response = await boundMcpRequest(incoming)
    expect((response as Response).status).toBe(400)
    expect(cancelled).toBe(true)
  })
  it('keeps the original body readable after an observer clone rejects it', async () => {
    const body = '{"jsonrpc":"2.0","id":1,"method":"resources/list"}'
    const bounded = await boundMcpRequest(request([body]))
    expect(bounded).toBeInstanceOf(Request)
    expect(await observerRequestAllowed(bounded as Request)).toBe(false)
    expect(await (bounded as Request).text()).toBe(body)
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
  it.each(['GET', 'HEAD'])('rejects an incoming %s body before reading it', async (method) => {
    const incoming = request(['body'])
    // Fetch constructors reject these bodies; incoming Workers may expose one.
    Object.defineProperty(incoming, 'method', { value: method })
    const response = await boundMcpRequest(incoming)
    expect((response as Response).status).toBe(400)
    expect(response.headers.get('Cache-Control')).toBe('no-store')
    expect(incoming.bodyUsed).toBe(false)
  })
  it('returns a private client error when the request stream fails', async () => {
    const failed = new Request('https://backroom.example/mcp', {
      method: 'POST',
      body: new ReadableStream({ start(controller) { controller.error(new Error('fixture read failure')) } }),
      duplex: 'half',
    } as RequestInit)
    const response = await boundMcpRequest(failed)
    expect((response as Response).status).toBe(400)
    expect(response.headers.get('Cache-Control')).toBe('no-store')
    expect(await response.text()).not.toContain('fixture read failure')
  })
})
