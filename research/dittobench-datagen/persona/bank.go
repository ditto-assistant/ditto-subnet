package persona

import (
	"hash/fnv"
	"math/rand"
	"sort"
	"strings"
)

// Per-seed grammar banks (bench_version >= 13).
//
// A public context-free grammar is exactly invertible: a harness holding the
// repository can build the grammar-inverse parser and recover every slot. The
// v13 surfaces therefore treat every grammar as the PRE-PASS input — the
// expansion a private surface pass later makes unregenerable — and, in the
// meantime, narrow the production set a parser trained on one seed can see of
// another. SeedBank derives, deterministically from (seed, salt), the subset of
// alternatives each NESTED symbol may draw from in ONE dataset. Only non-root
// symbols with at least seedBankMinAlternatives are thinned: the root symbol
// keeps its full alternative list so no seed loses a whole surface family, and
// a root-only grammar therefore draws from its full frame list on every seed.
// Thinning is a per-seed rotation of hand-reviewed alternatives, not the gate
// against a grammar-inverse parser — the parserprobe ceiling is.

// seedBankKeep is the share of a symbol's alternatives that stay active in one
// dataset, in thousandths. Two thirds keeps every bank varied within a run while
// no single seed exposes the whole hand-written list.
const seedBankKeep = 667

// seedBankMinAlternatives is the smallest bank SeedBank subsets. Shorter banks
// are already a coin flip; thinning them would collapse the surface instead of
// rotating it.
const seedBankMinAlternatives = 4

// SeedBank returns a copy of g whose non-root symbols carry only the
// alternatives active for (seed, salt). Alternatives keep their relative order
// so weighting by repetition ("", "", "", " Thanks!") is preserved in expectation.
// The root symbol is never thinned. Deterministic: symbols are visited in sorted
// key order and every draw comes from a hash of (seed, salt, symbol).
func SeedBank(seed int64, salt string, g Grammar) Grammar {
	out := make(Grammar, len(g))
	keys := make([]string, 0, len(g))
	for key := range g {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		alts := g[key]
		if key == "root" || len(alts) < seedBankMinAlternatives {
			out[key] = append([]string(nil), alts...)
			continue
		}
		keep := (len(alts)*seedBankKeep + 999) / 1000
		if keep < 2 {
			keep = 2
		}
		if keep > len(alts) {
			keep = len(alts)
		}
		r := rand.New(rand.NewSource(bankSeed(seed, salt, key)))
		perm := r.Perm(len(alts))[:keep]
		sort.Ints(perm)
		kept := make([]string, 0, keep)
		for _, index := range perm {
			kept = append(kept, alts[index])
		}
		out[key] = kept
	}
	return out
}

// WithSlots returns a copy of g with one single-alternative symbol per slot, so
// a grammar can reference pinned values as #alias# / #nickname# instead of a
// positional %s. Slot values are never expanded further: a value containing a
// literal '#' is escaped to a private symbol so it cannot be mistaken for a
// reference. Slot keys shadow grammar symbols of the same name.
func WithSlots(g Grammar, slots map[string]string) Grammar {
	out := make(Grammar, len(g)+len(slots))
	for key, alts := range g {
		out[key] = alts
	}
	keys := make([]string, 0, len(slots))
	for key := range slots {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		value := slots[key]
		if strings.Contains(value, "#") {
			// A literal hash inside a value would be read as a symbol reference;
			// route it through a one-alternative symbol that expands back to "#".
			out["hashmark"] = []string{"#"}
			value = strings.ReplaceAll(value, "#", "#hashmark#")
		}
		out[key] = []string{value}
	}
	return out
}

// ExpandSlots expands root against g after binding slots. It is the v13
// question/prompt renderer: the frame is drawn from the grammar, the pinned
// values are bound by name, and the expansion never rewrites a bound value.
func ExpandSlots(r *rand.Rand, g Grammar, root string, slots map[string]string) string {
	return Expand(r, WithSlots(g, slots), root)
}

func bankSeed(seed int64, salt, symbol string) int64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte("dittobench-v13-grammar-bank"))
	_, _ = h.Write([]byte{0})
	var seedBytes [8]byte
	for i := range seedBytes {
		seedBytes[i] = byte(uint64(seed) >> (8 * i))
	}
	_, _ = h.Write(seedBytes[:])
	_, _ = h.Write([]byte{0})
	_, _ = h.Write([]byte(salt))
	_, _ = h.Write([]byte{0})
	_, _ = h.Write([]byte(symbol))
	// math/rand's ALFG source mixes its seed weakly: nearby or structured seeds
	// leave the first draws' low bits correlated, which showed up as one root
	// frame dominating a run. Avalanche the digest (splitmix64 finalizer) so
	// every seed bit reaches every draw.
	z := h.Sum64()
	z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9
	z = (z ^ (z >> 27)) * 0x94d049bb133111eb
	z ^= z >> 31
	return int64(z & ((1 << 63) - 1))
}

// HashRand returns a rand.Rand seeded from (seed, parts...). Grammar renderers
// that must not perturb a shared generation stream draw from it instead. The
// first draw is discarded: even an avalanched seed leaves math/rand's very
// first output visibly seed-correlated.
func HashRand(seed int64, parts ...string) *rand.Rand {
	r := rand.New(rand.NewSource(bankSeed(seed, strings.Join(parts, ":"), "")))
	_ = r.Int63()
	return r
}
