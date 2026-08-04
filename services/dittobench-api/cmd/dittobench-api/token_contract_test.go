package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/efficiency"
	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The v7 quality-only token contract at the pipeline level: an extreme
// audited usage never moves the v7 composite, while the report still carries
// both the audited usage block and the neutral token-efficiency record.
// v5 keeps the absolute transform; pre-v5 reports are untouched.
func TestApplyTokenContractV7QualityOnly(t *testing.T) {
	hugeUsage := protocol.TokenUsage{
		Status: "complete", Successes: 10, UsageAvailable: 10,
		PromptTokens: 50_000_000, CompletionTokens: 1_000_000, TotalTokens: 51_000_000,
		Provider: "openrouter", ProfileRevision: "openrouter-route-a471cd87ae7df5b9-v1",
		Model: "openai/gpt-oss-20b",
	}
	mkReport := func() protocol.ScoreReport {
		return protocol.ScoreReport{
			Composite: 0.8, CompositeStderr: 0.02,
			Details: &protocol.RunDetails{BenchVersion: 7, RunSize: "full", TokenUsage: &hugeUsage},
		}
	}

	// v7: composite and stderr unchanged; audited usage + neutral record carried.
	got := applyTokenContract(mkReport(), protocol.BenchVersionV7, "full", hugeUsage)
	if got.Composite != 0.8 || got.RawComposite != 0.8 || got.CompositeStderr != 0.02 {
		t.Fatalf("v7 composite must ignore token usage: %+v", got)
	}
	if got.Details.TokenUsage == nil || got.Details.TokenUsage.TotalTokens != hugeUsage.TotalTokens {
		t.Fatalf("v7 report must still carry the audited usage block")
	}
	te := got.Details.TokenEfficiency
	if te == nil || te.Multiplier != 1 || te.PenaltyApplied ||
		te.FormulaVersion != efficiency.V7QualityOnlyFormula ||
		te.DecisionReason != efficiency.V7QualityOnlyReason ||
		te.ObservedTotalTokens != hugeUsage.TotalTokens {
		t.Fatalf("v7 token record must be the neutral quality-only audit record: %+v", te)
	}

	// v5: the absolute transform path runs (here fail-neutral on baseline
	// identity, but the record documents the absolute contract).
	v5 := applyTokenContract(mkReport(), protocol.BenchVersionV5, "full", hugeUsage)
	if v5.Details.TokenEfficiency == nil || v5.Details.TokenEfficiency.FormulaVersion != efficiency.FormulaVersion {
		t.Fatalf("v5 must keep the absolute transform record: %+v", v5.Details.TokenEfficiency)
	}

	// pre-v5: untouched, no token record.
	v3 := applyTokenContract(mkReport(), protocol.BenchVersionV3, "full", hugeUsage)
	if v3.Composite != 0.8 || v3.Details.TokenEfficiency != nil {
		t.Fatalf("pre-v5 reports must not carry a token transform: %+v", v3)
	}
}

// End-to-end reconciliation proof: in a SAFE production config (SSRF guard on,
// allowPrivate=false) with NO validator-side activation flag, a v7 run the
// PLATFORM dispatches (bench_version=7 in the request) on a correct locked
// model + versioned route with trusted metered accounting — but NO reviewed
// token baseline for that route — is admitted at every gate and still produces
// a quality-only composite (usage recorded, multiplier 1). Rollback: the same
// validator scores whatever bench the platform dispatches, so setting the
// active bench back to 6 needs no code/env change.
func TestV7DispatchedByPlatformAdmitsAndScoresQualityOnly(t *testing.T) {
	// A route with NO reviewed baseline: correct model, well-formed versioned
	// route, trusted accounting. The relay identity gate admits it — no baseline,
	// no env var.
	snapshot := relayHealthSnapshot{
		AccountingVersion: 2, Status: "ok",
		Provider: "groq", ProfileRevision: "openrouter-route-0123456789abcdef-v1",
		Model: llm.V7HarnessModel,
	}
	if _, ok := efficiency.LookupForVersion(protocol.BenchVersionV7, "full", protocol.TokenUsage{
		Provider: snapshot.Provider, ProfileRevision: snapshot.ProfileRevision, Model: snapshot.Model,
	}); ok {
		t.Fatal("test route must have no reviewed baseline, to prove none is required")
	}
	if err := requireTokenAccounting(snapshot, protocol.BenchVersionV7, "full"); err != nil {
		t.Fatalf("safe-prod v7 admission rejected a correct route without a baseline: %v", err)
	}

	// Only the active v8 contract is admitted by request routing.
	if got, msg := requestedBenchVersion(protocol.BenchVersionV8); got != protocol.BenchVersionV8 || msg != "" {
		t.Fatalf("platform-dispatched v8 rejected: (%d, %q)", got, msg)
	}

	// The scored v7 composite is quality-only: extreme usage never moves it.
	usage := protocol.TokenUsage{
		Status: "complete", Successes: 10, UsageAvailable: 10,
		PromptTokens: 40_000_000, CompletionTokens: 2_000_000, TotalTokens: 42_000_000,
		Provider: snapshot.Provider, ProfileRevision: snapshot.ProfileRevision, Model: snapshot.Model,
	}
	report := protocol.ScoreReport{
		Composite: 0.73, CompositeStderr: 0.01,
		Details: &protocol.RunDetails{BenchVersion: 7, RunSize: "full", TokenUsage: &usage},
	}
	got := applyTokenContract(report, protocol.BenchVersionV7, "full", usage)
	if got.Composite != 0.73 || got.RawComposite != 0.73 {
		t.Fatalf("quality-only composite moved under a baseline-less route: %+v", got)
	}
	te := got.Details.TokenEfficiency
	if te == nil || te.Multiplier != 1 || te.PenaltyApplied ||
		te.FormulaVersion != efficiency.V7QualityOnlyFormula ||
		te.ObservedTotalTokens != usage.TotalTokens {
		t.Fatalf("expected neutral quality-only audit record: %+v", te)
	}
}
