package main

// Unit tests for the answer-provenance value normalization the inference broker
// records with: canonical number/money handling and JSON chat message content
// extraction into bounded value-token hashes.

import (
	"testing"
)

func TestCanonicalNumber(t *testing.T) {
	cases := map[string]string{
		"1234":      "1234",
		"$1,234":    "1234",
		"1,234.50":  "1234.5",
		"1234.00":   "1234",
		"$12.34":    "12.34",
		"007":       "7",
		"0":         "0",
		"-0":        "0",
		"-1,000.00": "-1000",
		"12.340":    "12.34",
		"abc":       "",
		"":          "",
	}
	for in, want := range cases {
		if got := canonicalNumber(in); got != want {
			t.Errorf("canonicalNumber(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestParseChatContentAndTokens(t *testing.T) {
	req := []byte(`{"model":"m","messages":[{"role":"system","content":"you are ditto"},{"role":"user","content":"balance is 1234"}]}`)
	text, ok := parseChatInputText(req)
	if !ok {
		t.Fatal("parseChatInputText failed")
	}
	tokens, _ := valueTokenSet(text)
	if _, present := tokens[hashToken("1234")]; !present {
		t.Fatalf("input value token 1234 not extracted from %q -> %v", text, tokens)
	}
	// The envelope's model name and role labels must not leak as value tokens.
	if _, present := tokens[hashToken("m")]; present {
		t.Fatal("model field leaked into value tokens")
	}

	resp := []byte(`{"choices":[{"message":{"role":"assistant","content":"the answer is 1234"}}],"usage":{"prompt_tokens":50,"completion_tokens":7}}`)
	ctext, ok := parseChatCompletionText(resp)
	if !ok {
		t.Fatal("parseChatCompletionText failed")
	}
	ctokens, _ := valueTokenSet(ctext)
	if _, present := ctokens[hashToken("1234")]; !present {
		t.Fatalf("completion value token 1234 not extracted from %q", ctext)
	}
	// usage token counts (50, 7) must not enter the value token set.
	if _, present := ctokens[hashToken("50")]; present {
		t.Fatal("usage prompt_tokens leaked into completion value tokens")
	}
}
