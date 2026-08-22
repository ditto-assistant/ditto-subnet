package confirmationwire

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
)

func TestShadowHighDropIsCompletedNonCausalObservation(t *testing.T) {
	t.Parallel()
	inference, embedding, err := buildHighDropAblationEvidence(ablation.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	for _, evaluation := range []ablation.Evaluation{inference, embedding} {
		if evaluation.Evidence.Status != ablation.StatusFailed {
			t.Fatalf("status = %q, want failed", evaluation.Evidence.Status)
		}
		if evaluation.Evidence.Reason != ablation.ReasonObservationalDropNotCausal {
			t.Fatalf("reason = %q", evaluation.Evidence.Reason)
		}
		if evaluation.Evidence.BaselineScoresSHA256 == "" || evaluation.Evidence.AblatedScoresSHA256 == "" {
			t.Fatal("shadow observational drop must commit paired score digests")
		}
		if evaluation.Evidence.SemanticFactor != 0 || evaluation.Evidence.AppliedFactor != 1 {
			t.Fatalf("factors = semantic %v applied %v", evaluation.Evidence.SemanticFactor, evaluation.Evidence.AppliedFactor)
		}
	}
}

func TestCommittedUnavailableAblationMatchesProductionEvidence(t *testing.T) {
	t.Parallel()
	inference, embedding, err := buildHighDropAblationEvidence(ablation.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	want := struct {
		Inference DimensionFixture[ablation.Evidence] `json:"inference"`
		Embedding DimensionFixture[ablation.Evidence] `json:"embedding"`
	}{
		Inference: DimensionFixture[ablation.Evidence]{
			GoEvidenceSHA256: inference.SHA256,
			LatencyMS:        111,
			Evidence:         inference.Evidence,
		},
		Embedding: DimensionFixture[ablation.Evidence]{
			GoEvidenceSHA256: embedding.SHA256,
			LatencyMS:        222,
			Evidence:         embedding.Evidence,
		},
	}
	raw, err := json.MarshalIndent(want, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	raw = append(raw, '\n')
	path := filepath.Join("testdata", "go_ablation_observational_drop_v9.json")
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v\nwant:\n%s", path, err, raw)
	}
	if string(got) != string(raw) {
		t.Fatalf("%s drifted from production unavailable ablation evidence\nwant:\n%s", path, raw)
	}
}
