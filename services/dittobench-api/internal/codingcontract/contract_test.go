package codingcontract

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type goldenVectors struct {
	Manifest     json.RawMessage   `json:"manifest"`
	SeedRequest  json.RawMessage   `json:"seed_request"`
	RunRequest   json.RawMessage   `json:"run_request"`
	TaskEvidence json.RawMessage   `json:"task_evidence"`
	RunEvidence  json.RawMessage   `json:"run_evidence"`
	Digests      map[string]string `json:"digests"`
}

func loadGoldenVectors(t *testing.T) goldenVectors {
	t.Helper()
	body, err := os.ReadFile(filepath.Join("testdata", "coding_contract_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vectors goldenVectors
	if err := json.Unmarshal(body, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}

func TestGoldenVectorsHaveStableKnownFieldDigests(t *testing.T) {
	vectors := loadGoldenVectors(t)
	manifest, err := ParseRunManifest(vectors.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	seed, err := ParseSeedRequest(vectors.SeedRequest)
	if err != nil {
		t.Fatal(err)
	}
	run, err := ParseRunRequest(vectors.RunRequest)
	if err != nil {
		t.Fatal(err)
	}
	taskEvidence, err := ParseTaskEvidence(vectors.TaskEvidence)
	if err != nil {
		t.Fatal(err)
	}
	runEvidence, err := ParseRunEvidence(vectors.RunEvidence)
	if err != nil {
		t.Fatal(err)
	}

	for key, test := range map[string]func() (string, error){
		"manifest":      func() (string, error) { return Digest(manifest) },
		"seed_request":  func() (string, error) { return Digest(seed) },
		"run_request":   func() (string, error) { return Digest(run) },
		"task_evidence": func() (string, error) { return Digest(taskEvidence) },
		"run_evidence":  func() (string, error) { return Digest(runEvidence) },
	} {
		got, err := test()
		if err != nil {
			t.Fatalf("%s: %v", key, err)
		}
		if got != vectors.Digests[key] {
			t.Fatalf("%s digest = %s, want %s", key, got, vectors.Digests[key])
		}
	}
	if err := runEvidence.ValidateAgainst(manifest, []TaskEvidence{taskEvidence}); err != nil {
		t.Fatalf("cross-evidence replay: %v", err)
	}
}

func TestUnknownFieldsAreExcludedFromKnownFieldDigest(t *testing.T) {
	vectors := loadGoldenVectors(t)
	original, err := ParseRunManifest(vectors.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	extended := bytes.Replace(
		vectors.Manifest,
		[]byte(`"tasks"`),
		[]byte(`"future_unsigned_diagnostic":{"value":1},"tasks"`),
		1,
	)
	parsed, err := ParseRunManifest(extended)
	if err != nil {
		t.Fatal(err)
	}
	originalDigest, err := Digest(original)
	if err != nil {
		t.Fatal(err)
	}
	parsedDigest, err := Digest(parsed)
	if err != nil {
		t.Fatal(err)
	}
	if originalDigest != parsedDigest {
		t.Fatalf("unknown field changed digest: %s != %s", originalDigest, parsedDigest)
	}
}

func TestUnicodeAndHTMLCharactersHaveCrossLanguageCanonicalBytes(t *testing.T) {
	vectors := loadGoldenVectors(t)
	request, err := ParseRunRequest(vectors.RunRequest)
	if err != nil {
		t.Fatal(err)
	}
	request.Issue.Description = "Preserve café <tag> & separators \u2028 and \u2029."
	digest, err := Digest(request)
	if err != nil {
		t.Fatal(err)
	}
	if digest != vectors.Digests["unicode_run_request"] {
		t.Fatalf("unicode canonical digest = %s, want %s", digest, vectors.Digests["unicode_run_request"])
	}
}

func TestDuplicateAndMissingKnownFieldsFailClosed(t *testing.T) {
	if _, err := ParseRunManifest([]byte(`{"schema":"a","schema":"b"}`)); err == nil {
		t.Fatal("duplicate field was accepted")
	}
	vectors := loadGoldenVectors(t)
	missing := bytes.Replace(vectors.Manifest, []byte(`"weight_eligible": false,`), nil, 1)
	if _, err := ParseRunManifest(missing); err == nil {
		t.Fatal("missing weight_eligible was accepted")
	}
}

func TestShadowContractCannotBecomeWeightEligible(t *testing.T) {
	vectors := loadGoldenVectors(t)
	weighted := bytes.Replace(vectors.Manifest, []byte(`"weight_eligible": false`), []byte(`"weight_eligible": true`), 1)
	if _, err := ParseRunManifest(weighted); err == nil {
		t.Fatal("weight-eligible coding v1 manifest was accepted")
	}
}

func TestDigestRevalidatesMutableNestedCollections(t *testing.T) {
	vectors := loadGoldenVectors(t)
	manifest, err := ParseRunManifest(vectors.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	manifest.Tasks = append(manifest.Tasks, manifest.Tasks[0])
	if _, err := Digest(manifest); err == nil {
		t.Fatal("digest accepted a mutated duplicate task")
	}
}

func TestResolvedTaskRequiresCompletePassingGraderEvidence(t *testing.T) {
	vectors := loadGoldenVectors(t)
	failing := bytes.Replace(vectors.TaskEvidence, []byte(`"passed": 3, "total": 3`), []byte(`"passed": 2, "total": 3`), 1)
	if _, err := ParseTaskEvidence(failing); err == nil {
		t.Fatal("resolved evidence with a failed test was accepted")
	}
}

func TestCanonicalJSONHasOneTrailingNewline(t *testing.T) {
	vectors := loadGoldenVectors(t)
	manifest, err := ParseRunManifest(vectors.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	body, err := CanonicalJSON(manifest)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.HasSuffix(body, []byte("\n")) || bytes.HasSuffix(body, []byte("\n\n")) {
		t.Fatalf("canonical JSON has invalid terminator: %q", body[len(body)-2:])
	}
}
