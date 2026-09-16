package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestTheCommandCannotAssembleARecord(t *testing.T) {
	for _, subcommand := range []string{"assemble", "collect", "record"} {
		err := run(context.Background(), []string{subcommand}, &bytes.Buffer{})
		if err == nil || !strings.Contains(err.Error(), "unknown subcommand") {
			t.Fatalf("%s must not exist: %v", subcommand, err)
		}
	}
}

func TestObserveRequestedConfigRequiresApprovedInputs(t *testing.T) {
	err := run(context.Background(), []string{"observe-requested-config", "--language", "python"}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "required") {
		t.Fatalf("missing profile, image set and repository must be refused: %v", err)
	}
	// A caller can no longer name an image digest; only the pinned set can.
	err = run(context.Background(), []string{"observe-requested-config", "--image", "python=sha256:x"}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "flag provided but not defined") {
		t.Fatalf("--image must not exist: %v", err)
	}
}

func TestNetOnceWaitsForTheGateAndWritesAnExclusiveReport(t *testing.T) {
	directory := t.TempDir()
	plan := filepath.Join(directory, "plan.json")
	report := filepath.Join(directory, "report.json")
	gate := filepath.Join(directory, "gate")
	// A missing plan runs nothing and never touches the gate.
	if err := netOnce(context.Background(), []string{"--plan", plan, "--report", report, "--gate", gate}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(report); !os.IsNotExist(err) {
		t.Fatal("a stop without a plan wrote a report")
	}
	body := `{"schema":"dittobench-coding-native-net-plan-v1","requests":[{"op":"connect","address":"127.0.0.1","port":9,"timeout_ms":200}]}`
	if err := os.WriteFile(plan, []byte(body), 0o400); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(gate, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := awaitGate(gate, 50*time.Millisecond); err == nil {
		t.Fatal("an unopened gate passed")
	}
	done := make(chan error, 1)
	go func() {
		done <- netOnce(context.Background(), []string{"--plan", plan, "--report", report, "--gate", gate})
	}()
	time.Sleep(100 * time.Millisecond)
	if _, err := os.Stat(report); !os.IsNotExist(err) {
		t.Fatal("the plan ran before the gate opened")
	}
	writer, err := os.OpenFile(gate, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = writer.Write([]byte("1"))
	_ = writer.Close()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(report)
	if err != nil || !strings.Contains(string(raw), `"schema":"dittobench-coding-native-net-report-v1"`) {
		t.Fatalf("report = %s, %v", raw, err)
	}
	if err := netOnce(context.Background(), []string{"--plan", plan, "--report", report}); err == nil {
		t.Fatal("an existing report was replaced")
	}
}
