package codinghostedworker

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

func TestNativeGraderContractExport(t *testing.T) {
	path := os.Getenv("DITTO_GRADER_CONTRACT_OUT")
	if path == "" {
		t.Skip("Platform fixture export not configured")
	}
	body, _ := json.Marshal(map[string]string{"contract_sha256": codinggrader.HostedGraderContractSHA256()})
	if err := os.WriteFile(path, body, 0600); err != nil {
		t.Fatal(err)
	}
}

// Scripted trusted executor for the synthetic integration fixture, never
// available from the production constructor. It observes the pristine files.
type scriptedGrader struct {
	manifest      codinggrader.HostedManifest
	failPreflight bool
}

func (e *scriptedGrader) Preflight(_ context.Context, plan string) (codinggrader.ExecutorAttestation, error) {
	if e.failPreflight {
		return codinggrader.ExecutorAttestation{}, ErrAttempt
	}
	if plan != e.manifest.GraderPlanSHA256 {
		return codinggrader.ExecutorAttestation{}, ErrAttempt
	}
	return codinggrader.ExecutorAttestation{ExecutorInstanceID: "scripted-native-grader", GraderImageDigest: e.manifest.GraderImageDigest, GraderPlatform: e.manifest.GraderPlatform, GraderContractSHA256: e.manifest.GraderContractSHA256, GraderPlanSHA256: plan, ResourceProfileSHA256: e.manifest.ResourceProfileSHA256, NetworkDisabled: true, CandidateMountReadOnly: true, ProtectedMountHidden: true, ProcessGroupsIsolated: true}, nil
}
func (e *scriptedGrader) Build(context.Context, string, codingrunner.CommandSpec) (codinggrader.BuildRun, error) {
	return codinggrader.BuildRun{}, ErrAttempt
}
func (e *scriptedGrader) Test(ctx context.Context, workspace, protected string, group codinggrader.TestGroupSpec) (codinggrader.TestRun, error) {
	if ctx.Err() != nil {
		return codinggrader.TestRun{}, ctx.Err()
	}
	body, err := os.ReadFile(filepath.Join(workspace, "app.py"))
	if err != nil {
		return codinggrader.TestRun{}, err
	}
	hidden, err := os.ReadFile(filepath.Join(protected, "test_app.py"))
	if err != nil {
		return codinggrader.TestRun{}, err
	}
	commandSHA, err := codinggrader.CommandSHA256(group.Command.ID, group.Command.Argv, group.Command.Timeout.Milliseconds())
	if err != nil {
		return codinggrader.TestRun{}, err
	}
	result := codinggrader.TestRun{CommandID: group.Command.ID, CommandSHA256: commandSHA, ExecutorInstanceID: "scripted-native-grader", Completed: true, Total: 1, ReturnCode: 1}
	if strings.Contains(string(body), "print(2)") && strings.Contains(string(hidden), "assert 1 >= 0") {
		result.Passed = 1
		result.ReturnCode = 0
	}
	return result, nil
}

func TestEnforcementProbeManifestUsesTheHostedConversion(t *testing.T) {
	profile := GradingProfile{
		Schema: "dittobench-coding-hosted-grading-profile-v2", ImageDigest: "sha256:" + strings.Repeat("a", 64),
		GraderContractSHA256: codinggrader.HostedGraderContractSHA256(), GraderBundleSHA256: strings.Repeat("b", 64),
		TestManifestSHA256: strings.Repeat("c", 64),
		ResourcePolicy: codinggrader.ResourcePolicy{
			CandidateLimits: codingrunner.DefaultLimits(), ProtectedLimits: codingrunner.DefaultLimits(),
			MaxCombinedDiskBytes: 4 << 30, MemoryLimitBytes: 1 << 30, ScratchLimitBytes: 1 << 30, PidsLimit: 256, CPUQuotaMillis: 1000,
		},
		Build: codinggrader.BuildSpec{Required: true, Command: codingrunner.CommandSpec{ID: "build", Argv: []string{"dittobench-build"}, Timeout: time.Minute}},
		TestGroups: []codinggrader.TestGroupSpec{
			{Group: "hidden", Command: codingrunner.CommandSpec{ID: "hidden", Argv: []string{"dittobench-test-driver", "hidden"}, Timeout: time.Minute}, ExpectedTotal: 2},
			{Group: "visible", Command: codingrunner.CommandSpec{ID: "visible", Argv: []string{"dittobench-test-driver", "visible"}, Timeout: time.Minute}, ExpectedTotal: 2},
		},
		ExecutionTimeout: 10 * time.Minute,
	}
	image := "sha256:" + strings.Repeat("d", 64)
	manifest, err := profile.EnforcementProbeManifest(image, time.Now().Add(30*time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if manifest.GraderImageDigest != image || manifest.ResourcePolicy != profile.ResourcePolicy || manifest.Validate(time.Now()) != nil {
		t.Fatalf("manifest=%#v", manifest)
	}
	profile.GraderContractSHA256 = strings.Repeat("e", 64)
	if _, err := profile.EnforcementProbeManifest(image, time.Now().Add(30*time.Minute)); err == nil {
		t.Fatal("a profile with another grader contract must be refused")
	}
}
