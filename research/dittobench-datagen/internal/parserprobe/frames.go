// Package parserprobe is the generator-inverse harness (GIH, plan strategy
// N13) and the seed-trained router (N14) used as the Bench v13 surface gate.
//
// The public generator is a hand-reviewed set of frames, grammars, and
// component banks, and a context-free surface is exactly invertible. Every
// earlier surface metric (1-NN, family-id < 60%, 4-gram recurrence) was a
// strawman the generator itself beats at ~100%. This package is the honest
// adversary: it reads ONLY what reaches a harness on the wire (the /seed
// records, the staged questions, the tool prompts), assembles a parser from the
// repository's own frames, recovers (family, slots) for every record and
// question, evaluates the public oracle semantics (the same arithmetic
// universe/questions.go resolveWithEvidence performs), launders the derived
// value through one "reply exactly" completion, and is graded by the real
// deterministic grader. On a pass-off artifact it scores near the oracle BY
// CONSTRUCTION; that number is the published baseline the private surface pass
// has to pull down, not a defect.
//
// Frame matching is typo-tolerant to exactly the projector the generator owns
// (internal/textnoise): one Damerau-Levenshtein edit on a word of four or more
// letters (two on seven or more), the eleven grammar rewrites, and untouched
// words below four letters. Values, labels, names, and numbers are never
// edited by the projector, so slot captures stay byte-exact.
package parserprobe

import (
	"strings"
	"unicode"
)

// frame is one template with %s slots compiled into tokens.
type frame struct {
	raw    string
	tokens []frameToken
	slots  int
}

type frameToken struct {
	literal string // lowercase literal word incl. attached punctuation; "" for a slot
	slot    bool
	prefix  string // punctuation before a slot ("“", "\"", "(")
	suffix  string // punctuation after a slot (".", ",", "?”", "'s")
}

// compileFrame turns a generator template into a frame. %q is a quoted %s;
// %d is a %s that must capture digits.
func compileFrame(template string) frame {
	t := strings.ReplaceAll(template, "%q", "\"%s\"")
	t = strings.ReplaceAll(t, "%d", "%s")
	f := frame{raw: template}
	for _, tok := range strings.Fields(t) {
		if i := strings.Index(tok, "%s"); i >= 0 {
			f.tokens = append(f.tokens, frameToken{slot: true, prefix: tok[:i], suffix: tok[i+2:]})
			f.slots++
			continue
		}
		f.tokens = append(f.tokens, frameToken{literal: strings.ToLower(tok)})
	}
	return f
}

// compileFrames compiles a bank.
func compileFrames(templates ...string) []frame {
	out := make([]frame, 0, len(templates))
	for _, t := range templates {
		out = append(out, compileFrame(t))
	}
	return out
}

// maxSlotTokens bounds one slot capture; the longest generator slot value is
// the twelve-token v12 relational subject descriptor.
const maxSlotTokens = 14

// match aligns text against the frame and returns the slot captures. It is a
// memoised token alignment: literal tokens must fuzzy-equal the text token,
// slot tokens absorb 1..maxSlotTokens text tokens bounded by their prefix and
// suffix punctuation.
func (f frame) match(text string) ([]string, bool) {
	words := strings.Fields(text)
	if len(words) < len(f.tokens)-f.slots || len(words) > len(f.tokens)+f.slots*(maxSlotTokens-1) {
		return nil, false
	}
	type key struct{ ti, wi int }
	memo := map[key][]string{}
	failed := map[key]bool{}
	var rec func(ti, wi int) ([]string, bool)
	rec = func(ti, wi int) ([]string, bool) {
		if ti == len(f.tokens) {
			if wi == len(words) {
				return []string{}, true
			}
			return nil, false
		}
		k := key{ti, wi}
		if v, ok := memo[k]; ok {
			return v, true
		}
		if failed[k] {
			return nil, false
		}
		tok := f.tokens[ti]
		if !tok.slot {
			if wi < len(words) && fuzzyWord(tok.literal, strings.ToLower(words[wi])) {
				if rest, ok := rec(ti+1, wi+1); ok {
					memo[k] = rest
					return rest, true
				}
			}
			failed[k] = true
			return nil, false
		}
		for n := 1; n <= maxSlotTokens && wi+n <= len(words); n++ {
			first, last := words[wi], words[wi+n-1]
			if !strings.HasPrefix(first, tok.prefix) {
				break
			}
			if !strings.HasSuffix(last, tok.suffix) {
				continue
			}
			value := strings.Join(words[wi:wi+n], " ")
			value = strings.TrimPrefix(value, tok.prefix)
			value = strings.TrimSuffix(value, tok.suffix)
			if strings.TrimSpace(value) == "" {
				continue
			}
			// A slot never swallows the frame's next literal: that would let one
			// frame absorb another family's clause.
			if ti+1 < len(f.tokens) && !f.tokens[ti+1].slot && wi+n < len(words) && !fuzzyWord(f.tokens[ti+1].literal, strings.ToLower(words[wi+n])) {
				continue
			}
			if rest, ok := rec(ti+1, wi+n); ok {
				out := append([]string{value}, rest...)
				memo[k] = out
				return out, true
			}
		}
		failed[k] = true
		return nil, false
	}
	return rec(0, 0)
}

// matchAny returns the first frame in bank that matches text, with captures.
func matchAny(bank []frame, text string) (int, []string, bool) {
	for i, f := range bank {
		if slots, ok := f.match(text); ok {
			return i, slots, true
		}
	}
	return -1, nil, false
}

// grammarEquivalents are the single-token halves of the textnoise grammar
// rewrites ("don't" -> "dont", "could have" -> "could of", ...). The matcher
// treats each pair as equal so a projected function word still aligns with
// the generator's canonical frame. The two-token rewrites ("a lot" -> "alot")
// are left to the edit budget.
var grammarEquivalents = map[string]string{
	"dont": "don't", "doesnt": "doesn't", "im": "i'm", "its": "it's", "whose": "who's",
	"of": "have", "use": "used", "suppose": "supposed",
}

// fuzzyWord reports whether a projected text word matches a literal frame word.
// Attached punctuation must match exactly; the letter core tolerates the edit
// budget textnoise spends on words of that length.
func fuzzyWord(literal, word string) bool {
	if literal == word {
		return true
	}
	lp, lc, ls := splitPunct(literal)
	wp, wc, ws := splitPunct(word)
	if lp != wp || ls != ws {
		return false
	}
	if lc == wc {
		return true
	}
	if eq, ok := grammarEquivalents[wc]; ok && eq == lc {
		return true
	}
	if eq, ok := grammarEquivalents[lc]; ok && eq == wc {
		return true
	}
	if len(lc) < 4 {
		return false
	}
	budget := 1
	if len(lc) >= 7 {
		budget = 2
	}
	if abs(len(lc)-len(wc)) > budget {
		return false
	}
	return damerauLevenshtein(lc, wc, budget) <= budget
}

// splitPunct splits a token into leading punctuation, letter/digit core, and
// trailing punctuation.
func splitPunct(tok string) (string, string, string) {
	rs := []rune(tok)
	i, j := 0, len(rs)
	for i < j && !isCore(rs[i]) {
		i++
	}
	for j > i && !isCore(rs[j-1]) {
		j--
	}
	return string(rs[:i]), string(rs[i:j]), string(rs[j:])
}

func isCore(r rune) bool { return unicode.IsLetter(r) || unicode.IsDigit(r) || r == '\'' || r == '’' }

// damerauLevenshtein is the optimal-string-alignment distance with an early
// exit once the budget is exceeded.
func damerauLevenshtein(a, b string, budget int) int {
	ra, rb := []rune(a), []rune(b)
	n, m := len(ra), len(rb)
	prev2 := make([]int, m+1)
	prev := make([]int, m+1)
	cur := make([]int, m+1)
	for j := 0; j <= m; j++ {
		prev[j] = j
	}
	for i := 1; i <= n; i++ {
		cur[0] = i
		rowMin := cur[0]
		for j := 1; j <= m; j++ {
			cost := 1
			if ra[i-1] == rb[j-1] {
				cost = 0
			}
			v := min(prev[j]+1, cur[j-1]+1, prev[j-1]+cost)
			if i > 1 && j > 1 && ra[i-1] == rb[j-2] && ra[i-2] == rb[j-1] {
				v = min(v, prev2[j-2]+1)
			}
			cur[j] = v
			if v < rowMin {
				rowMin = v
			}
		}
		if rowMin > budget {
			return budget + 1
		}
		prev2, prev, cur = prev, cur, prev2
	}
	return prev[m]
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

// containsFuzzy reports whether any word of text fuzzy-matches keyword.
func containsFuzzy(text, keyword string) bool {
	kw := strings.ToLower(keyword)
	for _, w := range strings.Fields(strings.ToLower(text)) {
		_, core, _ := splitPunct(w)
		if fuzzyWord(kw, core) {
			return true
		}
	}
	return false
}

// containsPhraseFuzzy reports whether the consecutive words of phrase appear in
// text, each fuzzy-matched.
func containsPhraseFuzzy(text, phrase string) bool {
	pw := strings.Fields(strings.ToLower(phrase))
	tw := strings.Fields(strings.ToLower(text))
	if len(pw) == 0 || len(tw) < len(pw) {
		return false
	}
outer:
	for i := 0; i+len(pw) <= len(tw); i++ {
		for k, p := range pw {
			_, core, _ := splitPunct(tw[i+k])
			if !fuzzyWord(p, core) {
				continue outer
			}
		}
		return true
	}
	return false
}

// sentences splits prose into sentences on terminal punctuation followed by
// whitespace. Money ("$887.71 rather") and addresses never carry a space after
// their dot, so they survive intact.
func sentences(text string) []string {
	var out []string
	var b strings.Builder
	rs := []rune(text)
	for i, r := range rs {
		b.WriteRune(r)
		if i+1 < len(rs) && unicode.IsSpace(rs[i+1]) && (r == '.' || r == '!' || r == '?' || r == '”' || r == '"') {
			if s := strings.TrimSpace(b.String()); s != "" {
				out = append(out, s)
			}
			b.Reset()
		}
	}
	if s := strings.TrimSpace(b.String()); s != "" {
		out = append(out, s)
	}
	return out
}
