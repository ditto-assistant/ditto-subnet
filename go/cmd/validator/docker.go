package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

// harnessDriver launches a miner harness subprocess and exchanges JSON
// lines on stdio. It is the Go counterpart of
// ditto/bench/runner/docker.HarnessDriver.
//
// Implementations are split via the launcher interface so unit tests
// can swap an in-process echo binary for the real `docker run` without
// shelling out.
type harnessDriver struct {
	launcher launcher
	hotkey   string
	cmd      *exec.Cmd
	stdin    io.WriteCloser
	stdout   *bufio.Reader
	stderr   io.ReadCloser
}

// launcher abstracts how a harness is started so the validator's e2e
// test can use a plain Go binary (`cmd/echo-harness`) while production
// uses `docker run`.
type launcher interface {
	build(ctx context.Context, hotkey string) (*exec.Cmd, error)
	describe(hotkey string) string
}

const harnessScanMax = 8 * 1024 * 1024

func newHarnessDriver(ctx context.Context, hotkey string, l launcher) (*harnessDriver, error) {
	cmd, err := l.build(ctx, hotkey)
	if err != nil {
		return nil, err
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("launch %s: %w", l.describe(hotkey), err)
	}
	return &harnessDriver{
		launcher: l,
		hotkey:   hotkey,
		cmd:      cmd,
		stdin:    stdin,
		stdout:   bufio.NewReaderSize(stdout, harnessScanMax),
		stderr:   stderr,
	}, nil
}

// Close terminates the harness. It closes stdin (which a well-behaved
// harness uses as its shutdown signal) and waits a bounded time before
// killing the process.
func (h *harnessDriver) Close() error {
	if h == nil || h.cmd == nil {
		return nil
	}
	_ = h.stdin.Close()
	done := make(chan error, 1)
	go func() { done <- h.cmd.Wait() }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		_ = h.cmd.Process.Kill()
		<-done
	}
	return nil
}

var errHarnessTimeout = errors.New("harness: deadline exceeded")

// send writes one ChallengeRequest and reads back one MinerResponse,
// enforcing deadlineMs. A timeout returns errHarnessTimeout; the caller
// records a zero score with a deadline_overrun note.
func (h *harnessDriver) send(ctx context.Context, req bittensor.ChallengeRequest, deadlineMs int64) (bittensor.MinerResponse, error) {
	payload, err := json.Marshal(req)
	if err != nil {
		return bittensor.MinerResponse{}, fmt.Errorf("marshal request: %w", err)
	}
	payload = append(payload, '\n')
	if _, err := h.stdin.Write(payload); err != nil {
		return bittensor.MinerResponse{}, fmt.Errorf("write request: %w", err)
	}

	deadline := time.Now().Add(time.Duration(deadlineMs) * time.Millisecond)
	readCtx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()

	type result struct {
		line []byte
		err  error
	}
	resCh := make(chan result, 1)
	go func() {
		line, err := h.stdout.ReadBytes('\n')
		resCh <- result{line: line, err: err}
	}()
	select {
	case r := <-resCh:
		if r.err != nil && len(r.line) == 0 {
			return bittensor.MinerResponse{}, fmt.Errorf("read response: %w", r.err)
		}
		var resp bittensor.MinerResponse
		trimmed := []byte(strings.TrimRight(string(r.line), "\n"))
		if err := json.Unmarshal(trimmed, &resp); err != nil {
			return bittensor.MinerResponse{}, fmt.Errorf("invalid response JSON: %w", err)
		}
		return resp, nil
	case <-readCtx.Done():
		return bittensor.MinerResponse{}, errHarnessTimeout
	}
}

// Unmarshal a JSON string into a MinerResponse via stdlib.
func (h *harnessDriver) unmarshal(line []byte, out *bittensor.MinerResponse) error {
	return json.Unmarshal(line, out)
}

// dockerLauncher implements launcher by shelling out to `docker run`.
// Image is required; SelfTest in validator main.go switches to the
// in-process echo path instead.
type dockerLauncher struct {
	commitment MinerCommitment
}

func (d dockerLauncher) describe(hotkey string) string {
	return "docker:" + d.commitment.Image
}

func (d dockerLauncher) build(ctx context.Context, _ string) (*exec.Cmd, error) {
	if d.commitment.Image == "" {
		return nil, errors.New("commitment.Image is empty; cannot launch docker harness")
	}
	args := []string{
		"run", "--rm", "-i",
		"--network=" + defaultStr(d.commitment.Network, "none"),
		"--cpus=" + defaultStr(d.commitment.CPULimit, "2"),
		"--memory=" + defaultStr(d.commitment.MemoryLimit, "4g"),
	}
	// Stable env ordering so two validators with the same commitment
	// produce the same docker invocation (helps reproducibility audits).
	keys := sortedMapKeys(d.commitment.Env)
	for _, k := range keys {
		args = append(args, "-e", k+"="+d.commitment.Env[k])
	}
	args = append(args, d.commitment.ExtraArgs...)
	args = append(args, d.commitment.Image)
	cmd := exec.CommandContext(ctx, "docker", args...)
	return cmd, nil
}

func defaultStr(v, fallback string) string {
	if v == "" {
		return fallback
	}
	return v
}

func sortedMapKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	// Manual sort to avoid an import in this file; len is small.
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j-1] > out[j]; j-- {
			out[j-1], out[j] = out[j], out[j-1]
		}
	}
	return out
}

// formatInt is repeated here so this file compiles without importing
// the helper from chain/. Kept small and unexported.
func formatInt(i int) string { return strconv.Itoa(i) }
