package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestContainsWholeAnswerUsesTokenBoundaries(t *testing.T) {
	for _, test := range []struct {
		evidence string
		answer   string
		want     bool
	}{
		{"the current stay is 8 days", "8", true},
		{"recorded in 2028", "8", false},
		{"send to person@example.com", "person@example.com", true},
		{"the balance is 70 7355", "707355", false},
	} {
		if got := containsWholeAnswer(test.evidence, test.answer); got != test.want {
			t.Errorf("containsWholeAnswer(%q, %q)=%v, want %v", test.evidence, test.answer, got, test.want)
		}
	}
}

func TestV10MemoryTransformationGateAcrossQualificationSeeds(t *testing.T) {
	profile, ok := ProfileForVersion("full", protocol.BenchVersionV10)
	if !ok {
		t.Fatal("missing v10 full profile")
	}
	for seed := int64(1); seed <= 20; seed++ {
		artifact, err := GenerateDataset(seed, profile, protocol.BenchVersionV10)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		result, err := AuditV10MemoryExposure(artifact)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if result.Eligible < 200 {
			t.Fatalf("seed %d audited only %d evidence-bound cases", seed, result.Eligible)
		}
		if result.TransformedShare() < 0.50 {
			t.Errorf("seed %d transformed share %.4f is below 0.50", seed, result.TransformedShare())
		}
		if result.VerbatimShare() >= 0.50 {
			t.Errorf("seed %d verbatim share %.4f is not below 0.50", seed, result.VerbatimShare())
		}
	}
}

// TestMemoryExposureAuditIsAVersionFloor: the audit applies to every contract
// carrying evidence bindings (v10+), so v13 is audited the day it exists, and
// pre-v10 artifacts are refused rather than silently scored as transformed.
func TestMemoryExposureAuditIsAVersionFloor(t *testing.T) {
	v9, _ := ProfileForVersion("full", protocol.BenchVersionV9)
	old, err := GenerateDataset(1, v9, protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := AuditMemoryExposure(old); err == nil {
		t.Fatal("v9 artifact without evidence bindings was audited")
	}
	profile, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	for seed := int64(1); seed <= 5; seed++ {
		artifact, err := GenerateDataset(seed, profile, protocol.BenchVersionV13)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		result, err := AuditMemoryExposure(artifact)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if result.Eligible < 200 {
			t.Fatalf("seed %d audited only %d evidence-bound v13 cases", seed, result.Eligible)
		}
	}
}
