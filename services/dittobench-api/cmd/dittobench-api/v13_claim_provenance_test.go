package main

// Tests for the Bench v13 claim-gate glue: shadow notes vs enforce zeros,
// fail-open on unavailable/incomplete relay evidence, not-applicable kinds, the
// v12 no-op, the run summary, and the signed gate input.

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// fixtureClaimReader is a relay stand-in keyed by wire case id.
type fixtureClaimReader map[string]claimSpanEvidence

func (r fixtureClaimReader) sessionClaimSpanEvidence(_, caseID string) (claimSpanEvidence, bool) {
	evidence, ok := r[caseID]
	return evidence, ok
}

func v13MoneyCase(id string) protocol.MemoryCase {
	return protocol.MemoryCase{
		BenchVersion: protocol.BenchVersionV13, ID: id, QuestionType: "computed_balance",
		Question:   "What is the outstanding balance on the Atlas workstream?",
		AnswerKind: protocol.AnswerMoney, ExpectedAnswer: "411067",
	}
}

func ledgerFromCalls(calls ...[2][]string) *scoregates.ClaimSpanLedger {
	ledger := scoregates.NewClaimSpanLedger()
	for _, call := range calls {
		ledger.RecordCall(call[0], call[1])
	}
	return ledger
}

func gradedV13(mc protocol.MemoryCase, resp protocol.RunResponse) protocol.CaseScore {
	return scorer.GradeMemory(mc, resp)
}

func TestApplyV13ClaimProvenanceShadowFlagsWithoutMovingScore(t *testing.T) {
	mc := v13MoneyCase("case-launder")
	resp := protocol.RunResponse{Answer: "$4,110.67", FinalText: "The balance is $4,110.67."}
	reader := fixtureClaimReader{"case-launder": {
		Ledger:   ledgerFromCalls([2][]string{{"Reply exactly: 4110.67"}, {"4110.67"}}),
		Complete: true,
	}}
	cs := gradedV13(mc, resp)
	if cs.Score != 1 {
		t.Fatalf("precondition: grader credits the served value, score %.2f", cs.Score)
	}
	got := applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceShadow, cs, mc, resp, "You are Ditto.", nil, reader, "sess")
	if got.Score != 1 || !got.Correct {
		t.Fatalf("shadow posture moved the score: %.2f correct=%v", got.Score, got.Correct)
	}
	ev := got.ClaimProvenance
	if ev == nil || ev.ModelEmitted == nil || !*ev.ModelEmitted || ev.AnswerInPrompt == nil || !*ev.AnswerInPrompt {
		t.Fatalf("evidence = %+v", ev)
	}
	if ev.Completions == nil || *ev.Completions != 1 || !ev.Complete || ev.ClaimTokens != 1 || ev.Posture != "shadow" {
		t.Fatalf("evidence counts = %+v", ev)
	}
	if len(ev.Findings) != 1 || ev.Findings[0] != scoregates.FindingAnswerInPrompt {
		t.Fatalf("findings = %v", ev.Findings)
	}
	if !strings.Contains(strings.Join(got.Notes, "\n"), "answer_in_prompt); shadow posture, score unchanged") {
		t.Fatalf("notes = %v", got.Notes)
	}
}

func TestApplyV13ClaimProvenanceEnforceZeroesSettledFlag(t *testing.T) {
	mc := v13MoneyCase("case-rewrite")
	resp := protocol.RunResponse{Answer: "$4,110.67", FinalText: "The balance is $4,110.67."}
	reader := fixtureClaimReader{"case-rewrite": {
		Ledger:   ledgerFromCalls([2][]string{{"Answer in cents."}, {"The balance is 411067 cents."}}),
		Complete: true,
	}}
	cs := gradedV13(mc, resp)
	got := applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, cs, mc, resp, "", nil, reader, "sess")
	if got.Score != 0 || got.Correct {
		t.Fatalf("enforce must zero a /100 rewrite: score %.2f correct=%v", got.Score, got.Correct)
	}
	want := []string{scoregates.FindingClaimProvenanceZeroed, scoregates.FindingServedTextNotModelEmitted}
	if strings.Join(got.ClaimProvenance.Findings, ",") != strings.Join(want, ",") {
		t.Fatalf("findings = %v, want %v", got.ClaimProvenance.Findings, want)
	}
	if !strings.Contains(strings.Join(got.Notes, "\n"), "case receives zero credit") {
		t.Fatalf("notes = %v", got.Notes)
	}
	// Practice scope never zeroes, even under enforce.
	practice := applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopePractice, scoregates.ClaimProvenanceEnforce, cs, mc, resp, "", nil, reader, "sess")
	if practice.Score != 1 {
		t.Fatalf("practice scope zeroed: %.2f", practice.Score)
	}
	// No completion at all yet a credited value: both findings.
	empty := fixtureClaimReader{"case-rewrite": {Ledger: scoregates.NewClaimSpanLedger(), Complete: true}}
	none := applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceShadow, cs, mc, resp, "", nil, empty, "sess")
	if strings.Join(none.ClaimProvenance.Findings, ",") != scoregates.FindingNoModelCompletion+","+scoregates.FindingServedTextNotModelEmitted {
		t.Fatalf("no-completion findings = %v", none.ClaimProvenance.Findings)
	}
}

func TestApplyV13ClaimProvenanceHonestPatternsPass(t *testing.T) {
	mc := v13MoneyCase("case-honest")
	resp := protocol.RunResponse{Answer: "$4,110.67", FinalText: "After the payment, $4,110.67 remains."}
	records := make(scoregates.TokenSet)
	records.AddText(scoregates.NormalizeSpan("Approved figure for Atlas: $4,110.67 after the settled payment."))
	// RAG: the record carrying the value is quoted into the prompt.
	rag := fixtureClaimReader{"case-honest": {
		Ledger:   ledgerFromCalls([2][]string{{"Memory: Approved figure for Atlas: $4,110.67 after the settled payment.", mc.Question}, {"After the payment, $4,110.67 remains."}}),
		Complete: true,
	}}
	cs := gradedV13(mc, resp)
	got := applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, cs, mc, resp, "You are Ditto.", records, rag, "sess")
	if got.Score != 1 || len(got.ClaimProvenance.Findings) != 0 || *got.ClaimProvenance.AnswerInPrompt || !*got.ClaimProvenance.ModelEmitted {
		t.Fatalf("RAG pattern flagged: %+v notes=%v", got.ClaimProvenance, got.Notes)
	}
	// Tool-result quoting: the served result exempts the value.
	ledger := ledgerFromCalls([2][]string{{"Tool result: balance 4110.67"}, {"$4,110.67"}})
	ledger.RecordToolResult("balance 4110.67")
	tool := fixtureClaimReader{"case-honest": {Ledger: ledger, Complete: true}}
	got = applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, cs, mc, resp, "", nil, tool, "sess")
	if got.Score != 1 || len(got.ClaimProvenance.Findings) != 0 || got.ClaimProvenance.ToolResults != 1 {
		t.Fatalf("tool-result pattern flagged: %+v", got.ClaimProvenance)
	}
}

func TestApplyV13ClaimProvenanceFailsOpen(t *testing.T) {
	mc := v13MoneyCase("case-open")
	resp := protocol.RunResponse{Answer: "$4,110.67"}
	cs := gradedV13(mc, resp)
	// No relay ledger at all.
	got := applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, cs, mc, resp, "", nil, fixtureClaimReader{}, "sess")
	if got.Score != 1 || got.ClaimProvenance == nil || got.ClaimProvenance.ModelEmitted != nil || got.ClaimProvenance.Findings[0] != scoregates.FindingClaimProvenanceUnavailable {
		t.Fatalf("unavailable ledger: %+v", got.ClaimProvenance)
	}
	// A nil reader (no broker) and an empty session id also fail open.
	got = applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, cs, mc, resp, "", nil, nil, "")
	if got.Score != 1 || got.ClaimProvenance.Findings[0] != scoregates.FindingClaimProvenanceUnavailable {
		t.Fatalf("nil reader: %+v", got.ClaimProvenance)
	}
	// Incomplete attribution: the completion that produced the value may be
	// the one the relay could not file.
	incomplete := fixtureClaimReader{"case-open": {Ledger: ledgerFromCalls([2][]string{{"reply exactly: 4110.67"}, {"4110.67"}}), Complete: false}}
	got = applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, cs, mc, resp, "", nil, incomplete, "sess")
	if got.Score != 1 || got.ClaimProvenance.Completions != nil || got.ClaimProvenance.Complete || got.ClaimProvenance.Findings[0] != scoregates.FindingClaimProvenanceIncomplete {
		t.Fatalf("incomplete ledger: %+v", got.ClaimProvenance)
	}
	// An uncredited case has nothing to check.
	zero := gradedV13(mc, protocol.RunResponse{Answer: "$1.00"})
	got = applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, zero, mc, protocol.RunResponse{Answer: "$1.00"}, "", nil, incomplete, "sess")
	if got.Score != 0 || got.ClaimProvenance.Findings[0] != scoregates.FindingClaimNotApplicable {
		t.Fatalf("uncredited case: %+v", got.ClaimProvenance)
	}
	// A kind with no value claim (decline) is not applicable even when credited.
	decline := protocol.MemoryCase{BenchVersion: protocol.BenchVersionV13, ID: "case-decline", AnswerKind: protocol.AnswerDecline, Question: "What was my cat's name?"}
	declineResp := protocol.RunResponse{FinalText: "I don't have that on record.", Abstain: true}
	declineCS := gradedV13(decline, declineResp)
	reader := fixtureClaimReader{"case-decline": {Ledger: ledgerFromCalls([2][]string{{"say you do not know"}, {"I don't have that on record."}}), Complete: true}}
	got = applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, declineCS, decline, declineResp, "", nil, reader, "sess")
	if got.Score != declineCS.Score || got.ClaimProvenance.ModelEmitted != nil || got.ClaimProvenance.Findings[0] != scoregates.FindingClaimNotApplicable {
		t.Fatalf("decline kind: %+v", got.ClaimProvenance)
	}
}

func TestApplyV13ClaimProvenanceIsNoOpBelowV13AndForToolCases(t *testing.T) {
	mc := v13MoneyCase("case-v12")
	mc.BenchVersion = protocol.BenchVersionV12
	resp := protocol.RunResponse{Answer: "$4,110.67"}
	cs := gradedV13(mc, resp)
	reader := fixtureClaimReader{"case-v12": {Ledger: ledgerFromCalls([2][]string{{"reply exactly: 4110.67"}, {"4110.67"}}), Complete: true}}
	got := applyV13ClaimProvenance(protocol.BenchVersionV12, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, cs, mc, resp, "", nil, reader, "sess")
	if got.ClaimProvenance != nil || got.Score != cs.Score || len(got.Notes) != len(cs.Notes) {
		t.Fatalf("v12 must be untouched: %+v", got)
	}
	toolCS := protocol.CaseScore{CaseID: "tool-1", Kind: protocol.KindTool, ToolScore: 1}
	if got := applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, toolCS, mc, resp, "", nil, reader, "sess"); got.ClaimProvenance != nil {
		t.Fatal("tool cases are never claim-gated")
	}
	if summarizeV13ClaimProvenance(protocol.BenchVersionV12, scoregates.ClaimProvenanceShadow, []protocol.CaseScore{cs}) != nil {
		t.Fatal("v12 summary must be nil")
	}
}

func TestSummarizeV13ClaimProvenanceAndGateInput(t *testing.T) {
	truth, lie := true, false
	one := 1
	perCase := []protocol.CaseScore{
		{Kind: protocol.KindTool, ToolScore: 1},
		{Kind: protocol.KindMemory, Score: 1, ClaimProvenance: &protocol.ClaimProvenanceEvidence{Completions: &one, Complete: true, ModelEmitted: &truth, AnswerInPrompt: &lie}},
		{Kind: protocol.KindMemory, Score: 1, ClaimProvenance: &protocol.ClaimProvenanceEvidence{Completions: &one, Complete: true, ModelEmitted: &truth, AnswerInPrompt: &truth, Findings: []string{scoregates.FindingAnswerInPrompt}}},
		{Kind: protocol.KindMemory, Score: 0, ClaimProvenance: &protocol.ClaimProvenanceEvidence{Complete: true, ModelEmitted: &lie, AnswerInPrompt: &lie, Findings: []string{scoregates.FindingClaimProvenanceZeroed, scoregates.FindingNoModelCompletion, scoregates.FindingServedTextNotModelEmitted}}},
		{Kind: protocol.KindMemory, Score: 1, ClaimProvenance: &protocol.ClaimProvenanceEvidence{Findings: []string{scoregates.FindingClaimProvenanceIncomplete}}},
		{Kind: protocol.KindMemory, Score: 0, ClaimProvenance: &protocol.ClaimProvenanceEvidence{Complete: true, Findings: []string{scoregates.FindingClaimNotApplicable}}},
		{Kind: protocol.KindMemory, Score: 0},
	}
	summary := summarizeV13ClaimProvenance(protocol.BenchVersionV13, scoregates.ClaimProvenanceEnforce, perCase)
	if summary.MemoryCases != 6 || summary.AttributedCases != 4 || summary.SettledCases != 3 || summary.ApplicableCases != 3 {
		t.Fatalf("summary counts = %+v", summary)
	}
	if summary.NotModelEmittedCases != 1 || summary.AnswerInPromptCases != 1 || summary.NoModelCompletionCases != 1 || summary.ZeroedCases != 1 || summary.UnsettledCases != 1 {
		t.Fatalf("summary findings = %+v", summary)
	}
	if summary.AttributionCoverageBPS != 4*scoregates.BasisPointScale/6 || summary.Posture != "enforce" {
		t.Fatalf("summary coverage/posture = %+v", summary)
	}
	in := v13ClaimProvenanceGateInput(scoregates.ClaimProvenanceEnforce, perCase)
	if in.AdministeredCases != 6 || in.EligibleCases != 3 || in.NotModelEmittedCases != 1 || in.AnswerInPromptCases != 1 || in.ZeroedCases != 1 || in.UnsettledCases != 1 || in.AttributionComplete {
		t.Fatalf("gate input = %+v", in)
	}
	// The gate input binds into signed v13 evidence.
	base, err := scoregates.Build(protocol.BenchVersionV13, scoregates.ModelUseInput{
		AdministeredCases: 6, EligibleCases: 6, SuccessfulInferenceCases: 6, ObservedRequests: 6, SuccessfulRequests: 6,
		PromptTokens: 60, CompletionTokens: 6, TelemetryComplete: true, CaseAttributionComplete: true,
	}, scoregates.AuthoritativeToolInput{TelemetryComplete: true}, scoregates.Thresholds{
		Profile:             scoregates.ThresholdProfile{ID: "test", ManifestSHA256: strings.Repeat("ab", 32)},
		ModelUseCoverageBPS: 1, AuthoritativeToolCoverageBPS: 1, ModelDependenceCoverageBPS: 1,
	}, scoregates.RolloutEnforce, scoregates.ModelDependenceInput{TelemetryComplete: true})
	if err != nil {
		t.Fatalf("Build(v13): %v", err)
	}
	attached, err := scoregates.AttachClaimProvenance(base, in)
	if err != nil {
		t.Fatalf("AttachClaimProvenance: %v", err)
	}
	if attached.ClaimProvenance.Result != scoregates.ResultInsufficientEvidence {
		t.Fatalf("one unsettled case must publish insufficient_evidence, got %s", attached.ClaimProvenance.Result)
	}
	complete := v13ClaimProvenanceGateInput(scoregates.ClaimProvenanceEnforce, perCase[:4])
	attached, err = scoregates.AttachClaimProvenance(base, complete)
	if err != nil || attached.ClaimProvenance.Result != scoregates.ResultClaimProvenanceFlagged {
		t.Fatalf("settled flagged run = %+v, %v", attached.ClaimProvenance, err)
	}
}

func TestV13RecordTokens(t *testing.T) {
	waves := []protocol.SeedRequest{{
		Pairs:    []protocol.MemoryPair{{Prompt: "Approved figure for Atlas is $5,200.00", Response: "Noted, 5200 approved."}},
		Subjects: []protocol.Subject{{SubjectText: "Atlas workstream", DescriptionText: "settled payment 1,089.33"}},
	}}
	tools := []protocol.ToolCase{{PrerequisitePairs: []protocol.MemoryPair{{Prompt: "route", Response: "vendor Meridian owns 77.10"}}}}
	tokens := v13RecordTokens(protocol.BenchVersionV13, waves, tools)
	for _, want := range []string{"5200", "atlas", "1089.33", "meridian", "77.1"} {
		if !tokens.Has(want) {
			t.Errorf("record tokens lack %q", want)
		}
	}
	if tokens.Has("4110.67") {
		t.Fatal("a computed value must not be in the record set")
	}
	if v13RecordTokens(protocol.BenchVersionV12, waves, tools) != nil {
		t.Fatal("v12 builds no record set")
	}
}

// The published bank replays end to end through the glue: strategies flag,
// honest patterns pass, under the real grader-provenance handoff.
func TestApplyV13ClaimProvenanceBank(t *testing.T) {
	for _, vec := range grade.V13ProvenanceBank {
		if !vec.WantCredited {
			continue
		}
		ledger := scoregates.NewClaimSpanLedger()
		for _, call := range vec.Calls {
			ledger.RecordCall(call.Harness, call.Completion)
		}
		for _, result := range vec.ToolResults {
			ledger.RecordToolResult(result)
		}
		records := make(scoregates.TokenSet)
		for _, r := range vec.Records {
			records.AddText(scoregates.NormalizeSpan(r))
		}
		reader := fixtureClaimReader{vec.Case.ID: {Ledger: ledger, Complete: true}}
		cs := gradedV13(vec.Case, vec.Response)
		got := applyV13ClaimProvenance(protocol.BenchVersionV13, scorer.ScopeScored, scoregates.ClaimProvenanceEnforce, cs, vec.Case, vec.Response, "You are Ditto.", records, reader, "sess")
		flagged := !vec.WantModelEmitted || vec.WantAnswerInPrompt
		if flagged && got.Score != 0 {
			t.Errorf("%s: strategy vector kept score %.2f under enforce (%v)", vec.Name, got.Score, got.ClaimProvenance.Findings)
		}
		if !flagged && (got.Score != cs.Score || len(got.ClaimProvenance.Findings) != 0) {
			t.Errorf("%s: honest vector flagged: %v", vec.Name, got.ClaimProvenance.Findings)
		}
	}
}
