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
	"github.com/ditto-assistant/dittobench-api/internal/rootlessnetns"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

// Validate checks private startup files without consuming a directory, changing
// environment, contacting Platform, or running candidate code. In rootless-netns
// mode it also runs the read-only router precheck, which reads the Docker
// socket's peer credentials and inspects the default bridge network.
func Validate(path string) error {
	config, err := loadRuntimeConfig(path)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), routerPrecheckTimeout)
	defer cancel()
	return precheckRouter(ctx, config)
}

// Run is only for a dedicated Platform worker process. It replaces the process
// environment before constructing any execution adapters. Never embed it in
// Platform's HTTP server or a validator. No scheduler/gate is enabled here.
func Run(ctx context.Context, path string) (string, error) {
	if ctx == nil || ctx.Err() != nil {
		return "", ErrConfig
	}
	config, err := loadRuntimeConfig(path)
	if err != nil {
		return "", err
	}
	if ctx.Err() != nil {
		return "", ErrExecution
	}
	// A rootless-netns host that cannot serve the router refuses before the
	// one-shot state root is consumed. Listen repeats every check afterwards.
	if err := precheckRouter(ctx, config); err != nil {
		return "", err
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
	listener, err := listenRouter(ctx, config)
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

// Private test seams; production always uses the verified RootlessKit
// listener, its read-only precheck and the Engine API bridge inspection.
var (
	loadRuntimeConfig   = loadConfig
	rootlessListen      = rootlessnetns.Listen
	rootlessPrecheck    = rootlessnetns.Precheck
	daemonBridgeGateway = sandbox.DefaultBridgeGatewayFromSocket
)

const routerPrecheckTimeout = 20 * time.Second

// precheckRouter is read-only and runs before the attempt is consumed. It
// requires the configured daemon's default bridge gateway to be router_listen
// and every RootlessKit prerequisite of the listener to hold: the child pid is
// readable, the child and the daemon share the pinned user and network
// namespaces, and the daemon is not in detached-netns mode. Host mode has no
// router precheck.
func precheckRouter(ctx context.Context, config *runtimeConfig) error {
	if config.router == nil {
		return nil
	}
	gateway, err := daemonBridgeGateway(ctx, config.wire.DockerSocket)
	if err != nil || gateway != config.router.address.Addr() {
		return ErrConfig
	}
	if rootlessPrecheck(ctx, config.rootlessConfig()) != nil {
		return ErrConfig
	}
	return nil
}

func (c *runtimeConfig) rootlessConfig() rootlessnetns.Config {
	return rootlessnetns.Config{Address: c.router.address, DockerSocket: c.wire.DockerSocket, HelperExecutable: c.router.helper}
}

// listenRouter never falls back between namespaces. In rootless-netns mode the
// configured address must be the daemon's default bridge gateway, and the
// listener must come from the pinned RootlessKit network namespace.
func listenRouter(ctx context.Context, config *runtimeConfig) (net.Listener, error) {
	if config.router == nil {
		return (&net.ListenConfig{}).Listen(ctx, "tcp4", config.wire.RouterListen)
	}
	gateway, err := config.docker.DefaultBridgeGateway(ctx)
	// loadConfig binds HostGatewayIP (host.docker.internal) to this same address.
	if err != nil || gateway != config.router.address.Addr() {
		return nil, ErrExecution
	}
	listener, err := rootlessListen(ctx, config.rootlessConfig())
	if err != nil {
		return nil, ErrExecution
	}
	// Host nftables never see this traffic, so the worker ends candidate access
	// itself at min(connectivity expiry, attempt deadline) or when Run's context
	// ends (SIGTERM, including systemd stopping the unit with its egress guard).
	return rootlessnetns.WithAuthority(ctx, listener, config.router.expires)
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
