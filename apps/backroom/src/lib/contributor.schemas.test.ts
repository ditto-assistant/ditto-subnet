import { describe, expect, it } from 'vitest'
import { claPublishInputSchema } from './contributor.schemas'

const publication = {
  version: 1, sha256: 'a'.repeat(64), effectiveAt: '2026-10-13T12:00:00Z',
  reason: 'Validate the test fixture document byte boundary',
  confirmation: `PUBLISH CLA 1 ${'a'.repeat(64)}`,
}

describe('CLA publication UTF-8 byte limit', () => {
  it('rejects multibyte text within the character limit but beyond the byte limit', () => {
    expect(claPublishInputSchema.safeParse({ ...publication, text: '—'.repeat(200_000) }).success).toBe(false)
  })

  it('accepts documents within the exact byte boundary', () => {
    expect(claPublishInputSchema.safeParse({ ...publication, text: 'a'.repeat(262_144) }).success).toBe(true)
    expect(claPublishInputSchema.safeParse({ ...publication, text: '—'.repeat(87_381) }).success).toBe(true)
    expect(claPublishInputSchema.safeParse({ ...publication, text: '—'.repeat(87_382) }).success).toBe(false)
  })
})
