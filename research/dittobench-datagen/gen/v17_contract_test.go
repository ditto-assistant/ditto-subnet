package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// v17KnownVectorWant is the live v17 contract's datagen-half vector (seed
// 123456789, full profile, public surface salt 0). V17 is the v16 envelope
// with the answer-pool fix (answerPoolFixBenchVersion) and its own epoch and
// seed domain; its scoring change (priced-cost-v2) lives in the scorer.
// docs/bench-versions.md and the README publish the same string.
const v17KnownVectorWant = "68df415cf0b8b92525f1011b6c1a92bf9fc5d65bae96e76b3fcaa1787c166c48"

// TestV17KnownVector pins the active v17 dataset bytes so a later contract
// cannot move them: every v18 lever is gated on bench_version >= 18.
func TestV17KnownVector(t *testing.T) {
	prof, ok := ProfileForVersion("full", protocol.BenchVersionV17)
	if !ok {
		t.Fatal("v17 full profile missing")
	}
	a, err := GenerateDataset(123456789, prof, protocol.BenchVersionV17)
	if err != nil {
		t.Fatal(err)
	}
	digest, _, err := a.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	if digest != v17KnownVectorWant {
		t.Fatalf("v17 known vector: got %s, want %s", digest, v17KnownVectorWant)
	}
	if a.BenchVersion != 17 || a.GeneratedAt != "2027-09-01T00:00:00Z" {
		t.Fatalf("v17 envelope: version=%d epoch=%s", a.BenchVersion, a.GeneratedAt)
	}
	if len(a.ToolCases) != 50 || len(a.MemoryCases) != 125 {
		t.Fatalf("v17 datagen half: %d tool + %d memory cases, want 50 + 125", len(a.ToolCases), len(a.MemoryCases))
	}
	for _, doc := range []string{"../docs/bench-versions.md", "../README.md"} {
		if !strings.Contains(readFile(t, doc), v17KnownVectorWant) {
			t.Fatalf("%s does not publish the pinned v17 vector", doc)
		}
	}
}
