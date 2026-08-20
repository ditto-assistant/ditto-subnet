package codingcontract

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type goldenVectors struct {
	Manifest                json.RawMessage     `json:"manifest"`
	SeedRequest             json.RawMessage     `json:"seed_request"`
	RunRequest              json.RawMessage     `json:"run_request"`
	TaskEvidence            json.RawMessage     `json:"task_evidence"`
	ZeroModelTaskEvidence   json.RawMessage     `json:"zero_model_task_evidence"`
	NonresolvedTaskEvidence json.RawMessage     `json:"nonresolved_task_evidence"`
	RunEvidence             json.RawMessage     `json:"run_evidence"`
	AggregateRunEvidence    json.RawMessage     `json:"aggregate_run_evidence"`
	WireBoundaryVectors     wireBoundaryVectors `json:"wire_boundary_vectors"`
	Digests                 map[string]string   `json:"digests"`
}

type wireBoundaryVectors struct {
	PairedSurrogateJSONString         string `json:"paired_surrogate_json_string"`
	EscapedSurrogateLiteralJSONString string `json:"escaped_surrogate_literal_json_string"`
	ReplacementCharacterJSONString    string `json:"replacement_character_json_string"`
	LoneHighJSONString                string `json:"lone_high_json_string"`
	LoneLowJSONString                 string `json:"lone_low_json_string"`
	MaxJSONDepth                      int    `json:"max_json_depth"`
	RejectJSONDepth                   int    `json:"reject_json_depth"`
}

func loadGoldenVectors(t *testing.T) goldenVectors {
	t.Helper()
	body, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata", "coding_contract_v1.json"))
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
		"manifest":     func() (string, error) { return Digest(manifest) },
		"seed_request": func() (string, error) { return Digest(seed) },
		"run_request":  func() (string, error) { return Digest(run) },
		"task_evidence": func() (string, error) {
			return TaskEvidenceDigest(manifest, "validator-ticket-001", taskEvidence)
		},
		"run_evidence": func() (string, error) {
			return RunEvidenceDigest(manifest, "validator-ticket-001", runEvidence, []TaskEvidence{taskEvidence})
		},
	} {
		got, err := test()
		if err != nil {
			t.Fatalf("%s: %v", key, err)
		}
		if got != vectors.Digests[key] {
			t.Fatalf("%s digest = %s, want %s", key, got, vectors.Digests[key])
		}
	}
	if err := runEvidence.ValidateAgainst(manifest, "validator-ticket-001", []TaskEvidence{taskEvidence}); err != nil {
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

func TestUnicodeMemoryHasCrossLanguageCanonicalBytes(t *testing.T) {
	vectors := loadGoldenVectors(t)
	request, err := ParseSeedRequest(vectors.SeedRequest)
	if err != nil {
		t.Fatal(err)
	}
	request.Memories[0].Content = "Preserve café <tag> & separators \u2028 and \u2029."
	projection := struct {
		Memories []VisibleMemory `json:"memories"`
	}{Memories: request.Memories}
	digest, err := digestUnchecked(projection)
	if err != nil {
		t.Fatal(err)
	}
	if digest != vectors.Digests["unicode_seed_memory"] {
		t.Fatalf("unicode memory digest = %s, want %s", digest, vectors.Digests["unicode_seed_memory"])
	}
}

func TestRawUnicodeBoundaryVectorsMatchPython(t *testing.T) {
	vectors := loadGoldenVectors(t)
	description := []byte(`"The parser drops an incomplete trailing sequence."`)
	replaceDescription := func(raw string) []byte {
		t.Helper()
		return bytes.Replace(vectors.RunRequest, description, []byte(raw), 1)
	}

	paired, err := ParseRunRequest(replaceDescription(vectors.WireBoundaryVectors.PairedSurrogateJSONString))
	if err != nil {
		t.Fatalf("paired surrogate was rejected: %v", err)
	}
	if paired.Issue.Description != "😀" {
		t.Fatalf("paired surrogate decoded to %q", paired.Issue.Description)
	}
	for raw, expected := range map[string]string{
		vectors.WireBoundaryVectors.EscapedSurrogateLiteralJSONString: `\ud800`,
		vectors.WireBoundaryVectors.ReplacementCharacterJSONString:    "�",
	} {
		parsed, err := ParseRunRequest(replaceDescription(raw))
		if err != nil {
			t.Fatalf("valid Unicode boundary %q was rejected: %v", raw, err)
		}
		if parsed.Issue.Description != expected {
			t.Fatalf("valid Unicode boundary decoded to %q, want %q", parsed.Issue.Description, expected)
		}
	}
	for label, raw := range map[string]string{
		"lone high": vectors.WireBoundaryVectors.LoneHighJSONString,
		"lone low":  vectors.WireBoundaryVectors.LoneLowJSONString,
	} {
		if _, err := ParseRunRequest(replaceDescription(raw)); err == nil {
			t.Fatalf("%s surrogate was accepted", label)
		}
	}
	invalidUTF8 := replaceDescription(`"invalid"`)
	invalidUTF8 = bytes.Replace(invalidUTF8, []byte("invalid"), []byte{0xff}, 1)
	if _, err := ParseRunRequest(invalidUTF8); err == nil {
		t.Fatal("invalid UTF-8 was accepted")
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
	wrongFamily := bytes.Replace(vectors.Manifest, []byte(`"bench_family": "coding"`), []byte(`"bench_family": "memory"`), 1)
	if _, err := ParseRunManifest(wrongFamily); err == nil {
		t.Fatal("non-coding bench family was accepted")
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

func TestNullCollectionsAndExcessiveNestingFailClosed(t *testing.T) {
	vectors := loadGoldenVectors(t)
	seed, err := ParseSeedRequest(vectors.SeedRequest)
	if err != nil {
		t.Fatal(err)
	}
	seed.Memories[0].Supersedes = nil
	if err := seed.Validate(); err == nil {
		t.Fatal("nil supersedes was accepted")
	}

	run, err := ParseRunRequest(vectors.RunRequest)
	if err != nil {
		t.Fatal(err)
	}
	run.Issue.Constraints = nil
	if err := run.Validate(); err == nil {
		t.Fatal("nil issue constraints were accepted")
	}

	nestedBody := func(valueDepth int) []byte {
		t.Helper()
		wrappers := valueDepth - 1
		nested := strings.Repeat("[", wrappers) + `"leaf"` + strings.Repeat("]", wrappers)
		return bytes.Replace(
			vectors.Manifest,
			[]byte(`"tasks"`),
			[]byte(`"future_nested":`+nested+`,"tasks"`),
			1,
		)
	}
	if _, err := ParseRunManifest(nestedBody(vectors.WireBoundaryVectors.MaxJSONDepth)); err != nil {
		t.Fatalf("maximum JSON nesting was rejected: %v", err)
	}
	if _, err := ParseRunManifest(nestedBody(vectors.WireBoundaryVectors.RejectJSONDepth)); err == nil {
		t.Fatal("excessive JSON nesting was accepted")
	}
}

func TestSharedNonresolvedAndAggregateEvidenceVectors(t *testing.T) {
	vectors := loadGoldenVectors(t)
	manifest, err := ParseRunManifest(vectors.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	nonresolved, err := ParseTaskEvidence(vectors.NonresolvedTaskEvidence)
	if err != nil {
		t.Fatal(err)
	}
	if nonresolved.Authoring != nil || nonresolved.Grader != nil ||
		nonresolved.TerminalDomain != DomainValidatorInfrastructure || nonresolved.FailureCode == nil ||
		*nonresolved.FailureCode != "transport_pre_authoritative" {
		t.Fatal("nonresolved task vector lost its pre-authoritative null semantics")
	}
	digest, err := TaskEvidenceDigest(manifest, "validator-ticket-001", nonresolved)
	if err != nil {
		t.Fatal(err)
	}
	if digest != vectors.Digests["nonresolved_task_evidence"] {
		t.Fatalf("nonresolved task digest = %s, want %s", digest, vectors.Digests["nonresolved_task_evidence"])
	}

	aggregate, err := ParseRunEvidence(vectors.AggregateRunEvidence)
	if err != nil {
		t.Fatal(err)
	}
	if aggregate.ScoreableTaskCount != 6 || aggregate.RepairMeanMicros != 666_666 ||
		aggregate.ResolvedCount != 4 || aggregate.RepairFailureCount != 1 ||
		aggregate.InfrastructureCount != 1 || aggregate.InvalidCount != 1 ||
		aggregate.CandidateIntegrityCount != 1 || aggregate.ControlPlaneIntegrityCount != 1 {
		t.Fatal("aggregate vector did not preserve terminal-domain membership and floor arithmetic")
	}
	digest, err = digestUnchecked(aggregate)
	if err != nil {
		t.Fatal(err)
	}
	if digest != vectors.Digests["aggregate_run_evidence"] {
		t.Fatalf("aggregate run digest = %s, want %s", digest, vectors.Digests["aggregate_run_evidence"])
	}
}

func TestIntegerWidthsFailClosedAtGoWireBoundary(t *testing.T) {
	vectors := loadGoldenVectors(t)
	overflow := bytes.Replace(
		vectors.Manifest,
		[]byte(`"selection_block_number": 123456`),
		[]byte(`"selection_block_number": 18446744073709551616`),
		1,
	)
	if _, err := ParseRunManifest(overflow); err == nil {
		t.Fatal("selection block above uint64 was accepted")
	}

	testOverflow := bytes.Replace(
		vectors.TaskEvidence,
		[]byte(`"passed": 2, "total": 2`),
		[]byte(`"passed": 2, "total": 4294967296`),
		1,
	)
	if _, err := ParseTaskEvidence(testOverflow); err == nil {
		t.Fatal("test count above uint32 was accepted")
	}
}

func TestEvidenceDigestRequiresExactValidatorTicket(t *testing.T) {
	vectors := loadGoldenVectors(t)
	manifest, err := ParseRunManifest(vectors.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := ParseTaskEvidence(vectors.TaskEvidence)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := TaskEvidenceDigest(manifest, "other-ticket", evidence); err == nil {
		t.Fatal("task evidence was digestible against the wrong validator ticket")
	}
	mutations := map[string]func(*GraderEvidence){
		"bundle":   func(value *GraderEvidence) { value.GraderBundleSHA256 = strings.Repeat("1", 64) },
		"image":    func(value *GraderEvidence) { value.GraderImageDigest = "sha256:" + strings.Repeat("1", 64) },
		"platform": func(value *GraderEvidence) { value.GraderPlatform = "linux/arm64" },
		"tests":    func(value *GraderEvidence) { value.TestManifestSHA256 = strings.Repeat("1", 64) },
		"plan":     func(value *GraderEvidence) { value.GraderPlanSHA256 = strings.Repeat("1", 64) },
		"resource": func(value *GraderEvidence) { value.ResourceProfileSHA256 = strings.Repeat("1", 64) },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := evidence
			grader := *evidence.Grader
			mutate(&grader)
			changed.Grader = &grader
			if _, err := TaskEvidenceDigest(manifest, "validator-ticket-001", changed); err == nil {
				t.Fatal("task evidence accepted grader authority outside the selected manifest")
			}
		})
	}
}

func TestResolvedTaskRequiresCompletePassingGraderEvidence(t *testing.T) {
	vectors := loadGoldenVectors(t)
	failing := bytes.Replace(vectors.TaskEvidence, []byte(`"passed": 3, "total": 3`), []byte(`"passed": 2, "total": 3`), 1)
	if _, err := ParseTaskEvidence(failing); err == nil {
		t.Fatal("resolved evidence with a failed test was accepted")
	}
	missingReceipt := bytes.Replace(vectors.TaskEvidence, []byte(`"execution_receipt_count": 6`), []byte(`"execution_receipt_count": 5`), 1)
	if _, err := ParseTaskEvidence(missingReceipt); err == nil {
		t.Fatal("resolved evidence with an incomplete receipt chain was accepted")
	}
}

func TestZeroModelAttemptHasCanonicalAttributableEvidence(t *testing.T) {
	vectors := loadGoldenVectors(t)
	manifest, err := ParseRunManifest(vectors.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := ParseTaskEvidence(vectors.ZeroModelTaskEvidence)
	if err != nil {
		t.Fatal(err)
	}
	if evidence.Authoring == nil || evidence.Authoring.Model.Requests != 0 {
		t.Fatal("zero-model evidence did not preserve canonical zero accounting")
	}
	digest, err := TaskEvidenceDigest(manifest, "validator-ticket-001", evidence)
	if err != nil {
		t.Fatal(err)
	}
	if digest != vectors.Digests["zero_model_task_evidence"] {
		t.Fatalf("zero-model digest = %s, want %s", digest, vectors.Digests["zero_model_task_evidence"])
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
