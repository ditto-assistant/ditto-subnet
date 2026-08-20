package codingexecutor

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

func supervisorIdentity() (uint32, uint32) {
	uid, gid := uint32(os.Getuid()), uint32(os.Getgid())
	if uid == 0 {
		uid = 65532
	}
	if gid == 0 {
		gid = 65532
	}
	return uid, gid
}

func supervisorRequestFixture(t *testing.T, mode executionMode, argv []string, timeout time.Duration, expected uint32) supervisorRequest {
	t.Helper()
	uid, gid := supervisorIdentity()
	command := codingrunner.CommandSpec{ID: "supervisor-command", Argv: argv, Timeout: timeout}
	digest, err := commandDigest(command)
	if err != nil {
		t.Fatal(err)
	}
	return supervisorRequest{
		Schema: supervisorRequestSchema, Nonce: strings.Repeat("ab", 24), Mode: mode,
		CommandID: command.ID, CommandSHA256: digest, Argv: append([]string(nil), argv...),
		TimeoutMilliseconds: timeout.Milliseconds(), ExpectedTotal: expected,
		CandidateUID: uid, CandidateGID: gid,
	}
}

func runSupervisorFixture(t *testing.T, request supervisorRequest) supervisorResponse {
	t.Helper()
	return runSupervisorFixtureAt(t, request, t.TempDir())
}

func runSupervisorFixtureAt(t *testing.T, request supervisorRequest, workspace string) supervisorResponse {
	t.Helper()
	control := t.TempDir()
	requestPath := filepath.Join(control, "request.json")
	responsePath := filepath.Join(control, "response.json")
	if err := writeRequest(requestPath, request); err != nil {
		t.Fatal(err)
	}
	if err := supervisorMainAt(
		t.Context(), []string{"--request", requestPath, "--response", responsePath}, workspace,
	); err != nil {
		t.Fatal(err)
	}
	response, err := readResponse(responsePath)
	if err != nil {
		t.Fatal(err)
	}
	if err := response.validate(request, maxModelVisibleCommandOutput); err != nil {
		t.Fatal(err)
	}
	return response
}

func TestSupervisorDetectsAuthoringMutationAndKeepsHostWorkspacePristine(t *testing.T) {
	workspace := t.TempDir()
	authoring := supervisorRequestFixture(t, modeAuthoring, []string{
		"python3", "-c", "from pathlib import Path; Path('authoring.txt').write_text('kept')",
	}, time.Second, 0)
	authoringResponse := runSupervisorFixtureAt(t, authoring, workspace)
	if !authoringResponse.WorkspaceMutated {
		t.Fatal("authoring scratch mutation was not reported")
	}
	if _, err := os.Stat(filepath.Join(workspace, "authoring.txt")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("authoring scratch mutation escaped into candidate workspace: %v", err)
	}
	build := supervisorRequestFixture(t, modeBuild, []string{
		"python3", "-c", "from pathlib import Path; Path('build-only.txt').write_text('scratch')",
	}, time.Second, 0)
	runSupervisorFixtureAt(t, build, workspace)
	if _, err := os.Stat(filepath.Join(workspace, "build-only.txt")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("build scratch mutation escaped into candidate workspace: %v", err)
	}
}

func installTrustedTestDriver(t *testing.T) {
	t.Helper()
	directory := t.TempDir()
	path := filepath.Join(directory, trustedTestDriverName)
	script := `#!/bin/sh
mode=pass
if [ "${1:-}" = timeout ]; then mode=timeout; shift; fi
report=
nonce=
expected=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dittobench-report) report="$2"; shift 2 ;;
    --dittobench-nonce) nonce="$2"; shift 2 ;;
    --dittobench-expected) expected="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [ "$mode" = timeout ]; then
  sleep 60 &
  sleep 60
fi
umask 077
printf '{"schema":"dittobench-coding-trusted-test-report-v1","nonce":"%s","passed":%s,"total":%s,"completed":true}\n' "$nonce" "$expected" "$expected" > "$report"
`
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", directory+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func TestSupervisorRunsAuthoringAndTestCommandsWithTrustedReceipts(t *testing.T) {
	authoring := supervisorRequestFixture(
		t, modeAuthoring, []string{"python3", "-c", "import sys; print('visible'); print('diagnostic', file=sys.stderr)"},
		time.Second, 0,
	)
	authoringResponse := runSupervisorFixture(t, authoring)
	if !authoringResponse.Completed || authoringResponse.ReturnCode != 0 ||
		!strings.Contains(authoringResponse.Stdout, "visible") || !strings.Contains(authoringResponse.Stderr, "diagnostic") {
		t.Fatalf("authoring response=%#v", authoringResponse)
	}

	installTrustedTestDriver(t)
	testRequest := supervisorRequestFixture(t, modeTest, []string{trustedTestDriverName}, time.Second, 2)
	testResponse := runSupervisorFixture(t, testRequest)
	if !testResponse.Completed || testResponse.ReturnCode != 0 || testResponse.Passed != 2 || testResponse.Total != 2 ||
		testResponse.Stdout != "" || testResponse.Stderr != "" {
		t.Fatalf("test response=%#v", testResponse)
	}
}

func TestSupervisorKillsTheCompleteTimedOutProcessGroup(t *testing.T) {
	installTrustedTestDriver(t)
	request := supervisorRequestFixture(t, modeTest, []string{trustedTestDriverName, "timeout"}, 50*time.Millisecond, 1)
	response := runSupervisorFixture(t, request)
	if !response.TimedOut || response.Completed || response.ReturnCode != 124 || response.Passed != 0 || !response.ProcessTreeDead {
		t.Fatalf("timeout response=%#v", response)
	}
}

func TestSupervisorRejectsTamperedRequestsAndExclusiveResponseViolations(t *testing.T) {
	request := supervisorRequestFixture(t, modeBuild, []string{"python3", "-c", "pass"}, time.Second, 0)
	request.CommandSHA256 = strings.Repeat("f", 64)
	control := t.TempDir()
	requestPath := filepath.Join(control, "request.json")
	responsePath := filepath.Join(control, "response.json")
	if err := writeRequest(requestPath, request); err != nil {
		t.Fatal(err)
	}
	if err := supervisorMainAt(
		t.Context(), []string{"--request", requestPath, "--response", responsePath}, t.TempDir(),
	); err == nil {
		t.Fatal("tampered supervisor request was accepted")
	}

	request = supervisorRequestFixture(t, modeBuild, []string{"python3", "-c", "pass"}, time.Second, 0)
	control = t.TempDir()
	requestPath = filepath.Join(control, "request.json")
	responsePath = filepath.Join(control, "response.json")
	if err := writeRequest(requestPath, request); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(responsePath, []byte("occupied"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := supervisorMainAt(
		t.Context(), []string{"--request", requestPath, "--response", responsePath}, t.TempDir(),
	); err == nil {
		t.Fatal("pre-existing supervisor response was overwritten")
	}
}

func TestSupervisorStartFailureProducesNoCandidateReceipt(t *testing.T) {
	request := supervisorRequestFixture(t, modeBuild, []string{"definitely-missing-command"}, time.Second, 0)
	control := t.TempDir()
	requestPath := filepath.Join(control, "request.json")
	responsePath := filepath.Join(control, "response.json")
	if err := writeRequest(requestPath, request); err != nil {
		t.Fatal(err)
	}
	err := supervisorMainAt(
		context.Background(), []string{"--request", requestPath, "--response", responsePath}, t.TempDir(),
	)
	if err == nil || !strings.Contains(err.Error(), "start coding candidate command") {
		t.Fatalf("start failure=%v", err)
	}
	if _, statErr := os.Stat(responsePath); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("start failure fabricated a receipt: %v", statErr)
	}
}
