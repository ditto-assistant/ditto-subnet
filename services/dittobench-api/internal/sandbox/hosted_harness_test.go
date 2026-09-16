package sandbox

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"
)

func hostedHarness() *LocalDocker {
	return NewHostedHarnessDocker(HostedHarnessConfig{
		MemoryLimitBytes: 2 << 30, ScratchLimitBytes: 1 << 30, CPUQuotaMillis: 1500, PidsLimit: 256,
		HostGatewayIP: "192.0.2.44", EgressNetwork: "coding-restricted", EgressProxy: "http://172.21.0.2:3128",
	})
}

func TestHostedHarnessSetsSwapEqualToMemoryAndNeverPulls(t *testing.T) {
	d := hostedHarness()
	args := d.runArgsForNetwork("coding-runtime.invalid/python@sha256:"+strings.Repeat("a", 64), nil, "ditto-job-x", "abc")
	if !hasFlagPair(args, "--memory", "2147483648") || !hasFlagPair(args, "--memory-swap", "2147483648") ||
		!hasFlagPair(args, "--pull", "never") || !hasFlagPair(args, "--cpus", "1.500") ||
		!hasFlagPair(args, "--pids-limit", "256") || !hasFlagPair(args, "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=1073741824") ||
		!hasFlagPair(args, "--user", "65532:65532") || !slices.Contains(args, "--read-only") ||
		!hasFlagPair(args, "--cap-drop", "ALL") || !hasFlagPair(args, "--ulimit", "nofile=1024:1024") ||
		!hasFlagPair(args, "--log-opt", "max-size=8m") || !hasFlagPair(args, "--network", "ditto-job-x") {
		t.Fatalf("hosted harness args: %v", args)
	}
	if args[0] != "run" || args[1] != "-d" || args[2] != "--pull" {
		t.Fatalf("--pull never must precede the container options: %v", args)
	}
	// The shared v8 sandbox keeps Docker's default swap and pull policy.
	legacy := NewLocalDocker()
	legacyArgs := legacy.runArgsForNetwork("operator-image:latest", nil, "", "")
	if slices.Contains(legacyArgs, "--memory-swap") || slices.Contains(legacyArgs, "--pull") {
		t.Fatalf("v8 sandbox args changed: %v", legacyArgs)
	}
}

func TestEnforcementWorkloadKeepsEveryHarnessArgument(t *testing.T) {
	d := hostedHarness()
	image := "coding-runtime.invalid/go@sha256:" + strings.Repeat("b", 64)
	production := d.runArgsForNetwork(image, map[string]string{}, "ditto-job-x", "abc")
	workload := []string{"workload", "hold", "--nonce", "0123456789abcdef", "--hold-ms", "10"}
	probe := d.runArgsForWorkload(image, map[string]string{}, "ditto-job-x", "abc", &enforcementWorkload{runner: "/opt/ditto-coding-hosted/rev/bin/probe", argv: workload})
	// Everything before the image is the production launch, then only the
	// read-only runner mount and entrypoint are inserted.
	prefix := production[:len(production)-1]
	if !slices.Equal(probe[:len(prefix)], prefix) {
		t.Fatalf("workload changed a harness argument:\n%v\n%v", production, probe)
	}
	tail := probe[len(prefix):]
	want := append([]string{
		"--mount", "type=bind,src=/opt/ditto-coding-hosted/rev/bin/probe,dst=" + EnforcementRunnerPath + ",readonly",
		"--entrypoint", EnforcementRunnerPath, image,
	}, workload...)
	if !slices.Equal(tail, want) {
		t.Fatalf("workload tail: %v", tail)
	}
}

func TestEnforcementWorkloadRunsThroughTheRetainingLaunch(t *testing.T) {
	d := hostedHarness()
	var calls [][]string
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		calls = append(calls, args)
		if args[0] == "run" {
			return []byte("start failed"), errors.New("no image")
		}
		return nil, nil
	}
	handle, err := d.RunEnforcementWorkload(t.Context(), "coding-runtime.invalid/go@sha256:"+strings.Repeat("b", 64), "/opt/probe", []string{"workload", "hold", "--nonce", "0123456789abcdef"})
	if err == nil || handle == nil || !strings.HasPrefix(handle.NetworkName, "ditto-job-") || !strings.HasPrefix(handle.ContainerID, "dittobench-") {
		t.Fatalf("a failed start must retain its handle: %#v %v", handle, err)
	}
	if len(calls) != 2 || calls[0][0] != "network" || calls[1][0] != "run" || !hasFlagPair(calls[1], "--entrypoint", EnforcementRunnerPath) {
		t.Fatalf("calls: %v", calls)
	}
	for _, bad := range []struct {
		runner string
		argv   []string
	}{
		{"relative/probe", []string{"workload", "hold"}},
		{"/opt/probe,dst=/etc", []string{"workload", "hold"}},
		{"/opt/probe", []string{"net-agent"}},
		{"/opt/probe", []string{"workload"}},
		{"/opt/probe", []string{"workload", ""}},
	} {
		if _, err := d.RunEnforcementWorkload(t.Context(), "image", bad.runner, bad.argv); err == nil {
			t.Fatalf("accepted %v %v", bad.runner, bad.argv)
		}
	}
}
