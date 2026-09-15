package codingexecutor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
)

// RequestedResourceConfig is the resource configuration Docker reports for a
// created, never-started executor container built by the production launch
// code. It is requested configuration, not enforcement: no process ran, no
// cgroup file was read and no write was attempted. Because the policy
// inspection refuses any container whose configuration differs from the
// attested plan, a successful result can only echo the approved values; it
// proves the launch code requests them, nothing more.
type RequestedResourceConfig struct {
	GraderImageDigest string
	DriverProfile     string
	MemoryLimitBytes  int64
	MemorySwapBytes   int64
	NanoCPUs          int64
	PidsLimit         int64
	ReadonlyRootfs    bool
	ScratchLimitBytes int64
}

// InspectRequestedResourceConfig creates one grading-mode container through the
// production create path, runs the production policy inspection, reads back
// Docker's reported resource configuration and removes the container by exact
// id. The container is never started.
func (executor *Executor) InspectRequestedResourceConfig(ctx context.Context) (RequestedResourceConfig, error) {
	if executor == nil || ctx == nil {
		return RequestedResourceConfig{}, errors.New("coding executor requested-config inspection context is required")
	}
	if executor.config.AuthoringOnly {
		return RequestedResourceConfig{}, errors.New("coding executor requested-config inspection requires a grading executor")
	}
	if err := executor.ensurePreflight(ctx); err != nil {
		return RequestedResourceConfig{}, err
	}
	requested := RequestedResourceConfig{
		GraderImageDigest: executor.config.Manifest.GraderImageDigest,
		DriverProfile:     executor.driverProfile,
		ScratchLimitBytes: int64(executor.temporaryBytes()),
	}
	err := executor.withProbeContainer(ctx, func(container, workspace, protected, control string) error {
		if err := executor.inspectContainerPolicy(ctx, container, modeTest, workspace, protected, control); err != nil {
			return err
		}
		inspection, err := executor.inspectContainerConfig(ctx, container)
		if err != nil {
			return err
		}
		requested.MemoryLimitBytes = inspection.HostConfig.Memory
		requested.MemorySwapBytes = inspection.HostConfig.MemorySwap - inspection.HostConfig.Memory
		requested.NanoCPUs = inspection.HostConfig.NanoCPUs
		requested.PidsLimit = inspection.HostConfig.PidsLimit
		requested.ReadonlyRootfs = inspection.HostConfig.ReadonlyRootfs
		return nil
	})
	if err != nil {
		return RequestedResourceConfig{}, err
	}
	return requested, nil
}

func (executor *Executor) inspectContainerConfig(ctx context.Context, container string) (dockerContainerInspection, error) {
	raw, err := executor.docker.Output(ctx, "container", "inspect", container)
	if err != nil {
		return dockerContainerInspection{}, fmt.Errorf("inspect coding sandbox resource configuration: %w", err)
	}
	var values []dockerContainerInspection
	if err := json.Unmarshal(bytes.TrimSpace(raw), &values); err != nil || len(values) != 1 {
		return dockerContainerInspection{}, errors.New("coding sandbox resource inspection is invalid")
	}
	return values[0], nil
}
