package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// v13GraderOnlyRunKeys are the JSON keys the v13 grader-only protocol fields
// (MemoryCase.Claims / TwinRelation, ToolCase.TwinRelation / Restraint,
// ToolSpec.RequiredArgClaims) would take if they ever serialized.
var v13GraderOnlyRunKeys = []string{"claims", "twin_relation", "required_arg_claims", "restraint", "expected_answer", "answer_kind", "distractor_answers"}

// TestV13GraderOnlyFieldsNeverReachRunPayload is the runner-side half of
// gen.TestV13GraderOnlyFieldsNeverReachHarnessWire (issue #1824): the memory
// phase hands the runner only (case id, question), the /run body is a
// protocol.RunRequest, and that body — captured from a live loopback harness —
// carries none of the grader-only keys or values even when the staged case is
// fully populated. The structural half asserts RunRequest has no field that
// could ever embed a MemoryCase, ToolCase, ToolSpec, or Claim.
func TestV13GraderOnlyFieldsNeverReachRunPayload(t *testing.T) {
	forbiddenTypes := map[reflect.Type]bool{
		reflect.TypeOf(protocol.MemoryCase{}):     true,
		reflect.TypeOf(protocol.ToolCase{}):       true,
		reflect.TypeOf(protocol.ToolSpec{}):       true,
		reflect.TypeOf(protocol.Claim{}):          true,
		reflect.TypeOf(protocol.RestraintClaim{}): true,
	}
	var walk func(rt reflect.Type, path string)
	walk = func(rt reflect.Type, path string) {
		for rt.Kind() == reflect.Ptr || rt.Kind() == reflect.Slice || rt.Kind() == reflect.Array || rt.Kind() == reflect.Map {
			rt = rt.Elem()
		}
		if forbiddenTypes[rt] {
			t.Fatalf("RunRequest embeds grader-bearing type %s at %s", rt, path)
		}
		if rt.Kind() != reflect.Struct {
			return
		}
		for i := 0; i < rt.NumField(); i++ {
			f := rt.Field(i)
			walk(f.Type, path+"."+f.Name)
		}
	}
	walk(reflect.TypeOf(protocol.RunRequest{}), "RunRequest")

	staged := gen.StagedCase{
		Case: protocol.MemoryCase{
			BenchVersion:      protocol.BenchVersionV13,
			ID:                "case-v13",
			QuestionType:      "world-contact-current",
			Question:          "Who is the current contact for the onboarding workstream?",
			ExpectedAnswer:    "Dana Whitfield",
			AnswerKind:        protocol.AnswerAbsence,
			DistractorAnswers: []string{"Priya Natarajan"},
			TwinRelation:      protocol.TwinRelationDecision,
			Claims:            []protocol.Claim{{Kind: "entity", Expected: "Dana Whitfield", Accept: []string{"D. Whitfield"}, Critical: true}},
		},
		V10Provenance: &universe.V10CaseProvenance{Relation: "counterfactual"},
	}

	var captured []byte
	harness := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/run" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		captured, _ = io.ReadAll(r.Body)
		_ = json.NewEncoder(w).Encode(protocol.RunResponse{FinalText: "Dana Whitfield", Answer: "Dana Whitfield"})
	}))
	defer harness.Close()

	// The memory phase calls the runner with exactly (case id, question); the
	// staged case never enters the runner package. Reproduce that call.
	ctx := runner.TrustSandbox(context.Background())
	if _, err := runner.RunCase(ctx, harness.URL, staged.Case.ID, staged.Case.Question, nil, runner.CaseOptions{BenchVersion: protocol.BenchVersionV13, UserID: gen.PrimaryUser}); err != nil {
		t.Fatalf("run case: %v", err)
	}
	if len(captured) == 0 {
		t.Fatal("no /run body captured")
	}
	var body map[string]any
	if err := json.Unmarshal(captured, &body); err != nil {
		t.Fatal(err)
	}
	for _, key := range v13GraderOnlyRunKeys {
		if _, present := body[key]; present {
			t.Fatalf("/run payload carries grader-only key %q: %s", key, captured)
		}
	}
	lower := strings.ToLower(string(captured))
	for _, leak := range []string{"dana whitfield", "d. whitfield", "priya", "decision_twin", "counterfactual", "absence", "world-contact"} {
		if strings.Contains(lower, leak) {
			t.Fatalf("/run payload leaks grader-only value %q: %s", leak, captured)
		}
	}
	if body["case_id"] != staged.Case.ID || body["user_input"] != staged.Case.Question {
		t.Fatalf("/run payload lost the case surface: %s", captured)
	}
	// Option A of #1519: the public wire version stays pinned even for a v13 run.
	if body["bench_version"] != float64(protocol.BenchVersionV9) {
		t.Fatalf("/run payload bench_version = %v, want the pinned public wire version %d", body["bench_version"], protocol.BenchVersionV9)
	}
}

// TestCarryV13ProvenanceRelationIsReportOnlyAndVersionGated pins the report
// side of the plumbing: the generator relation reaches CaseScore.Relation only
// for bench_version >= 13, and v9..v12 reports keep their exact shape.
func TestCarryV13ProvenanceRelationIsReportOnlyAndVersionGated(t *testing.T) {
	sc := gen.StagedCase{V10Provenance: &universe.V10CaseProvenance{Relation: "metamorphic"}}
	base := protocol.CaseScore{CaseID: "c", Kind: "memory", Score: 1}
	for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV10, protocol.BenchVersionV11, protocol.BenchVersionV12} {
		got := carryV13ProvenanceRelation(version, sc, base)
		if !reflect.DeepEqual(got, base) {
			t.Fatalf("v%d report changed shape: %+v", version, got)
		}
		body, _ := json.Marshal(got)
		if strings.Contains(string(body), "relation") {
			t.Fatalf("v%d report serialized relation: %s", version, body)
		}
	}
	got := carryV13ProvenanceRelation(protocol.BenchVersionV13, sc, base)
	if got.Relation != "metamorphic" {
		t.Fatalf("v13 report missing relation: %+v", got)
	}
	body, _ := json.Marshal(got)
	if !strings.Contains(string(body), `"relation":"metamorphic"`) {
		t.Fatalf("v13 report did not serialize relation: %s", body)
	}
	if without := carryV13ProvenanceRelation(protocol.BenchVersionV13, gen.StagedCase{}, base); !reflect.DeepEqual(without, base) {
		t.Fatalf("v13 case without provenance changed shape: %+v", without)
	}
}
