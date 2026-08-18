package main

// End-to-end proof of the Bench v12 inference-latency review gate (adversary
// review C2). It drives the real production assembly path —
// v12InferenceLatencyTelemetry -> v9base.AttachInferenceLatencyGate ->
// scoregates — the same code a v12 run executes, modeling the harness archetypes
// by their trusted per-case wall time:
//
//   - a modeled parser answers every scored case in a flat ~119ms, so nearly its
//     whole inference-required, model-reached population is sub-floor and the run
//     is flagged (composite zeroed under enforce, preserved-but-flagged under the
//     default review posture);
//   - a genuine agent's latency varies with case complexity (900-5000ms) and
//     clears the floor, so its sub-floor share is ~0 and it passes;
//   - tool-only / conversational cases never reached the model, so a fast
//     deterministic answer there is never counted eligible and never penalized;
//   - v9/v10/v11 assembly omits the gate entirely.

import (
	"fmt"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// latencyTranscripts builds per-case scores plus the trusted validator-side
// execution evidence a v12 run records: a wall time per case, and whether the
// case reached the model. Model-reached cases also carry a passing
// counterfactual verdict so the dependence gate does not confound the composite.
func latencyTranscripts(durationsMs []int64, modelReached []bool) ([]protocol.CaseScore, []transcriptCase) {
	n := len(durationsMs)
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	for i := 0; i < n; i++ {
		caseID := fmt.Sprintf("case-%d", i)
		perCase = append(perCase, protocol.CaseScore{CaseID: caseID})
		execution := runner.CaseExecution{TotalDurationMs: durationsMs[i]}
		if modelReached[i] {
			dependent := true
			execution.ModelInferenceObserved = true
			execution.ModelAttributionComplete = true
			execution.ModelCounterfactualObserved = true
			execution.ModelCounterfactualDependent = &dependent
		}
		transcripts = append(transcripts, transcriptCase{CaseID: caseID, Execution: execution})
	}
	return perCase, transcripts
}

// assembleV12Latency runs the real assembly: base + dependence gate, then the
// inference-latency gate, then the signed v9 base root. It returns the wire
// details and the effective (gate-applied) score.
func assembleV12Latency(t *testing.T, perCase []protocol.CaseScore, transcripts []transcriptCase, cfg v9base.LatencyGateConfig) (protocol.V9BaseDetails, scoregates.Score) {
	t.Helper()
	dependence := v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)
	gates, err := v9base.BuildGateEvidence(protocol.BenchVersionV12, perCase, passingModelTelemetry(len(perCase)), true, dependence...)
	if err != nil {
		t.Fatalf("BuildGateEvidence(v12) error = %v", err)
	}
	latency := v12InferenceLatencyTelemetry(protocol.BenchVersionV12, perCase, transcripts, cfg.FloorMS)
	gates, err = v9base.AttachInferenceLatencyGate(protocol.BenchVersionV12, gates, latency, cfg)
	if err != nil {
		t.Fatalf("AttachInferenceLatencyGate(v12) error = %v", err)
	}
	details, _, effective, err := v9base.Build(v9base.Inputs{
		RunID: "v12-latency-run", BenchVersion: protocol.BenchVersionV12,
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

func flatDurations(ms int64, n int) []int64 {
	out := make([]int64, n)
	for i := range out {
		out[i] = ms
	}
	return out
}

func allReached(n int) []bool {
	out := make([]bool, n)
	for i := range out {
		out[i] = true
	}
	return out
}

// (a) A modeled parser answering in a flat ~119ms is flagged. Under enforce the
// composite is zeroed; under the default review posture the composite is
// preserved but the run carries the signed latency_implausible flag.
func TestV12InferenceLatencyCatchesFlatParser(t *testing.T) {
	const cases = 12
	perCase, transcripts := latencyTranscripts(flatDurations(119, cases), allReached(cases))

	enforceCfg := v9base.DefaultLatencyGateConfig()
	enforceCfg.Posture = scoregates.LatencyEnforce
	details, effective := assembleV12Latency(t, perCase, transcripts, enforceCfg)

	lat := details.ScoreGates.InferenceLatency
	if lat == (protocol.V9InferenceLatencyGateEvidence{}) {
		t.Fatal("v12 evidence omitted the inference-latency gate")
	}
	if lat.EligibleCases != cases || lat.FlaggedCases != cases || lat.SubFloorBPS != scoregates.BasisPointScale {
		t.Fatalf("parser latency evidence = %+v, want all %d cases sub-floor", lat, cases)
	}
	if lat.Result != string(scoregates.ResultLatencyImplausible) || lat.FactorBPS != 0 {
		t.Fatalf("parser under enforce not gated: %+v", lat)
	}
	if details.ScoreGates.ModelUse.FactorBPS != scoregates.BasisPointScale ||
		details.ScoreGates.ModelDependence.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("parser should still satisfy model_use/dependence: %+v", details.ScoreGates)
	}
	if details.SemanticGateFactorBPS != 0 || effective != (scoregates.Score{}) {
		t.Fatalf("parser composite not zeroed under enforce: semantic=%d effective=%+v", details.SemanticGateFactorBPS, effective)
	}

	// Default review posture: flagged, but composite preserved.
	reviewDetails, reviewEffective := assembleV12Latency(t, perCase, transcripts, v9base.DefaultLatencyGateConfig())
	rlat := reviewDetails.ScoreGates.InferenceLatency
	if rlat.Result != string(scoregates.ResultLatencyImplausible) || rlat.Posture != string(scoregates.LatencyReview) || rlat.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("parser under review not flagged-non-zeroing: %+v", rlat)
	}
	if reviewDetails.SemanticGateFactorBPS != scoregates.BasisPointScale || reviewEffective.Composite != 0.997012 {
		t.Fatalf("review posture changed composite: semantic=%d effective=%+v", reviewDetails.SemanticGateFactorBPS, reviewEffective)
	}
}

// (b) A modeled honest agent whose latency varies with case complexity clears
// the floor and passes under enforce; its 0.997 composite is preserved.
func TestV12InferenceLatencyHonestAgentPasses(t *testing.T) {
	durations := []int64{920, 1500, 3100, 4800, 900, 2600, 5000, 1100, 3900, 1750, 2200, 4400}
	reached := allReached(len(durations))
	perCase, transcripts := latencyTranscripts(durations, reached)

	cfg := v9base.DefaultLatencyGateConfig()
	cfg.Posture = scoregates.LatencyEnforce
	details, effective := assembleV12Latency(t, perCase, transcripts, cfg)

	lat := details.ScoreGates.InferenceLatency
	if lat.EligibleCases != len(durations) || lat.FlaggedCases != 0 || lat.SubFloorBPS != 0 {
		t.Fatalf("honest agent latency evidence = %+v, want 0 sub-floor", lat)
	}
	if lat.Result != string(scoregates.ResultPassed) || lat.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("honest agent not passed: %+v", lat)
	}
	if details.SemanticGateFactorBPS != scoregates.BasisPointScale || effective.Composite != 0.997012 {
		t.Fatalf("honest agent composite not preserved: semantic=%d effective=%+v", details.SemanticGateFactorBPS, effective)
	}
}

// (c) Tool-only / conversational cases never reached the model, so their fast
// deterministic wall time is not counted eligible and the LATENCY gate cannot
// flag them — even under enforce with an aggressive floor. The telemetry step is
// the single place this exclusion happens, so it is asserted directly (a run
// with zero model-reached cases is legitimately governed by model_use /
// dependence for its composite; latency must simply stay inert).
func TestV12InferenceLatencyToolOnlyNotPenalized(t *testing.T) {
	// Every case is fast (100ms) but NONE reached the model.
	const cases = 8
	perCase, transcripts := latencyTranscripts(flatDurations(100, cases), make([]bool, cases))

	cfg := v9base.DefaultLatencyGateConfig()
	cfg.Posture = scoregates.LatencyEnforce
	telem := v12InferenceLatencyTelemetry(protocol.BenchVersionV12, perCase, transcripts, cfg.FloorMS)
	if telem.EligibleCases != 0 || telem.FlaggedCases != 0 {
		t.Fatalf("tool-only cases counted eligible/flagged: %+v", telem)
	}

	gates, err := v9base.BuildGateEvidence(protocol.BenchVersionV12, perCase, passingModelTelemetry(cases), true,
		v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)...)
	if err != nil {
		t.Fatalf("BuildGateEvidence error = %v", err)
	}
	gates, err = v9base.AttachInferenceLatencyGate(protocol.BenchVersionV12, gates, telem, cfg)
	if err != nil {
		t.Fatalf("AttachInferenceLatencyGate error = %v", err)
	}
	lat := gates.InferenceLatency
	if lat.Result != scoregates.ResultNotApplicable || lat.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("tool-only run penalized by latency gate: %+v", lat)
	}
}

// A mixed run: fast tool-only cases plus honest model-reached cases. Only the
// model-reached cases are eligible; none is sub-floor, so the run passes and the
// fast tool cases do not drag it into the flagged band.
func TestV12InferenceLatencyMixedPopulation(t *testing.T) {
	durations := []int64{80, 90, 1200, 3400, 110, 2600, 4800, 950}
	reached := []bool{false, false, true, true, false, true, true, true}
	perCase, transcripts := latencyTranscripts(durations, reached)

	cfg := v9base.DefaultLatencyGateConfig()
	cfg.Posture = scoregates.LatencyEnforce
	details, effective := assembleV12Latency(t, perCase, transcripts, cfg)

	lat := details.ScoreGates.InferenceLatency
	if lat.EligibleCases != 5 || lat.FlaggedCases != 0 {
		t.Fatalf("mixed population eligibility wrong: %+v (want 5 eligible, 0 flagged)", lat)
	}
	if lat.Result != string(scoregates.ResultPassed) || effective.Composite != 0.997012 {
		t.Fatalf("mixed honest run not preserved: %+v effective=%+v", lat, effective)
	}
}

// (d) v9/v10/v11 assembly must not emit the inference-latency gate and must
// leave the composite untouched, proving the gate is inert before v12.
func TestPreV12AssemblyOmitsInferenceLatencyGate(t *testing.T) {
	const cases = 6
	perCase, transcripts := latencyTranscripts(flatDurations(50, cases), allReached(cases))
	cfg := v9base.DefaultLatencyGateConfig()
	cfg.Posture = scoregates.LatencyEnforce

	for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV10, protocol.BenchVersionV11} {
		// Telemetry is a no-op before v12.
		if telem := v12InferenceLatencyTelemetry(version, perCase, transcripts, cfg.FloorMS); telem != (v9base.InferenceLatencyTelemetry{}) {
			t.Fatalf("v%d emitted latency telemetry: %+v", version, telem)
		}
		gates, err := v9base.BuildGateEvidence(version, perCase, passingModelTelemetry(cases), true)
		if err != nil {
			t.Fatalf("BuildGateEvidence(v%d) error = %v", version, err)
		}
		// Attach is a no-op before v12; the flat 50ms population must NOT gate.
		gates, err = v9base.AttachInferenceLatencyGate(version, gates, v12InferenceLatencyTelemetry(version, perCase, transcripts, cfg.FloorMS), cfg)
		if err != nil {
			t.Fatalf("AttachInferenceLatencyGate(v%d) error = %v", version, err)
		}
		if gates.InferenceLatency != (scoregates.InferenceLatencyEvidence{}) {
			t.Fatalf("v%d carried inference-latency evidence: %+v", version, gates.InferenceLatency)
		}
		details, _, effective, err := v9base.Build(v9base.Inputs{
			RunID: "prev12-latency", BenchVersion: version,
			ArtifactSHA256:   "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			DatasetSHA256:    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
			TranscriptSHA256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
			Ordinary:         scoregates.Score{Composite: 0.5, CompositeStderr: 0.05},
			Gates:            gates,
		})
		if err != nil {
			t.Fatalf("v9base.Build(v%d) error = %v", version, err)
		}
		if details.ScoreGates.InferenceLatency != (protocol.V9InferenceLatencyGateEvidence{}) {
			t.Fatalf("v%d wire evidence carried inference_latency", version)
		}
		if effective.Composite != 0.5 {
			t.Fatalf("v%d composite changed by latency gate: %v", version, effective.Composite)
		}
	}
}
