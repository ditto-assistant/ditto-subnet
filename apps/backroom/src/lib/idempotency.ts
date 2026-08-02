// Idempotency keys for platform operator actions.
//
// The platform admin API requires a `request_id` on every validator recovery,
// queue withdrawal, and score replacement, and stores it as the row's primary
// key. It compares a replay against the stored actor, reason, and expected
// snapshot, and answers `idempotent` when they all match.
//
// That key is not a decision an operator can meaningfully make. Asking a caller
// to invent a UUID per item is pure ceremony, and a random UUID per attempt is
// actively worse than nothing: it makes a re-sent request look brand new, so
// the only thing standing between a duplicated network call and a duplicated
// grant is the optimistic-concurrency snapshot.
//
// Deriving the key from the exact fields the platform compares on replay gives
// the caller idempotency for free. The same operator re-issuing the same action
// against the same state produces the same key and gets the platform's
// `idempotent` answer; a different agent, actor, reason, or snapshot produces a
// different key and is a genuinely different request.

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/**
 * Derive a stable RFC 9562 v4-shaped UUID from the inputs of one operator
 * action. `namespace` separates operations that could otherwise share inputs
 * (a retry and a withdrawal of the same agent by the same operator).
 */
export async function deriveRequestId(
  namespace: string,
  parts: ReadonlyArray<string | number>,
): Promise<string> {
  // JSON encoding keeps the parts unambiguous: no separator can be smuggled in
  // through a reason string to collide two different actions.
  const encoded = new TextEncoder().encode(
    JSON.stringify([namespace, ...parts]),
  )
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', encoded))
  const bytes = digest.slice(0, 16)
  // Version 4 and the RFC variant bits. The platform accepts any UUID version,
  // but a well-formed v4 keeps the value valid everywhere it is echoed.
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const value = hex(bytes)
  return [
    value.slice(0, 8),
    value.slice(8, 12),
    value.slice(12, 16),
    value.slice(16, 20),
    value.slice(20, 32),
  ].join('-')
}
