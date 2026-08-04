package persona

import (
	"math/rand"
	"strings"
	"testing"
)

var testGrammar = Grammar{
	"root":    {"#verb# the #noun#.", "Please #verb# it."},
	"verb":    {"check", "inspect", "review"},
	"noun":    {"queue", "roster", "list"},
	"literal": {"no references here"},
}

// TestExpandDeterministic pins the core contract: expansion is a pure
// function of the seeded RNG stream.
func TestExpandDeterministic(t *testing.T) {
	for seed := int64(1); seed <= 50; seed++ {
		a := Expand(rand.New(rand.NewSource(seed)), testGrammar, "root")
		b := Expand(rand.New(rand.NewSource(seed)), testGrammar, "root")
		if a != b {
			t.Fatalf("seed %d: same seed produced %q then %q", seed, a, b)
		}
	}
}

// TestExpandResolvesReferences confirms every #symbol# reference is replaced
// and the output never leaks grammar syntax.
func TestExpandResolvesReferences(t *testing.T) {
	r := rand.New(rand.NewSource(7))
	for i := 0; i < 200; i++ {
		out := Expand(r, testGrammar, "root")
		if strings.Contains(out, "#") {
			t.Fatalf("expansion leaked grammar syntax: %q", out)
		}
		if out == "" {
			t.Fatal("expansion of an acyclic grammar returned empty")
		}
	}
}

// TestExpandUnknownSymbol pins the loud-failure contract: a typo'd reference
// survives verbatim (hashes included) so surface tests catch it.
func TestExpandUnknownSymbol(t *testing.T) {
	g := Grammar{"root": {"see #missing# here"}}
	out := Expand(rand.New(rand.NewSource(1)), g, "root")
	if out != "see #missing# here" {
		t.Fatalf("unknown symbol not preserved verbatim: %q", out)
	}
}

// TestExpandDepthCap pins the non-hanging contract: a deliberately cyclic
// grammar terminates (the reference resolves to "" past the cap) instead of
// recursing forever.
func TestExpandDepthCap(t *testing.T) {
	g := Grammar{"root": {"x#root#"}}
	out := Expand(rand.New(rand.NewSource(1)), g, "root")
	want := strings.Repeat("x", maxGrammarDepth+1)
	if out != want {
		t.Fatalf("cyclic grammar expansion = %q, want %q", out, want)
	}
}

// TestExpandSurfaceCount confirms a small grammar already outgrows a flat
// template list: distinct expansions across draws exceed the root count.
func TestExpandSurfaceCount(t *testing.T) {
	r := rand.New(rand.NewSource(42))
	seen := map[string]bool{}
	for i := 0; i < 2000; i++ {
		seen[Expand(r, testGrammar, "root")] = true
	}
	// 3 verbs x 3 nouns + 3 verbs = 12 possible surfaces from 2 roots.
	if len(seen) != 12 {
		t.Fatalf("distinct surfaces = %d, want the full product 12", len(seen))
	}
}
