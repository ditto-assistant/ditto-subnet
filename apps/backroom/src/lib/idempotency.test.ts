import { describe, expect, it } from 'vitest'
import { deriveRequestId } from './idempotency'

const AGENT = '90cb5697-cbc1-40f4-a27e-439a7986a054'
const SNAPSHOT = 'ab'.repeat(32)

describe('deriveRequestId', () => {
  it('is a well-formed v4 UUID', async () => {
    const id = await deriveRequestId('validation-retry', [
      AGENT,
      'operator@example.com',
      'Verified validator OOM',
      SNAPSHOT,
    ])
    expect(id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
  })

  it('repeats for the same action so a re-sent request stays idempotent', async () => {
    const parts = [AGENT, 'operator@example.com', 'Verified validator OOM', SNAPSHOT]
    await expect(deriveRequestId('validation-retry', parts)).resolves.toBe(
      await deriveRequestId('validation-retry', parts),
    )
  })

  it('changes when any field the platform compares on replay changes', async () => {
    const base = await deriveRequestId('validation-retry', [
      AGENT,
      'operator@example.com',
      'Verified validator OOM',
      SNAPSHOT,
    ])
    const others = await Promise.all([
      deriveRequestId('validation-retry', [
        '11111111-1111-4111-8111-111111111111',
        'operator@example.com',
        'Verified validator OOM',
        SNAPSHOT,
      ]),
      deriveRequestId('validation-retry', [
        AGENT,
        'someone-else@example.com',
        'Verified validator OOM',
        SNAPSHOT,
      ]),
      deriveRequestId('validation-retry', [
        AGENT,
        'operator@example.com',
        'Verified validator disk exhaustion',
        SNAPSHOT,
      ]),
      // A moved snapshot is a different request, which is what keeps a stale
      // replay from reusing a key the platform would answer idempotently.
      deriveRequestId('validation-retry', [
        AGENT,
        'operator@example.com',
        'Verified validator OOM',
        'cd'.repeat(32),
      ]),
    ])
    expect(new Set([base, ...others]).size).toBe(5)
  })

  it('separates different operations on the same inputs', async () => {
    const parts = [AGENT, 'operator@example.com', 'Verified validator OOM', SNAPSHOT]
    await expect(deriveRequestId('validation-retry', parts)).resolves.not.toBe(
      await deriveRequestId('validation-withdraw', parts),
    )
  })

  it('cannot be collided by punctuation inside a reason', async () => {
    const first = await deriveRequestId('validation-retry', [
      AGENT,
      'operator@example.com',
      'outage","extra',
      SNAPSHOT,
    ])
    const second = await deriveRequestId('validation-retry', [
      AGENT,
      'operator@example.com',
      'outage',
      'extra',
      SNAPSHOT,
    ])
    expect(first).not.toBe(second)
  })
})
