package v9base

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Go produces this packet; Python ingests it and binds its digest into a real
// sr25519 signature. V13's existing fixtures are never regenerated here.
func TestV14SignedBasePacketGolden(t *testing.T) {
	perCase := []protocol.CaseScore{
		{Kind: protocol.KindMemory, Category: gen.QTDeclarativeAck, Correct: true, Observed: true, Called: []string{"save_memory"}},
		{Kind: protocol.KindMemory, Category: "single-session-recall", Correct: true, Observed: true, Called: []string{"gmail_send"}},
	}
	in := validInput(t)
	in.RunID, in.BenchVersion = "run-v14-vector", protocol.BenchVersionV14
	in.Ordinary.Composite = scorer.CompositeGateForVersion(perCase, in.BenchVersion)
	if in.Ordinary.Composite != .875 {
		t.Fatalf("v14 ordinary composite = %v", in.Ordinary.Composite)
	}
	gates, err := BuildGateEvidence(in.BenchVersion, perCase, AggregateModelTelemetry{ObservedRequests: 2, SuccessfulRequests: 2, PromptTokens: 120, CompletionTokens: 40, TelemetryComplete: true, DistinctCaseAttributionComplete: true, SuccessfulDistinctCases: 2}, true, ModelDependenceTelemetry{EligibleCases: 2, DependentCases: 2, TelemetryComplete: true, SliceAttributionComplete: true})
	if err != nil {
		t.Fatal(err)
	}
	in.Gates, err = scoregates.AttachClaimProvenance(gates, scoregates.ClaimProvenanceInput{
		AdministeredCases: 2, EligibleCases: 2, Posture: scoregates.ClaimProvenanceShadow, TelemetryComplete: true, AttributionComplete: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	details, digest, _, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := CanonicalBytes(details)
	if err != nil {
		t.Fatal(err)
	}
	got, err := json.MarshalIndent(map[string]any{
		"details": details, "base_evidence_sha256": digest, "base_canonical": string(canonical),
	}, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	path := "../../testdata/v14_base_contract_vector.json"
	if os.Getenv("V14_UPDATE_GOLDEN") == "1" {
		if err := os.WriteFile(path, got, 0644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("v14 signed packet drift:\n%s", got)
	}
}

// The dueling bench (v16) signs its datagen half with the inherited gate stack:
// the score-gate upper bound must admit it, and the root must validate.
func TestDuelingBenchSignsTheInheritedBaseEvidence(t *testing.T) {
	perCase := []protocol.CaseScore{
		{Kind: protocol.KindMemory, Category: gen.QTDeclarativeAck, Correct: true, Observed: true, Called: []string{"save_memory"}},
		{Kind: protocol.KindMemory, Category: "single-session-recall", Correct: true, Observed: true},
	}
	in := validInput(t)
	in.RunID, in.BenchVersion = "run-v16", protocol.DuelingMinBenchVersion
	gates, err := BuildGateEvidence(in.BenchVersion, perCase, AggregateModelTelemetry{ObservedRequests: 2, SuccessfulRequests: 2, PromptTokens: 120, CompletionTokens: 40, TelemetryComplete: true, DistinctCaseAttributionComplete: true, SuccessfulDistinctCases: 2}, true, ModelDependenceTelemetry{TelemetryComplete: true})
	if err != nil {
		t.Fatal(err)
	}
	in.Gates, err = scoregates.AttachClaimProvenance(gates, scoregates.ClaimProvenanceInput{
		AdministeredCases: 2, EligibleCases: 2, Posture: scoregates.ClaimProvenanceShadow, TelemetryComplete: true, AttributionComplete: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	details, _, _, err := Build(in)
	if err != nil {
		t.Fatalf("v16 base evidence refused: %v", err)
	}
	if details.BenchVersion != protocol.DuelingMinBenchVersion || details.ScoreGates.BenchVersion != protocol.DuelingMinBenchVersion {
		t.Fatalf("v16 root carries versions %d/%d", details.BenchVersion, details.ScoreGates.BenchVersion)
	}
	if err := Validate(details); err != nil {
		t.Fatal(err)
	}
}
