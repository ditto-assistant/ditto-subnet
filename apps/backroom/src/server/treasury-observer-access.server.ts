import '@tanstack/react-start/server-only'

export const BACKROOM_TREASURY_OBSERVE_SCOPE = 'backroom:treasury:observe'
export const OBSERVER_TOOL_NAMES = new Set(['get_treasury_settings', 'record_treasury_receipt'])

/** Dedicated grants must never inherit the ordinary read/write floors. */
export function observerGrant(scopes: Array<string>) {
  if (!scopes.includes(BACKROOM_TREASURY_OBSERVE_SCOPE)) return false
  if (new Set(scopes).size !== 1) throw new Error('Observer scope cannot be mixed with other scopes')
  return true
}

/** Bound and reject direct protocol escape routes before SDK dispatch. */
export async function observerRequestAllowed(request: Request) {
  if (request.method !== 'POST') return false
  const body = request.clone().body
  if (!body) return false
  const reader = body.getReader()
  const chunks: Array<Uint8Array> = []
  let size = 0
  try {
    while (true) {
      const next = await reader.read()
      if (next.done) break
      size += next.value.length
      if (size > 65_536) return false
      chunks.push(next.value)
    }
    const bytes = new Uint8Array(size)
    let offset = 0
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length }
    const parsed: unknown = JSON.parse(new TextDecoder().decode(bytes))
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') return false
    const message = parsed as Record<string, unknown>
    if (message.jsonrpc !== '2.0') return false
    if (['initialize', 'notifications/initialized', 'ping', 'tools/list'].includes(String(message.method))) return true
    if (message.method !== 'tools/call' || !message.params || typeof message.params !== 'object') return false
    const name = (message.params as Record<string, unknown>).name
    return typeof name === 'string' && OBSERVER_TOOL_NAMES.has(name)
  } catch { return false } finally {
    // A cloned request tees the original body. Awaiting cancel before SDK
    // dispatch would wait forever for its unread peer on an oversized body.
    void reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
