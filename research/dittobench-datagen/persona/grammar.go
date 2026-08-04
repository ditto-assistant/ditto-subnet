package persona

import (
	"math/rand"
	"strings"
)

// Grammar is a small Tracery-style context-free grammar for prompt surfaces.
// Keys are symbol names; each value is the list of alternatives for that
// symbol, and an alternative may reference other symbols as #symbol#. Expand
// multiplies the surface a template matcher must cover (clause order, synonym
// slots, optional phrases) far beyond a flat template list, while every
// alternative stays hand-reviewed so no production can leak an answer or
// mangle a pinned value.
//
// Contract:
//   - Deterministic: the only randomness is draws from the caller's seeded
//     *rand.Rand, one draw per symbol resolution, references resolved left to
//     right. No map iteration: alternatives are slices and map access is by
//     exact key only.
//   - Placeholder-safe: %s fill stays OUT of the grammar. Alternatives may
//     contain a literal %s; the caller expands first, then fills, so canonical
//     values are never rewritten by expansion.
//   - Non-hanging: expansion is depth-capped. A symbol reference more than
//     maxGrammarDepth levels deep resolves to "", so a cyclic grammar
//     terminates instead of recursing forever.
//   - An unknown #symbol# resolves to itself verbatim (including the hashes)
//     so a typo is loudly visible in the output and caught by surface tests.
type Grammar map[string][]string

// maxGrammarDepth caps recursive symbol resolution. Real prompt grammars are
// 2-3 levels deep; the cap only exists so a cyclic grammar cannot hang.
const maxGrammarDepth = 8

// Expand resolves root against g, choosing among alternatives with r.
func Expand(r *rand.Rand, g Grammar, root string) string {
	return expandSymbol(r, g, root, 0)
}

func expandSymbol(r *rand.Rand, g Grammar, symbol string, depth int) string {
	if depth > maxGrammarDepth {
		return ""
	}
	alts, ok := g[symbol]
	if !ok || len(alts) == 0 {
		return "#" + symbol + "#"
	}
	alt := alts[r.Intn(len(alts))]
	if !strings.Contains(alt, "#") {
		return alt
	}
	var b strings.Builder
	for {
		i := strings.Index(alt, "#")
		if i < 0 {
			b.WriteString(alt)
			break
		}
		j := strings.Index(alt[i+1:], "#")
		if j < 0 {
			b.WriteString(alt)
			break
		}
		b.WriteString(alt[:i])
		b.WriteString(expandSymbol(r, g, alt[i+1:i+1+j], depth+1))
		alt = alt[i+1+j+1:]
	}
	return b.String()
}
