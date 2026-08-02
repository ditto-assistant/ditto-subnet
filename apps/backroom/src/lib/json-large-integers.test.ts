import { describe, expect, it } from 'vitest'

import {
  parseJsonPreservingLargeIntegers,
  quoteLargeIntegerLiterals,
} from './json-large-integers'

// Verbatim from production on 2026-07-25:
// GET https://platform-api.heyditto.ai/api/v1/public/agent/454a09ad-d2b1-48c8-8db3-fe11e4999f63/scores
const PRODUCTION_SEEDS = [
  '989366151180340909',
  '6211616870656561578',
  '8713514997902241464',
  '8811366100733494301',
]

describe('parseJsonPreservingLargeIntegers', () => {
  it('keeps every real production seed byte-exact', () => {
    for (const seed of PRODUCTION_SEEDS) {
      const parsed = parseJsonPreservingLargeIntegers(`{"seed":${seed}}`) as {
        seed: string
      }
      expect(parsed.seed).toBe(seed)
      // Establishes the bug this exists to prevent, using the same input.
      expect(String((JSON.parse(`{"seed":${seed}}`) as { seed: number }).seed)).not.toBe(
        seed,
      )
    }
  })

  it('round-trips a seed through JSON.stringify unchanged', () => {
    const seed = PRODUCTION_SEEDS[0]!
    const parsed = parseJsonPreservingLargeIntegers(`{"dataset_seed":${seed}}`)
    expect(JSON.stringify(parsed)).toBe(`{"dataset_seed":"${seed}"}`)
  })

  it('leaves representable numbers as numbers so existing schemas keep validating', () => {
    const parsed = parseJsonPreservingLargeIntegers(
      '{"dataset_seed_block":8691487,"bench_version":7,"composite":0.969703,' +
        '"stderr":1.1479e-2,"negative":-42,"zero":0,"boundary":9007199254740991}',
    ) as Record<string, unknown>
    expect(parsed).toEqual({
      dataset_seed_block: 8691487,
      bench_version: 7,
      composite: 0.969703,
      stderr: 0.011479,
      negative: -42,
      zero: 0,
      boundary: 9007199254740991,
    })
  })

  it('rewrites at the exact safe-integer boundary and not before it', () => {
    expect(quoteLargeIntegerLiterals('{"n":9007199254740991}')).toBe(
      '{"n":9007199254740991}',
    )
    expect(quoteLargeIntegerLiterals('{"n":9007199254740992}')).toBe(
      '{"n":"9007199254740992"}',
    )
    expect(quoteLargeIntegerLiterals('{"n":-9007199254740992}')).toBe(
      '{"n":"-9007199254740992"}',
    )
  })

  it('handles unsigned 64-bit seeds above the signed range', () => {
    const parsed = parseJsonPreservingLargeIntegers('{"seed":18446744073709551615}') as {
      seed: string
    }
    expect(parsed.seed).toBe('18446744073709551615')
  })

  it('never rewrites digits inside string values', () => {
    const text =
      '{"transcript_sha256":"9007199254740992","reason":"seed 989366151180340909 ' +
      'and an escaped quote \\" 12345678901234567890","agent_name":"lihai-99999999999999999999"}'
    expect(quoteLargeIntegerLiterals(text)).toBe(text)
    const parsed = parseJsonPreservingLargeIntegers(text) as Record<string, string>
    expect(parsed.transcript_sha256).toBe('9007199254740992')
    expect(parsed.agent_name).toBe('lihai-99999999999999999999')
  })

  it('rewrites large integers nested in arrays and objects', () => {
    const parsed = parseJsonPreservingLargeIntegers(
      `{"versions":[{"seeds":[${PRODUCTION_SEEDS[1]},${PRODUCTION_SEEDS[2]}]}]}`,
    ) as { versions: Array<{ seeds: Array<string> }> }
    expect(parsed.versions[0]?.seeds).toEqual([PRODUCTION_SEEDS[1], PRODUCTION_SEEDS[2]])
  })

  it('leaves floats and exponents alone even when they are enormous', () => {
    // Inherently approximate, and no seed is ever written this way.
    const parsed = parseJsonPreservingLargeIntegers(
      '{"a":1.0e30,"b":98936615118034090.9}',
    ) as Record<string, number>
    expect(typeof parsed.a).toBe('number')
    expect(typeof parsed.b).toBe('number')
  })

  it('preserves the document byte-for-byte when nothing needs rewriting', () => {
    const text = '{"entries":[{"rank":1,"composite":0.969703}],"emissions":null}'
    expect(quoteLargeIntegerLiterals(text)).toBe(text)
  })

  it('still throws on malformed JSON so callers keep their error path', () => {
    expect(() => parseJsonPreservingLargeIntegers('not json')).toThrow()
  })
})
