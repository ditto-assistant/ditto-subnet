package probe

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
)

func rootlessDocker() *fakeDocker {
	return &fakeDocker{responses: map[string][]byte{
		"info --format {{json .SecurityOptions}}": []byte(`["name=rootless"]`),
		"info --format {{json .Labels}}":          []byte(`["io.heyditto.dittobench.isolated=true"]`),
	}}
}

func TestObserveRequestedConfigRefusesANonCanonicalProfile(t *testing.T) {
	_, err := ObserveHostedGradingRequestedConfig(context.Background(), rootlessDocker(), HostedGradingRequest{
		GradingProfile: []byte(`{"schema": "dittobench-coding-hosted-grading-profile-v2"}`),
		Repository:     "registry.example/coding", Images: map[string]string{"python": "sha256:" + sixtyFour},
	})
	if err == nil || !strings.Contains(err.Error(), "canonical") {
		t.Fatalf("non-canonical profile must be refused, got %v", err)
	}
}

func TestObserveRequestedConfigRefusesARootfulDaemon(t *testing.T) {
	docker := &fakeDocker{responses: map[string][]byte{"info --format {{json .SecurityOptions}}": []byte(`[]`)}}
	_, err := ObserveHostedGradingRequestedConfig(context.Background(), docker, HostedGradingRequest{
		Repository: "registry.example/coding", Images: map[string]string{"python": "sha256:" + sixtyFour},
	})
	if err == nil {
		t.Fatal("a rootful daemon must be refused")
	}
}

func TestRequestedConfigEntryNeverClaimsEnforcement(t *testing.T) {
	entry := requestedConfigEntry("python", ResolvedImage{ID: "sha256:" + sixtyFour, RepoDigest: approvedRef},
		codingexecutor.RequestedResourceConfig{MemoryLimitBytes: 1 << 30, NanoCPUs: 1_000_000_000, PidsLimit: 256, ReadonlyRootfs: true})
	if entry["source"] != "docker_inspect_created_unstarted_container" {
		t.Fatalf("entry source = %v", entry["source"])
	}
	for key := range entry {
		if key == "cgroup" || key == "observed" || key == "matched" || key == "expect" {
			t.Fatalf("requested-config entry carries an evidence key %q", key)
		}
	}
	encoded, err := EncodeReport(map[string]any{"schema": ObservationReportSchema, "enforcement_measured": false, "entries": []any{entry}})
	if err != nil || strings.Contains(string(encoded), "evidence-v1") {
		t.Fatalf("report encoding: %v %s", err, encoded)
	}
}

func TestCIGradingProfileIsAnAcceptedHostedProfile(t *testing.T) {
	raw, err := os.ReadFile("testdata/ci-grading-profile.json")
	if err != nil {
		t.Fatal(err)
	}
	profile, err := parseGradingProfile(raw)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := profile.EnforcementProbeManifest("sha256:"+sixtyFour, time.Now().Add(30*time.Minute)); err != nil {
		t.Fatalf("CI grading profile does not convert to a hosted manifest: %v", err)
	}
}
