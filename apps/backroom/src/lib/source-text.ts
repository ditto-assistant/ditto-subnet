// Miner source is untrusted display data. React escapes HTML, but Unicode
// directional controls can still visually reorder an excerpt so that what the
// reviewer READS is not what the compiler PARSES (the classic "trojan source"
// trick). Render them as visible escape sequences instead of honoring them,
// and surface other zero-width/invisible characters the same way. Excerpt
// containers must additionally isolate direction (dir="ltr" + plaintext
// bidi isolation) so surrounding markup cannot be influenced either.
//
// U+061C  Arabic letter mark          U+200B-U+200F zero-width + LRM/RLM
// U+202A-U+202E embedding/override    U+2066-U+2069 isolates
// U+FEFF  zero-width no-break space
const BIDI_AND_INVISIBLE_CONTROLS =
  /[\u061C\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]/g

export function sanitizeSourceLine(text: string): string {
  return text.replace(
    BIDI_AND_INVISIBLE_CONTROLS,
    (char) =>
      `\\u${char.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')}`,
  )
}
