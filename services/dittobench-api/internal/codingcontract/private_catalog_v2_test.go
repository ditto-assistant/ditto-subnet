package codingcontract

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestPrivateCatalogTaskV2Validation(t *testing.T) {
	task := PrivateCatalogTaskV2{
		Schema: "dittobench-coding-private-catalog-task-v2", CodingContractVersion: 2,
		CorpusReleaseID: "coding-private-v2-r1", CatalogIndex: 7,
		TaskVersionID: "private-group-001-v1", BaseTaskGroupID: "private-group-001",
		Condition: PrivateCatalogV1, RepositoryEpoch: "epoch-stream-2",
		PrivateReleaseSHA256:      "1111111111111111111111111111111111111111111111111111111111111111",
		GroupManifestSHA256:       "2222222222222222222222222222222222222222222222222222222222222222",
		VisibleSnapshotTreeSHA256: "3333333333333333333333333333333333333333333333333333333333333333",
		VisibleIssueSHA256:        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		HiddenGraderTreeSHA256:    "4444444444444444444444444444444444444444444444444444444444444444",
		MemoryBundleSHA256:        "5555555555555555555555555555555555555555555555555555555555555555",
		RuntimePolicySHA256:       "6666666666666666666666666666666666666666666666666666666666666666",
		ResourceProfileSHA256:     "7777777777777777777777777777777777777777777777777777777777777777",
		CalibrationSHA256:         "8888888888888888888888888888888888888888888888888888888888888888",
		SemanticReviewSHA256:      "9999999999999999999999999999999999999999999999999999999999999999",
		RunnerProfileSHA256:       "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		TaskCommitmentSHA256:      "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
	}
	digest, err := PrivateCatalogTaskV2Digest(task)
	if err != nil {
		t.Fatal(err)
	}
	task.TaskCommitmentSHA256 = digest
	if err := task.Validate(); err != nil {
		t.Fatal(err)
	}
	task.Condition = "unknown"
	if err := task.Validate(); err == nil {
		t.Fatal("expected condition rejection")
	}
}

func TestPrivateCatalogTaskV2MatchesSharedVector(t *testing.T) {
	body, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata", "coding_private_catalog_v2.json"))
	if err != nil {
		t.Fatal(err)
	}
	var task PrivateCatalogTaskV2
	if err := json.Unmarshal(body, &task); err != nil {
		t.Fatal(err)
	}
	digest, err := PrivateCatalogTaskV2Digest(task)
	if err != nil {
		t.Fatal(err)
	}
	if digest != task.TaskCommitmentSHA256 {
		t.Fatalf("digest=%s commitment=%s", digest, task.TaskCommitmentSHA256)
	}
	if err := task.Validate(); err != nil {
		t.Fatal(err)
	}
}
