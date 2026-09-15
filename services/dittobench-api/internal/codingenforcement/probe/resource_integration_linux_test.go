//go:build native_probe_integration

package probe

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

// This test needs a real rootless Docker daemon carrying the isolated label
// (selected by DOCKER_HOST). The approved image must already be loaded and
// addressable as DITTOBENCH_NATIVE_PROBE_REPOSITORY@DITTOBENCH_NATIVE_PROBE_IMAGE_DIGEST.
// The CI job imports a synthetic supervisor-labelled image and tags it locally,
// which gives it a repository digest without any registry. The test never
// skips: the CI job gates the daemon prerequisites.
//
// It proves requested configuration only. The container is created, inspected
// and removed, never started; no enforcement is measured.
func TestHostedGradingRequestedConfigMatchesTheApprovedProfile(t *testing.T) {
	repository := os.Getenv("DITTOBENCH_NATIVE_PROBE_REPOSITORY")
	digest := os.Getenv("DITTOBENCH_NATIVE_PROBE_IMAGE_DIGEST")
	if repository == "" || digest == "" {
		t.Fatal("DITTOBENCH_NATIVE_PROBE_REPOSITORY and DITTOBENCH_NATIVE_PROBE_IMAGE_DIGEST are required")
	}
	raw, err := os.ReadFile("testdata/ci-grading-profile.json")
	if err != nil {
		t.Fatal(err)
	}
	profile, err := parseGradingProfile(raw)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 4*time.Minute)
	defer cancel()
	docker := ExecDocker{}
	report, err := ObserveHostedGradingRequestedConfig(ctx, docker, HostedGradingRequest{
		GradingProfile: raw, Repository: repository, Images: map[string]string{"python": digest},
	})
	if err != nil {
		t.Fatalf("observe requested configuration: %v", err)
	}
	if report["schema"] != ObservationReportSchema || report["enforcement_measured"] != false {
		t.Fatalf("report identity = %v", report)
	}
	entries := report["entries"].([]any)
	if len(entries) != 1 {
		t.Fatalf("entries = %v", entries)
	}
	requested := entries[0].(map[string]any)["requested_config"].(map[string]any)
	policy := profile.ResourcePolicy
	want := map[string]any{
		"memory_limit_bytes": int64(policy.MemoryLimitBytes), "memory_swap_bytes": int64(0),
		"cpu_quota_millis": int64(policy.CPUQuotaMillis), "pids_limit": int64(policy.PidsLimit),
		"scratch_limit_bytes": int64(policy.ScratchLimitBytes), "read_only_rootfs": true,
	}
	for key, value := range want {
		if requested[key] != value {
			t.Fatalf("requested %s = %v, approved profile says %v", key, requested[key], value)
		}
	}
	left, err := docker.Output(ctx, "ps", "-aq", "--filter", "label=io.heyditto.dittobench.coding-executor")
	if err != nil || strings.TrimSpace(string(left)) != "" {
		t.Fatalf("executor containers remain after inspection: %q %v", left, err)
	}
}
