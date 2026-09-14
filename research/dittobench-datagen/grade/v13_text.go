package grade

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

// v13 text primitives. Every v2..v12 grader runs on the byte-level Normalize /
// isAlnum / Hit family, which is frozen: a fullwidth digit, a no-break space,
// or a curly apostrophe breaks a bounded match there, and an honest reply in
// Spanish or German carries letters the ASCII boundary test does not know. The
// v13 policy therefore uses this rune-based family instead. It is stdlib only
// and table-bounded so a third party can reproduce the fold exactly.
//
// foldV13 is an NFKC-style COMPATIBILITY fold over the code points a reply can
// plausibly carry, followed by Unicode case folding and whitespace collapse:
//
//   - fullwidth ASCII (U+FF01..U+FF5E) and the ideographic space map to ASCII;
//   - every Unicode space separator collapses to one ASCII space; zero-width
//     joiners/spaces/BOM are dropped;
//   - the Latin ligatures ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ ĳ, the Kelvin/Angstrom/Ohm signs, and
//     superscript/subscript digits map to their compatibility sequences;
//   - typographic apostrophes and quotes map to their ASCII forms (a formatting
//     fold beyond NFKC, so "don’t" and "don't" are one phrase);
//   - case folding lowercases every letter and expands ß/ẞ to "ss", İ to "i",
//     and long s to "s".
//
// Diacritics are deliberately preserved: graded values stay canonical through
// the multilingual pass, so "São Paulo" must not collide with "Sao Paulo".
func foldV13(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	lastSpace := true // trims leading whitespace and collapses runs
	for _, r := range s {
		switch {
		case r == 0x200B || r == 0x200C || r == 0x200D || r == 0xFEFF || r == 0x2060:
			continue // zero-width: drop
		case unicode.IsSpace(r) || r == 0x3000:
			if !lastSpace {
				b.WriteByte(' ')
				lastSpace = true
			}
			continue
		}
		lastSpace = false
		if mapped, ok := compatibilityFold[r]; ok {
			b.WriteString(mapped)
			continue
		}
		if r >= 0xFF01 && r <= 0xFF5E {
			r = r - 0xFF01 + '!'
		}
		switch r {
		case 'ß', 'ẞ':
			b.WriteString("ss")
		case 'İ':
			b.WriteByte('i')
		case 'ſ':
			b.WriteByte('s')
		default:
			b.WriteRune(unicode.ToLower(r))
		}
	}
	return strings.TrimRight(b.String(), " ")
}

// compatibilityFold is the bounded table of non-ASCII code points foldV13 maps
// to a compatibility sequence. Kept explicit and small so the fold is
// reviewable; anything not listed is only case-folded.
var compatibilityFold = map[rune]string{
	'ﬀ': "ff", 'ﬁ': "fi", 'ﬂ': "fl", 'ﬃ': "ffi", 'ﬄ': "ffl", 'ﬅ': "st", 'ﬆ': "st",
	'ĳ': "ij", 'Ĳ': "ij",
	'\u212A': "k", '\u212B': "å", '\u2126': "ω", // Kelvin, Angstrom, Ohm signs
	'⁰': "0", '¹': "1", '²': "2", '³': "3", '⁴': "4", '⁵': "5", '⁶': "6", '⁷': "7", '⁸': "8", '⁹': "9",
	'₀': "0", '₁': "1", '₂': "2", '₃': "3", '₄': "4", '₅': "5", '₆': "6", '₇': "7", '₈': "8", '₉': "9",
	'’': "'", '‘': "'", '‛': "'", '′': "'",
	'“': `"`, '”': `"`, '„': `"`, '″': `"`,
	'…': "...",
	'−': "-", // U+2212 MINUS SIGN: a negative amount must stay attached as one
}

// normalizeV13 is the v13 counterpart of Normalize: fold, then trim the
// surrounding punctuation a value is commonly wrapped in.
func normalizeV13(s string) string {
	return strings.Trim(foldV13(s), "\"'.,!?;:¿¡«»()[]")
}

// isWordRuneV13 is the rune-aware boundary test: letters, digits, and
// combining marks are word-internal; everything else bounds a phrase.
func isWordRuneV13(r rune) bool {
	return unicode.IsLetter(r) || unicode.IsDigit(r) || unicode.Is(unicode.Mn, r)
}

// hitV13 is the v13 counterpart of Hit: normalized bounded containment with
// rune boundaries, exact number-token matching for purely numeric values, and
// the same single-common-word refusal.
func hitV13(expected, response string) bool {
	e := normalizeV13(expected)
	if e == "" {
		return false
	}
	r := foldV13(response)
	if isPureNumber(e) {
		return containsNumberTokenV13(r, e)
	}
	if !strings.Contains(e, " ") && commonWords[e] {
		return false
	}
	return indexBoundedV13(r, e, 0) >= 0
}

// hitAnyV13 mirrors hitAny for the v13 fold.
func hitAnyV13(accept []string, response string) bool {
	for _, a := range accept {
		if hitV13(a, response) {
			return true
		}
	}
	return false
}

// indexBoundedV13 returns the byte index of the first occurrence of phrase in
// text at or after from whose neighbours are non-word runes, or -1. Both
// arguments must already be folded.
func indexBoundedV13(text, phrase string, from int) int {
	if phrase == "" || from > len(text) {
		return -1
	}
	for i := from; ; {
		j := strings.Index(text[i:], phrase)
		if j < 0 {
			return -1
		}
		j += i
		before := true
		if j > 0 {
			r, _ := utf8.DecodeLastRuneInString(text[:j])
			before = !isWordRuneV13(r)
		}
		after := true
		if end := j + len(phrase); end < len(text) {
			r, _ := utf8.DecodeRuneInString(text[end:])
			after = !isWordRuneV13(r)
		}
		if before && after {
			return j
		}
		i = j + 1
	}
}

// containsBoundedV13 reports whether phrase occurs bounded in folded text.
func containsBoundedV13(text, phrase string) bool {
	return indexBoundedV13(text, phrase, 0) >= 0
}

// anyPhraseV13 reports whether any lexicon phrase occurs bounded in text.
func anyPhraseV13(text string, phrases []string) bool {
	r := foldV13(text)
	for _, p := range phrases {
		if containsBoundedV13(r, p) {
			return true
		}
	}
	return false
}

// containsNumberTokenV13 is the rune-aware counterpart of containsNumberToken.
func containsNumberTokenV13(text, num string) bool {
	return indexNumberTokenV13(text, num, 0) >= 0
}

// indexNumberTokenV13 returns the byte index of the first occurrence of num in
// text at or after from whose neighbours are not number-attached runes
// (digits, ".", ",", "-"), or -1.
func indexNumberTokenV13(text, num string, from int) int {
	if num == "" || from > len(text) {
		return -1
	}
	for i := from; ; {
		j := strings.Index(text[i:], num)
		if j < 0 {
			return -1
		}
		j += i
		before := true
		if j > 0 {
			r, _ := utf8.DecodeLastRuneInString(text[:j])
			before = !numAttachedV13(r)
		}
		after := true
		if end := j + len(num); end < len(text) {
			r, _ := utf8.DecodeRuneInString(text[end:])
			after = !numAttachedV13(r)
		}
		if before && after {
			return j
		}
		i = j + 1
	}
}

func numAttachedV13(r rune) bool {
	return unicode.IsDigit(r) || r == '.' || r == ',' || r == '-'
}

// countDistinctHitsV13 mirrors countDistinctHits for the v13 fold.
func countDistinctHitsV13(vals []string, response string) int {
	n := 0
	seen := map[string]bool{}
	for _, v := range vals {
		nv := normalizeV13(v)
		if nv == "" || seen[nv] {
			continue
		}
		seen[nv] = true
		if hitV13(v, response) {
			n++
		}
	}
	return n
}

// stancePhraseV13 mirrors stancePhrase (bounded, negation-aware) on the v13
// fold. negatedAt works on space-separated words, which the fold guarantees.
func stancePhraseV13(text string, phrases []string) bool {
	r := foldV13(text)
	for _, p := range phrases {
		for from := 0; ; {
			j := indexBoundedV13(r, p, from)
			if j < 0 {
				break
			}
			if !negatedAt(r, j) {
				return true
			}
			from = j + 1
		}
	}
	return false
}

// orderedHitV13 mirrors orderedHit on the v13 fold.
func orderedHitV13(items []string, text string) bool {
	if len(items) == 0 {
		return false
	}
	r := foldV13(text)
	pos := 0
	for _, it := range items {
		e := normalizeV13(it)
		j := indexBoundedV13(r, e, pos)
		if j < 0 {
			return false
		}
		pos = j + len(e)
	}
	return true
}
