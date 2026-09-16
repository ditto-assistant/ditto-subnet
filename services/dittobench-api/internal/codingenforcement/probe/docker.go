package probe

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"slices"
	"strings"
)

// DockerCLI runs a docker subcommand and returns its combined output. It is the
// only outside effect this package has, and the runner reaches nothing but the
// local daemon through it.
type DockerCLI interface {
	Output(ctx context.Context, args ...string) ([]byte, error)
}

// ExecDocker runs the real docker binary. The daemon it reaches is chosen by
// the caller's DOCKER_HOST; the runner never sets a context, TLS or credential.
type ExecDocker struct {
	// Binary defaults to "docker" when empty.
	Binary string
}

const maxDockerOutput = 1 << 20

// Output runs docker and returns bounded combined output.
func (d ExecDocker) Output(ctx context.Context, args ...string) ([]byte, error) {
	binary := d.Binary
	if binary == "" {
		binary = "docker"
	}
	var buffer bytes.Buffer
	command := exec.CommandContext(ctx, binary, args...)
	command.Stdout = &buffer
	command.Stderr = &buffer
	err := command.Run()
	if buffer.Len() > maxDockerOutput {
		return nil, errors.New("probe: docker output exceeded its bound")
	}
	return append([]byte(nil), buffer.Bytes()...), err
}

// requireRootlessIsolatedDaemon fails closed unless the daemon is rootless and
// carries the release-owned isolated ownership label, matching the executor's
// own preflight. The probe never runs untrusted containers on a host daemon.
func requireRootlessIsolatedDaemon(ctx context.Context, cli DockerCLI) error {
	security, err := cli.Output(ctx, "info", "--format", "{{json .SecurityOptions}}")
	if err != nil {
		return fmt.Errorf("probe: docker daemon unavailable: %s", strings.TrimSpace(string(security)))
	}
	if !strings.Contains(strings.ToLower(string(security)), "rootless") {
		return errors.New("probe: docker daemon is not rootless")
	}
	labels, err := cli.Output(ctx, "info", "--format", "{{json .Labels}}")
	if err != nil {
		return fmt.Errorf("probe: docker labels unavailable: %s", strings.TrimSpace(string(labels)))
	}
	if !daemonHasLabel(labels, isolatedDaemonLabel) {
		return errors.New("probe: docker daemon lacks the isolated ownership label")
	}
	return nil
}

// daemonHasLabel mirrors the executor's exact label match: the daemon's label
// list (or map) must carry the exact key=value entry, never a substring of an
// unrelated label.
func daemonHasLabel(body []byte, expected string) bool {
	trimmed := bytes.TrimSpace(body)
	var labels []string
	if json.Unmarshal(trimmed, &labels) == nil {
		return slices.Contains(labels, expected)
	}
	var labelMap map[string]string
	if json.Unmarshal(trimmed, &labelMap) == nil {
		key, value, ok := strings.Cut(expected, "=")
		got, present := labelMap[key]
		return ok && present && got == value
	}
	return false
}

// isolatedDaemonLabel mirrors the label the executor and sandbox require.
const isolatedDaemonLabel = "io.heyditto.dittobench.isolated=true"
