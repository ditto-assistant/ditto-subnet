import { describe, expect, it } from 'vitest'
import {
  accessLevelForEmail,
  isSameOriginRequest,
  isVerifiedOmniauraEmail,
  parseAdminEmails,
  safeReturnTo,
  SESSION_LIFETIME_MS,
  SESSION_MAX_AGE_SECONDS,
} from './auth.policy'

describe('staff session lifetime', () => {
  it('bounds console and cookie sessions to seven days', () => {
    expect(SESSION_LIFETIME_MS).toBe(7 * 24 * 60 * 60 * 1000)
    expect(SESSION_MAX_AGE_SECONDS).toBe(SESSION_LIFETIME_MS / 1000)
  })
})

describe('isVerifiedOmniauraEmail', () => {
  it('accepts verified OmniAura identities case-insensitively', () => {
    expect(isVerifiedOmniauraEmail(' Peyton@OmniAura.AI ', true)).toBe(true)
  })

  it('rejects unverified identities', () => {
    expect(isVerifiedOmniauraEmail('peyton@omniaura.ai', false)).toBe(false)
  })

  it('rejects lookalike suffixes', () => {
    expect(isVerifiedOmniauraEmail('peyton@omniaura.ai.example.com', true)).toBe(false)
  })
})

describe('Backroom roles', () => {
  it('grants write only to configured OmniAura admins', () => {
    const configured = ' Peyton@OmniAura.AI,invalid@example.com, alan@omniaura.ai '
    expect(parseAdminEmails(configured)).toEqual(
      new Set(['peyton@omniaura.ai', 'alan@omniaura.ai']),
    )
    expect(accessLevelForEmail('peyton@omniaura.ai', configured)).toBe('write')
    expect(accessLevelForEmail('member@omniaura.ai', configured)).toBe('read')
  })

  it('fails closed outside the Workspace domain', () => {
    expect(() => accessLevelForEmail('peyton@example.com', '')).toThrow(
      'This account is not authorized to enter Backroom',
    )
  })
})

describe('isSameOriginRequest', () => {
  it('accepts an exact same-origin request', () => {
    expect(
      isSameOriginRequest(
        new Request('https://backroom.dittobench.ai/_server', {
          headers: { Origin: 'https://backroom.dittobench.ai' },
        }),
      ),
    ).toBe(true)
  })

  it('rejects missing, malformed, and cross-origin headers', () => {
    expect(isSameOriginRequest(new Request('https://backroom.dittobench.ai/_server'))).toBe(false)
    expect(
      isSameOriginRequest(
        new Request('https://backroom.dittobench.ai/_server', {
          headers: { Origin: 'not a valid origin' },
        }),
      ),
    ).toBe(false)
    expect(
      isSameOriginRequest(
        new Request('https://backroom.dittobench.ai/_server', {
          headers: { Origin: 'https://evil.example' },
        }),
      ),
    ).toBe(false)
  })
})

describe('safeReturnTo', () => {
  it('preserves local OAuth consent paths and query strings', () => {
    expect(safeReturnTo('/oauth/consent?request=sealed')).toBe('/oauth/consent?request=sealed')
  })

  it('rejects absolute and protocol-relative redirects', () => {
    expect(safeReturnTo('https://evil.example/callback')).toBe('/screener-capacity')
    expect(safeReturnTo('//evil.example/callback')).toBe('/screener-capacity')
  })
})
