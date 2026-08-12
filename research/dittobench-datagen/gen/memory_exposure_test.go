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
