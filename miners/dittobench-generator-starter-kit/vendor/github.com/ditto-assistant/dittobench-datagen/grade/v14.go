package grade

import (
	"regexp"
	"sort"
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
// passive removal in a set-membership clause. "on" is deliberately absent: it
// almost always opens a date ("is removed on Friday" is a schedule, not a
// completed removal), never the list the item left.
var removalPlacePrepositionsV14 = map[string]bool{"from": true, "in": true, "off": true}

// removalTimeWordsV14 mark a tail that names a time rather than a place
// ("removed in March", "removed in two weeks"): a scheduled or dated removal
// reads as a plan, which keeps the item a candidate.
var removalTimeWordsV14 = map[string]bool{
	"monday": true, "tuesday": true, "wednesday": true, "thursday": true, "friday": true, "saturday": true, "sunday": true,
	"january": true, "february": true, "march": true, "april": true, "may": true, "june": true, "july": true,
	"august": true, "september": true, "october": true, "november": true, "december": true,
	"today": true, "tonight": true, "tomorrow": true, "yesterday": true, "morning": true, "afternoon": true,
	"evening": true, "night": true, "weekend": true, "week": true, "weeks": true, "month": true, "months": true,
	"year": true, "years": true, "day": true, "days": true, "hour": true, "hours": true, "minute": true,
	"minutes": true, "moment": true, "time": true, "fortnight": true, "fortnights": true, "next": true, "later": true, "soon": true,
	"spring": true, "summer": true, "autumn": true, "fall": true, "winter": true,
	"season": true, "seasons": true,
}

// maxRemovalPlaceWordsV14 bounds the words after the preposition ("from the
// list", "from your packing list").
const maxRemovalPlaceWordsV14 = 3

// removedThenPlaceV14 reports whether a set-membership clause (with the case's
// semantic values masked) ends in completed passive removal followed only by a
// short place phrase: a preposition and at most three plain words, none naming
// a time. A masked value inside the phrase ("removed from <value>") does not
// qualify, and the clause's trailing punctuation is ignored ("(PE kit has
// been removed)").
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
			if removalTimeWordsV14[w] {
				plain = false
			}
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
// doubles only in a one-syllable verb (stop -> stopping, quit -> quitting, so
// "hop" cannot read as "hoping"). A longer verb doubles only when
// multisyllableDoublingV14 lists it (admit -> admitting; cancel -> canceling
// and cancelling): open -> opening, never "openning".
func progressiveVerbV14(w string) []string {
	if len(w) < 2 {
		return nil
	}
	for _, r := range w {
		if r < 'a' || r > 'z' {
			return nil
		}
	}
	if (strings.HasSuffix(w, "ing") || strings.HasSuffix(w, "ed")) && !baseVerbsLikeInflectionsV14[w] {
		return nil
	}
	// A "u" after "q" spells a consonant sound (quit, equip).
	isVowel := func(i int) bool {
		return strings.IndexByte("aeiou", w[i]) >= 0 && !(w[i] == 'u' && i > 0 && w[i-1] == 'q')
	}
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
	if last == 'c' && n >= 3 && isVowel(n-2) {
		// A final c after a vowel takes k to keep its hard sound: panic ->
		// panicking, mimic -> mimicking, picnic -> picnicking.
		return []string{w + "king"}
	}
	if n >= 3 && !isVowel(n-1) && isVowel(n-2) && !isVowel(n-3) && strings.IndexByte("wxy", last) < 0 {
		doubled := w + string(last) + "ing"
		groups := 0
		for i := 0; i < n; i++ {
			if isVowel(i) && (i == 0 || !isVowel(i-1)) {
				groups++
			}
		}
		if groups == 1 {
			return []string{doubled}
		}
		if both, ok := multisyllableDoublingV14[w]; ok {
			if both {
				return []string{w + "ing", doubled}
			}
			return []string{doubled}
		}
	}
	return []string{w + "ing"}
}

// baseVerbsLikeInflectionsV14 are base verbs whose spelling happens to end in
// -ing or -ed, so the inflection guard in progressiveVerbV14 must not skip
// them ("bring it" -> "bringing it", "need it" -> "needing it"). Any other
// word ending in -ing or -ed is read as already inflected and gets no form.
var baseVerbsLikeInflectionsV14 = map[string]bool{
	"bring": true, "cling": true, "fling": true, "ring": true, "sing": true, "sling": true,
	"spring": true, "sting": true, "string": true, "swing": true, "wing": true, "wring": true,
	"bleed": true, "breed": true, "embed": true, "exceed": true, "feed": true, "heed": true,
	"need": true, "proceed": true, "seed": true, "shed": true, "shred": true, "sled": true,
	"speed": true, "succeed": true, "wed": true, "weed": true,
}

// multisyllableDoublingV14 lists the multisyllabic verbs whose final
// consonant doubles before -ing. true: both spellings are standard (US
// "canceling", UK "cancelling"); false: only the doubled one is (stress on
// the final syllable: "admitting", "preferring"). Every other multisyllabic
// verb takes plain -ing ("opening", "listening", "visiting").
var multisyllableDoublingV14 = map[string]bool{
	"cancel": true, "channel": true, "counsel": true, "label": true, "level": true,
	"model": true, "signal": true, "total": true, "travel": true,
	"admit": false, "begin": false, "commit": false, "compel": false, "confer": false,
	"control": false, "defer": false, "equip": false, "expel": false, "forget": false,
	"infer": false, "occur": false, "omit": false, "patrol": false, "permit": false,
	"prefer": false, "propel": false, "rebel": false, "refer": false, "regret": false,
	"submit": false, "transfer": false, "unplug": false, "unzip": false, "embed": false,
}

// ── Gerund and post-posed negation of an action (progressiveActions) ────────

// gerundNegationV14 are verbs and prepositions that take a gerund object and
// negate it: "I'd avoid pausing it", "we decided against pausing it", "we
// stopped pausing it". Accepting the -ing form of an action makes these
// natural, so in an action claim each one rejects the reviewed value it
// immediately governs. A verb followed by anything else ("stop the charges by
// pausing it") rejects nothing.
var gerundNegationV14 = foldedPhrasesV14(
	"avoid", "avoids", "avoided", "avoiding", "against",
	"stop", "stops", "stopped", "stopping", "quit", "quits", "quitting",
)

// postposedRejectionV14 are predicates that negate the value right before
// them: "pausing isn't possible", "pausing it is not an option". In an action
// claim they reject that value only; the rest of the clause keeps its status,
// so "we'll cancel it because pausing isn't possible" still asserts cancel.
var postposedRejectionV14 = foldedPhrasesV14(
	"isn't possible", "isnt possible", "is not possible", "wasn't possible", "was not possible",
	"not possible", "is impossible", "isn't an option", "is not an option",
	"isn't allowed", "is not allowed",
)

// foldedPhrasesV14 folds phrases like the claim lexicon does and orders them
// longest first, as matchPhraseAt expects.
func foldedPhrasesV14(phrases ...string) []string {
	out := make([]string, 0, len(phrases))
	for _, p := range phrases {
		out = append(out, foldV13(p))
	}
	sort.SliceStable(out, func(i, j int) bool { return len(out[i]) > len(out[j]) })
	return out
}
