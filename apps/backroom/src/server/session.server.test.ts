import { afterEach, describe, expect, it } from 'vitest'
import { previewSessionFromEnv } from './session.server'

const original = {
  mode: process.env.BACKROOM_PREVIEW_MODE,
  base: process.env.DITTO_PLATFORM_API_BASE_URL,
  token: process.env.DITTO_ADMIN_API_TOKEN,
}

afterEach(() => {
  if (original.mode === undefined) delete process.env.BACKROOM_PREVIEW_MODE
  else process.env.BACKROOM_PREVIEW_MODE = original.mode
  if (original.base === undefined) delete process.env.DITTO_PLATFORM_API_BASE_URL
  else process.env.DITTO_PLATFORM_API_BASE_URL = original.base
  if (original.token === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = original.token
})

describe('preview session boundary', () => {
  it('is disabled by default', () => {
    delete process.env.BACKROOM_PREVIEW_MODE
    expect(previewSessionFromEnv(1)).toBeNull()
  })

  it('creates a write session only for the isolated Platform address', () => {
    process.env.BACKROOM_PREVIEW_MODE = 'true'
    process.env.DITTO_PLATFORM_API_BASE_URL = 'http://platform:8000'
    process.env.DITTO_ADMIN_API_TOKEN = 'a'.repeat(32)
    expect(previewSessionFromEnv(1)).toMatchObject({
      uid: 'sn118-preview',
      email: 'preview@localhost.invalid',
      accessLevel: 'write',
      issuedAt: 1,
    })
  })

  it('refuses production or weak-token configurations', () => {
    process.env.BACKROOM_PREVIEW_MODE = 'true'
    process.env.DITTO_ADMIN_API_TOKEN = 'a'.repeat(32)
    process.env.DITTO_PLATFORM_API_BASE_URL = 'https://platform-api.heyditto.ai'
    expect(() => previewSessionFromEnv()).toThrow(/isolated preview Platform URL/)
    process.env.DITTO_PLATFORM_API_BASE_URL = 'http://platform:8000'
    process.env.DITTO_ADMIN_API_TOKEN = 'short'
    expect(() => previewSessionFromEnv()).toThrow(/isolated admin token/)
  })
})
