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
//	                                 grading executor containers (created, never started)
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
		return errors.New("a subcommand is required: resolve-images | observe-requested-config")
	}
	switch args[0] {
	case "resolve-images":
		return resolveImages(ctx, args[1:], stdout)
	case "observe-requested-config":
		return observeRequestedConfig(ctx, args[1:], stdout)
	default:
		return fmt.Errorf("unknown subcommand %q", args[0])
	}
}

type imageFlags map[string]string

func (f imageFlags) String() string { return "" }

func (f imageFlags) Set(value string) error {
	language, digest, ok := strings.Cut(value, "=")
	if !ok || language == "" || digest == "" {
		return errors.New("--image must be language=sha256:digest")
	}
	if _, repeated := f[language]; repeated {
		return fmt.Errorf("--image %s is repeated", language)
	}
	f[language] = digest
	return nil
}

func observeRequestedConfig(ctx context.Context, args []string, stdout io.Writer) error {
	flags := flag.NewFlagSet("observe-requested-config", flag.ContinueOnError)
	profilePath := flags.String("grading-profile", "", "exact canonical approved hosted grading profile")
	repository := flags.String("executor-repository", "", "approved executor image repository")
	images := imageFlags{}
	flags.Var(images, "image", "language=sha256:digest of an approved image (repeatable)")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *profilePath == "" || *repository == "" || len(images) == 0 || flags.NArg() != 0 {
		return errors.New("--grading-profile, --executor-repository and at least one --image are required")
	}
	profile, err := os.ReadFile(*profilePath)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	report, err := probe.ObserveHostedGradingRequestedConfig(ctx, probe.ExecDocker{}, probe.HostedGradingRequest{
		GradingProfile: profile, Repository: *repository, Images: images,
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
