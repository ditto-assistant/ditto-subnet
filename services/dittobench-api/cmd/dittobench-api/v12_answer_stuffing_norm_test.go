package main

// Unit tests for the answer-stuffing value normalization and the value-provenance
// rule that back the pass: canonical number/money handling, JSON message content
// extraction, the input-before-completion ordering, and the computed-vs-verbatim
// determination.

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
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

func TestMoneyMajorUnits(t *testing.T) {
	cases := map[string]string{
		"1234":  "12.34",
		"5":     "0.05",
		"50":    "0.50",
		"100":   "1.00",
		"-1234": "-12.34",
		"12.3":  "", // not an integer minor-unit value
		"abc":   "",
	}
	for in, want := range cases {
		if got := moneyMajorUnits(in); got != want {
			t.Errorf("moneyMajorUnits(%q) = %q, want %q", in, got, want)
		}
	}
}

// A money answer given in minor units matches a completion that rendered it in
// major units, via the derived candidate, without matching an unrelated operand.
func TestAnswerStuffingCandidatesMoney(t *testing.T) {
	mc := protocol.MemoryCase{ExpectedAnswer: "1234", AnswerKind: protocol.AnswerMoney}
	candidates := make([]map[uint64]struct{}, 0)
	for _, form := range answerStuffingCandidates(mc) {
		if tokens := answerCandidateTokens(form); len(tokens) > 0 {
			candidates = append(candidates, tokens)
		}
	}
	// Completion printed the major-unit rendering.
	if !candidatePresent(tokenSet("12.34"), candidates) {
		t.Fatal("major-unit rendering did not match the minor-unit money answer")
	}
	// The bare minor-unit value the engine computed matches directly.
	if !candidatePresent(tokenSet("1234"), candidates) {
		t.Fatal("minor-unit value did not match")
	}
	// An unrelated operand must not match.
	if candidatePresent(tokenSet("37"), candidates) {
		t.Fatal("unrelated operand matched the money answer")
	}
}

// The value-provenance rule: stuffed iff the answer's earliest occurrence is an
// input (input of call k ranks before completion of call k).
func TestAnswerStuffedOrdering(t *testing.T) {
	answer := tokenSet("500")
	cands := []map[uint64]struct{}{answer}

	// Answer in the first call's input (and echoed) -> stuffed.
	stuffed := caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet("500"), tokenSet("500"))}}
	if !answerStuffed(stuffed, cands) {
		t.Fatal("answer in input-before-completion not detected as stuffed")
	}
	// Answer only in a completion -> clean.
	clean := caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet("100", "400"), tokenSet("500"))}}
	if answerStuffed(clean, cands) {
		t.Fatal("model-derived answer falsely flagged")
	}
	// Answer first produced in call 0's completion, reused in call 1's input -> clean.
	multiTurn := caseModelIOLog{calls: []caseModelCall{
		ioCall(tokenSet("100", "400"), tokenSet("500")),
		ioCall(tokenSet("500"), tokenSet("ok")),
	}}
	if answerStuffed(multiTurn, cands) {
		t.Fatal("legit multi-turn reuse falsely flagged")
	}
	// Answer in an input, never in any completion -> stuffed (harness-authored).
	inputOnly := caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet("500"), tokenSet("ok"))}}
	if !answerStuffed(inputOnly, cands) {
		t.Fatal("input-only answer not detected as stuffed")
	}
	// Answer never appears anywhere -> clean.
	absent := caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet("1"), tokenSet("2"))}}
	if answerStuffed(absent, cands) {
		t.Fatal("absent answer falsely flagged")
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

func TestMemoryAnswerIsComputed(t *testing.T) {
	evidence := map[string]string{
		"p1": "draft approved 500 dollars",
		"p2": "paid 200 dollars later",
	}
	// Computed: answer 300 is not verbatim in the evidence.
	computed := protocol.MemoryCase{ExpectedAnswer: "300"}
	if !memoryAnswerIsComputed(computed, []string{"p1", "p2"}, evidence) {
		t.Fatal("a non-verbatim computed answer was not marked computed")
	}
	// Verbatim recall: answer 500 is present in the evidence -> excluded.
	recall := protocol.MemoryCase{ExpectedAnswer: "500"}
	if memoryAnswerIsComputed(recall, []string{"p1", "p2"}, evidence) {
		t.Fatal("a verbatim-recall answer was wrongly marked computed")
	}
	// Missing evidence pin -> conservatively non-computed (excluded).
	if memoryAnswerIsComputed(computed, []string{"p1", "missing"}, evidence) {
		t.Fatal("an unresolved-evidence case was wrongly marked computed")
	}
	// No declared evidence -> excluded.
	if memoryAnswerIsComputed(computed, nil, evidence) {
		t.Fatal("a case with no declared evidence was wrongly marked computed")
	}
	// Empty answer -> excluded.
	if memoryAnswerIsComputed(protocol.MemoryCase{ExpectedAnswer: ""}, []string{"p1"}, evidence) {
		t.Fatal("an empty-answer case was wrongly marked computed")
	}
}
