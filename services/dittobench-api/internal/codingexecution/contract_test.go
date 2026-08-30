package codingexecution

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

type executionVector struct {
	Schema          string          `json:"schema"`
	ContractVersion int             `json:"coding_contract_version"`
	WeightEligible  bool            `json:"weight_eligible"`
	RuntimePolicy   json.RawMessage `json:"runtime_policy"`
	RunnerPlan      json.RawMessage `json:"runner_plan"`
	GraderPlan      json.RawMessage `json:"grader_plan"`
	GraderResource  json.RawMessage `json:"grader_resource_profile"`
	Expected        expectedDigests `json:"expected"`
}

type expectedDigests struct {
	RunnerPlanSHA256      string `json:"runner_plan_sha256"`
	GraderPlanSHA256      string `json:"grader_plan_sha256"`
	ResourceProfileSHA256 string `json:"grader_resource_profile_sha256"`
}

func loadExecutionVector(t *testing.T) executionVector {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata",
		"coding_execution_plan_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var vector executionVector
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	if vector.Schema != "dittobench-coding-execution-plan-vector-v1" ||
		vector.ContractVersion != codingcontract.ContractVersion || vector.WeightEligible {
		t.Fatal("execution-plan vector metadata is invalid")
	}
	return vector
}

func parseExecutionVector(
	t *testing.T,
) (executionVector, RunnerPlan, codingcontract.RuntimePolicy, GraderPlan, ResourceProfile) {
	t.Helper()
	vector := loadExecutionVector(t)
	runner, err := ParseRunnerPlan(vector.RunnerPlan)
	if err != nil {
		t.Fatal(err)
	}
	grader, err := ParseGraderPlan(vector.GraderPlan)
	if err != nil {
		t.Fatal(err)
	}
	resource, err := ParseResourceProfile(vector.GraderResource)
	if err != nil {
		t.Fatal(err)
	}
	var runtimePolicy codingcontract.RuntimePolicy
	if err := json.Unmarshal(vector.RuntimePolicy, &runtimePolicy); err != nil || runtimePolicy.Validate() != nil {
		t.Fatal("runtime-policy vector is invalid")
	}
	return vector, runner, runtimePolicy, grader, resource
}

func TestExecutionPlanVectorMatchesPythonAndRuntimeManifests(t *testing.T) {
	vector, runner, runtimePolicy, grader, resource := parseExecutionVector(t)
	runnerDigest, err := RunnerPlanSHA256(runner)
	if err != nil || runnerDigest != vector.Expected.RunnerPlanSHA256 {
		t.Fatalf("runner digest=%s want=%s err=%v", runnerDigest, vector.Expected.RunnerPlanSHA256, err)
	}
	graderDigest, err := GraderPlanSHA256(grader)
	if err != nil || graderDigest != vector.Expected.GraderPlanSHA256 {
		t.Fatalf("grader digest=%s want=%s err=%v", graderDigest, vector.Expected.GraderPlanSHA256, err)
	}
	resourceDigest, err := ResourceProfileSHA256(resource)
	if err != nil || resourceDigest != vector.Expected.ResourceProfileSHA256 {
		t.Fatalf("resource digest=%s want=%s err=%v", resourceDigest, vector.Expected.ResourceProfileSHA256, err)
	}
	if err := ValidateBundle(Bundle{
		Runner: runner, RuntimePolicy: runtimePolicy, Grader: grader, Resource: resource,
	}); err != nil {
		t.Fatal(err)
	}

	now := time.Date(2026, 8, 23, 9, 0, 0, 0, time.UTC)
	deadline := now.Add(time.Hour)
	runnerManifest, err := runner.Manifest(RunnerBinding{
		TicketID:            "11111111-1111-4111-8111-111111111111",
		ProfileCapabilityID: "profile-synthetic-001", Deadline: deadline,
	}, now)
	if err != nil || runnerManifest.CaseID != runner.CaseID ||
		len(runnerManifest.TestCommands) != 1 || len(runnerManifest.BuildCommands) != 1 {
		t.Fatalf("runner manifest=%#v err=%v", runnerManifest, err)
	}
	graderManifest, err := grader.Manifest(resource, deadline, now)
	if err != nil || graderManifest.GraderPlanSHA256 != vector.Expected.GraderPlanSHA256 ||
		graderManifest.ResourceProfileSHA256 != vector.Expected.ResourceProfileSHA256 {
		t.Fatalf("grader manifest=%#v err=%v", graderManifest, err)
	}

	runner.TestCommands[0].Argv[0] = "mutated"
	grader.TestGroups[0].Command.Argv[0] = "mutated"
	if runnerManifest.TestCommands[0].Argv[0] != "python" ||
		graderManifest.TestGroups[0].Command.Argv[0] != "python" {
		t.Fatal("runtime manifests aliased caller-owned plan slices")
	}
}

func TestExecutionPlanKnownFieldProjectionAndPhasePrivacy(t *testing.T) {
	vector, runner, _, _, _ := parseExecutionVector(t)
	var extended map[string]any
	if err := json.Unmarshal(vector.RunnerPlan, &extended); err != nil {
		t.Fatal(err)
	}
	extended["future_diagnostic"] = map[string]any{"ignored": true}
	commands := extended["test_commands"].([]any)
	commands[0].(map[string]any)["future_hint"] = "ignored"
	body, err := json.Marshal(extended)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseRunnerPlan(body)
	if err != nil {
		t.Fatal(err)
	}
	want, _ := RunnerPlanSHA256(runner)
	got, _ := RunnerPlanSHA256(parsed)
	if got != want {
		t.Fatalf("forward-compatible digest=%s want=%s", got, want)
	}
	serialized, err := canonicalJSON(runner)
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range [][]byte{[]byte(`"grader"`), []byte("grader/"), []byte("hidden")} {
		if bytes.Contains(serialized, forbidden) {
			t.Fatalf("authoring runner plan exposed protected marker %q", forbidden)
		}
	}
	duplicate := bytes.Replace(vector.RunnerPlan, []byte(`"schema":`), []byte(`"schema":"duplicate","schema":`), 1)
	if _, err := ParseRunnerPlan(duplicate); !errors.Is(err, ErrInvalid) {
		t.Fatalf("duplicate field err=%v", err)
	}
}

func TestExecutionPlanRejectsDriftAndUnsafeAuthority(t *testing.T) {
	_, runner, runtimePolicy, grader, resource := parseExecutionVector(t)
	base := Bundle{Runner: runner, RuntimePolicy: runtimePolicy, Grader: grader, Resource: resource}
	tests := map[string]func(*Bundle){
		"runtime paths":    func(value *Bundle) { value.RuntimePolicy.EditablePaths = []string{} },
		"runtime commands": func(value *Bundle) { value.RuntimePolicy.TestCommandIDs = []string{} },
		"runner limits":    func(value *Bundle) { value.Runner.Limits.MaxWorkspaceBytes = 2 << 30 },
		"grader case":      func(value *Bundle) { value.Grader.CaseID = "other-case" },
		"grader resource":  func(value *Bundle) { value.Grader.ResourceProfileSHA256 = strings.Repeat("f", 64) },
		"grader contract":  func(value *Bundle) { value.Grader.GraderContractSHA256 = strings.Repeat("f", 64) },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			value := Bundle{
				Runner: cloneRunnerPlan(base.Runner), RuntimePolicy: cloneRuntimePolicy(base.RuntimePolicy),
				Grader: cloneGraderPlan(base.Grader), Resource: base.Resource,
			}
			mutate(&value)
			if err := ValidateBundle(value); !errors.Is(err, ErrInvalid) {
				t.Fatalf("drift err=%v", err)
			}
		})
	}

	unsafe := cloneRunnerPlan(runner)
	unsafe.TestCommands[0].Argv = []string{"sh", "-c", "pytest"}
	unsafeBody, _ := json.Marshal(unsafe)
	if _, err := ParseRunnerPlan(unsafeBody); !errors.Is(err, ErrInvalid) {
		t.Fatalf("shell command err=%v", err)
	}
	overlap := cloneRunnerPlan(runner)
	overlap.CreatablePaths = []string{"src/parser.py"}
	overlapBody, _ := json.Marshal(overlap)
	if _, err := ParseRunnerPlan(overlapBody); !errors.Is(err, ErrInvalid) {
		t.Fatalf("overlap err=%v", err)
	}
	if _, err := runner.Manifest(RunnerBinding{
		TicketID: "not-a-uuid", ProfileCapabilityID: "profile", Deadline: time.Now().Add(time.Hour),
	}, time.Now()); !errors.Is(err, ErrInvalid) {
		t.Fatalf("ticket binding err=%v", err)
	}
	overflow := cloneRunnerPlan(runner)
	overflow.TestCommands[0].TimeoutMilliseconds = 288230376151711745
	overflowBody, _ := json.Marshal(overflow)
	if _, err := ParseRunnerPlan(overflowBody); !errors.Is(err, ErrInvalid) {
		t.Fatalf("overflow timeout err=%v", err)
	}
}

func TestExecutionPlanDiagnosticsDoNotExposeCommands(t *testing.T) {
	_, runner, runtimePolicy, grader, resource := parseExecutionVector(t)
	values := []any{runner.TestCommands[0], grader.TestGroups[0], runner, grader, resource, Bundle{
		Runner: runner, RuntimePolicy: runtimePolicy, Grader: grader, Resource: resource,
	}}
	for _, value := range values {
		diagnostic := fmt.Sprintf("%v %#v", value, value)
		if strings.Contains(diagnostic, "pytest") || strings.Contains(diagnostic, "grader/") {
			t.Fatalf("diagnostic exposed execution authority: %s", diagnostic)
		}
	}
}
