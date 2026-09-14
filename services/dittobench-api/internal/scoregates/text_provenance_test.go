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
		"José":                               "jose",   // NFD + combining-mark strip folds the diacritic
		"Ōsaka":                              "osaka",  // macron folds; not "saka"
		"Zürich":                             "zurich", // umlaut folds; not "z rich"
		"Москва":                             "москва", // non-Latin letters are kept, lowercased
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

// The v13 tokenizer is Unicode-aware: diacritic variants fold to ONE token,
// non-Latin values tokenize to a claim, and the token floor counts runes. The
// v12 ValueTokenHashes rule is untouched (it backs the v12 answer-IO capture).
func TestSpanTokensFoldDiacriticsAndKeepNonLatin(t *testing.T) {
	for _, pair := range [][2]string{{"José", "Jose"}, {"Ōsaka", "Osaka"}, {"Zürich", "Zurich"}, {"Ｔｏｋｙｏ", "tokyo"}} {
		a, _ := SpanTokens(pair[0])
		b, _ := SpanTokens(pair[1])
		if len(a) != 1 || len(b) != 1 || !a.Subset(b) || !b.Subset(a) {
			t.Errorf("%q and %q must fold to one identical token: %v vs %v", pair[0], pair[1], a, b)
		}
	}
	cyrillic, _ := SpanTokens("Москва")
	if len(cyrillic) != 1 || !cyrillic.Has("москва") {
		t.Fatalf("a Cyrillic value must produce a non-empty claim token: %v", cyrillic)
	}
	cjk, _ := SpanTokens("東京都新宿区")
	if len(cjk) != 1 {
		t.Fatalf("a CJK run must produce one token: %v", cjk)
	}
	// The floor is a rune count: "Ōsaka" is 5 letters, not 6 bytes.
	if short, _ := SpanTokens("Ōsa"); len(short) != 0 {
		t.Fatalf("a 3-rune token must fall below the floor: %v", short)
	}
	// The v12 rule still drops non-ASCII letters (its capture bytes never move).
	v12, _ := ValueTokenHashes("ōsaka")
	if v12.Has("ōsaka") || !v12.Has("saka") {
		t.Fatalf("v12 ValueTokenHashes changed: %v", v12)
	}
}

func TestServedClaimTokens(t *testing.T) {
	claim, ok := ServedClaimTokens("The outstanding balance is $4,110.67.", [][]string{{"4110.67"}})
	if !ok || len(claim) != 1 || !claim.Has("4110.67") {
		t.Fatalf("money claim = %v ok=%v", claim, ok)
	}
	// The served span must contain the WHOLE form: a partial multi-word form
	// does not become a claim.
	if _, ok := ServedClaimTokens("moderately", [][]string{{"moderately conservative"}}); ok {
		t.Fatal("partial form must not form a claim")
	}
	claim, ok = ServedClaimTokens("You are moderately conservative.", [][]string{{"moderately conservative"}})
	if !ok || len(claim) != 2 {
		t.Fatalf("multi-token claim = %v ok=%v", claim, ok)
	}
	// A value below the token floor cannot be located: not applicable, fail open.
	if _, ok := ServedClaimTokens("Rio", [][]string{{"Rio"}}); ok {
		t.Fatal("sub-floor form must be not-applicable")
	}
	// The union of every present form across groups is the claim (a list).
	claim, ok = ServedClaimTokens("Osaka and Lima", [][]string{{"Osaka"}, {"Lima"}, {"Cairo"}})
	if !ok || len(claim) != 2 || !claim.Has("osaka") || !claim.Has("lima") || claim.Has("cairo") {
		t.Fatalf("list claim = %v ok=%v", claim, ok)
	}
	if _, ok := ServedClaimTokens("nothing relevant", [][]string{{"4110.67"}}); ok {
		t.Fatal("absent form must be not-applicable")
	}
	if _, ok := ServedClaimTokens("4110.67", nil); ok {
		t.Fatal("no forms must be not-applicable")
	}
}

// The claim-span gate accepts ANY grader-accepted form of a credited unit in
// the completions: a formatter that renders the model's "three" as "3", its
// "Lisboa" as "Lisbon", or its "went up" as "increase" is honest. A form the
// grader does not accept is never a group member, so a rewrite still fails.
func TestEvaluateClaimAcceptsAnyGraderFormPerUnit(t *testing.T) {
	number := ledgerFromStrings([]string{"how many?"}, []string{"Three of them."})
	got := EvaluateClaim("3", [][]string{{"3", "three"}}, number)
	if !got.Applicable || !got.ModelEmitted || got.AnswerInPrompt {
		t.Fatalf("number-word formatter = %+v, want applicable, model-emitted, not answer_in_prompt", got)
	}
	// Without the word form in the group, the same completion is a rewrite.
	if got := EvaluateClaim("3", [][]string{{"3"}}, number); got.ModelEmitted {
		t.Fatal("a digit the model never emitted in an accepted form must not be model-emitted")
	}
	alias := ledgerFromStrings([]string{"which city?"}, []string{"You moved to Lisboa."})
	if got := EvaluateClaim("Lisbon", [][]string{{"Lisbon", "Lisboa"}}, alias); !got.ModelEmitted {
		t.Fatal("an accept-set alias the model emitted must be model-emitted")
	}
	// A list is per unit: every credited item needs some accepted form emitted.
	list := ledgerFromStrings([]string{"which cities?"}, []string{"Ōsaka and Lima."})
	if got := EvaluateClaim("Osaka and Lima", [][]string{{"Osaka"}, {"Lima"}}, list); !got.ModelEmitted || got.ClaimTokens != 2 {
		t.Fatalf("diacritic-folded list = %+v", got)
	}
	partial := ledgerFromStrings([]string{"which cities?"}, []string{"Osaka."})
	if got := EvaluateClaim("Osaka and Lima", [][]string{{"Osaka"}, {"Lima"}}, partial); got.ModelEmitted {
		t.Fatal("a credited item the model never emitted must fail the claim-span gate")
	}
	// Direction: an accepted phrase in the completion is the model's answer;
	// an unlisted paraphrase is a rewrite.
	direction := ledgerFromStrings([]string{"up or down?"}, []string{"It went up after the revision."})
	if got := EvaluateClaim("increase", [][]string{{"increase", "went up", "rose"}}, direction); !got.ModelEmitted {
		t.Fatal("an accepted direction phrase must be model-emitted")
	}
	// The causal gate tests the SERVED form: "three" in the prompt does not
	// make a served "3" answer_in_prompt, but a served "3" in the prompt does.
	prompted := ledgerFromStrings([]string{"Give three examples."}, []string{"Three."})
	if got := EvaluateClaim("3", [][]string{{"3", "three"}}, prompted); got.AnswerInPrompt {
		t.Fatal("a number word in the template must not flag the served digit as answer_in_prompt")
	}
	planted := ledgerFromStrings([]string{"Reply exactly: 3"}, []string{"3"})
	if got := EvaluateClaim("3", [][]string{{"3", "three"}}, planted); !got.AnswerInPrompt {
		t.Fatal("a served digit authored into the prompt must flag answer_in_prompt")
	}
}

func ledgerFromStrings(harness, completion []string) *ClaimSpanLedger {
	ledger := NewClaimSpanLedger()
	ledger.RecordCall(harness, completion)
	return ledger
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
	session := make(TokenSet)
	for _, prior := range vec.SessionCompletions {
		session.AddSpan(prior)
	}
	for _, call := range vec.Calls {
		spans := make([]RequestSpan, 0, len(call.Harness)+len(call.Assistant))
		for _, span := range call.Harness {
			spans = append(spans, RequestSpan{Text: span})
		}
		for _, span := range call.Assistant {
			spans = append(spans, RequestSpan{Text: span, Assistant: true})
		}
		ledger.RecordRequest(spans, call.Completion, session)
		for _, span := range call.Completion {
			session.AddSpan(span)
		}
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
	if len(grade.V13ProvenanceBank) < 21 {
		t.Fatalf("bank has %d vectors; the published set has at least 21", len(grade.V13ProvenanceBank))
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
			records.AddSpan(r)
		}
		question := make(TokenSet)
		question.AddSpan(vec.Case.Question)
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

// Assistant-role spans are tested against the session-wide completion set: a
// carried model turn from another case is model-derived, a fabricated prefill
// carrying a value no completion ever produced is harness-first.
func TestClaimSpanLedgerAssistantRoleUsesSessionCompletions(t *testing.T) {
	session := make(TokenSet)
	session.AddSpan("Summary: Atlas has 4110.67 outstanding.")
	carried := NewClaimSpanLedger()
	carried.RecordRequest([]RequestSpan{
		{Text: "What is outstanding on Atlas?"},
		{Text: "Earlier I told you: 4110.67 outstanding.", Assistant: true},
	}, []string{"$4,110.67"}, session)
	if carried.HarnessFirst.Has("4110.67") {
		t.Fatal("an assistant turn the model produced earlier in the session must not be harness-first")
	}
	if !carried.HarnessFirst.Has("atlas") {
		t.Fatal("non-assistant template tokens stay harness-first")
	}
	// The same text under a USER role is harness-authored regardless of history.
	user := NewClaimSpanLedger()
	user.RecordRequest([]RequestSpan{{Text: "Earlier I told you: 4110.67 outstanding."}}, []string{"$4,110.67"}, session)
	if !user.HarnessFirst.Has("4110.67") {
		t.Fatal("a user-role span is harness-first even when the session saw the value")
	}
	// A fabricated prefill: no completion anywhere produced the value.
	prefill := NewClaimSpanLedger()
	prefill.RecordRequest([]RequestSpan{{Text: "The balance is 4110.67.", Assistant: true}}, []string{"4110.67"}, TokenSet{})
	if !prefill.HarnessFirst.Has("4110.67") {
		t.Fatal("an assistant prefill carrying a never-emitted value must be harness-first")
	}
	// nil history is accepted.
	nilHistory := NewClaimSpanLedger()
	nilHistory.RecordRequest([]RequestSpan{{Text: "prefill 4110.67", Assistant: true}}, nil, nil)
	if !nilHistory.HarnessFirst.Has("4110.67") || nilHistory.Completions != 1 {
		t.Fatalf("nil session history: %+v", nilHistory)
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
