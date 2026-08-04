package gen

import "testing"

func TestLexicalOverlap(t *testing.T) {
	ev := "My favorite color is teal." // stored fact
	// High-overlap question: shares "favorite" + "color".
	hi := lexicalOverlap("What is my favorite color?", ev)
	// Low-overlap question: shares nothing content-bearing with the fact.
	lo := lexicalOverlap("Which shade do I gravitate toward?", ev)
	if !(hi > lo) {
		t.Fatalf("expected high-overlap (%v) > low-overlap (%v)", hi, lo)
	}
	if hi <= 0 {
		t.Fatalf("high-overlap question should share content words, got %v", hi)
	}
	// Empty inputs are 0, not a divide-by-zero.
	if lexicalOverlap("", ev) != 0 || lexicalOverlap("anything", "") != 0 {
		t.Fatal("empty inputs must yield 0 overlap")
	}
}

func TestContentTokensDropsScaffolding(t *testing.T) {
	// Question words and articles drop, but "favorite" is content (not a
	// stopword), so expect {favorite, color}, not just {color}.
	toks := contentTokens("What is my favorite color?")
	if toks["what"] || toks["is"] || toks["my"] {
		t.Fatalf("scaffolding words leaked: %v", toks)
	}
	if !toks["favorite"] || !toks["color"] {
		t.Fatalf("content words missing: %v", toks)
	}
}
