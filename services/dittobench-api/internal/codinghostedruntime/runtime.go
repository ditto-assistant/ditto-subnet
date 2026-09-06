package codinghostedruntime

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedworker"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
)

// Run is only for a dedicated Platform worker process. It replaces the process
// environment before constructing any execution adapters. Never embed it in
// Platform's HTTP server or a validator. No scheduler/gate is enabled here.
func Run(ctx context.Context, path string) (string, error) {
	if ctx == nil || ctx.Err() != nil {
		return "", ErrConfig
	}
	config, err := loadConfig(path)
	if err != nil {
		return "", err
	}
	if ctx.Err() != nil {
		return "", ErrExecution
	}
	// Commit before environment preparation or any execution. A crash or
	// ambiguous failure never becomes permission to reuse this directory.
	if err := consume(config.wire.StateRoot); err != nil {
		return "", err
	}
	if installEnvironment(config) != nil {
		return "", ErrConfig
	}
	runtime, err := codingharness.NewHostedSandboxRuntime(config.docker)
	if err != nil || runtime.Available(ctx) != nil {
		return "", ErrExecution
	}
	registry := codingsource.NewRegistry(nil)
	listener, err := (&net.ListenConfig{}).Listen(ctx, "tcp4", config.wire.RouterListen)
	if err != nil {
		return "", ErrExecution
	}
	router, err := codingsource.NewRouter(codingsource.RouterConfig{Listener: listener, PublicBaseURL: config.publicBase, Registry: registry, MaxRoutes: 2})
	if err != nil {
		_ = listener.Close()
		return "", ErrExecution
	}
	closeRouter := func() error {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		return router.Close(ctx)
	}
	harnesses, err := codingharness.NewHosted(codingharness.HostedConfig{Config: codingharness.Config{Runtime: runtime, Sources: registry, MaxInstances: 1}, Starts: config.starts})
	if err != nil {
		_ = closeRouter()
		return "", ErrExecution
	}
	worker, err := codinghostedworker.New(codinghostedworker.Config{Harnesses: harnesses, Executors: config.executors, Router: router, Control: config.control})
	if err != nil {
		_ = closeRouter()
		return "", ErrExecution
	}
	attempt, err := worker.Attempt(config.wire.Harness)
	if err != nil {
		_ = closeRouter()
		return "", ErrExecution
	}
	result, runErr := runAttempt(ctx, attempt)
	// Do not pretend a server with unresolved routes/cleanup was shut down.
	// The command returns a fixed recovery error; no automatic deletion/retry
	// can erase the persisted tombstone, frozen workspace or evidence spool.
	if closeRouter() != nil {
		return "", ErrCleanup
	}
	return result, runErr
}

func installEnvironment(config *runtimeConfig) error {
	root := config.wire.StateRoot
	// Both directories must be new. In particular, never reuse a Docker config
	// that could contain registry credentials, a context or a credential helper.
	for _, name := range []string{"docker-config", "tmp"} {
		if os.Mkdir(filepath.Join(root, name), 0700) != nil {
			return ErrConfig
		}
	}
	environment := map[string]string{
		"PATH":          filepath.Dir(config.wire.DockerExecutable),
		"DOCKER_HOST":   "unix://" + config.wire.DockerSocket,
		"DOCKER_CONFIG": filepath.Join(root, "docker-config"),
		"TMPDIR":        filepath.Join(root, "tmp"),
	}
	os.Clearenv()
	for key, value := range environment {
		if os.Setenv(key, value) != nil {
			return ErrConfig
		}
	}
	return nil
}

type attemptLifecycle interface {
	Run(context.Context) (codingrunner.HostedReplayAuthority, error)
	RetryFinalization(context.Context) (codingrunner.HostedReplayAuthority, error)
	Grade(context.Context) (string, error)
	Cleanup() error
}

func runAttempt(ctx context.Context, attempt attemptLifecycle) (string, error) {
	// Every retry below is an exact-byte finalization/publication retry on the
	// SAME object. Run is never called twice. Grade internally consumes its
	// one-shot grading claim before execution and subsequently republishes only
	// its captured bytes. Failed claims cannot become a second grade.
	_, err := attempt.Run(ctx)
	for retry := 0; err != nil && retry < 2 && ctx.Err() == nil; retry++ {
		_, err = attempt.RetryFinalization(ctx)
	}
	result := ""
	if err == nil && ctx.Err() == nil {
		for retry := 0; retry < 3 && ctx.Err() == nil; retry++ {
			result, err = attempt.Grade(ctx)
			if err == nil {
				break
			}
		}
	}
	cleaned := false
	for retry := 0; retry < 3; retry++ {
		if attempt.Cleanup() == nil {
			cleaned = true
			break
		}
	}
	if !cleaned {
		return "", ErrCleanup
	}
	if err != nil || ctx.Err() != nil || len(result) != 64 || strings.ContainsFunc(result, func(r rune) bool { return !(r >= '0' && r <= '9' || r >= 'a' && r <= 'f') }) {
		return "", ErrExecution
	}
	return result, nil
}
