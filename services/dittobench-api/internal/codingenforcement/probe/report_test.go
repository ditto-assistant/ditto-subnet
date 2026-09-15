package probe

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedworker"
)

// ciEnforcementImages fills the committed CI image set template with the
// synthetic python image digest the rootless job creates.
func ciEnforcementImages(pythonDigest string) ([]byte, error) {
	raw, err := os.ReadFile("testdata/ci-enforcement-images.template.json")
	if err != nil {
		return nil, err
	}
	placeholder := []byte("sha256:" + strings.Repeat("0", 64))
	if bytes.Count(raw, placeholder) != 1 || len(pythonDigest) != len(placeholder) {
		return nil, errors.New("CI enforcement image template is malformed")
	}
	return bytes.Replace(raw, placeholder, []byte(pythonDigest), 1), nil
}

func rootlessDocker() *fakeDocker {
	return &fakeDocker{responses: map[string][]byte{
		"info --format {{json .SecurityOptions}}": []byte(`["name=rootless"]`),
		"info --format {{json .Labels}}":          []byte(`["io.heyditto.dittobench.isolated=true"]`),
	}}
}

func TestObserveRequestedConfigRefusesANonCanonicalProfile(t *testing.T) {
	_, err := ObserveHostedGradingRequestedConfig(context.Background(), rootlessDocker(), HostedGradingRequest{
		GradingProfile: []byte(`{"schema": "dittobench-coding-hosted-grading-profile-v2"}`),
		Repository:     "registry.example/coding",
	})
	if err == nil || !strings.Contains(err.Error(), "canonical") {
		t.Fatalf("non-canonical profile must be refused, got %v", err)
	}
}

func TestObserveRequestedConfigRefusesARootfulDaemon(t *testing.T) {
	docker := &fakeDocker{responses: map[string][]byte{"info --format {{json .SecurityOptions}}": []byte(`[]`)}}
	_, err := ObserveHostedGradingRequestedConfig(context.Background(), docker, HostedGradingRequest{
		Repository: "registry.example/coding",
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
	raw, err = ciEnforcementImages("sha256:" + sixtyFour)
	if err != nil {
		t.Fatal(err)
	}
	images, err := catalog.ParseEnforcementImages(raw)
	if err != nil {
		t.Fatal(err)
	}
	for _, language := range catalog.Languages {
		image := images.Images[language]
		manifest, err := profile.EnforcementProbeManifest(codinghostedworker.EnforcementProbeImage{
			ImageDigest: image.ImageDigest, BuildArgv: image.BuildArgv, TestArgv: image.TestArgv,
		}, time.Now().Add(30*time.Minute))
		if err != nil {
			t.Fatalf("CI grading profile does not convert to a %s hosted manifest: %v", language, err)
		}
		if manifest.GraderImageDigest != image.ImageDigest || !slices.Equal(manifest.TestGroups[0].Command.Argv, image.TestArgv["hidden"]) {
			t.Fatalf("%s manifest does not carry its pinned image and commands", language)
		}
	}
}

func TestObserveRequestedConfigRefusesUnpinnedImagesAndLanguages(t *testing.T) {
	profile, err := os.ReadFile("testdata/ci-grading-profile.json")
	if err != nil {
		t.Fatal(err)
	}
	images, err := ciEnforcementImages("sha256:" + sixtyFour)
	if err != nil {
		t.Fatal(err)
	}
	request := func(change func(*HostedGradingRequest)) error {
		value := HostedGradingRequest{
			GradingProfile: profile, EnforcementImages: images, Repository: "registry.example/coding",
			RunnerSHA256: func() (string, error) { return sixtyFour, nil },
		}
		change(&value)
		_, err := ObserveHostedGradingRequestedConfig(context.Background(), rootlessDocker(), value)
		return err
	}
	// Each refusal must come from its own guard, not from a later failure.
	for name, test := range map[string]struct {
		change func(*HostedGradingRequest)
		reason string
	}{
		"no image set":     {func(r *HostedGradingRequest) { r.EnforcementImages = nil }, "enforcement images are malformed"},
		"unknown language": {func(r *HostedGradingRequest) { r.Languages = []string{"java"} }, "unknown language"},
		"repeated":         {func(r *HostedGradingRequest) { r.Languages = []string{"python", "python"} }, "repeated"},
		"unmeasured runner": {func(r *HostedGradingRequest) {
			r.RunnerSHA256 = func() (string, error) { return "", errors.New("unreadable") }
		}, "could not be measured"},
		"malformed runner digest": {func(r *HostedGradingRequest) {
			r.RunnerSHA256 = func() (string, error) { return strings.Repeat("A", 64), nil }
		}, "could not be measured"},
	} {
		if err := request(test.change); err == nil || !strings.Contains(err.Error(), test.reason) {
			t.Errorf("%s: want %q, got %v", name, test.reason, err)
		}
	}
	sum := sha256.Sum256(profile)
	otherProfile := bytes.Replace(images, []byte(hex.EncodeToString(sum[:])), []byte(sixtyFour), 1)
	if bytes.Equal(otherProfile, images) {
		t.Fatal("template does not name the CI profile")
	}
	if err := request(func(r *HostedGradingRequest) { r.EnforcementImages = otherProfile }); err == nil ||
		!strings.Contains(err.Error(), "another grading profile") {
		t.Fatalf("an image set for another profile must be refused, got %v", err)
	}
}
