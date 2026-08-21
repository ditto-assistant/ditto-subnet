package confirmationwire

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
)

func TestUnavailableShadowAblationOmitsNumericScoreCommitments(t *testing.T) {
	t.Parallel()
	inference, embedding, err := buildHighDropAblationEvidence(ablation.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	for _, evaluation := range []ablation.Evaluation{inference, embedding} {
		if evaluation.Evidence.Status != ablation.StatusUnavailable {
			t.Fatalf("status = %q, want unavailable", evaluation.Evidence.Status)
		}
		if evaluation.Evidence.Reason != ablation.ReasonEnforceProofUnavailable {
			t.Fatalf("reason = %q", evaluation.Evidence.Reason)
		}
		if evaluation.Evidence.BaselineScoresSHA256 != "" || evaluation.Evidence.AblatedScoresSHA256 != "" {
			t.Fatal("unavailable ablation must not commit paired score digests")
		}
		raw, err := json.Marshal(evaluation.Evidence)
		if err != nil {
			t.Fatal(err)
		}
		var decoded map[string]json.RawMessage
		if err := json.Unmarshal(raw, &decoded); err != nil {
			t.Fatal(err)
		}
		if _, ok := decoded["baseline_scores_sha256"]; ok {
			t.Fatal("baseline_scores_sha256 must be omitted")
		}
		if _, ok := decoded["ablated_scores_sha256"]; ok {
			t.Fatal("ablated_scores_sha256 must be omitted")
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
	path := filepath.Join("testdata", "go_ablation_unavailable_v9.json")
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v\nwant:\n%s", path, err, raw)
	}
	if string(got) != string(raw) {
		t.Fatalf("%s drifted from production unavailable ablation evidence\nwant:\n%s", path, raw)
	}
}
