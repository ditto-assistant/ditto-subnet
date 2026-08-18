package main

// End-to-end proof of the Bench v12 causal model-dependence gate (issue #532).
//
// It models the two harness archetypes by whether their correctness survives
// FULL model ablation and drives the real production assembly path —
// v9DependenceTelemetryForVersion -> v9base.BuildGateEvidence -> v9base.Build —
// the same code a v12 run executes:
//
//   - a compute-then-launder harness recovers the correct answer through a
//     non-model path, so it stays CORRECT under ablation: zero dependent cases ->
//     the dependence gate zeroes the composite even though model_use and
//     authoritative_tool both pass and the ordinary score is 0.997;
//   - a genuine agent's correctness COLLAPSES under ablation, so every
//     clean-correct slice case is dependent -> the gate passes and the composite
//     is preserved.
//
// It also proves v9/v10/v11 assembly is byte-identical: no dependence telemetry
// is emitted and the composite is untouched.
//
// Run: go test ./cmd/dittobench-api/ -run TestV12ModelDependenceGate -v -count=1

import (
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// laundererCorrect models a compute-then-launder harness: its non-model path
// recovers the correct answer whether or not the model was ablated, so clean and
// ablated correctness are IDENTICAL -> not dependent.
func laundererCorrect(cleanCorrect, _ bool) bool { return cleanCorrect }

// genuineCorrect models a genuine agent: correct under the clean run, but its
// correctness collapses to false once the model is fully ablated -> dependent.
func genuineCorrect(cleanCorrect, ablated bool) bool { return cleanCorrect && ablated }

// dependenceTranscripts builds the trusted per-case counterfactual verdicts a
// v12 relay records: for each clean-correct model-reached slice case it records
// whether correctness survived full model ablation (dependent == it did NOT).
func dependenceTranscripts(correct func(cleanCorrect, ablated bool) bool, cases int) ([]protocol.CaseScore, []transcriptCase) {
	perCase := make([]protocol.CaseScore, 0, cases)
	transcripts := make([]transcriptCase, 0, cases)
	for i := 0; i < cases; i++ {
		caseID := "case-" + string(rune('a'+i))
		// Every case's clean run is correct; the ablated correctness differs by
		// archetype. Dependent == clean correct AND ablated incorrect.
		dependent := correct(true, true) && !correct(true, false)

		perCase = append(perCase, protocol.CaseScore{CaseID: caseID})
		transcripts = append(transcripts, transcriptCase{
			CaseID: caseID,
			Execution: runner.CaseExecution{
				ModelInferenceObserved:       true,
				ModelAttributionComplete:     true,
				ModelCounterfactualObserved:  true,
				ModelCounterfactualDependent: &dependent,
			},
		})
	}
	return perCase, transcripts
}

// passingModelTelemetry makes model_use and authoritative_tool both pass so the
// only thing separating the two harnesses is the causal-dependence gate.
func passingModelTelemetry(cases int) v9base.AggregateModelTelemetry {
	return v9base.AggregateModelTelemetry{
		ObservedRequests: uint64(cases), SuccessfulRequests: uint64(cases),
		PromptTokens: uint64(cases * 100), CompletionTokens: uint64(cases * 20),
		TelemetryComplete:               true,
		DistinctCaseAttributionComplete: true,
		SuccessfulDistinctCases:         cases,
	}
}

func assembleV12(t *testing.T, correct func(cleanCorrect, ablated bool) bool, cases int) (protocol.V9BaseDetails, scoregates.Score) {
	t.Helper()
	perCase, transcripts := dependenceTranscripts(correct, cases)
	dependence := v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)
	gates, err := v9base.BuildGateEvidence(protocol.BenchVersionV12, perCase, passingModelTelemetry(cases), true, dependence...)
	if err != nil {
		t.Fatalf("BuildGateEvidence(v12) error = %v", err)
	}
	details, _, effective, err := v9base.Build(v9base.Inputs{
		RunID: "v12-run", BenchVersion: protocol.BenchVersionV12,
		ArtifactSHA256:   "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		DatasetSHA256:    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		TranscriptSHA256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		Ordinary:         scoregates.Score{Composite: 0.997012, CompositeStderr: 0.01},
		Gates:            gates,
	})
	if err != nil {
		t.Fatalf("v9base.Build(v12) error = %v", err)
	}
	if err := v9base.Validate(details); err != nil {
		t.Fatalf("v9base.Validate(v12) error = %v", err)
	}
	return details, effective
}

func TestV12ModelDependenceGateCatchesKVParser(t *testing.T) {
	const cases = 12

	// A modeled KV-parser: model_use and authoritative_tool pass, but no answer
	// moves under the counterfactual, so the dependence gate zeroes the composite.
	kvDetails, kvEffective := assembleV12(t, laundererCorrect, cases)
	dep := kvDetails.ScoreGates.ModelDependence
	if dep == (protocol.V9ModelDependenceGateEvidence{}) {
		t.Fatal("v12 evidence omitted the model-dependence gate")
	}
	if kvDetails.ScoreGates.ModelUse.FactorBPS != scoregates.BasisPointScale ||
		kvDetails.ScoreGates.AuthoritativeTool.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("KV-parser should still satisfy model_use/tool: model=%d tool=%d",
			kvDetails.ScoreGates.ModelUse.FactorBPS, kvDetails.ScoreGates.AuthoritativeTool.FactorBPS)
	}
	if dep.DependentCases != 0 || dep.DependenceBPS != 0 ||
		dep.Result != string(scoregates.ResultBelowThreshold) || dep.FactorBPS != 0 {
		t.Fatalf("KV-parser dependence not gated: %+v", dep)
	}
	if kvDetails.SemanticGateFactorBPS != 0 || kvDetails.AppliedGateFactorBPS != 0 {
		t.Fatalf("KV-parser composite not zeroed: semantic=%d applied=%d",
			kvDetails.SemanticGateFactorBPS, kvDetails.AppliedGateFactorBPS)
	}
	if kvEffective != (scoregates.Score{}) {
		t.Fatalf("KV-parser effective score = %+v, want zero", kvEffective)
	}

	// A genuine agent: every answer tracks the completion, so the gate passes and
	// the 0.997 composite is preserved.
	genDetails, genEffective := assembleV12(t, genuineCorrect, cases)
	gdep := genDetails.ScoreGates.ModelDependence
	if gdep.DependentCases != cases || gdep.Result != string(scoregates.ResultPassed) || gdep.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("genuine agent dependence did not pass: %+v", gdep)
	}
	if genDetails.SemanticGateFactorBPS != scoregates.BasisPointScale ||
		genDetails.AppliedGateFactorBPS != scoregates.BasisPointScale {
		t.Fatalf("genuine agent composite not preserved: semantic=%d applied=%d",
			genDetails.SemanticGateFactorBPS, genDetails.AppliedGateFactorBPS)
	}
	if genEffective.Composite != 0.997012 {
		t.Fatalf("genuine agent effective composite = %v, want 0.997012", genEffective.Composite)
	}
}

// Until the relay populates the per-case counterfactual verdict, the v12 gate
// must fail closed to a signed insufficient_evidence 0.00 rather than pass.
func TestV12ModelDependenceFailsClosedWithoutRelayEvidence(t *testing.T) {
	const cases = 6
	perCase := make([]protocol.CaseScore, 0, cases)
	transcripts := make([]transcriptCase, 0, cases)
	for i := 0; i < cases; i++ {
		caseID := "case-" + string(rune('a'+i))
		perCase = append(perCase, protocol.CaseScore{CaseID: caseID})
		// Model-reached but NO counterfactual verdict recorded (relay not integrated).
		transcripts = append(transcripts, transcriptCase{
			CaseID:    caseID,
			Execution: runner.CaseExecution{ModelInferenceObserved: true, ModelAttributionComplete: true},
		})
	}
	dependence := v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)
	if len(dependence) != 1 || dependence[0].SliceAttributionComplete {
		t.Fatalf("missing relay verdicts must leave slice attribution incomplete: %+v", dependence)
	}
	gates, err := v9base.BuildGateEvidence(protocol.BenchVersionV12, perCase, passingModelTelemetry(cases), true, dependence...)
	if err != nil {
		t.Fatalf("BuildGateEvidence error = %v", err)
	}
	if gates.ModelDependence.Result != scoregates.ResultInsufficientEvidence || gates.ModelDependence.FactorBPS != 0 {
		t.Fatalf("uninstrumented v12 run not fail-closed: %+v", gates.ModelDependence)
	}
	factor, err := gates.CombinedFactorBPS()
	if err != nil || factor != 0 {
		t.Fatalf("CombinedFactorBPS = %d, %v; want 0, nil", factor, err)
	}
}

// v9/v10/v11 assembly must not emit dependence telemetry and must leave the
// composite untouched — proving the gate is inert before v12.
func TestPreV12AssemblyOmitsDependenceGate(t *testing.T) {
	const cases = 4
	perCase, transcripts := dependenceTranscripts(genuineCorrect, cases)
	for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV10, protocol.BenchVersionV11} {
		if dep := v9DependenceTelemetryForVersion(version, perCase, transcripts); dep != nil {
			t.Fatalf("v%d emitted dependence telemetry: %+v", version, dep)
		}
		gates, err := v9base.BuildGateEvidence(version, perCase, passingModelTelemetry(cases), true)
		if err != nil {
			t.Fatalf("BuildGateEvidence(v%d) error = %v", version, err)
		}
		if gates.ModelDependence != (scoregates.ModelDependenceEvidence{}) {
			t.Fatalf("v%d carried dependence evidence: %+v", version, gates.ModelDependence)
		}
		details, _, effective, err := v9base.Build(v9base.Inputs{
			RunID: "prev12", BenchVersion: version,
			ArtifactSHA256:   "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			DatasetSHA256:    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
			TranscriptSHA256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
			Ordinary:         scoregates.Score{Composite: 0.5, CompositeStderr: 0.05},
			Gates:            gates,
		})
		if err != nil {
			t.Fatalf("v9base.Build(v%d) error = %v", version, err)
		}
		if details.ScoreGates.ModelDependence != (protocol.V9ModelDependenceGateEvidence{}) {
			t.Fatalf("v%d wire evidence carried model_dependence", version)
		}
		if effective.Composite != 0.5 {
			t.Fatalf("v%d composite changed: %v", version, effective.Composite)
		}
	}
}
