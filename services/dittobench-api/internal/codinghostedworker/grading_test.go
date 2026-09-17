package codinghostedworker

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
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
	image := EnforcementProbeImage{
		ImageDigest: "sha256:" + strings.Repeat("d", 64),
		BuildArgv:   []string{"cargo", "build", "--offline"},
		TestArgv: map[string][]string{
			"hidden":  {"dittobench-test-driver", "--group", "hidden", "--authority", "rust/hidden.json", "--authority-sha256", strings.Repeat("a", 64)},
			"visible": {"dittobench-test-driver", "--group", "visible", "--authority", "rust/visible.json", "--authority-sha256", strings.Repeat("b", 64)},
		},
	}
	manifest, err := profile.EnforcementProbeManifest(image, time.Now().Add(30*time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if manifest.GraderImageDigest != image.ImageDigest || manifest.ResourcePolicy != profile.ResourcePolicy || manifest.Validate(time.Now()) != nil {
		t.Fatalf("manifest=%#v", manifest)
	}
	// Each language's own commands are used; limits, timeouts and IDs stay.
	if !slices.Equal(manifest.Build.Command.Argv, image.BuildArgv) || manifest.Build.Command.Timeout != time.Minute ||
		!slices.Equal(manifest.TestGroups[0].Command.Argv, image.TestArgv["hidden"]) ||
		manifest.TestGroups[1].Command.ID != "visible" || manifest.TestGroups[1].ExpectedTotal != 2 {
		t.Fatalf("per-language commands were not applied: %#v", manifest)
	}
	// The approved profile itself is never modified through shared slices.
	if profile.TestGroups[0].Command.Argv[1] != "hidden" || profile.Build.Command.Argv[0] != "dittobench-build" {
		t.Fatalf("approved profile was mutated: %#v", profile)
	}
	for name, broken := range map[string]EnforcementProbeImage{
		"missing group":    {ImageDigest: image.ImageDigest, BuildArgv: image.BuildArgv, TestArgv: map[string][]string{"hidden": image.TestArgv["hidden"]}},
		"renamed group":    {ImageDigest: image.ImageDigest, BuildArgv: image.BuildArgv, TestArgv: map[string][]string{"hidden": image.TestArgv["hidden"], "other": image.TestArgv["visible"]}},
		"no build":         {ImageDigest: image.ImageDigest, TestArgv: image.TestArgv},
		"untrusted driver": {ImageDigest: image.ImageDigest, BuildArgv: image.BuildArgv, TestArgv: map[string][]string{"hidden": {"cargo", "test"}, "visible": image.TestArgv["visible"]}},
		"tag image":        {ImageDigest: "latest", BuildArgv: image.BuildArgv, TestArgv: image.TestArgv},
	} {
		if _, err := profile.EnforcementProbeManifest(broken, time.Now().Add(30*time.Minute)); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
	profile.GraderContractSHA256 = strings.Repeat("e", 64)
	if _, err := profile.EnforcementProbeManifest(image, time.Now().Add(30*time.Minute)); err == nil {
		t.Fatal("a profile with another grader contract must be refused")
	}
}

// The pre-exec variant retimes exactly one group's expected total: the public
// fixture suite's, never the benchmark's, and never another group's.
func TestPreexecProbeManifestRetimesOnlyTheNamedGroup(t *testing.T) {
	profile := preexecProbeProfile()
	image := preexecProbeImage()
	original := slices.Clone(profile.TestGroups)
	manifest, err := profile.PreexecProbeManifest(image, time.Now().Add(30*time.Minute), "hidden", 2)
	if err != nil {
		t.Fatal(err)
	}
	if manifest.Validate(time.Now()) != nil {
		t.Fatalf("manifest is invalid: %v", manifest.Validate(time.Now()))
	}
	hidden := slices.IndexFunc(manifest.TestGroups, func(spec codinggrader.TestGroupSpec) bool { return spec.Group == "hidden" })
	visible := slices.IndexFunc(manifest.TestGroups, func(spec codinggrader.TestGroupSpec) bool { return spec.Group == "visible" })
	if manifest.TestGroups[hidden].ExpectedTotal != 2 {
		t.Fatalf("hidden expected total = %d", manifest.TestGroups[hidden].ExpectedTotal)
	}
	if manifest.TestGroups[visible].ExpectedTotal != original[1].ExpectedTotal {
		t.Fatalf("visible expected total = %d", manifest.TestGroups[visible].ExpectedTotal)
	}
	if !slices.Equal(manifest.TestGroups[hidden].Command.Argv, image.TestArgv["hidden"]) {
		t.Fatalf("hidden argv = %v", manifest.TestGroups[hidden].Command.Argv)
	}
	if original[0].ExpectedTotal != profile.TestGroups[0].ExpectedTotal {
		t.Fatal("the caller's profile was edited")
	}
}

// A suite of fewer than two tests, or a group the profile does not define,
// cannot produce a usable control observation, so neither builds a manifest.
func TestPreexecProbeManifestRefusesUnusableRequests(t *testing.T) {
	profile := preexecProbeProfile()
	image := preexecProbeImage()
	deadline := time.Now().Add(30 * time.Minute)
	for _, testCase := range []struct {
		name  string
		group string
		total uint32
	}{
		{"one test", "hidden", 1},
		{"no test", "hidden", 0},
		{"unknown group", "secret", 2},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			if _, err := profile.PreexecProbeManifest(image, deadline, testCase.group, testCase.total); err == nil {
				t.Fatal("the request built a manifest")
			}
		})
	}
}

func preexecProbeProfile() GradingProfile {
	return GradingProfile{
		Schema: "dittobench-coding-hosted-grading-profile-v2", ImageDigest: "sha256:" + strings.Repeat("a", 64),
		GraderContractSHA256: codinggrader.HostedGraderContractSHA256(), GraderBundleSHA256: strings.Repeat("b", 64),
		TestManifestSHA256: strings.Repeat("c", 64),
		ResourcePolicy: codinggrader.ResourcePolicy{
			CandidateLimits: codingrunner.DefaultLimits(), ProtectedLimits: codingrunner.DefaultLimits(),
			MaxCombinedDiskBytes: 4 << 30, MemoryLimitBytes: 1 << 30, ScratchLimitBytes: 1 << 30, PidsLimit: 256, CPUQuotaMillis: 1000,
		},
		Build: codinggrader.BuildSpec{Required: true, Command: codingrunner.CommandSpec{ID: "build", Argv: []string{"dittobench-build"}, Timeout: time.Minute}},
		TestGroups: []codinggrader.TestGroupSpec{
			{Group: "hidden", Command: codingrunner.CommandSpec{ID: "hidden", Argv: []string{"dittobench-test-driver", "hidden"}, Timeout: time.Minute}, ExpectedTotal: 37},
			{Group: "visible", Command: codingrunner.CommandSpec{ID: "visible", Argv: []string{"dittobench-test-driver", "visible"}, Timeout: time.Minute}, ExpectedTotal: 11},
		},
		ExecutionTimeout: 10 * time.Minute,
	}
}

func preexecProbeImage() EnforcementProbeImage {
	return EnforcementProbeImage{
		ImageDigest: "sha256:" + strings.Repeat("d", 64),
		BuildArgv:   []string{"go", "build", "./..."},
		TestArgv: map[string][]string{
			"hidden":  {"dittobench-test-driver", "--group", "hidden", "--suite", "hidden_test.go"},
			"visible": {"dittobench-test-driver", "--group", "visible", "--suite", "visible_test.go"},
		},
	}
}
