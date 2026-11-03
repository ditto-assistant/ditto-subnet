export const MAX_MCP_BODY_BYTES = 1024 * 1024

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
      chunks.push(value)
    }
  } catch {
    return Response.json({ error: 'Could not read MCP request body' }, {
      status: 400, headers: { 'Cache-Control': 'no-store' },
    })
  } finally {
    reader.releaseLock()
  }
  const body = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) {
    body.set(chunk, offset)
    offset += chunk.byteLength
  }
  return new Request(request, { body })
}
