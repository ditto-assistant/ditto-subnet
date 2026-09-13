package confirmationwire

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
)

func TestCommittedFixtureMatchesProductionEvidence(t *testing.T) {
	t.Parallel()
	// One committed vector per confirmation epoch. The v9 bytes are frozen —
	// four Python suites replay them — and the v12 vector is what proves the
	// Python wire converter carries the producer's bench_version instead of
	// pinning it, using evidence Go actually produced rather than a
	// Python-side hand edit of the v9 bytes.
	for _, benchVersion := range []int{9, 12} {
		t.Run(fmt.Sprintf("bench_version_%d", benchVersion), func(t *testing.T) {
			t.Parallel()
			want, err := MarshalFixtureForBenchVersion(benchVersion)
			if err != nil {
				t.Fatal(err)
			}
			path := filepath.Join("testdata", fmt.Sprintf("go_confirmation_evidence_v%d.json", benchVersion))
			got, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read %s: %v\nwant:\n%s", path, err, want)
			}
			if !bytes.Equal(got, want) {
				t.Fatalf("%s drifted from the production Go evidence contracts\nwant:\n%s", path, want)
			}
			fixture, err := BuildFixtureForBenchVersion(benchVersion)
			if err != nil {
				t.Fatal(err)
			}
			if fixture.LongMemEval.Evidence.BenchVersion != benchVersion ||
				fixture.InferenceAblation.Evidence.BenchVersion != benchVersion ||
				fixture.EmbeddingAblation.Evidence.BenchVersion != benchVersion {
				t.Fatal("producer evidence does not carry the run's bench_version")
			}
			if fixture.FixtureSchema != "dittobench-v9-confirmation-go-evidence-v1" ||
				fixture.InferenceAblation.Evidence.ContractVersion != ablationContractVersion {
				t.Fatal("frozen transport contract labels must not move with the epoch")
			}
		})
	}
}

func TestBuildFixtureFollowsConfirmationInstrumentFloor(t *testing.T) {
	t.Parallel()
	// No committed v13 vector yet (the v12 one already proves the Python wire
	// converter carries bench_version), but the builder must not fail a v13
	// instrument closed, and must still refuse the unbuilt v10/v11 epochs and
	// the first version the scorer does not accept.
	fixture, err := BuildFixtureForBenchVersion(scoregates.BenchVersionV13)
	if err != nil {
		t.Fatalf("v13 confirmation fixture: %v", err)
	}
	if fixture.LongMemEval.Evidence.BenchVersion != scoregates.BenchVersionV13 ||
		fixture.InferenceAblation.Evidence.BenchVersion != scoregates.BenchVersionV13 ||
		fixture.EmbeddingAblation.Evidence.BenchVersion != scoregates.BenchVersionV13 {
		t.Fatal("v13 producer evidence does not carry bench_version 13")
	}
	for _, benchVersion := range []int{8, 10, 11, scoregates.BenchVersionV13 + 1} {
		if _, err := BuildFixtureForBenchVersion(benchVersion); err == nil {
			t.Fatalf("confirmation fixture accepted bench_version %d", benchVersion)
		}
	}
}

func TestFixturePinsProducerSchemaVersions(t *testing.T) {
	t.Parallel()
	fixture, err := BuildFixture()
	if err != nil {
		t.Fatal(err)
	}
	if fixture.LongMemEval.Evidence.SchemaVersion != longmemevalEvidenceSchemaVersion {
		t.Fatalf("LongMem evidence schema = %d, want %d", fixture.LongMemEval.Evidence.SchemaVersion, longmemevalEvidenceSchemaVersion)
	}
	if fixture.InferenceAblation.Evidence.ContractVersion != ablationContractVersion ||
		fixture.EmbeddingAblation.Evidence.ContractVersion != ablationContractVersion {
		t.Fatal("ablation evidence contract drifted")
	}
	if fixture.InferenceAblation.Evidence.AblationProfileSHA256 != fixture.EmbeddingAblation.Evidence.AblationProfileSHA256 {
		t.Fatal("ablation interventions do not share one global frozen profile checksum")
	}
	if fixture.InferenceAblation.Evidence.ArtifactSHA256 != fixture.EmbeddingAblation.Evidence.ArtifactSHA256 ||
		fixture.InferenceAblation.Evidence.CoordinatorSHA256 != fixture.EmbeddingAblation.Evidence.CoordinatorSHA256 {
		t.Fatal("ablation interventions did not retain their shared run-specific coordinator bindings")
	}
}

const (
	longmemevalEvidenceSchemaVersion = 2
	ablationContractVersion          = "dittobench-v9-ablation-v1"
)
