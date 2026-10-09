package grade

import (
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v14 matcher additions (#2734). Each is reachable only through a v14
// gradingPolicy field; v13 and older grade byte-identically.

// ── Hyphen-joined multi-word values (hyphenJoinedValues) ────────────────────

// joinHyphenatedValuesV14 rewrites, in the reply's answer slot and final_text,
// every ASCII hyphen that joins two letter-ended words of a known multi-word
// case value back to a space: with the value "blue purple", "blue-purple"
// reads as "blue purple", and "true-blue accent" as "true blue accent". The
// reply then grades exactly as the spaced form would, on every rule.
//
// Known values are every hidden value the grader matches: expected, accepted,
// list items, distractors, the forbidden value, dump-guard values, and claim
// values, so a hyphen-joined distractor or forbidden value is still caught.
// Only a gap between two letters can be joined ("4 pm" cannot), the words
// themselves must match ("blue-green" never reads as "bluey green"), and the
// rewrite is one byte for one byte, so offsets into the reply are kept. A
// rewrite that would break an occurrence of a known value which itself
// contains a hyphen ("follow-up", "short-and-fast") is not applied.
func joinHyphenatedValuesV14(mc protocol.MemoryCase, resp protocol.RunResponse) protocol.RunResponse {
	values := caseValuesV14(mc)
	var patterns []*regexp.Regexp
	seen := map[string]bool{}
	for _, v := range values {
		if p := joinedValuePatternV14(normalizeV13(v)); p != nil && !seen[p.String()] {
			seen[p.String()] = true
			patterns = append(patterns, p)
		}
	}
	if len(patterns) == 0 {
		return resp
	}
	var hyphenated []string
	for _, v := range append(append(values, mc.GroundingTokens...), mc.SlotLexicon...) {
		if n := normalizeV13(v); strings.Contains(n, "-") {
			hyphenated = append(hyphenated, n)
		}
	}
	resp.Answer = joinHyphenatedTextV14(resp.Answer, patterns, hyphenated)
	resp.FinalText = joinHyphenatedTextV14(resp.FinalText, patterns, hyphenated)
	return resp
}

// caseValuesV14 lists the hidden values of a memory case that a reply can
// name.
func caseValuesV14(mc protocol.MemoryCase) []string {
	values := []string{mc.ExpectedAnswer, mc.ForbiddenAnswer}
	values = append(values, mc.AcceptAny...)
	values = append(values, mc.AnswerItems...)
	for _, alternatives := range mc.AnswerItemAcceptAny {
		values = append(values, alternatives...)
	}
	values = append(values, mc.DistractorAnswers...)
	values = append(values, mc.DumpGuard...)
	for _, claim := range mc.Claims {
		values = append(values, claim.Expected)
		values = append(values, claim.Accept...)
	}
	return values
}

// joinedValuePatternV14 compiles the case-insensitive pattern of a folded
// value in which every gap between two letters may be written either as
// whitespace or as one ASCII hyphen (captured), and every other gap only as
// whitespace. It returns nil for a value with no letter-letter gap.
func joinedValuePatternV14(folded string) *regexp.Regexp {
	words := strings.Split(folded, " ")
	if len(words) < 2 {
		return nil
	}
	const space = `[\s\p{Zs}]+`
	var b strings.Builder
	b.WriteString("(?i)")
	b.WriteString(regexp.QuoteMeta(words[0]))
	joinable := false
	for i := 1; i < len(words); i++ {
		left, _ := utf8.DecodeLastRuneInString(words[i-1])
		right, _ := utf8.DecodeRuneInString(words[i])
		if unicode.IsLetter(left) && unicode.IsLetter(right) {
			joinable = true
			b.WriteString(`(-|` + space + `)`)
		} else {
			b.WriteString(space)
		}
		b.WriteString(regexp.QuoteMeta(words[i]))
	}
	if !joinable {
		return nil
	}
	return regexp.MustCompile(b.String())
}

// joinHyphenatedTextV14 applies the joined-value patterns to one reply field.
func joinHyphenatedTextV14(text string, patterns []*regexp.Regexp, hyphenated []string) string {
	if !strings.Contains(text, "-") {
		return text
	}
	for _, p := range patterns {
		for from := 0; from < len(text); {
			loc := p.FindStringSubmatchIndex(text[from:])
			if loc == nil {
				break
			}
			start, end := from+loc[0], from+loc[1]
			bounded := true
			if start > 0 {
				r, _ := utf8.DecodeLastRuneInString(text[:start])
				bounded = !isWordRuneV13(r)
			}
			if end < len(text) {
				r, _ := utf8.DecodeRuneInString(text[end:])
				bounded = bounded && !isWordRuneV13(r)
			}
			if !bounded {
				_, size := utf8.DecodeRuneInString(text[start:])
				from = start + size
				continue
			}
			joined := []byte(text)
			changed := false
			for g := 2; g+1 < len(loc); g += 2 {
				if loc[g] >= 0 && loc[g+1]-loc[g] == 1 && text[from+loc[g]] == '-' {
					joined[from+loc[g]] = ' '
					changed = true
				}
			}
			if changed && !breaksHyphenatedValueV14(text, string(joined), hyphenated) {
				text = string(joined)
			}
			from = end
		}
	}
	return text
}

// breaksHyphenatedValueV14 reports whether rewriting before into after loses a
// bounded occurrence of a known value that itself contains a hyphen.
func breaksHyphenatedValueV14(before, after string, hyphenated []string) bool {
	if len(hyphenated) == 0 {
		return false
	}
	fb, fa := foldV13(before), foldV13(after)
	for _, h := range hyphenated {
		if countBoundedV14(fa, h) < countBoundedV14(fb, h) {
			return true
		}
	}
	return false
}

// countBoundedV14 counts the bounded occurrences of phrase in folded text.
func countBoundedV14(text, phrase string) int {
	n := 0
	for from := 0; ; {
		j := indexBoundedV13(text, phrase, from)
		if j < 0 {
			return n
		}
		n++
		from = j + 1
	}
}

// ── Completed removal with a place phrase (setRemovalPlace) ─────────────────

// removalPlacePrepositionsV14 open the place phrase that may follow completed
// passive removal in a set-membership clause.
var removalPlacePrepositionsV14 = map[string]bool{"from": true, "in": true, "on": true, "off": true}

// maxRemovalPlaceWordsV14 bounds the words after the preposition ("from the
// list", "from your packing list").
const maxRemovalPlaceWordsV14 = 3

// removedThenPlaceV14 reports whether a set-membership clause (with the case's
// semantic values masked) ends in completed passive removal followed only by a
// short place phrase: a preposition and at most three plain words. A masked
// value inside the phrase ("removed from <value>") does not qualify, and the
// clause's trailing punctuation is ignored ("(PE kit has been removed)").
func removedThenPlaceV14(context string) bool {
	text := strings.TrimRight(context, " .,;:!)")
	for _, suffix := range []string{" has been removed", " was removed", " is removed"} {
		if strings.HasSuffix(text, suffix) {
			return true
		}
		i := strings.LastIndex(text, suffix+" ")
		if i < 0 {
			continue
		}
		tail := strings.Fields(text[i+len(suffix):])
		if len(tail) < 2 || len(tail) > 1+maxRemovalPlaceWordsV14 || !removalPlacePrepositionsV14[tail[0]] {
			continue
		}
		plain := true
		for _, w := range tail[1:] {
			for _, r := range w {
				if !unicode.IsLetter(r) && r != '\'' {
					plain = false
				}
			}
		}
		if plain {
			return true
		}
	}
	return false
}

// ── Progressive action forms (progressiveActions) ───────────────────────────

// progressiveFormsV14 returns the progressive forms of each value: its first
// word put in the -ing form ("pause it" -> "pausing it"). Values whose first
// word is not plain lowercase ASCII, or already ends in -ing or -ed, add
// nothing.
func progressiveFormsV14(values []string) []string {
	var out []string
	for _, v := range values {
		n := normalizeV13(v)
		first, rest, _ := strings.Cut(n, " ")
		for _, ing := range progressiveVerbV14(first) {
			if rest != "" {
				ing += " " + rest
			}
			out = append(out, ing)
		}
	}
	return out
}

// progressiveVerbV14 spells the -ing form of a verb conservatively: a final
// silent e drops (pause -> pausing, upgrade -> upgrading; see, free keep
// theirs), -ie becomes -ying, and a final consonant after a single vowel
// doubles in a one-syllable verb (stop -> stopping). A longer verb with that
// ending gets both spellings (cancel -> canceling, cancelling), never the
// undoubled one alone for a one-syllable verb, so "hop" cannot read as
// "hoping".
func progressiveVerbV14(w string) []string {
	if len(w) < 2 {
		return nil
	}
	for _, r := range w {
		if r < 'a' || r > 'z' {
			return nil
		}
	}
	if strings.HasSuffix(w, "ing") || strings.HasSuffix(w, "ed") {
		return nil
	}
	isVowel := func(c byte) bool { return strings.IndexByte("aeiou", c) >= 0 }
	n := len(w)
	switch {
	case strings.HasSuffix(w, "ie"):
		return []string{w[:n-2] + "ying"}
	case n == 2 || strings.HasSuffix(w, "ee") || strings.HasSuffix(w, "ye") || strings.HasSuffix(w, "oe"):
		return []string{w + "ing"}
	case w[n-1] == 'e':
		return []string{w[:n-1] + "ing"}
	}
	last := w[n-1]
	if n >= 3 && !isVowel(last) && isVowel(w[n-2]) && !isVowel(w[n-3]) && strings.IndexByte("wxy", last) < 0 {
		doubled := w + string(last) + "ing"
		groups := 0
		for i := 0; i < n; i++ {
			if isVowel(w[i]) && (i == 0 || !isVowel(w[i-1])) {
				groups++
			}
		}
		if groups == 1 {
			return []string{doubled}
		}
		return []string{w + "ing", doubled}
	}
	return []string{w + "ing"}
}
