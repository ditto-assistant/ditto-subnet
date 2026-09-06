// The worker runs only inside trusted Platform infrastructure. Invocation does
// not install a service, schedule jobs or activate any validator/scoring gate.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/ditto-assistant/dittobench-api/internal/codinghostedruntime"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdout, os.Stderr))
}

func run(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("dittobench-coding-hosted-worker", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	once := flags.Bool("private-shadow-once", false, "run one approved private shadow attempt")
	validate := flags.Bool("validate-only", false, "validate private files without starting an attempt")
	path := flags.String("config", "", "absolute owner-only Platform configuration file")
	if flags.Parse(args) != nil || flags.NArg() != 0 || *once == *validate || *path == "" {
		_, _ = fmt.Fprintln(stderr, "requires --private-shadow-once --config <protected-file>")
		return 2
	}
	if *validate {
		if codinghostedruntime.Validate(*path) != nil {
			_, _ = fmt.Fprintln(stderr, "hosted worker configuration rejected")
			return 1
		}
		if _, err := fmt.Fprintln(stdout, "hosted worker configuration valid"); err != nil {
			return 1
		}
		return 0
	}
	sha, err := codinghostedruntime.Run(ctx, *path)
	if err != nil {
		// Only this fixed error vocabulary is permitted; never print a path,
		// capability, child output, config or exception received from adapters.
		message := "hosted worker failed; reconciliation required"
		if err == codinghostedruntime.ErrConfig {
			message = "hosted worker configuration rejected"
		} else if err == codinghostedruntime.ErrConsumed {
			message = "hosted worker directory consumed; reconciliation required"
		} else if err == codinghostedruntime.ErrCleanup {
			message = "hosted worker cleanup unconfirmed; operator recovery required"
		}
		_, _ = fmt.Fprintln(stderr, message)
		return 1
	}
	// This is an opaque local completion receipt, NOT the signed public result
	// and not a claim that the candidate passed its private tests.
	if json.NewEncoder(stdout).Encode(map[string]any{"schema": "dittobench-coding-hosted-runtime-result-v2", "terminal_evidence_sha256": sha, "shadow_only": true, "weight_eligible": false}) != nil {
		return 1
	}
	return 0
}
