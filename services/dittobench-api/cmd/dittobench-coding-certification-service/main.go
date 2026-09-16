// Binary dittobench-coding-certification-service is the default-off host
// certification service for the contract-v1 public coding canary. It runs as a
// dedicated non-root user beside that user's rootless Docker daemon and serves
// only the canary and its readiness probe on one fixed Unix socket. It exits
// without side effects unless DITTOBENCH_CODING_CERTIFICATION_SERVICE_ENABLED is
// exactly "true" and the rootless topology is proven.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertservice"
)

const (
	exitOK        = 0
	exitDisabled  = 3
	exitRefused   = 4
	exitPlacement = 5
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Getenv, os.Geteuid(), os.Stderr, codingcertservice.Run))
}

func run(
	ctx context.Context,
	args []string,
	getenv func(string) string,
	euid int,
	stderr io.Writer,
	serve func(context.Context, func(string) string, int) error,
) int {
	if len(args) != 0 {
		_, _ = fmt.Fprintln(stderr, "coding certification service takes no arguments")
		return exitRefused
	}
	err := serve(ctx, getenv, euid)
	switch {
	case err == nil:
		return exitOK
	case errors.Is(err, codingcertservice.ErrDisabled):
		_, _ = fmt.Fprintln(stderr, codingcertservice.ErrDisabled.Error())
		return exitDisabled
	case errors.Is(err, codingcertservice.ErrPlacement):
		_, _ = fmt.Fprintln(stderr, codingcertservice.ErrPlacement.Error())
		return exitPlacement
	case errors.Is(err, codingcertservice.ErrConfig):
		_, _ = fmt.Fprintln(stderr, codingcertservice.ErrConfig.Error())
		return exitRefused
	default:
		// Never print a wrapped error: only fixed, value-free messages.
		_, _ = fmt.Fprintln(stderr, codingcertservice.ErrUnavailable.Error())
		return exitRefused
	}
}
