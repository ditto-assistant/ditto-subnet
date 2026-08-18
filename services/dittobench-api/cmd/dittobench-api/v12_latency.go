package main

// Integration point for the Bench v12 inference-latency review gate (adversary
// review C2). It is the latency analogue of v9DependenceTelemetryForVersion: for
// every inference-required, model-reached scored case it reads the trusted
// validator-side wall time (runner.CaseExecution.TotalDurationMs -- never miner
// self-report) and flags the case when that wall time is implausibly low for a
// real gpt-oss-20b completion. It then hands v9base an aggregate: administered /
// eligible / flagged counts plus the attribution trust bit.
//
// A KV-parser that emulates the model answers each case in a flat ~119ms, so
// nearly its whole eligible population is sub-floor and the run is flagged. A
// genuine agent's latency varies with case complexity and clears the floor, so
// its sub-floor share is near zero and it passes. Tool-only, conversational, and
// abstention cases never reach the model, so ExcludedFromModelPopulation and the
// ModelInferenceObserved check keep them out of the eligible population entirely
// -- a fast deterministic tool answer is never penalized.

import (
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// v12LatencyGateConfigFromEnv returns the compiled defaults with optional
// operator overrides, following the broker's envOr/envIntDefault conventions. An
// unknown posture falls back to the safe (review) default.
func v12LatencyGateConfigFromEnv() v9base.LatencyGateConfig {
	cfg := v9base.DefaultLatencyGateConfig()
	cfg.FloorMS = int64(envIntDefault("DITTOBENCH_V12_LATENCY_FLOOR_MS", int(cfg.FloorMS)))
	cfg.ShareBPS = envIntDefault("DITTOBENCH_V12_LATENCY_SHARE_BPS", cfg.ShareBPS)
	switch strings.ToLower(strings.TrimSpace(envOr("DITTOBENCH_V12_LATENCY_POSTURE", string(cfg.Posture)))) {
	case string(v9base.DefaultLatencyPosture):
		cfg.Posture = v9base.DefaultLatencyPosture
	case "enforce":
		cfg.Posture = "enforce"
	case "review":
		cfg.Posture = "review"
	}
	return cfg
}

// v12InferenceLatencyTelemetry computes the trusted per-case wall-time aggregate
// the v12 inference-latency gate consumes. It returns an empty aggregate for
// bench_version<12 (the attach path is a no-op there). Misaligned transcripts or
// an eligible case that carries no trusted wall time leave AttributionComplete
// false, so the gate fails OPEN rather than penalize an honest run.
func v12InferenceLatencyTelemetry(
	benchVersion int,
	perCase []protocol.CaseScore,
	transcripts []transcriptCase,
	floorMS int64,
) v9base.InferenceLatencyTelemetry {
	if benchVersion < protocol.BenchVersionV12 {
		return v9base.InferenceLatencyTelemetry{}
	}
	telemetry := v9base.InferenceLatencyTelemetry{
		AdministeredCases: len(perCase),
		TelemetryComplete: true,
	}
	if len(perCase) != len(transcripts) {
		// Misaligned transcripts cannot attribute per-case wall time; leave
		// attribution incomplete so the gate fails open.
		return telemetry
	}
	settled := true
	for index, score := range perCase {
		if v9base.ExcludedFromModelPopulation(score) {
			continue
		}
		execution := transcripts[index].Execution
		if !execution.ModelInferenceObserved {
			// A case that never reached the model is not part of the inference
			// slice: a fast deterministic answer there is legitimate.
			continue
		}
		if execution.TotalDurationMs <= 0 {
			// An eligible case with no trusted wall time cannot be judged; mark
			// attribution incomplete so the whole gate fails open.
			settled = false
			continue
		}
		telemetry.EligibleCases++
		if execution.TotalDurationMs < floorMS {
			telemetry.FlaggedCases++
		}
	}
	telemetry.AttributionComplete = settled
	return telemetry
}
