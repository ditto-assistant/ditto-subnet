import { describe, expect, it } from 'vitest'
import { claPublishInputSchema, githubAccountInputSchema, githubLinkStatusSchema } from './contributor.schemas'

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

describe('GitHub account ids', () => {
  const link = {
    link_id: '00000000-0000-4000-8000-000000000001', netuid: 118, hotkey: 'hotkey', coldkey: 'coldkey',
    github_user_id: 100, github_login: 'miner-fixture', link_attempt_id: '00000000-0000-4000-8000-000000000002',
    key_kind: 'coldkey', signer: 'coldkey', signature: 'a'.repeat(128), nonce: '00000000-0000-4000-8000-000000000003',
    issued_at: '2026-10-13T12:00:00Z', linked_at: '2026-10-13T12:00:00Z', revoked_at: null,
    owner_block: 1, owner_block_hash: '0x' + 'ab'.repeat(32),
  }
  // Parse the response text as Backroom does, so an unsafe int64 id arrives rounded.
  const status = (id: string) => githubLinkStatusSchema.safeParse(JSON.parse(
    JSON.stringify({ netuid: 118, enabled: true, link }).replace('"github_user_id":100', `"github_user_id":${id}`),
  ))

  it('accepts every id a JS number represents exactly', () => {
    expect(status('100').success).toBe(true)
    expect(status(String(Number.MAX_SAFE_INTEGER)).success).toBe(true)
  })

  it('rejects ids that JSON parsing would round, in responses and inputs alike', () => {
    for (const id of ['9007199254740993', '9223372036854775807']) {
      expect(status(id).success).toBe(false)
    }
    expect(githubAccountInputSchema.safeParse({ githubUserId: 2 ** 53 }).success).toBe(false)
    expect(githubAccountInputSchema.safeParse({ githubUserId: Number.MAX_SAFE_INTEGER }).success).toBe(true)
  })
})
