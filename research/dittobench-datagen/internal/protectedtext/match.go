// Package protectedtext matches literal values without treating fragments of
// unrelated words or identifiers as protected values. It is private-surface
// policy only; legacy/public benchmark generation must not depend on it.
package protectedtext

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

func word(r rune) bool {
	return unicode.IsLetter(r) || unicode.IsNumber(r) || unicode.IsMark(r) || r == '_'
}

// At reports an exact occurrence with word boundaries at word-like edges.
// Punctuation inside a literal remains exact; this is not normalization.
func At(text, value string, offset int) bool {
	if value == "" || offset < 0 || offset > len(text) || !strings.HasPrefix(text[offset:], value) {
		return false
	}
	first, _ := utf8.DecodeRuneInString(value)
	last, _ := utf8.DecodeLastRuneInString(value)
	if offset > 0 && word(first) {
		prev, _ := utf8.DecodeLastRuneInString(text[:offset])
		if word(prev) {
			return false
		}
	}
	end := offset + len(value)
	if end < len(text) && word(last) {
		next, _ := utf8.DecodeRuneInString(text[end:])
		if word(next) {
			return false
		}
	}
	return true
}

// Count counts non-overlapping exact occurrences, including overlapping
// *different* protected literals when called separately for each value.
func Count(text, value string) int {
	if value == "" {
		return 0
	}
	count := 0
	for offset := 0; offset < len(text); {
		i := strings.Index(text[offset:], value)
		if i < 0 {
			break
		}
		i += offset
		if At(text, value, i) {
			count++
			offset = i + len(value)
		} else {
			_, n := utf8.DecodeRuneInString(text[i:])
			offset = i + n
		}
	}
	return count
}

// Replace performs one left-to-right pass, preferring the supplied order
// (callers sort longest first). Replacements are never searched again.
func Replace(text string, values, replacements []string) string {
	var out strings.Builder
	for offset := 0; offset < len(text); {
		found := false
		for i, value := range values {
			if At(text, value, offset) {
				out.WriteString(replacements[i])
				offset += len(value)
				found = true
				break
			}
		}
		if !found {
			_, n := utf8.DecodeRuneInString(text[offset:])
			out.WriteString(text[offset : offset+n])
			offset += n
		}
	}
	return out.String()
}
