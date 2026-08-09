package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
	"golang.org/x/sys/unix"
)

// transcriptCase is one graded case's full graded inputs: the RunResponse
// exactly as the grader saw it (for memory cases the validator-observed
// trajectory is already substituted into tool_calls) plus the raw observed
// trajectory from the mock tool endpoint. Together with the regenerable
// dataset, this is everything a third party needs to re-run the public grader
// and reproduce the case's score.
type transcriptCase struct {
	CaseID   string                      `json:"case_id"`
	Kind     string                      `json:"kind"`
	UserID   string                      `json:"user_id,omitempty"`
	Response protocol.RunResponse        `json:"response"`
	Observed []protocol.ObservedToolCall `json:"observed,omitempty"`
	// Execution is trusted validator-side evidence. It describes delivery of the
	// question to the harness without exposing the prompt or raw infrastructure
	// errors, and is never populated from miner-supplied response fields.
	Execution runner.CaseExecution `json:"execution"`
}

type executionSummary struct {
	Cases            int   `json:"cases"`
	Succeeded        int   `json:"succeeded"`
	TimedOut         int   `json:"timed_out"`
	Cancelled        int   `json:"cancelled"`
	Retried          int   `json:"retried"`
	TotalAttempts    int   `json:"total_attempts"`
	MedianDurationMs int64 `json:"median_duration_ms"`
	P95DurationMs    int64 `json:"p95_duration_ms"`
	MaxDurationMs    int64 `json:"max_duration_ms"`
}

func summarizeExecution(cases []transcriptCase) executionSummary {
	summary := executionSummary{Cases: len(cases)}
	durations := make([]int64, 0, len(cases))
	for _, c := range cases {
		exec := c.Execution
		summary.TotalAttempts += len(exec.Attempts)
		if len(exec.Attempts) > 1 {
			summary.Retried++
		}
		if exec.TerminalOutcome == "success" {
			summary.Succeeded++
		}
		if exec.TimedOut {
			summary.TimedOut++
		}
		if exec.Cancelled {
			summary.Cancelled++
		}
		durations = append(durations, exec.TotalDurationMs)
		if exec.TotalDurationMs > summary.MaxDurationMs {
			summary.MaxDurationMs = exec.TotalDurationMs
		}
	}
	if len(durations) == 0 {
		return summary
	}
	sort.Slice(durations, func(i, j int) bool { return durations[i] < durations[j] })
	summary.MedianDurationMs = durations[(len(durations)-1)/2]
	p95Index := (95*len(durations) + 99) / 100
	if p95Index < 1 {
		p95Index = 1
	}
	summary.P95DurationMs = durations[p95Index-1]
	return summary
}

// transcriptArtifact is the canonical, content-addressed record of a run's
// graded inputs. The digest of its canonical bytes is published with the run
// (run status `transcript_sha256`; the platform embeds it in the signed score
// payload) so the artifact cannot be swapped after the fact.
type transcriptArtifact struct {
	RunID         string                `json:"run_id"`
	Seed          int64                 `json:"seed"`
	BenchVersion  int                   `json:"bench_version"`
	DatasetSHA256 string                `json:"dataset_sha256"`
	Cases         []transcriptCase      `json:"cases"`
	Execution     executionSummary      `json:"execution"`
	ModelRelay    relayExecutionSummary `json:"model_relay"`
	// ProjectionSHA256 binds this public, canonicalized transcript to the
	// validator-private V9 replay manifest without publishing aliases or key.
	ProjectionSHA256 string `json:"projection_sha256,omitempty"`
}

func projectionReplayArtifact(manifest gen.HarnessProjectionManifest) (string, []byte, error) {
	body, err := json.Marshal(manifest)
	if err != nil {
		return "", nil, err
	}
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:]), body, nil
}

func writePrivateProjectionArtifact(dir, runID string, body []byte) error {
	if !filepath.IsAbs(dir) {
		return fmt.Errorf("private projection directory must be absolute")
	}
	parsedRunID, err := uuid.Parse(runID)
	if err != nil || parsedRunID.String() != runID {
		return fmt.Errorf("invalid projection run id")
	}
	dir = filepath.Clean(dir)
	info, err := os.Lstat(dir)
	if os.IsNotExist(err) {
		if mkdirErr := os.Mkdir(dir, 0o700); mkdirErr != nil && !os.IsExist(mkdirErr) {
			return fmt.Errorf("create private projection directory: %w", mkdirErr)
		}
		info, err = os.Lstat(dir)
	}
	if err != nil {
		return fmt.Errorf("inspect private projection directory: %w", err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("private projection directory must be a real directory")
	}
	if info.Mode().Perm() != 0o700 {
		return fmt.Errorf("private projection directory permissions must be 0700")
	}

	dirFD, err := unix.Open(dir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW, 0)
	if err != nil {
		return fmt.Errorf("open private projection directory: %w", err)
	}
	defer unix.Close(dirFD)
	var stat unix.Stat_t
	if err := unix.Fstat(dirFD, &stat); err != nil {
		return fmt.Errorf("verify private projection directory: %w", err)
	}
	if stat.Mode&unix.S_IFMT != unix.S_IFDIR || stat.Mode&0o777 != 0o700 {
		return fmt.Errorf("private projection directory changed during verification")
	}

	name := runID + ".projection.json"
	fileFD, err := unix.Openat(dirFD, name, unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW, 0o600)
	if err != nil {
		return fmt.Errorf("create private projection artifact: %w", err)
	}
	file := os.NewFile(uintptr(fileFD), name)
	if file == nil {
		unix.Close(fileFD)
		return fmt.Errorf("create private projection artifact file handle")
	}
	keep := false
	defer func() {
		_ = file.Close()
		if !keep {
			_ = unix.Unlinkat(dirFD, name, 0)
		}
	}()
	if err := writeAll(file, body); err != nil {
		return fmt.Errorf("write private projection artifact: %w", err)
	}
	if err := file.Sync(); err != nil {
		return fmt.Errorf("sync private projection artifact: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close private projection artifact: %w", err)
	}
	if err := unix.Fsync(dirFD); err != nil {
		return fmt.Errorf("sync private projection directory: %w", err)
	}
	keep = true
	return nil
}

func writeAll(writer io.Writer, body []byte) error {
	for len(body) > 0 {
		n, err := writer.Write(body)
		if err != nil {
			return err
		}
		if n <= 0 || n > len(body) {
			return io.ErrShortWrite
		}
		body = body[n:]
	}
	return nil
}

// canonicalBytes returns the artifact's canonical JSON encoding and its
// SHA-256 hex digest. Cases are sorted by case_id so the bytes are independent
// of per-case completion order under bounded concurrency.
func (a transcriptArtifact) canonicalBytes() (string, []byte, error) {
	sort.Slice(a.Cases, func(i, j int) bool { return a.Cases[i].CaseID < a.Cases[j].CaseID })
	a.Execution = summarizeExecution(a.Cases)
	body, err := json.Marshal(a)
	if err != nil {
		return "", nil, err
	}
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:]), body, nil
}
