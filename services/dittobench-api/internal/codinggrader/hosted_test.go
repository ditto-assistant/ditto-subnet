package codinggrader

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

func hostedFixture(t *testing.T) (HostedManifest, HostedGradingAuthority, codingrunner.FrozenSubmission, []byte, []byte) {
	t.Helper()
	submission, visible, limits := frozenFixtureVersion(t, true)
	manifest, protected := graderFixture(t, submission, limits)
	manifest.CodingContractVersion = 2
	manifest.CaseID, manifest.VariantID = submission.CaseID, "opaque-hosted-variant"
	manifest.GraderContractSHA256 = HostedGraderContractSHA256()
	manifest.ResourceProfileSHA256, _ = HostedResourceProfileSHA256(manifest.ResourcePolicy)
	manifest.TestGroups = []TestGroupSpec{
		{Group: "hidden", Command: codingrunner.CommandSpec{ID: "hidden-tests", Argv: []string{"dittobench-test-driver", "hidden"}, Timeout: time.Minute}, ExpectedTotal: 3},
		{Group: "visible", Command: codingrunner.CommandSpec{ID: "visible-tests", Argv: []string{"dittobench-test-driver", "visible"}, Timeout: time.Minute}, ExpectedTotal: 2},
	}
	manifest.GraderPlanSHA256, _ = HostedGraderPlanSHA256(HostedManifest(manifest))
	authority := HostedGradingAuthority{
		HostedReplayAuthority: codingrunner.HostedReplayAuthority{
			HostedAuthority:   codingrunner.HostedAuthority{EvaluationID: "10000000-0000-4000-8000-000000000001", AttemptID: submission.CaseID, AssignmentSHA256: strings.Repeat("a", 64)},
			FrozenPatchSHA256: submission.FrozenPatchSHA256,
		}, GraderPlanSHA256: manifest.GraderPlanSHA256, Deadline: manifest.Deadline,
	}
	if err := HostedManifest(manifest).Validate(time.Now()); err != nil {
		t.Fatal(err)
	}
	return HostedManifest(manifest), authority, submission, visible, protected
}

func protectedReader(body []byte) ProtectedBundleOpener {
	return func(context.Context) (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(body)), nil }
}

func TestHostedGradeRunsNativeFreezeReplayAndTwoRealRequiredGroups(t *testing.T) {
	manifest, authority, submission, visible, protected := hostedFixture(t)
	executor := passingExecutor(Manifest(manifest))
	result := GradeHosted(t.Context(), authority, manifest, submission, bytes.NewReader(visible), protectedReader(protected), executor)
	if result.Result.TerminalDomain != codingcontract.DomainResolved || result.Result.RepairScoreMicros != 1_000_000 || result.CodingContractVersion != 2 || !result.ShadowOnly || result.WeightEligible {
		t.Fatalf("unexpected result: %#v", result)
	}
	if !slices.Equal(executor.groups, []string{"visible", "hidden"}) {
		t.Fatal("wrong execution groups")
	}
	if len(result.Result.ExecutionReceipts) != 3 || len(result.Result.Evidence.TestGroups) != 2 {
		t.Fatal("fabricated or missing test groups")
	}
	for _, receipt := range result.Result.ExecutionReceipts {
		if receipt.Schema != "dittobench-coding-grader-receipt-v2" {
			t.Fatal("receipt mislabeled as v1")
		}
	}
	if result.Result.Evidence.Validate() == nil {
		t.Fatal("legacy evidence validator accepted v2")
	}
	body, err := json.Marshal(result)
	if err != nil || !bytes.Contains(body, []byte(`"schema":"dittobench-coding-hosted-grading-result-v2"`)) {
		t.Fatal("wrong result version")
	}
}

func TestHostedGradeRejectsWrongFreezeOrPlanBeforeAnyPrivateRead(t *testing.T) {
	for _, change := range []string{"freeze", "plan", "rewritten_plan", "deadline", "v1"} {
		t.Run(change, func(t *testing.T) {
			manifest, authority, submission, visible, _ := hostedFixture(t)
			switch change {
			case "freeze":
				authority.FrozenPatchSHA256 = strings.Repeat("f", 64)
			case "plan":
				authority.GraderPlanSHA256 = strings.Repeat("f", 64)
			case "rewritten_plan":
				manifest.TestGroups[0].Command.Argv = []string{"dittobench-test-driver", "different"}
				manifest.GraderPlanSHA256, _ = HostedGraderPlanSHA256(manifest)
			case "v1":
				manifest.CodingContractVersion = 1
			case "deadline":
				manifest.Deadline = time.Now().Add(45 * time.Minute)
				authority.Deadline = manifest.Deadline.Add(-time.Minute)
			}
			opened := false
			executor := passingExecutor(Manifest(manifest))
			result := GradeHosted(t.Context(), authority, manifest, submission, bytes.NewReader(visible), func(context.Context) (io.ReadCloser, error) { opened = true; return nil, errors.New("unexpected read") }, executor)
			if result.Result.TerminalDomain != codingcontract.DomainControlPlaneIntegrity || opened || executor.preflightPlan != "" {
				t.Fatal("invalid authority reached execution or private access")
			}
		})
	}
}

func TestHostedGradePreservesFailureAndIntegritySemantics(t *testing.T) {
	for _, scenario := range []string{"test_failure", "count_drift", "executor_failure", "candidate_mutation", "grader_mutation", "receipt_drift", "nil_executor"} {
		t.Run(scenario, func(t *testing.T) {
			manifest, authority, submission, visible, protected := hostedFixture(t)
			executor := passingExecutor(Manifest(manifest))
			want := codingcontract.DomainControlPlaneIntegrity
			switch scenario {
			case "test_failure":
				executor.testRuns["hidden"] = TestRun{Completed: true, ReturnCode: 1, Passed: 2, Total: 3}
				want = codingcontract.DomainRepairFailure
			case "count_drift":
				executor.testRuns["hidden"] = TestRun{Completed: true, Passed: 3, Total: 4}
			case "executor_failure":
				executor.testErrors["hidden"] = errors.New("PRIVATE_MARKER")
				want = codingcontract.DomainValidatorInfrastructure
			case "candidate_mutation":
				executor.mutate = true
				want = codingcontract.DomainCandidateIntegrity
			case "grader_mutation":
				executor.mutateProtected = true
			case "receipt_drift":
				executor.corruptReceipt = true
			case "nil_executor":
				executor = nil
				want = codingcontract.DomainValidatorInfrastructure
			}
			result := GradeHosted(t.Context(), authority, manifest, submission, bytes.NewReader(visible), protectedReader(protected), executor)
			if result.Result.TerminalDomain != want || result.Result.RepairScoreMicros != 0 {
				t.Fatalf("wrong failure: %#v", result)
			}
			body, _ := json.Marshal(result)
			if bytes.Contains(body, []byte("PRIVATE_MARKER")) {
				t.Fatal("private error leaked")
			}
		})
	}
}

func TestHostedProfileCannotBeUsedByV1AndRejectsMissingOrUnsafeGroups(t *testing.T) {
	manifest, _, _, _, _ := hostedFixture(t)
	if Manifest(manifest).Validate(time.Now()) == nil {
		t.Fatal("v1 accepted hosted manifest")
	}
	if GraderContractSHA256() == HostedGraderContractSHA256() {
		t.Fatal("contract domains collide")
	}
	legacyResource, _ := ResourceProfileSHA256(manifest.ResourcePolicy)
	if legacyResource == manifest.ResourceProfileSHA256 {
		t.Fatal("resource domains collide")
	}
	for _, change := range []string{"missing", "duplicate", "driver", "count", "deadline"} {
		bad := HostedManifest(cloneManifest(Manifest(manifest)))
		switch change {
		case "missing":
			bad.TestGroups = bad.TestGroups[:1]
		case "duplicate":
			bad.TestGroups[1].Group = "hidden"
		case "driver":
			bad.TestGroups[0].Command.Argv = []string{"python", "hidden.py"}
		case "count":
			bad.TestGroups[0].ExpectedTotal = 1_000_001
		case "deadline":
			bad.Deadline = time.Now().Add(90 * time.Minute)
		}
		bad.GraderPlanSHA256, _ = HostedGraderPlanSHA256(bad)
		if bad.Validate(time.Now()) == nil {
			t.Fatalf("accepted %s", change)
		}
	}
}
