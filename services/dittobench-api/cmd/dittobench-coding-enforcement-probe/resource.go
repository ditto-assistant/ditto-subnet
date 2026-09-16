package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/probe"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedruntime"
	"github.com/ditto-assistant/dittobench-api/internal/codinglaunchjournal"
)

const maxProfileBytes = 64 << 10

// resourceAgent serves the resource line protocol on stdin/stdout for the root
// collector. SIGTERM cancels every run; the agent waits for production cleanup
// before it exits (the cleanup_recovery runner_sigterm scenario).
func resourceAgent(ctx context.Context, args []string, stdin io.Reader, stdout io.Writer) error {
	flags := flag.NewFlagSet("resource-agent", flag.ContinueOnError)
	executionPath := flags.String("execution-profile", "", "exact approved execution profile")
	gradingPath := flags.String("grading-profile", "", "exact approved grading profile")
	imagesPath := flags.String("enforcement-images", "", "pinned per-language enforcement image set")
	runner := flags.String("runner", "", "host path of this probe runner, mounted into harness workloads")
	workDirectory := flags.String("work-dir", "", "private exec-permitted directory for probe workspaces")
	seccomp := flags.String("seccomp-profile", "", "host seccomp profile name, as the hosted runtime passes it")
	apparmor := flags.String("apparmor-profile", "", "host AppArmor profile name, as the hosted runtime passes it")
	journalDirectory := flags.String("launch-journal", "", "private launch journal directory; runs become journaled attempts (cleanup_recovery)")
	attemptState := flags.String("attempt-state", "", "private attempt state directory consumed once, as the hosted runtime's state_root")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *executionPath == "" || *gradingPath == "" || *imagesPath == "" || *runner == "" || *workDirectory == "" || flags.NArg() != 0 {
		return errors.New("resource-agent needs --execution-profile, --grading-profile, --enforcement-images, --runner and --work-dir")
	}
	if (*journalDirectory == "") != (*attemptState == "") {
		return errors.New("resource-agent needs --launch-journal and --attempt-state together")
	}
	var journal *codinglaunchjournal.Journal
	if *attemptState != "" {
		// The hosted runtime's own single-use marker, committed before
		// anything else. A consumed attempt is refused with one fixed line.
		if err := codinghostedruntime.ConsumeAttempt(*attemptState); err != nil {
			refused := probe.ResourceResponse{Schema: probe.ResourceAgentSchema, Op: "attempt", Error: "probe: attempt state refused"}
			if errors.Is(err, codinghostedruntime.ErrConsumed) {
				refused.Error = "probe: attempt state consumed"
			}
			_ = json.NewEncoder(stdout).Encode(refused)
			return errors.New(refused.Error)
		}
		opened, err := codinglaunchjournal.Open(*journalDirectory, "native-enforcement-"+randomID(), "native-enforcement-agent")
		if err != nil {
			return err
		}
		defer opened.Close()
		journal = opened
	}
	inputs := map[string][]byte{}
	for name, path := range map[string]string{"execution": *executionPath, "grading": *gradingPath, "images": *imagesPath} {
		raw, err := readBounded(path, maxProfileBytes)
		if err != nil {
			return err
		}
		inputs[name] = raw
	}
	ctx, stop := signal.NotifyContext(ctx, syscall.SIGTERM)
	defer stop()
	agent, err := probe.NewResourceAgent(ctx, probe.ResourceAgentConfig{
		ExecutionProfile: inputs["execution"], GradingProfile: inputs["grading"], EnforcementImages: inputs["images"],
		Runner: *runner, WorkDirectory: *workDirectory, SeccompProfile: *seccomp, AppArmorProfile: *apparmor,
		Backend:       probe.ProductionResourceBackend{Docker: probe.ExecDocker{}},
		LaunchJournal: journal, JournalDocker: codinglaunchjournal.ExecDocker{},
	})
	if err != nil {
		return err
	}
	return probe.ServeResourceAgent(ctx, agent, stdin, stdout)
}

func readBounded(path string, maximum int64) ([]byte, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil {
		return nil, err
	}
	if int64(len(raw)) > maximum {
		return nil, errors.New("input exceeds its bound")
	}
	return raw, nil
}

func randomID() string {
	var value [8]byte
	_, _ = rand.Read(value[:])
	return hex.EncodeToString(value[:])
}

// reconcileLaunchJournal runs the hosted runtime's explicit reconciler for the
// collector's cleanup_recovery runner_sigkill scenario and prints its counts.
func reconcileLaunchJournal(ctx context.Context, args []string, stdout io.Writer) error {
	flags := flag.NewFlagSet("reconcile-launch-journal", flag.ContinueOnError)
	directory := flags.String("launch-journal", "", "private launch journal directory")
	dockerExecutable := flags.String("docker-executable", "", "protected docker executable")
	dockerSocket := flags.String("docker-socket", "", "owner-only local Docker socket")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *directory == "" || *dockerExecutable == "" || *dockerSocket == "" || flags.NArg() != 0 {
		return errors.New("reconcile-launch-journal needs --launch-journal, --docker-executable and --docker-socket")
	}
	report, err := codinghostedruntime.ReconcileLaunchJournal(ctx, *directory, *dockerExecutable, *dockerSocket)
	if err != nil {
		return errors.New("the launch journal was not reconciled")
	}
	return json.NewEncoder(stdout).Encode(map[string]any{
		"schema": "dittobench-coding-launch-journal-reconcile-v1", "entries": report.Entries,
		"removed_containers": report.RemovedContainers, "removed_networks": report.RemovedNetworks,
		"absent_containers": report.AbsentContainers, "absent_networks": report.AbsentNetworks,
	})
}
