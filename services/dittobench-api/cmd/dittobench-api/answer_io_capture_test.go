package main

// Unit tests for the broker-side answer-provenance capture bounds: a real
// full-context prompt must be captured whole, and only a body beyond the entire
// model context window may trip the truncation safety net.

import (
	"strings"
	"testing"
)

func TestValueTokenSetGenerousBound(t *testing.T) {
	// A realistic full-context prompt: 40000 distinct numbers plus the answer.
	var b strings.Builder
	for i := 0; i < 40000; i++ {
		b.WriteString(itoa(2_000_000 + i))
		b.WriteByte(' ')
	}
	b.WriteString("987654321") // the answer value
	tokens, truncated := valueTokenSet(b.String())
	if truncated {
		t.Fatalf("a full-context-sized prompt (40000 distinct value tokens) tripped truncation; ceiling is %d", answerIOMaxTokensPerSide)
	}
	if _, ok := tokens[hashToken("987654321")]; !ok {
		t.Fatal("answer value token not captured from the large prompt")
	}
	if len(tokens) != 40001 {
		t.Fatalf("expected 40001 distinct value-token hashes, got %d", len(tokens))
	}

	// Only a pathological input beyond the entire context window trips the net.
	var big strings.Builder
	for i := 0; i < answerIOMaxTokensPerSide+50; i++ {
		big.WriteString(itoa(3_000_000 + i))
		big.WriteByte(' ')
	}
	if _, over := valueTokenSet(big.String()); !over {
		t.Fatalf("an input beyond the %d ceiling did not trip truncation", answerIOMaxTokensPerSide)
	}
}
