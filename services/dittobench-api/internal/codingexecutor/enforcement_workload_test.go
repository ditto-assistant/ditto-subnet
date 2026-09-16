package codingexecutor

import (
	"slices"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

func enforcementCommand(args ...string) codingrunner.CommandSpec {
	return codingrunner.CommandSpec{
		ID:      "native-enforcement-hold",
		Argv:    append(slices.Clone(EnforcementWorkloadPrefix), args...),
		Timeout: 5 * time.Second,
	}
}

func TestEnforcementWorkloadUsesTheHostedLaunchInBuildMode(t *testing.T) {
	config := hostedConfig(t)
	docker := newFakeDocker(config)
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	command := enforcementCommand("hold", "--nonce", "0123456789abcdef")
	result, err := executor.RunEnforcementWorkload(t.Context(), t.TempDir(), command)
	if err != nil || !result.Completed || !result.ProcessTreeDead || result.RetainedOutputBytes != 0 {
		t.Fatalf("result=%#v err=%v", result, err)
	}
	if len(docker.requests) != 1 || docker.requests[0].Mode != modeBuild ||
		!slices.Equal(docker.requests[0].Argv, command.Argv) || docker.requests[0].ExpectedTotal != 0 {
		t.Fatalf("requests=%#v", docker.requests)
	}
	var created []string
	for _, run := range docker.runs {
		if run[0] == "create" && !slices.Contains(run, "--name") {
			t.Fatal("unnamed container")
		}
		if run[0] == "create" {
			created = run
		}
	}
	policy := config.Manifest.ResourcePolicy
	if flagValue(created, "--network") != "none" || flagValue(created, "--log-driver") != "none" ||
		flagValue(created, "--memory-swap") != flagValue(created, "--memory") ||
		flagValue(created, "--pids-limit") != itoa(uint64(policy.PidsLimit)) ||
		len(flagValues(created, "--mount")) != 2 {
		t.Fatalf("workload container is not the grading launch: %v", created)
	}
	if len(docker.active) != 0 {
		t.Fatal("workload container was not removed")
	}
}

func TestEnforcementWorkloadCountsRetainedOutput(t *testing.T) {
	config := hostedConfig(t)
	docker := newFakeDocker(config)
	docker.responseMutate = func(response *supervisorResponse) { response.Stdout = "leak" }
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	_, err = executor.RunEnforcementWorkload(t.Context(), t.TempDir(), enforcementCommand("log", "--nonce", "0123456789abcdef", "--bytes", "1"))
	// Build receipts must not carry output; the production validation refuses one.
	if err == nil {
		t.Fatal("a build-mode receipt with candidate output was accepted")
	}
}

func TestEnforcementWorkloadRefusesOtherCommandsAndExecutors(t *testing.T) {
	config := hostedConfig(t)
	executor, err := newWithDocker(config, newFakeDocker(config))
	if err != nil {
		t.Fatal(err)
	}
	for _, argv := range [][]string{
		{"go", "build", "./..."},
		{"nice", "-n", "0", "/workspace/other", "workload", "hold"},
		{"nice", "-n", "10", EnforcementWorkloadExecutable, "workload", "hold"},
		slices.Clone(EnforcementWorkloadPrefix),
		{trustedTestDriverName, "hidden.json"},
	} {
		command := codingrunner.CommandSpec{ID: "x", Argv: argv, Timeout: time.Second}
		if _, err := executor.RunEnforcementWorkload(t.Context(), t.TempDir(), command); err == nil {
			t.Fatalf("accepted %v", argv)
		}
	}
	legacy := testConfig(t)
	legacyExecutor, err := newWithDocker(legacy, newFakeDocker(legacy))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := legacyExecutor.RunEnforcementWorkload(t.Context(), t.TempDir(), enforcementCommand("hold", "--nonce", "0123456789abcdef")); err == nil {
		t.Fatal("a non-hosted executor ran an enforcement workload")
	}
}

func itoa(value uint64) string {
	digits := []byte{}
	for {
		digits = append([]byte{byte('0' + value%10)}, digits...)
		value /= 10
		if value == 0 {
			return string(digits)
		}
	}
}
