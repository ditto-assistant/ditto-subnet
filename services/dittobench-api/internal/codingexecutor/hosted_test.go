package codingexecutor

import (
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

func hostedConfig(t *testing.T) Config {
	t.Helper()
	config := testConfig(t)
	config.Manifest.CodingContractVersion = 2
	config.Manifest.GraderContractSHA256 = codinggrader.HostedGraderContractSHA256()
	config.Manifest.ResourceProfileSHA256, _ = codinggrader.HostedResourceProfileSHA256(config.Manifest.ResourcePolicy)
	config.Manifest.TestGroups = []codinggrader.TestGroupSpec{
		{Group: "hidden", Command: codingrunner.CommandSpec{ID: "hidden", Argv: []string{trustedTestDriverName, "hidden.json"}, Timeout: time.Second}, ExpectedTotal: 2},
		{Group: "visible", Command: codingrunner.CommandSpec{ID: "visible", Argv: []string{trustedTestDriverName, "visible.json"}, Timeout: time.Second}, ExpectedTotal: 1},
	}
	config.Manifest.GraderPlanSHA256, _ = codinggrader.HostedGraderPlanSHA256(codinggrader.HostedManifest(config.Manifest))
	config.hosted = true
	return config
}

func TestHostedExecutorUsesPinnedV2PlanAndExistingSandbox(t *testing.T) {
	config := hostedConfig(t)
	docker := newFakeDocker(config)
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	attestation, err := executor.Preflight(t.Context(), config.Manifest.GraderPlanSHA256)
	if err != nil || attestation.GraderContractSHA256 != codinggrader.HostedGraderContractSHA256() || !attestation.ProtectedMountHidden || !attestation.NetworkDisabled {
		t.Fatalf("preflight failed: %#v %v", attestation, err)
	}
	group := config.Manifest.TestGroups[0]
	result, err := executor.Test(t.Context(), t.TempDir(), t.TempDir(), group)
	if err != nil || result.Total != group.ExpectedTotal || !result.Completed {
		t.Fatalf("test receipt failed: %#v %v", result, err)
	}
	if len(docker.active) != 0 {
		t.Fatal("container was not removed")
	}
}

func TestHostedExecutorRejectsFixtureLegacyAndMismatchedOperations(t *testing.T) {
	config := hostedConfig(t)
	if _, err := New(config); err == nil {
		t.Fatal("legacy constructor accepted hosted mode")
	}
	legacyInput := config
	legacyInput.hosted = false
	if _, err := New(legacyInput); err == nil {
		t.Fatal("legacy constructor inferred v2")
	}
	fixture := config
	fixture.AllowCertificationImage = true
	if _, err := NewHostedGrading(fixture, codinggrader.HostedManifest(fixture.Manifest)); err == nil {
		t.Fatal("hosted grader accepted fixture opt-in")
	}
	docker := newFakeDocker(config)
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := executor.Preflight(t.Context(), strings.Repeat("f", 64)); err == nil {
		t.Fatal("wrong plan accepted")
	}
	if _, err := executor.Execute(t.Context(), "unused", config.Manifest.Build.Command); err == nil {
		t.Fatal("authoring allowed on grading executor")
	}
	badBuild := config.Manifest.Build.Command
	badBuild.Argv = []string{"python", "different.py"}
	if _, err := executor.Build(t.Context(), "unused", badBuild); err == nil {
		t.Fatal("unplanned build accepted")
	}
	for _, bad := range []codinggrader.TestGroupSpec{
		{Group: "other", Command: config.Manifest.TestGroups[0].Command, ExpectedTotal: 2},
		{Group: "hidden", Command: config.Manifest.TestGroups[0].Command, ExpectedTotal: 3},
		{Group: "hidden", Command: config.Manifest.TestGroups[1].Command, ExpectedTotal: 2},
	} {
		if _, err := executor.Test(t.Context(), "unused", "unused", bad); err == nil {
			t.Fatal("unplanned test accepted")
		}
	}
	if len(docker.runs) != 0 {
		t.Fatal("invalid operation reached Docker")
	}
}

func TestHostedPreflightStillRejectsFixtureAndUnisolatedDaemons(t *testing.T) {
	for _, mode := range []string{"fixture", "rootful", "unisolated"} {
		t.Run(mode, func(t *testing.T) {
			config := hostedConfig(t)
			docker := newFakeDocker(config)
			switch mode {
			case "fixture":
				docker.image.Config.Labels["io.heyditto.dittobench.coding-supervisor-fixture"] = "true"
			case "rootful":
				docker.security = []string{"name=seccomp"}
			case "unisolated":
				docker.labels = map[string]string{}
			}
			executor, err := newWithDocker(config, docker)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := executor.Preflight(t.Context(), config.Manifest.GraderPlanSHA256); err == nil {
				t.Fatal("unsafe hosted sandbox accepted")
			}
		})
	}
}
