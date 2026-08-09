package confirmationwire

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func TestCommittedFixtureMatchesProductionEvidence(t *testing.T) {
	t.Parallel()
	want, err := MarshalFixture()
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join("testdata", "go_confirmation_evidence_v9.json")
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v\nwant:\n%s", path, err, want)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("%s drifted from the production Go evidence contracts\nwant:\n%s", path, want)
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
