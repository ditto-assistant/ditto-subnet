package codinghostedruntime

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codinglaunchjournal"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

type fakeAttempt struct {
	events                                        []string
	runErr, finalErr, gradeErr                    error
	finalFailures, gradeFailures, cleanupFailures int
	cancel                                        context.CancelFunc
}

func (a *fakeAttempt) Run(ctx context.Context) (codingrunner.HostedReplayAuthority, error) {
	a.events = append(a.events, "run")
	if a.cancel != nil {
		a.cancel()
		<-ctx.Done()
		return codingrunner.HostedReplayAuthority{}, ctx.Err()
	}
	return codingrunner.HostedReplayAuthority{}, a.runErr
}
func (a *fakeAttempt) RetryFinalization(context.Context) (codingrunner.HostedReplayAuthority, error) {
	a.events = append(a.events, "finalize")
	if a.finalFailures > 0 {
		a.finalFailures--
		return codingrunner.HostedReplayAuthority{}, ErrExecution
	}
	return codingrunner.HostedReplayAuthority{}, a.finalErr
}
func (a *fakeAttempt) Grade(context.Context) (string, error) {
	a.events = append(a.events, "grade")
	if a.gradeFailures > 0 {
		a.gradeFailures--
		return "", ErrExecution
	}
	return strings.Repeat("a", 64), a.gradeErr
}
func (a *fakeAttempt) Cleanup() error {
	a.events = append(a.events, "cleanup")
	if a.cleanupFailures > 0 {
		a.cleanupFailures--
		return ErrCleanup
	}
	return nil
}

func TestAttemptLifecycleRetriesOnlyFinalizationAndAlwaysCleans(t *testing.T) {
	for _, tc := range []struct {
		name    string
		attempt fakeAttempt
		events  []string
		err     error
	}{
		{"success", fakeAttempt{}, []string{"run", "grade", "cleanup"}, nil},
		{"publication", fakeAttempt{runErr: ErrExecution, finalFailures: 1, gradeFailures: 1, cleanupFailures: 1}, []string{"run", "finalize", "finalize", "grade", "grade", "cleanup", "cleanup"}, nil},
		{"authoring_failed", fakeAttempt{runErr: ErrExecution, finalErr: ErrExecution}, []string{"run", "finalize", "finalize", "cleanup"}, ErrExecution},
		{"grading_failed", fakeAttempt{gradeErr: ErrExecution}, []string{"run", "grade", "grade", "grade", "cleanup"}, ErrExecution},
		{"cleanup_failed", fakeAttempt{cleanupFailures: 3}, []string{"run", "grade", "cleanup", "cleanup", "cleanup"}, ErrCleanup},
	} {
		t.Run(tc.name, func(t *testing.T) {
			result, err := runAttempt(t.Context(), &tc.attempt)
			if !errors.Is(err, tc.err) || !slices.Equal(tc.attempt.events, tc.events) || (err == nil) != (result != "") {
				t.Fatal("unexpected lifecycle", err, tc.attempt.events)
			}
		})
	}
}

func TestCancellationDoesNotGradeOrRetryExecution(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	a := &fakeAttempt{cancel: cancel}
	if _, err := runAttempt(ctx, a); err != ErrExecution || !slices.Equal(a.events, []string{"run", "cleanup"}) {
		t.Fatal("cancelled attempt was retried", err, a.events)
	}
}

func TestConsumeIsDurableExclusiveAndNeverReopens(t *testing.T) {
	root := privateTemp(t)
	var success atomic.Int32
	var wait sync.WaitGroup
	for range 16 {
		wait.Go(func() {
			if consume(root) == nil {
				success.Add(1)
			}
		})
	}
	wait.Wait()
	if success.Load() != 1 || consume(root) != ErrConsumed {
		t.Fatal("consumed attempt reopened")
	}
	body, err := readPrivate(filepath.Join(root, "consumed"), 1024)
	if err != nil || string(body) != "dittobench-coding-hosted-runtime-consumed-v2\n" {
		t.Fatal("durable marker missing")
	}
	// Even an incomplete marker from interrupted creation blocks restart.
	if err := os.Truncate(filepath.Join(root, "consumed"), 0); err != nil {
		t.Fatal(err)
	}
	if consume(root) != ErrConsumed {
		t.Fatal("partial marker reopened")
	}
}

func TestEnvironmentIsReplacedInDedicatedProcess(t *testing.T) {
	if os.Getenv("DITTO_RUNTIME_ENV_TEST") == "1" {
		root := os.Getenv("DITTO_RUNTIME_ENV_ROOT")
		config := &runtimeConfig{wire: configWire{StateRoot: root, DockerExecutable: "/approved/bin/docker", DockerSocket: "/private/docker.sock"}}
		if installEnvironment(config) != nil {
			os.Exit(2)
		}
		_ = json.NewEncoder(os.Stdout).Encode(os.Environ())
		os.Exit(0)
	}
	root := privateTemp(t)
	command := exec.Command(os.Args[0], "-test.run=^TestEnvironmentIsReplacedInDedicatedProcess$")
	command.Env = append(os.Environ(), "DITTO_RUNTIME_ENV_TEST=1", "DITTO_RUNTIME_ENV_ROOT="+root, "POSTGRES_PASSWORD=must-not-inherit", "OPENROUTER_API_KEY=must-not-inherit", "DOCKER_CONTEXT=poison", "DOCKER_HOST=tcp://poison", "DITTOBENCH_SANDBOX_HARDEN=0", "HTTP_PROXY=http://poison")
	body, err := command.Output()
	if err != nil {
		t.Fatal(err)
	}
	var environment []string
	if json.Unmarshal(body, &environment) != nil {
		t.Fatal("invalid subprocess output")
	}
	want := []string{"PATH=/approved/bin", "DOCKER_HOST=unix:///private/docker.sock", "DOCKER_CONFIG=" + filepath.Join(root, "docker-config"), "TMPDIR=" + filepath.Join(root, "tmp")}
	slices.Sort(environment)
	slices.Sort(want)
	if !slices.Equal(environment, want) || !privateDirectory(filepath.Join(root, "docker-config")) || !privateDirectory(filepath.Join(root, "tmp")) {
		t.Fatal("ambient environment crossed worker boundary")
	}
	entries, err := os.ReadDir(filepath.Join(root, "docker-config"))
	if err != nil || len(entries) != 0 {
		t.Fatal("Docker configuration not empty")
	}
}

func TestLaunchHookRefusesOutsideAReconciledAttempt(t *testing.T) {
	slot := &launchSlot{}
	if slot.intent(t.Context(), "abcd", []string{"dittobench-abcd"}, nil) == nil {
		t.Fatal("launch allowed before the journal was reconciled and opened")
	}
	root := privateTemp(t)
	journal, err := codinglaunchjournal.Open(root, "20000000-0000-4000-8000-000000000002", "30000000-0000-4000-8000-000000000003")
	if err != nil {
		t.Fatal(err)
	}
	defer journal.Close()
	slot.set(journal)
	if err := slot.intent(t.Context(), "abcd", []string{"dittobench-abcd"}, []string{"ditto-job-abcd"}); err != nil {
		t.Fatal(err)
	}
	if err := finishJournal(slot, failingReconciler{}, nil); err != ErrCleanup {
		t.Fatalf("unconfirmed reconciliation = %v", err)
	}
	if slot.intent(t.Context(), "abce", []string{"dittobench-abce"}, nil) == nil {
		t.Fatal("launch allowed after the attempt finished")
	}
}

type failingReconciler struct{}

func (failingReconciler) Reconcile(context.Context, codinglaunchjournal.Docker) (codinglaunchjournal.Report, error) {
	return codinglaunchjournal.Report{}, codinglaunchjournal.ErrUnconfirmed
}

func TestExplicitReconcileUsesOnlyTheNamedDaemonAndAnEmptyClientConfig(t *testing.T) {
	root := privateTemp(t)
	journalDir := filepath.Join(root, "journal")
	bin := filepath.Join(root, "bin")
	for _, dir := range []string{journalDir, bin} {
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
	}
	socket := filepath.Join(root, "docker.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if err := os.Chmod(socket, 0600); err != nil {
		t.Fatal(err)
	}
	log := filepath.Join(root, "calls")
	docker := filepath.Join(bin, "docker")
	// PATH holds only the docker directory, so the script uses shell builtins.
	script := "#!/bin/sh\n" +
		"n=0; [ -d \"$DOCKER_CONFIG\" ] || n=missing\n" +
		"for f in \"$DOCKER_CONFIG\"/* \"$DOCKER_CONFIG\"/.[!.]*; do [ -e \"$f\" ] && n=nonempty; done\n" +
		"printf '%s|%s|%s|%s\\n' \"$DOCKER_HOST\" \"$n\" \"$OPENROUTER_API_KEY\" \"$*\" >> " + log + "\n" +
		"echo \"Error: No such container: $5\"\nexit 1\n"
	if err := os.WriteFile(docker, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	journal, err := codinglaunchjournal.Open(journalDir, "attempt", "worker")
	if err != nil {
		t.Fatal(err)
	}
	if err := journal.Record("abcd", []string{"dittobench-abcd"}, nil); err != nil {
		t.Fatal(err)
	}
	// A live owner holds the directory: the explicit reconcile refuses.
	allow := func(path string) bool { return path == docker }
	if _, err := reconcileLaunchJournal(t.Context(), journalDir, docker, socket, allow); !errors.Is(err, codinglaunchjournal.ErrLocked) {
		t.Fatalf("reconciled under a live owner: %v", err)
	}
	_ = journal.Close()
	t.Setenv("OPENROUTER_API_KEY", "must-not-inherit")
	report, err := reconcileLaunchJournal(t.Context(), journalDir, docker, socket, allow)
	if err != nil || report.AbsentContainers != 1 {
		t.Fatalf("report = %+v %v", report, err)
	}
	calls, _ := os.ReadFile(log)
	want := "unix://" + socket + "|0||container inspect --format {{.Id}} {{json .Config.Labels}} dittobench-abcd\n"
	if string(calls) != want {
		t.Fatalf("docker calls = %q", calls)
	}
	entries, _ := os.ReadDir(journalDir)
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), "docker-config-") || entry.Name() == codinglaunchjournal.FileName {
			t.Fatalf("left %s behind", entry.Name())
		}
	}
	for name, args := range map[string][3]string{
		"shared journal": {root + "/missing", docker, socket},
		"executable":     {journalDir, "/usr/bin/docker", socket},
		"socket":         {journalDir, docker, filepath.Join(root, "calls")},
	} {
		if _, err := reconcileLaunchJournal(t.Context(), args[0], args[1], args[2], allow); err != ErrConfig {
			t.Fatalf("%s accepted: %v", name, err)
		}
	}
}
