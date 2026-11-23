package main

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestMemoryFactorRecordedFromBench16Only(t *testing.T) {
	if memoryFactorFor(15, nil, nil) != nil {
		t.Fatal("bench 15 recorded a memory factor")
	}
	summary := memoryFactorFor(18, nil, nil)
	if summary == nil || summary.Posture != scoregates.MemoryFactorPostureShadow ||
		summary.Result != scoregates.MemoryFactorResultNoTelemetry || summary.Applied {
		t.Fatalf("bench 18 without a sampler = %+v", summary)
	}
	if memoryFactorFor(18, &benchmarkRuntimeSettings{MemoryFactorPosture: "off"}, nil) != nil {
		t.Fatal("off posture recorded a block")
	}
	// The lease's cap stands in when no sampler reported one.
	capped := memoryFactorFor(18, &benchmarkRuntimeSettings{SandboxMemoryLimitMiB: 2048}, nil)
	if capped.LimitBytes != 2048<<20 {
		t.Fatalf("lease cap not recorded: %+v", capped)
	}
}

func TestLeasePolicyOverridesTheDefaults(t *testing.T) {
	free := 0
	policy := (&benchmarkRuntimeSettings{
		MemoryFactorPosture:  "enforce",
		MemoryFactorFreeMiB:  &free,
		MemoryFactorFloorBPS: 8000,
	}).memoryFactorPolicy()
	if policy.Posture != "enforce" || policy.FreeMiB != 0 || policy.FloorBPS != 8000 {
		t.Fatalf("policy = %+v", policy)
	}
	if got := (*benchmarkRuntimeSettings)(nil).memoryFactorPolicy(); got != scoregates.DefaultMemoryFactorPolicy() {
		t.Fatalf("nil runtime policy = %+v", got)
	}
}

func TestAppliedMemoryFactorScalesTheGateAndComposite(t *testing.T) {
	applied := &protocol.MemoryFactorSummary{Applied: true, AppliedFactorBPS: 9000}
	shadow := &protocol.MemoryFactorSummary{Applied: false, FactorBPS: 9000, AppliedFactorBPS: 10000}
	half := scorer.DatagenHalf{Raw: 0.8, Gate: 0.5}
	if got := applyMemoryFactorToHalf(half, applied).Gate; got != 0.45 {
		t.Fatalf("enforced gate = %v", got)
	}
	if got := applyMemoryFactorToHalf(half, shadow).Gate; got != 0.5 {
		t.Fatalf("shadow changed the gate: %v", got)
	}
	report := protocol.ScoreReport{Composite: 0.7, CompositeStderr: 0.02}
	if got := applyMemoryFactorToReport(report, applied); got.Composite != 0.63 || got.CompositeStderr != 0.018 {
		t.Fatalf("enforced report = %+v", got)
	}
	if got := applyMemoryFactorToReport(report, nil); got.Composite != 0.7 {
		t.Fatalf("nil block changed the composite: %+v", got)
	}
}

func TestBenchmarkRuntimeRejectsABadMemoryFactorPolicy(t *testing.T) {
	base := func() *benchmarkRuntimeSettings {
		return &benchmarkRuntimeSettings{CaseConcurrency: 4, RelayDelayFingerprintMode: delayFingerprintOff}
	}
	if err := base().validate(18); err != nil {
		t.Fatalf("defaults rejected: %v", err)
	}
	bad := base()
	bad.MemoryFactorPosture = "maybe"
	if err := bad.validate(18); err == nil || !strings.Contains(err.Error(), "memory_factor_posture") {
		t.Fatalf("bad posture accepted: %v", err)
	}
	negative := -1
	bad = base()
	bad.MemoryFactorFreeMiB = &negative
	if err := bad.validate(18); err == nil || !strings.Contains(err.Error(), "memory_factor_free_mib") {
		t.Fatalf("negative free allowance accepted: %v", err)
	}
	bad = base()
	bad.MemoryFactorFloorBPS = 4000
	if err := bad.validate(18); err == nil || !strings.Contains(err.Error(), "memory_factor_floor_bps") {
		t.Fatalf("floor below 5000 accepted: %v", err)
	}
}
