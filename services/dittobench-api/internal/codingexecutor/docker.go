package codingexecutor

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"slices"
	"strings"
	"time"
)

const maxDockerControlOutput = 1 << 20

type execDocker struct{}

func (execDocker) Output(ctx context.Context, args ...string) ([]byte, error) {
	var output boundedBuffer
	output.maximum = maxDockerControlOutput
	command := exec.CommandContext(ctx, "docker", args...)
	command.Stdout = &output
	command.Stderr = &output
	err := command.Run()
	if output.overflow {
		return nil, errors.New("docker control output exceeded its bound")
	}
	return append([]byte(nil), output.Bytes()...), err
}

func (execDocker) Run(ctx context.Context, args ...string) error {
	command := exec.CommandContext(ctx, "docker", args...)
	command.Stdout = io.Discard
	command.Stderr = io.Discard
	return command.Run()
}

type boundedBuffer struct {
	bytes.Buffer
	maximum  int
	overflow bool
}

func (buffer *boundedBuffer) Write(value []byte) (int, error) {
	original := len(value)
	remaining := buffer.maximum - buffer.Len()
	if remaining <= 0 {
		buffer.overflow = true
		return original, nil
	}
	if len(value) > remaining {
		value = value[:remaining]
		buffer.overflow = true
	}
	_, _ = buffer.Buffer.Write(value)
	return original, nil
}

type dockerImageInspection struct {
	ID           string   `json:"Id"`
	RepoDigests  []string `json:"RepoDigests"`
	OS           string   `json:"Os"`
	Architecture string   `json:"Architecture"`
	Config       struct {
		Env     []string          `json:"Env"`
		Volumes map[string]any    `json:"Volumes"`
		Labels  map[string]string `json:"Labels"`
	} `json:"Config"`
}

type dockerMountInspection struct {
	Type        string `json:"Type"`
	Source      string `json:"Source"`
	Destination string `json:"Destination"`
	RW          bool   `json:"RW"`
}

type dockerContainerInspection struct {
	ID     string `json:"Id"`
	Name   string `json:"Name"`
	Image  string `json:"Image"`
	Config struct {
		Image      string   `json:"Image"`
		User       string   `json:"User"`
		Entrypoint []string `json:"Entrypoint"`
		Env        []string `json:"Env"`
	} `json:"Config"`
	HostConfig struct {
		ReadonlyRootfs bool              `json:"ReadonlyRootfs"`
		NetworkMode    string            `json:"NetworkMode"`
		IpcMode        string            `json:"IpcMode"`
		UTSMode        string            `json:"UTSMode"`
		CapDrop        []string          `json:"CapDrop"`
		CapAdd         []string          `json:"CapAdd"`
		SecurityOpt    []string          `json:"SecurityOpt"`
		Memory         int64             `json:"Memory"`
		MemorySwap     int64             `json:"MemorySwap"`
		NanoCPUs       int64             `json:"NanoCpus"`
		PidsLimit      int64             `json:"PidsLimit"`
		Tmpfs          map[string]string `json:"Tmpfs"`
		Privileged     bool              `json:"Privileged"`
		AutoRemove     bool              `json:"AutoRemove"`
		LogConfig      struct {
			Type string `json:"Type"`
		} `json:"LogConfig"`
	} `json:"HostConfig"`
	Mounts []dockerMountInspection `json:"Mounts"`
	State  struct {
		Running   bool `json:"Running"`
		OOMKilled bool `json:"OOMKilled"`
		ExitCode  int  `json:"ExitCode"`
	} `json:"State"`
}

func (executor *Executor) preflightDocker(ctx context.Context) error {
	securityRaw, err := executor.docker.Output(ctx, "info", "--format", "{{json .SecurityOptions}}")
	if err != nil {
		return fmt.Errorf("inspect coding Docker security options: %w", err)
	}
	var security []string
	if err := json.Unmarshal(bytes.TrimSpace(securityRaw), &security); err != nil ||
		!slices.ContainsFunc(security, func(value string) bool {
			value = strings.ToLower(strings.TrimSpace(value))
			return value == "rootless" || value == "name=rootless" || strings.HasPrefix(value, "name=rootless,")
		}) {
		return errors.New("coding Docker daemon is not rootless")
	}
	labelsRaw, err := executor.docker.Output(ctx, "info", "--format", "{{json .Labels}}")
	if err != nil {
		return fmt.Errorf("inspect coding Docker labels: %w", err)
	}
	if !daemonHasLabel(labelsRaw, isolatedDaemonLabel) {
		return errors.New("coding Docker daemon lacks the isolated ownership label")
	}
	imageRaw, err := executor.docker.Output(ctx, "image", "inspect", executor.config.ImageRef)
	if err != nil {
		return fmt.Errorf("inspect coding supervisor image: %w", err)
	}
	var images []dockerImageInspection
	if err := json.Unmarshal(bytes.TrimSpace(imageRaw), &images); err != nil || len(images) != 1 {
		return errors.New("coding supervisor image inspection is invalid")
	}
	image := images[0]
	digest := executor.config.Manifest.GraderImageDigest
	digestMatches := slices.ContainsFunc(image.RepoDigests, func(value string) bool {
		return strings.HasSuffix(value, "@"+digest)
	})
	if !digestMatches || !validDockerObjectID(image.ID) ||
		image.OS+"/"+image.Architecture != executor.config.Manifest.GraderPlatform {
		return errors.New("coding supervisor image digest or platform mismatch")
	}
	if len(image.Config.Volumes) != 0 || slices.ContainsFunc(image.Config.Env, credentialImageEnvironment) ||
		image.Config.Labels["io.heyditto.dittobench.coding-supervisor-contract"] != "1" {
		return errors.New("coding supervisor image declares a volume or credential-shaped environment")
	}
	if image.Config.Labels["io.heyditto.dittobench.coding-supervisor-fixture"] == "true" &&
		!executor.config.AllowCertificationImage {
		return errors.New("coding supervisor certification fixture is not a production grader image")
	}
	executor.imageID = image.ID
	return executor.probeContainerPolicy(ctx)
}

func validDockerObjectID(value string) bool {
	if !strings.HasPrefix(value, "sha256:") {
		return false
	}
	digest := strings.TrimPrefix(value, "sha256:")
	decoded, err := hex.DecodeString(digest)
	return err == nil && len(decoded) == 32 && hex.EncodeToString(decoded) == digest
}

func credentialImageEnvironment(value string) bool {
	name, _, _ := strings.Cut(value, "=")
	upper := strings.ToUpper(name)
	if upper == "HTTP_PROXY" || upper == "HTTPS_PROXY" || upper == "ALL_PROXY" {
		return true
	}
	for _, marker := range []string{"KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"} {
		if strings.Contains(upper, marker) {
			return true
		}
	}
	return false
}

func daemonHasLabel(body []byte, expected string) bool {
	trimmed := bytes.TrimSpace(body)
	var labels []string
	if json.Unmarshal(trimmed, &labels) == nil {
		return slices.Contains(labels, expected)
	}
	var labelMap map[string]string
	if json.Unmarshal(trimmed, &labelMap) == nil {
		parts := strings.SplitN(expected, "=", 2)
		return len(parts) == 2 && labelMap[parts[0]] == parts[1]
	}
	return false
}

func (executor *Executor) inspectContainerPolicy(
	ctx context.Context,
	container string,
	mode executionMode,
	workspace string,
	protected string,
	control string,
) error {
	raw, err := executor.docker.Output(ctx, "container", "inspect", container)
	if err != nil {
		return fmt.Errorf("inspect coding sandbox policy: %w", err)
	}
	var values []dockerContainerInspection
	if err := json.Unmarshal(bytes.TrimSpace(raw), &values); err != nil || len(values) != 1 {
		return errors.New("coding sandbox policy inspection is invalid")
	}
	value := values[0]
	policy := executor.config.Manifest.ResourcePolicy
	security := append([]string(nil), value.HostConfig.SecurityOpt...)
	for index, option := range security {
		if option == "no-new-privileges:true" {
			security[index] = "no-new-privileges"
		}
	}
	slices.Sort(security)
	wantSecurity := []string{"no-new-privileges"}
	if executor.config.AppArmorProfile != "" {
		wantSecurity = append(wantSecurity, "apparmor="+executor.config.AppArmorProfile)
	}
	if executor.config.SeccompProfile != "" {
		wantSecurity = append(wantSecurity, "seccomp="+executor.config.SeccompProfile)
	}
	slices.Sort(wantSecurity)
	capDrop := upperSorted(value.HostConfig.CapDrop)
	capAdd := upperSorted(value.HostConfig.CapAdd)
	wantCapAdd := []string{"CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"}
	if value.Image != executor.imageID || value.Config.User != "0:0" ||
		!slices.Equal(value.Config.Entrypoint, []string{executor.config.SupervisorPath}) ||
		!value.HostConfig.ReadonlyRootfs || value.HostConfig.NetworkMode != "none" ||
		value.HostConfig.IpcMode != "none" || value.HostConfig.UTSMode != "private" ||
		!slices.Equal(capDrop, []string{"ALL"}) || !slices.Equal(capAdd, wantCapAdd) ||
		!slices.Equal(security, wantSecurity) || value.HostConfig.Privileged || value.HostConfig.AutoRemove ||
		value.HostConfig.Memory != int64(policy.MemoryLimitBytes) ||
		value.HostConfig.MemorySwap != int64(policy.MemoryLimitBytes) ||
		value.HostConfig.NanoCPUs != int64(policy.CPUQuotaMillis)*1_000_000 ||
		value.HostConfig.PidsLimit != int64(policy.PidsLimit) || value.HostConfig.LogConfig.Type != "none" ||
		!tmpfsMatches(value.HostConfig.Tmpfs["/tmp"], policy.ScratchLimitBytes) ||
		slices.ContainsFunc(value.Config.Env, credentialImageEnvironment) {
		return errors.New("coding sandbox container policy does not satisfy the attested plan")
	}
	return verifyContainerMounts(value.Mounts, mode, workspace, protected, control)
}

func (executor *Executor) inspectTerminalState(container string) (dockerContainerInspection, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	raw, err := executor.docker.Output(ctx, "container", "inspect", container)
	if err != nil {
		return dockerContainerInspection{}, fmt.Errorf("inspect coding sandbox terminal state: %w", err)
	}
	var values []dockerContainerInspection
	if err := json.Unmarshal(bytes.TrimSpace(raw), &values); err != nil || len(values) != 1 {
		return dockerContainerInspection{}, errors.New("coding sandbox terminal state is invalid")
	}
	return values[0], nil
}

func upperSorted(values []string) []string {
	result := make([]string, len(values))
	for index, value := range values {
		result[index] = strings.ToUpper(value)
	}
	slices.Sort(result)
	return result
}

func tmpfsMatches(options string, expected uint64) bool {
	parts := strings.Split(options, ",")
	wantSize := "size=" + fmt.Sprint(expected)
	return slices.Contains(parts, "rw") && slices.Contains(parts, "noexec") && slices.Contains(parts, "nosuid") &&
		slices.Contains(parts, "nodev") && slices.Contains(parts, wantSize)
}

func verifyContainerMounts(
	mounts []dockerMountInspection,
	mode executionMode,
	workspace string,
	protected string,
	control string,
) error {
	want := map[string]struct {
		source string
		rw     bool
	}{
		workspaceMountPath: {source: workspace, rw: false},
		controlMountPath:   {source: control, rw: true},
	}
	if mode == modeTest {
		want[protectedMountPath] = struct {
			source string
			rw     bool
		}{source: protected, rw: false}
	}
	for _, mount := range mounts {
		if mount.Type == "tmpfs" && mount.Destination == "/tmp" {
			continue
		}
		expected, ok := want[mount.Destination]
		if !ok || mount.Type != "bind" || mount.Source != expected.source || mount.RW != expected.rw {
			return errors.New("coding sandbox mount policy mismatch")
		}
		delete(want, mount.Destination)
	}
	if len(want) != 0 {
		return errors.New("coding sandbox is missing an expected mount")
	}
	return nil
}

func (executor *Executor) cleanupContainer(target string, uncertain bool) error {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	consecutiveAbsent := 0
	for {
		out, err := executor.docker.Output(ctx, "container", "inspect", target)
		if err != nil {
			if !noSuchContainer(out) {
				return fmt.Errorf("inspect coding sandbox before removal: %w", err)
			}
			consecutiveAbsent++
			if !uncertain || consecutiveAbsent >= 20 {
				return nil
			}
		} else {
			consecutiveAbsent = 0
			if _, err := executor.docker.Output(ctx, "rm", "-f", "-v", target); err != nil {
				return fmt.Errorf("remove coding sandbox container: %w", err)
			}
			uncertain = false
		}
		select {
		case <-ctx.Done():
			return errors.New("coding sandbox cleanup could not prove absence")
		case <-time.After(100 * time.Millisecond):
		}
	}
}

func noSuchContainer(output []byte) bool {
	text := strings.ToLower(string(output))
	return strings.Contains(text, "no such container") || strings.Contains(text, "no such object")
}
