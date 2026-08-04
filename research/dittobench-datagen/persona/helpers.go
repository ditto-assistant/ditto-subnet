package persona

import (
	"fmt"
	"math/rand"
	"strings"
	"unicode"
)

// pick returns a uniformly-random element of a non-empty pool.
func pick(r *rand.Rand, pool []string) string {
	if len(pool) == 0 {
		return ""
	}
	return pool[r.Intn(len(pool))]
}

// pickStr is pick over a template list (kept separate for readability at call
// sites where the slice is a set of phrasings rather than a value pool).
func pickStr(r *rand.Rand, options []string) string {
	return pick(r, options)
}

// pickDistinct returns a random pool element not equal to avoid (falls back to
// pick if the pool has no alternative).
func pickDistinct(r *rand.Rand, pool []string, avoid string) string {
	if len(pool) <= 1 {
		return pick(r, pool)
	}
	for {
		v := pool[r.Intn(len(pool))]
		if v != avoid {
			return v
		}
	}
}

// pickN returns count distinct random elements of pool in random order (capped
// at len(pool)). Uses r.Perm so the draw is a pure function of the stream.
func pickN(r *rand.Rand, pool []string, count int) []string {
	if count > len(pool) {
		count = len(pool)
	}
	if count <= 0 {
		return nil
	}
	perm := r.Perm(len(pool))
	out := make([]string, 0, count)
	for i := 0; i < count; i++ {
		out = append(out, pool[perm[i]])
	}
	return out
}

// jitterSize draws a list-family size from the seeded range [max(floor, base-2),
// base+3], so per-seed counts vary around the profile knob instead of being one
// constant. A base of 0 stays 0 (the family is disabled); when the knob sits
// below the floor, the floor wins (quota satisfiability over a tiny knob).
func jitterSize(r *rand.Rand, base, floor int) int {
	if base <= 0 {
		return 0
	}
	lo := base - 2
	if lo < floor {
		lo = floor
	}
	hi := base + 3
	if lo >= hi {
		return lo
	}
	return lo + r.Intn(hi-lo+1)
}

// shuffleInts shuffles a slice of ints in place with the run's stream.
func shuffleInts(r *rand.Rand, xs []int) {
	r.Shuffle(len(xs), func(i, j int) { xs[i], xs[j] = xs[j], xs[i] })
}

// fill substitutes a single value into a "%s" template.
func fill(template, value string) string {
	if !strings.Contains(template, "%s") {
		return template
	}
	return fmt.Sprintf(template, value)
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// capitalize upper-cases the first rune of s.
func capitalize(s string) string {
	if s == "" {
		return s
	}
	rs := []rune(s)
	rs[0] = unicode.ToUpper(rs[0])
	return string(rs)
}
