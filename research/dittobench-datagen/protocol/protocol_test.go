package protocol

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestRunRequestBenchVersionIsAdditiveForV7Only(t *testing.T) {
	legacy, err := json.Marshal(RunRequest{CaseID: "v6", BenchVersion: 0})
	if err != nil {
		t.Fatal(err)
	}
	var legacyObject map[string]any
	if err := json.Unmarshal(legacy, &legacyObject); err != nil {
		t.Fatal(err)
	}
	if _, present := legacyObject["bench_version"]; present {
		t.Fatal("legacy v2-v6 request must omit bench_version")
	}

	v7, err := json.Marshal(RunRequest{CaseID: "v7", BenchVersion: BenchVersionV7})
	if err != nil {
		t.Fatal(err)
	}
	var v7Object map[string]any
	if err := json.Unmarshal(v7, &v7Object); err != nil {
		t.Fatal(err)
	}
	if got := v7Object["bench_version"]; got != float64(BenchVersionV7) {
		t.Fatalf("v7 bench_version = %v, want %d", got, BenchVersionV7)
	}
}

func TestScoreReportZeroCompositeStderrUsesHistoricalOmitEmptyShape(t *testing.T) {
	reportJSON, err := json.Marshal(ScoreReport{
		RunID: "v9-zero", GeneratedAt: "2026-08-11T00:00:00Z",
		Composite: 0, CompositeStderr: 0, PerCase: []CaseScore{},
	})
	if err != nil {
		t.Fatal(err)
	}
	var reportObject map[string]any
	if err := json.Unmarshal(reportJSON, &reportObject); err != nil {
		t.Fatal(err)
	}
	if _, present := reportObject["composite_stderr"]; present {
		t.Fatal("zero composite_stderr must preserve the historical omitempty shape")
	}
}

// TestV13GraderOnlyFieldsNeverReachHarnessWire pins the wire boundary of the
// v13 grader-only MemoryCase fields: the harness /run request (RunRequest) is a
// separate type that carries none of them, exactly as it carries no
// expected_answer or distractor_answers. The scorer builds RunRequest from a
// MemoryCase by field, so a grader-only field cannot leak without a RunRequest
// field being added for it — which this test would catch.
func TestV13GraderOnlyFieldsNeverReachHarnessWire(t *testing.T) {
	wire := map[string]bool{}
	rt := reflect.TypeOf(RunRequest{})
	for i := 0; i < rt.NumField(); i++ {
		tag := strings.Split(rt.Field(i).Tag.Get("json"), ",")[0]
		wire[tag] = true
	}
	for _, graderOnly := range []string{"expected_answer", "distractor_answers", "forbidden_answer", "grounding_tokens", "twin_relation", "twin_pair_id", "dump_guard", "writing_protected"} {
		if wire[graderOnly] {
			t.Fatalf("grader-only field %q has a harness wire counterpart on RunRequest", graderOnly)
		}
	}
	mt := reflect.TypeOf(MemoryCase{})
	for _, name := range []string{"TwinRelation", "TwinPairID", "GroundingTokens"} {
		f, ok := mt.FieldByName(name)
		if !ok {
			t.Fatalf("MemoryCase.%s missing", name)
		}
		// Grader-only (json:"-") or additive omitempty: either way no v2..v12
		// artifact byte moves because the field is never populated there.
		if tag := f.Tag.Get("json"); tag != "-" && !strings.HasSuffix(tag, ",omitempty") {
			t.Fatalf("MemoryCase.%s must be omitted below v13 (json tag %q)", name, tag)
		}
	}
}
