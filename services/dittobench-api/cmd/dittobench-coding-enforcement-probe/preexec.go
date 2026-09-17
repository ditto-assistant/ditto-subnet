package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"os/signal"
	"syscall"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/probe"
)

const maxFixturesBytes = 256 << 10

// preexecAgent serves the pre-exec line protocol on stdin/stdout for the root
// collector. One request is one public fixture run through the production
// hosted grading launch; SIGTERM cancels every run and waits for its cleanup.
func preexecAgent(ctx context.Context, args []string, stdin io.Reader, stdout io.Writer) error {
	flags := flag.NewFlagSet("preexec-agent", flag.ContinueOnError)
	executionPath := flags.String("execution-profile", "", "exact approved execution profile")
	gradingPath := flags.String("grading-profile", "", "exact approved grading profile")
	imagesPath := flags.String("enforcement-images", "", "pinned per-language enforcement image set")
	fixturesPath := flags.String("preexec-fixtures", "", "approval-pinned public pre-exec fixture manifest")
	checkout := flags.String("checkout", "", "reviewed checkout the pinned fixture files are read from")
	runner := flags.String("runner", "", "host path of this probe runner")
	workDirectory := flags.String("work-dir", "", "private exec-permitted directory for fixture workspaces")
	seccomp := flags.String("seccomp-profile", "", "host seccomp profile name, as the hosted runtime passes it")
	apparmor := flags.String("apparmor-profile", "", "host AppArmor profile name, as the hosted runtime passes it")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *executionPath == "" || *gradingPath == "" || *imagesPath == "" || *fixturesPath == "" ||
		*checkout == "" || *runner == "" || *workDirectory == "" || flags.NArg() != 0 {
		return errors.New("preexec-agent needs --execution-profile, --grading-profile, --enforcement-images, --preexec-fixtures, --checkout, --runner and --work-dir")
	}
	inputs := map[string][]byte{}
	for name, item := range map[string]struct {
		path    string
		maximum int64
	}{
		"execution": {*executionPath, maxProfileBytes},
		"grading":   {*gradingPath, maxProfileBytes},
		"images":    {*imagesPath, maxProfileBytes},
		"fixtures":  {*fixturesPath, maxFixturesBytes},
	} {
		raw, err := readBounded(item.path, item.maximum)
		if err != nil {
			return err
		}
		inputs[name] = raw
	}
	ctx, stop := signal.NotifyContext(ctx, syscall.SIGTERM)
	defer stop()
	agent, err := probe.NewPreexecAgent(ctx, probe.PreexecAgentConfig{
		ExecutionProfile: inputs["execution"], GradingProfile: inputs["grading"],
		EnforcementImages: inputs["images"], PreexecFixtures: inputs["fixtures"],
		Checkout: *checkout, Runner: *runner, WorkDirectory: *workDirectory,
		SeccompProfile: *seccomp, AppArmorProfile: *apparmor,
		Backend: probe.ProductionPreexecBackend{Docker: probe.ExecDocker{}},
	})
	if err != nil {
		return err
	}
	return probe.ServePreexecAgent(ctx, agent, stdin, stdout)
}
