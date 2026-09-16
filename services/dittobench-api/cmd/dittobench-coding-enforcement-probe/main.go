// Command dittobench-coding-enforcement-probe is the default-off B5 native
// enforcement probe runner.
//
// In PR2 it writes no evidence record. No evidence kind is measured end to end
// yet, so no path here can assemble one. It emits only an observation report
// (schema dittobench-coding-native-probe-observations-v1) that the offline
// verifier refuses as a record:
//
//	resolve-images REF...            pin approved repository@sha256 images; never pull
//	observe-requested-config ...     requested resource configuration of hosted
//	                                 grading executor containers (created, never started),
//	                                 for images and commands taken only from the
//	                                 pinned --enforcement-images set
//	net-agent [--unix PATH]          network observation agent (B5 PR4): single
//	                                 handshake-only attempts, one JSON line per
//	                                 request, on stdin/stdout or one Unix socket
//	net-once --plan F --report F     one-shot attempts from a fixed plan, after
//	         [--gate FIFO]           an optional collector gate
//	workload MODE --nonce HEX ...    in-container helper (B5 PR5): drives one
//	                                 resource to its limit and holds, so the
//	                                 collector can measure it from outside
//	resource-agent ...               host-side launcher (B5 PR5): starts
//	                                 workloads through the production executor
//	                                 and hosted harness launch paths
//	reconcile-launch-journal ...     the hosted runtime's launch journal
//	                                 reconciler (B5 PR5 cleanup_recovery)
//
// The network agent reports catalog outcome names to the root collector
// (infra/scripts/collect-coding-native-enforcement.py), which measures this
// binary from outside and assembles the record. The agent never decides a
// matched value.
//
// Nothing invokes it from a host workflow. It mints no approval, reads no
// custody path and reaches only the daemon selected by DOCKER_HOST.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/probe"
)

func main() {
	if err := run(context.Background(), os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "dittobench-coding-enforcement-probe:", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("a subcommand is required: resolve-images | observe-requested-config | net-agent | net-once | workload | resource-agent | reconcile-launch-journal")
	}
	switch args[0] {
	case "resolve-images":
		return resolveImages(ctx, args[1:], stdout)
	case "observe-requested-config":
		return observeRequestedConfig(ctx, args[1:], stdout)
	case "net-agent":
		return netAgent(ctx, args[1:], os.Stdin, stdout)
	case "net-once":
		return netOnce(ctx, args[1:])
	case "resource-agent":
		return resourceAgent(ctx, args[1:], os.Stdin, stdout)
	case "reconcile-launch-journal":
		return reconcileLaunchJournal(ctx, args[1:], stdout)
	case probe.WorkloadSubcommand:
		options, err := probe.ParseWorkloadArgs(args[1:])
		if err != nil {
			return err
		}
		return probe.RunWorkload(ctx, options, stdout)
	default:
		return fmt.Errorf("unknown subcommand %q", args[0])
	}
}

type languageFlags []string

func (f *languageFlags) String() string { return strings.Join(*f, ",") }

func (f *languageFlags) Set(value string) error {
	*f = append(*f, value)
	return nil
}

func observeRequestedConfig(ctx context.Context, args []string, stdout io.Writer) error {
	flags := flag.NewFlagSet("observe-requested-config", flag.ContinueOnError)
	profilePath := flags.String("grading-profile", "", "exact canonical approved hosted grading profile")
	imagesPath := flags.String("enforcement-images", "", "pinned per-language image digests and commands for that profile")
	repository := flags.String("executor-repository", "", "approved executor image repository")
	var languages languageFlags
	flags.Var(&languages, "language", "observe only this language from the pinned image set (repeatable)")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *profilePath == "" || *imagesPath == "" || *repository == "" || flags.NArg() != 0 {
		return errors.New("--grading-profile, --enforcement-images and --executor-repository are required")
	}
	profile, err := os.ReadFile(*profilePath)
	if err != nil {
		return err
	}
	images, err := os.ReadFile(*imagesPath)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	report, err := probe.ObserveHostedGradingRequestedConfig(ctx, probe.ExecDocker{}, probe.HostedGradingRequest{
		GradingProfile: profile, EnforcementImages: images, Languages: languages, Repository: *repository,
	})
	if err != nil {
		return err
	}
	encoded, err := probe.EncodeReport(report)
	if err != nil {
		return err
	}
	_, err = stdout.Write(append(encoded, '\n'))
	return err
}

func resolveImages(ctx context.Context, args []string, stdout io.Writer) error {
	flags := flag.NewFlagSet("resolve-images", flag.ContinueOnError)
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() == 0 {
		return errors.New("at least one approved registry@sha256 image reference is required")
	}
	resolved := make(map[string]string, flags.NArg())
	for _, reference := range flags.Args() {
		image, err := probe.ResolveApprovedImage(ctx, probe.ExecDocker{}, reference)
		if err != nil {
			return err
		}
		resolved[reference] = image.ID
	}
	return json.NewEncoder(stdout).Encode(resolved)
}
