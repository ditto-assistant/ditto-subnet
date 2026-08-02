/**
 * Lossless JSON parsing for integers that exceed IEEE-754 double precision.
 *
 * DittoBench dataset seeds are 64-bit values. The platform publishes them as
 * bare JSON numbers, so `JSON.parse` silently rounds them to the nearest
 * double the moment the response body is read: production seed
 * `989366151180340909` becomes `989366151180340864`. The corrupted value is
 * still a "valid" number, which is why this had to be fixed at the parse
 * boundary rather than in a schema — by the time a schema sees the value, the
 * exact digits are already gone and unrecoverable.
 *
 * Seeds are the reproducibility contract of the benchmark: they are printed in
 * published reproduction commands and re-derived on chain. A seed that is off
 * by 45 reproduces a different dataset, so a rounded seed is worse than no
 * seed at all.
 *
 * This parser rewrites exactly those integer literals that `JSON.parse` cannot
 * represent exactly into JSON strings, then parses normally. Numbers that
 * round-trip through a double without loss are left untouched, so every field
 * that already parsed correctly keeps its existing type and every existing
 * schema keeps validating unchanged. Fields that can carry a 64-bit value
 * declare `seedSchema` (string in, string out) and receive the exact digits.
 *
 * Deliberately not used: `JSON.parse` with a reviver (revivers are handed the
 * already-rounded number), and `bigint` (it does not survive `JSON.stringify`,
 * and MCP tool results are serialized JSON).
 */

/** Matches a JSON number token: `-?int(.frac)?([eE]exp)?`. */
const NUMBER_TOKEN = /^-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?/

/**
 * True when `digits` (a bare decimal integer literal, optional leading `-`)
 * cannot survive a round trip through a JavaScript number.
 *
 * Compared with BigInt rather than by string length so the boundary is exact
 * and never depends on a lossy intermediate number.
 */
function exceedsSafeInteger(digits: string) {
  const magnitude = digits.startsWith('-') ? BigInt(digits.slice(1)) : BigInt(digits)
  return magnitude > BigInt(Number.MAX_SAFE_INTEGER)
}

/**
 * Parse JSON, preserving integers too large for a double as decimal strings.
 *
 * Only unrepresentable *integer* literals are rewritten. Literals with a
 * fraction or exponent are left as numbers: they are inherently approximate,
 * and no seed is ever written that way.
 */
export function parseJsonPreservingLargeIntegers(text: string): unknown {
  return JSON.parse(quoteLargeIntegerLiterals(text))
}

/**
 * Rewrite unrepresentable integer literals in `text` into JSON strings.
 *
 * Exported for tests. Scans the document rather than running a bare regex over
 * it, so digits inside string values (transcript hashes, reasons, miner-supplied
 * agent names) are never rewritten and the output stays byte-identical to the
 * input wherever no rewrite is needed.
 */
export function quoteLargeIntegerLiterals(text: string) {
  let out = ''
  let cursor = 0
  let index = 0

  while (index < text.length) {
    const char = text[index]!

    if (char === '"') {
      // Skip the whole string literal, honouring backslash escapes, so its
      // contents are never mistaken for a number token.
      index += 1
      while (index < text.length) {
        const inner = text[index]!
        if (inner === '\\') {
          index += 2
          continue
        }
        index += 1
        if (inner === '"') break
      }
      continue
    }

    if (char === '-' || (char >= '0' && char <= '9')) {
      const token = NUMBER_TOKEN.exec(text.slice(index))
      if (!token) {
        index += 1
        continue
      }
      const literal = token[0]
      const isInteger = token[1] === undefined && token[2] === undefined
      if (isInteger && exceedsSafeInteger(literal)) {
        out += text.slice(cursor, index) + '"' + literal + '"'
        cursor = index + literal.length
      }
      index += literal.length
      continue
    }

    index += 1
  }

  return cursor === 0 ? text : out + text.slice(cursor)
}
