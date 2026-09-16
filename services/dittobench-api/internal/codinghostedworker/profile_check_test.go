package codinghostedworker

import (
	"archive/tar"
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

func profileCheckFixture() GradingProfile {
	limits := codingrunner.DefaultLimits()
	limits.MaxBundleBytes = 4 << 20
	limits.MaxWorkspaceBytes = 16 << 20
	limits.MaxFileBytes = 4 << 20
	limits.MaxPatchBytes = 1024
	policy := codinggrader.ResourcePolicy{CandidateLimits: limits, ProtectedLimits: limits, MaxCombinedDiskBytes: 1 << 30, MemoryLimitBytes: 1 << 30, ScratchLimitBytes: 512 << 20, PidsLimit: 256, CPUQuotaMillis: 2000}
	command := func(group string) codingrunner.CommandSpec {
		return codingrunner.CommandSpec{ID: group, Argv: []string{"dittobench-test-driver", "--group", group, "--suite", "tests/" + group + ".py"}, Timeout: time.Minute}
	}
	return GradingProfile{Schema: "dittobench-coding-hosted-grading-profile-v2", ImageDigest: "sha256:" + strings.Repeat("a", 64), GraderContractSHA256: codinggrader.HostedGraderContractSHA256(), GraderBundleSHA256: strings.Repeat("b", 64), ResourcePolicy: policy, Build: codinggrader.BuildSpec{Command: codingrunner.CommandSpec{ID: "build", Argv: []string{"python", "-m", "compileall", "app.py"}, Timeout: time.Minute}}, TestGroups: []codinggrader.TestGroupSpec{{Group: "hidden", Command: command("hidden"), ExpectedTotal: 3}, {Group: "visible", Command: command("visible"), ExpectedTotal: 2}}, ExecutionTimeout: 10 * time.Minute}
}

func tarBundle(t *testing.T, names ...string) []byte {
	t.Helper()
	var body bytes.Buffer
	writer := tar.NewWriter(&body)
	for _, name := range names {
		if err := writer.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: 1, Typeflag: tar.TypeReg}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write([]byte("x")); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return body.Bytes()
}

func TestGradingProfileBytesAreExactCanonicalAndContractBound(t *testing.T) {
	profile := profileCheckFixture()
	body, err := GradingProfileBytes(profile)
	if err != nil || codingcontract.RequireExactCanonicalJSON(body) != nil {
		t.Fatal("valid profile did not produce canonical bytes")
	}
	if bytes.Contains(body, []byte("test_manifest")) {
		t.Fatal("hosted grading profile names a test manifest")
	}
	again, err := GradingProfileBytes(profile)
	if err != nil || !bytes.Equal(body, again) {
		t.Fatal("profile bytes are not deterministic")
	}
	for name, mutate := range map[string]func(*GradingProfile){
		"contract": func(p *GradingProfile) { p.GraderContractSHA256 = strings.Repeat("c", 64) },
		"order":    func(p *GradingProfile) { p.TestGroups[0], p.TestGroups[1] = p.TestGroups[1], p.TestGroups[0] },
		"count":    func(p *GradingProfile) { p.TestGroups[0].ExpectedTotal = 0 },
		"driver":   func(p *GradingProfile) { p.TestGroups[0].Command.Argv[0] = "pytest" },
		"timeout":  func(p *GradingProfile) { p.ExecutionTimeout = 2 * time.Hour },
		"schema":   func(p *GradingProfile) { p.Schema = "other" },
	} {
		changed := profileCheckFixture()
		mutate(&changed)
		if _, err := GradingProfileBytes(changed); err == nil {
			t.Fatalf("%s accepted", name)
		}
	}
}

func TestRequireSuiteMatchesGroupAndBundle(t *testing.T) {
	bundle := tarBundle(t, "tests/hidden.py", "./tests/visible.py")
	group := func(argv ...string) codinggrader.TestGroupSpec {
		return codinggrader.TestGroupSpec{Group: "hidden", Command: codingrunner.CommandSpec{ID: "hidden", Argv: append([]string{"dittobench-test-driver"}, argv...), Timeout: time.Minute}, ExpectedTotal: 1}
	}
	if requireSuite(group("--group", "hidden", "--suite", "tests/hidden.py", "--module", "app"), bundle) != nil {
		t.Fatal("present suite rejected")
	}
	if requireSuite(group("--group", "hidden", "--suite", "tests/visible.py"), bundle) != nil {
		t.Fatal("dot-prefixed tar entry rejected")
	}
	if requireSuite(group("--group", "hidden", "--authority", "rust.json"), bundle) != nil {
		t.Fatal("suite-free driver shape rejected")
	}
	for name, argv := range map[string][]string{
		"absent":         {"--group", "hidden", "--suite", "tests/other.py"},
		"group-mismatch": {"--group", "visible", "--suite", "tests/hidden.py"},
		"no-group":       {"--suite", "tests/hidden.py"},
		"two-groups":     {"--group", "hidden", "--group", "hidden", "--suite", "tests/hidden.py"},
		"two-suites":     {"--group", "hidden", "--suite", "tests/hidden.py", "--suite", "tests/hidden.py"},
		"dangling":       {"--group", "hidden", "--suite"},
		"absolute":       {"--group", "hidden", "--suite", "/tests/hidden.py"},
		"escape":         {"--group", "hidden", "--suite", "../tests/hidden.py"},
		"unclean":        {"--group", "hidden", "--suite", "tests//hidden.py"},
	} {
		if requireSuite(group(argv...), bundle) == nil {
			t.Fatalf("%s accepted", name)
		}
	}
	if requireSuite(group("--group", "hidden", "--suite", "tests/hidden.py"), nil) == nil {
		t.Fatal("missing bundle accepted")
	}
}

func TestCheckGradingProfileRejectsBeforeReadingIncompleteInputs(t *testing.T) {
	body, err := GradingProfileBytes(profileCheckFixture())
	if err != nil {
		t.Fatal(err)
	}
	objects := map[string][]byte{"catalog_record": {1}, "visible_bundle": {1}, "grader_bundle": {1}, "runtime_policy": {1}, "resource_profile": {1}}
	binding := GradingBinding{CatalogIndex: 0, TaskCommitmentSHA256: strings.Repeat("d", 64), PrivateReleaseSHA256: strings.Repeat("e", 64)}
	if CheckGradingProfile(context.Background(), binding, append(bytes.Clone(body[:len(body)-1]), ' ', '\n'), objects) == nil {
		t.Fatal("noncanonical profile accepted")
	}
	delete(objects, "runtime_policy")
	if CheckGradingProfile(context.Background(), binding, body, objects) == nil {
		t.Fatal("incomplete objects accepted")
	}
	objects["runtime_policy"] = nil
	if CheckGradingProfile(context.Background(), binding, body, objects) == nil {
		t.Fatal("empty object accepted")
	}
}
