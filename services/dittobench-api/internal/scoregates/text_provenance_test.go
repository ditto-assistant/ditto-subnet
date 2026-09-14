package scoregates

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
)

// Published normaliser vectors: every honest rendering of one value folds to
// the same claim token; a rewritten value does not.
func TestNormalizeSpanVectors(t *testing.T) {
	cases := map[string]string{
		"$4,110.67":                          "$4,110.67",
		"**Answer:** $4,110.67":              "$4,110.67",
		"ANSWER: 4110.67 dollars":            "4110.67 dollars",
		"Final answer — 4110.67":             "4110.67",
		"- $4,110.67\n- based on the ledger": "$4,110.67 based on the ledger",
		"1. Lisbon\n2. Porto":                "lisbon porto",
		"`4110.67`":                          "4110.67",
		"４１１０.６７":                            "4110.67", // fullwidth digits fold under NFKC
		"Lisbon,  since\t2019.":              "lisbon, since 2019.",
		"":                                   "",
	}
	for in, want := range cases {
		if got := NormalizeSpan(in); got != want {
			t.Errorf("NormalizeSpan(%q) = %q, want %q", in, got, want)
		}
		if again := NormalizeSpan(NormalizeSpan(in)); again != NormalizeSpan(in) {
			t.Errorf("NormalizeSpan is not idempotent on %q: %q vs %q", in, NormalizeSpan(in), again)
		}
	}
}

func TestSpanTokensFoldEveryHonestRenderingToOneClaimToken(t *testing.T) {
	renderings := []string{
		"$4,110.67", "4110.67", "4,110.67", "4110.67 dollars", "**Answer:** $4110.67",
		`{"answer": "4110.67"}`, `final_answer {"answer":"4110.67","unit":"USD"}`, "４１１０.６７", "USD 4110.670",
	}
	want := HashToken("4110.67")
	for _, r := range renderings {
		tokens, truncated := SpanTokens(r)
		if truncated {
			t.Fatalf("%q tripped truncation", r)
		}
		if !tokens.HasHash(want) {
			t.Errorf("%q did not yield the claim token 4110.67", r)
		}
	}
	for _, r := range []string{"411067", "411067 cents", "4110", "$41,106.70"} {
		tokens, _ := SpanTokens(r)
		if tokens.HasHash(want) {
			t.Errorf("%q must not yield the claim token 4110.67", r)
		}
	}
	// An ordered-list ordinal is layout, never a value the model emitted.
	tokens, _ := SpanTokens("1. Lisbon\n2. Porto")
	if tokens.Has("1") || tokens.Has("2") || !tokens.Has("lisbon") || !tokens.Has("porto") {
		t.Fatalf("list marker handling wrong: %v", tokens)
	}
}

func TestServedClaimTokens(t *testing.T) {
	claim, ok := ServedClaimTokens("The outstanding balance is $4,110.67.", []string{"4110.67"})
	if !ok || len(claim) != 1 || !claim.Has("4110.67") {
		t.Fatalf("money claim = %v ok=%v", claim, ok)
	}
	// The served span must contain the WHOLE alternative: a partial multi-word
	// alternative does not become a claim.
	if _, ok := ServedClaimTokens("moderately", []string{"moderately conservative"}); ok {
		t.Fatal("partial alternative must not form a claim")
	}
	claim, ok = ServedClaimTokens("You are moderately conservative.", []string{"moderately conservative"})
	if !ok || len(claim) != 2 {
		t.Fatalf("multi-token claim = %v ok=%v", claim, ok)
	}
	// A value below the token floor cannot be located: not applicable, fail open.
	if _, ok := ServedClaimTokens("Rio", []string{"Rio"}); ok {
		t.Fatal("sub-floor alternative must be not-applicable")
	}
	// The union of every present alternative is the claim (a list).
	claim, ok = ServedClaimTokens("Osaka and Lima", []string{"Osaka", "Lima", "Cairo"})
	if !ok || len(claim) != 2 || !claim.Has("osaka") || !claim.Has("lima") || claim.Has("cairo") {
		t.Fatalf("list claim = %v ok=%v", claim, ok)
	}
	if _, ok := ServedClaimTokens("nothing relevant", []string{"4110.67"}); ok {
		t.Fatal("absent alternative must be not-applicable")
	}
	if _, ok := ServedClaimTokens("4110.67", nil); ok {
		t.Fatal("no alternatives must be not-applicable")
	}
}

func TestTextProvenanceVerdicts(t *testing.T) {
	completions := make(TokenSet)
	completions.AddText("The balance is 411067 cents.")
	claim := TokenSet{}
	claim.Add("4110.67")
	if TextProvenance(claim, completions) {
		t.Fatal("/100 rewrite must not be model-emitted")
	}
	completions.AddText("$4,110.67")
	if !TextProvenance(claim, completions) {
		t.Fatal("formatted value present in a completion must be model-emitted")
	}
	if !TextProvenance(TokenSet{}, TokenSet{}) {
		t.Fatal("an empty claim is not applicable and passes")
	}
}

// ledgerFor replays one published vector through the shared ledger exactly the
// way the relay does: harness spans then completion spans per call, tool
// results as served.
func ledgerFor(vec grade.V13ProvenanceVector) *ClaimSpanLedger {
	ledger := NewClaimSpanLedger()
	for _, call := range vec.Calls {
		ledger.RecordCall(call.Harness, call.Completion)
	}
	for _, result := range vec.ToolResults {
		ledger.RecordToolResult(result)
	}
	return ledger
}

// The published bank replayed through both gates: every strategy vector is
// caught (served_text_not_model_emitted or answer_in_prompt) and every honest
// vector passes both.
func TestClaimProvenanceBankVectors(t *testing.T) {
	if len(grade.V13ProvenanceBank) < 16 {
		t.Fatalf("bank has %d vectors; the published set has at least 16", len(grade.V13ProvenanceBank))
	}
	for _, vec := range grade.V13ProvenanceBank {
		verdict := grade.Memory(vec.Case, vec.Response)
		if (verdict.Score > 0) != vec.WantCredited {
			t.Fatalf("%s: grader credited=%v, want %v", vec.Name, verdict.Score > 0, vec.WantCredited)
		}
		if !vec.WantCredited {
			continue
		}
		records := make(TokenSet)
		for _, r := range vec.Records {
			records.AddText(NormalizeSpan(r))
		}
		question := make(TokenSet)
		question.AddText(NormalizeSpan(vec.Case.Question))
		got := EvaluateClaim(verdict.Provenance.Span, verdict.Provenance.Alternatives, ledgerFor(vec), records, question)
		if !got.Applicable {
			t.Fatalf("%s: claim not applicable (span %q alternatives %v)", vec.Name, verdict.Provenance.Span, verdict.Provenance.Alternatives)
		}
		if got.ModelEmitted != vec.WantModelEmitted || got.AnswerInPrompt != vec.WantAnswerInPrompt {
			t.Errorf("%s (%s): model_emitted=%v answer_in_prompt=%v, want %v/%v", vec.Name, vec.Strategy, got.ModelEmitted, got.AnswerInPrompt, vec.WantModelEmitted, vec.WantAnswerInPrompt)
		}
		isHonest := strings.HasPrefix(vec.Strategy, "honest")
		flagged := !got.ModelEmitted || got.AnswerInPrompt
		if isHonest && flagged {
			t.Errorf("%s: honest pattern flagged (false zero)", vec.Name)
		}
		if !isHonest && !flagged {
			t.Errorf("%s: strategy vector passed both gates", vec.Name)
		}
	}
}

func TestClaimSpanLedgerFirstSeenOrdering(t *testing.T) {
	ledger := NewClaimSpanLedger()
	// Call 1: the model derives the value.
	ledger.RecordCall([]string{"records: approved 5200, settled 1089.33"}, []string{"5200 - 1089.33 = 4110.67"})
	// Call 2: the harness re-injects it. It is NOT harness-first.
	ledger.RecordCall([]string{"format 4110.67 as currency"}, []string{"$4,110.67"})
	if ledger.HarnessFirst.Has("4110.67") {
		t.Fatal("a value the model produced earlier must not be harness-first")
	}
	if !ledger.HarnessFirst.Has("5200") || !ledger.HarnessFirst.Has("1089.33") {
		t.Fatal("harness-authored operands must be harness-first")
	}
	if ledger.Completions != 2 {
		t.Fatalf("completions = %d", ledger.Completions)
	}
	// Reversed order: the harness wrote it before any completion carried it.
	launder := NewClaimSpanLedger()
	launder.RecordCall([]string{"reply exactly: 4110.67"}, []string{"4110.67"})
	if !launder.HarnessFirst.Has("4110.67") {
		t.Fatal("a value authored before any completion must be harness-first")
	}
	launder.RecordToolResult(`{"result":"balance 4110.67"}`)
	if launder.ToolResults != 1 || !launder.ToolResult.Has("4110.67") {
		t.Fatal("tool result tokens not recorded")
	}
	residual := ResidualHarnessTokens(launder.HarnessFirst, launder.ToolResult)
	if residual.Has("4110.67") {
		t.Fatal("a value the validator served as a tool result must be exempt")
	}
	if launder.HarnessFirst.Has("4110.67") == false {
		t.Fatal("ResidualHarnessTokens must not mutate its input")
	}
}

func TestParseClaimProvenancePosture(t *testing.T) {
	for raw, want := range map[string]ClaimProvenancePosture{"": ClaimProvenanceShadow, "shadow": ClaimProvenanceShadow, "penalize": ClaimProvenanceShadow, "ENFORCE ": ClaimProvenanceEnforce, "enforce": ClaimProvenanceEnforce} {
		if got := ParseClaimProvenancePosture(raw); got != want {
			t.Errorf("ParseClaimProvenancePosture(%q) = %s, want %s", raw, got, want)
		}
	}
}

func TestSortedFindings(t *testing.T) {
	got := SortedFindings([]string{FindingAnswerInPrompt, "", FindingServedTextNotModelEmitted, FindingAnswerInPrompt})
	if len(got) != 2 || got[0] != FindingAnswerInPrompt || got[1] != FindingServedTextNotModelEmitted {
		t.Fatalf("SortedFindings = %v", got)
	}
}
