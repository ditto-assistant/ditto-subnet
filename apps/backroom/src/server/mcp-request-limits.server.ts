export const MAX_MCP_BODY_BYTES = 1024 * 1024
const MAX_BUFFERED_CHUNKS = 1024

// Scope and observer checks parse the body before the SDK sees it. Bound that
// first read as well as the transport, including requests without Content-Length.
export async function boundMcpRequest(request: Request, limit = MAX_MCP_BODY_BYTES): Promise<Request | Response> {
  if (!request.body) return request
  if (request.method === 'GET' || request.method === 'HEAD') {
    return Response.json({ error: 'MCP request method must not include a body' }, {
      status: 400, headers: { 'Cache-Control': 'no-store' },
    })
  }
  const reader = request.body.getReader()
  const chunks: Uint8Array[] = []
  let size = 0
  let coalesced: Uint8Array | undefined
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > limit) {
        await reader.cancel().catch(() => {})
        return Response.json({ error: 'MCP request body is too large' }, {
          status: 413, headers: { 'Cache-Control': 'no-store' },
        })
      }
      if (coalesced) {
        if (size > coalesced.byteLength) {
          const grown = new Uint8Array(Math.min(limit, Math.max(size, coalesced.byteLength * 2)))
          grown.set(coalesced)
          coalesced = grown
        }
        coalesced.set(value, size - value.byteLength)
      } else {
        chunks.push(value)
        if (chunks.length >= MAX_BUFFERED_CHUNKS) {
          // Bound per-chunk overhead for streams fragmented into tiny pieces.
          coalesced = new Uint8Array(Math.min(limit, Math.max(size, 1024)))
          let offset = 0
          for (const chunk of chunks) {
            coalesced.set(chunk, offset)
            offset += chunk.byteLength
          }
          chunks.length = 0
        }
      }
    }
  } catch {
    return Response.json({ error: 'Could not read MCP request body' }, {
      status: 400, headers: { 'Cache-Control': 'no-store' },
    })
  } finally {
    reader.releaseLock()
  }
  if (coalesced) chunks.push(coalesced.slice(0, size))
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    },
  })
  return new Request(request, { body, duplex: 'half' } as RequestInit)
}
