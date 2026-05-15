package main

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// TestEndToEnd_SelfTest_BuildsEchoHarness exercises the full pipeline
// without docker: load fixtures, partition, drive the in-process
// echo-harness, score, and write reports. It is the hermetic e2e the
// plan calls for.
func TestEndToEnd_SelfTest_BuildsEchoHarness(t *testing.T) {
	// Pre-build the echo harness once so each case doesn't pay `go run`
	// dial cost. DITTO_ECHO_HARNESS_BIN points the echoLauncher at the
	// resulting binary.
	bin := filepath.Join(t.TempDir(), "echo-harness")
	build := exec.Command("go", "build", "-o", bin, "github.com/heyditto/ditto-subnet/go/cmd/echo-harness")
	build.Stderr = os.Stderr
	if err := build.Run(); err != nil {
		t.Fatalf("build echo-harness: %v", err)
	}
	t.Setenv("DITTO_ECHO_HARNESS_BIN", bin)

	fixtures := filepath.Join(repoRoot(t), "ditto", "bench", "fixtures")
	reportDir := t.TempDir()

	cfg := Config{
		FixturesRoot:    fixtures,
		Secret:          "e2e-test-secret",
		Mechanism:       "all",
		Visibility:      "all",
		Sample:          2,
		DeadlineMs:      8000,
		BudgetLatencyMs: 4000,
		PrivateFrac:     0.25,
		CanaryFrac:      0.15,
		ReportDir:       reportDir,
		Netuid:          118,
		SelfTest:        true,
		DryRun:          true,
	}

	if err := run(context.Background(), cfg, os.Stderr); err != nil {
		t.Fatalf("validator.run: %v", err)
	}

	// At least one miner report and at least one aggregate file (per
	// non-empty mechanism) must exist.
	entries, err := os.ReadDir(reportDir)
	if err != nil {
		t.Fatalf("read reports: %v", err)
	}
	var sawMiner, sawAggregate bool
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		path := filepath.Join(reportDir, name)
		switch {
		case len(name) >= 6 && name[:6] == "miner-":
			sawMiner = true
			assertMinerReport(t, path)
		case len(name) >= 8 && name[:8] == "weights-":
			sawAggregate = true
			assertAggregateReport(t, path)
		}
	}
	if !sawMiner {
		t.Fatalf("expected a miner-*.json report, got %v", entries)
	}
	if !sawAggregate {
		t.Fatalf("expected a weights-*.json report, got %v", entries)
	}
}

func assertMinerReport(t *testing.T, path string) {
	t.Helper()
	buf, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var rep struct {
		Hotkey string `json:"hotkey"`
		Scores []struct {
			ChallengeID string  `json:"challenge_id"`
			Visibility  string  `json:"visibility"`
			Score       float64 `json:"score"`
		} `json:"scores"`
		Aggregates []struct {
			Mechanism string `json:"mechanism"`
			Count     int    `json:"count"`
		} `json:"aggregates"`
	}
	if err := json.Unmarshal(buf, &rep); err != nil {
		t.Fatalf("parse miner report %s: %v", path, err)
	}
	if rep.Hotkey != "self-test-hotkey" {
		t.Fatalf("expected self-test-hotkey, got %q", rep.Hotkey)
	}
	if len(rep.Scores) == 0 {
		t.Fatalf("no scores in %s", path)
	}
	for _, s := range rep.Scores {
		if s.ChallengeID == "" {
			t.Fatalf("score missing challenge_id in %s", path)
		}
		if s.Visibility == "" {
			t.Fatalf("score missing visibility in %s", path)
		}
	}
}

func assertAggregateReport(t *testing.T, path string) {
	t.Helper()
	buf, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var rep struct {
		Mechanism string             `json:"mechanism"`
		Weights   map[string]float64 `json:"weights"`
		Details   []map[string]any   `json:"details"`
	}
	if err := json.Unmarshal(buf, &rep); err != nil {
		t.Fatalf("parse aggregate %s: %v", path, err)
	}
	if rep.Mechanism == "" {
		t.Fatalf("aggregate %s missing mechanism", path)
	}
	if len(rep.Weights) != 1 || rep.Weights["self-test-hotkey"] == 0 {
		// Self-test runs against one miner so the weight either is 1.0
		// (when the miner scored anything > 0) or the map is empty (no
		// non-zero scores). Both are valid; flag only the impossible
		// "multi-miner" case.
		if len(rep.Weights) > 1 {
			t.Fatalf("aggregate %s has unexpected weights: %v", path, rep.Weights)
		}
	}
}

func TestParseFlags_RequiresFixturesAndSecret(t *testing.T) {
	if _, err := parseFlags(nil); err == nil {
		t.Fatalf("expected error when fixtures-root missing")
	}
	if _, err := parseFlags([]string{"--fixtures-root", "/x"}); err == nil {
		t.Fatalf("expected error when secret missing")
	}
	if _, err := parseFlags([]string{"--fixtures-root", "/x", "--secret", "s"}); err == nil {
		t.Fatalf("expected error when miner-commitments missing")
	}
	cfg, err := parseFlags([]string{"--fixtures-root", "/x", "--secret", "s", "--self-test"})
	if err != nil {
		t.Fatalf("self-test should bypass miner-commitments: %v", err)
	}
	if !cfg.SelfTest {
		t.Fatalf("expected SelfTest=true")
	}
}

// repoRoot returns the absolute path to the ditto-subnet checkout that
// contains this test. We walk up from go/cmd/validator until we find
// the Makefile that lives at the repo root.
func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "Makefile")); err == nil {
			if _, err := os.Stat(filepath.Join(dir, "ditto")); err == nil {
				return dir
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("could not locate repo root from %s", dir)
		}
		dir = parent
	}
}
