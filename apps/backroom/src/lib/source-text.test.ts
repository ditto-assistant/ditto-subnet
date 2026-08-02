import { describe, expect, it } from 'vitest'
import { sanitizeSourceLine } from './source-text'

describe('sanitizeSourceLine', () => {
  it('renders bidi override and isolate controls as visible escapes', () => {
    expect(sanitizeSourceLine('if (isAdmin‮) {}')).toBe('if (isAdmin\\u202E) {}')
    expect(sanitizeSourceLine('⁦hidden⁩')).toBe('\\u2066hidden\\u2069')
  })

  it('renders zero-width characters visibly', () => {
    expect(sanitizeSourceLine('a​b﻿c')).toBe('a\\u200Bb\\uFEFFc')
  })

  it('leaves ordinary source untouched, including non-ASCII comments', () => {
    const line = '    let total = 0; // résumé ✓'
    expect(sanitizeSourceLine(line)).toBe(line)
  })
})
