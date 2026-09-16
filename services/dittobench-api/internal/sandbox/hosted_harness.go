package sandbox

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// HostedHarnessConfig is the hosted-v2 harness sandbox authority: the
// execution profile's resource policy and the host-owned network and
// confinement names.
type HostedHarnessConfig struct {
	MemoryLimitBytes  uint64
	ScratchLimitBytes uint64
	CPUQuotaMillis    uint32
	PidsLimit         uint32
	HostGatewayIP     string
	EgressNetwork     string
	EgressProxy       string
	SeccompProfile    string
	AppArmorProfile   string
	// LaunchIntent is the hosted runtime's launch journal hook.
	LaunchIntent func(ctx context.Context, run string, containers, networks []string) error
}

// NewHostedHarnessDocker is the one construction of the hosted-v2 harness
// sandbox, shared by the hosted runtime and the native enforcement probe
// runner, so the limits the probe evidences are the limits the runtime passes.
// Unlike the v8 sandbox it sets --memory-swap equal to --memory and --pull
// never (Peyton, 2026-09-15).
func NewHostedHarnessDocker(config HostedHarnessConfig) *LocalDocker {
	memory := strconv.FormatUint(config.MemoryLimitBytes, 10)
	return &LocalDocker{
		HarnessPort: "8080", MemoryLimit: memory,
		TmpfsLimit: strconv.FormatUint(config.ScratchLimitBytes, 10),
		CPULimit:   fmt.Sprintf("%d.%03d", config.CPUQuotaMillis/1000, config.CPUQuotaMillis%1000),
		PidsLimit:  int(config.PidsLimit), StartTimeout: 2 * time.Minute,
		Harden: true, RequireRootless: true, RequireIsolatedDaemon: true,
		HostGatewayIP: config.HostGatewayIP, EgressNetwork: config.EgressNetwork, EgressProxy: config.EgressProxy,
		SeccompProfile: config.SeccompProfile, AppArmorProfile: config.AppArmorProfile,
		MemorySwapLimit: memory, PullNever: true, LaunchIntent: config.LaunchIntent,
	}
}

// EnforcementRunnerPath is where a native enforcement workload container
// mounts the measured probe runner, read-only, as its entrypoint.
const EnforcementRunnerPath = "/opt/dittobench-probe/runner"

type enforcementWorkload struct {
	runner string
	argv   []string
}

// RunEnforcementWorkload starts one native enforcement workload (B5 PR5)
// through the harness launch path: the same isolated job network, run
// arguments and failed-start retention as RunRetainingFailedHandle, with the
// measured probe runner bind-mounted read-only as the entrypoint instead of
// the harness image's server. A nonnil handle must be stopped with
// StopRetainingImage even when err is nonnil.
func (d *LocalDocker) RunEnforcementWorkload(ctx context.Context, image, runner string, argv []string) (*Handle, error) {
	if d == nil || ctx == nil {
		return nil, errors.New("sandbox enforcement workload context is required")
	}
	if !filepath.IsAbs(runner) || filepath.Clean(runner) != runner || strings.ContainsAny(runner, ",=\x00\n") {
		return nil, errors.New("sandbox enforcement runner path is invalid")
	}
	if len(argv) < 2 || argv[0] != "workload" {
		return nil, errors.New("sandbox enforcement workload argv is invalid")
	}
	for _, argument := range argv {
		if argument == "" || strings.ContainsAny(argument, "\x00\n") {
			return nil, errors.New("sandbox enforcement workload argv is invalid")
		}
	}
	return d.run(ctx, image, map[string]string{}, true, &enforcementWorkload{runner: runner, argv: append([]string(nil), argv...)})
}
