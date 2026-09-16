package codinghostedruntime

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedworker"
	"github.com/ditto-assistant/dittobench-api/internal/codinglaunchjournal"
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
	// Reconcile whatever an earlier killed invocation journaled before this
	// attempt may launch anything; an unreconciled journal refuses the attempt.
	journal, err := codinglaunchjournal.Open(config.wire.LaunchJournalDir, config.wire.Expected.AttemptID, config.wire.Expected.WorkerID)
	if err != nil {
		return "", ErrCleanup
	}
	defer journal.Close()
	docker := codinglaunchjournal.ExecDocker{}
	if _, err := journal.Reconcile(ctx, docker); err != nil {
		return "", ErrCleanup
	}
	runtime, err := codingharness.NewHostedSandboxRuntime(config.docker)
	if err != nil || runtime.Available(ctx) != nil {
		return "", ErrExecution
	}
	if _, err := journal.Sentinel(ctx, docker); err != nil {
		return "", ErrExecution
	}
	config.launch.set(journal)
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
	// Unconfirmed attempt cleanup keeps the journal pending for operator
	// recovery (the explicit reconcile, or the next start); nothing more is
	// removed automatically here.
	if runErr == ErrCleanup {
		config.launch.set(nil)
		return "", ErrCleanup
	}
	if err := finishJournal(config.launch, journal, docker); err != nil {
		return "", err
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

// launchSlot is the launch hook both launch paths hold from configuration
// time. It refuses every launch until Run has reconciled and opened the
// journal, and again once the attempt is finished.
type launchSlot struct {
	mu      sync.Mutex
	journal *codinglaunchjournal.Journal
}

func (s *launchSlot) set(journal *codinglaunchjournal.Journal) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.journal = journal
}

func (s *launchSlot) intent(ctx context.Context, run string, containers, networks []string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.journal == nil {
		return codinglaunchjournal.ErrJournal
	}
	return s.journal.Intent(ctx, run, containers, networks)
}

type reconciler interface {
	Reconcile(context.Context, codinglaunchjournal.Docker) (codinglaunchjournal.Report, error)
}

// finishJournal closes the launch hook, then reconciles: the attempt's own
// cleanup has already removed its objects, so this removes the sentinel and
// confirms every other journaled object is absent before rotating.
func finishJournal(slot *launchSlot, journal reconciler, docker codinglaunchjournal.Docker) error {
	slot.set(nil)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	if _, err := journal.Reconcile(ctx, docker); err != nil {
		return ErrCleanup
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

// ReconcileLaunchJournal is the explicit operator reconcile: it removes the
// objects a killed invocation journaled, through the given Docker executable
// and socket with an empty private client configuration, and rotates the
// journal. It refuses while another process owns the journal directory.
func ReconcileLaunchJournal(ctx context.Context, directory, dockerExecutable, dockerSocket string) (codinglaunchjournal.Report, error) {
	return reconcileLaunchJournal(ctx, directory, dockerExecutable, dockerSocket, protectedExecutable)
}

func reconcileLaunchJournal(ctx context.Context, directory, dockerExecutable, dockerSocket string, executable func(string) bool) (codinglaunchjournal.Report, error) {
	if ctx == nil || !privateDirectory(directory) || !executable(dockerExecutable) || filepath.Base(dockerExecutable) != "docker" || !privateSocket(dockerSocket) {
		return codinglaunchjournal.Report{}, ErrConfig
	}
	journal, err := codinglaunchjournal.Open(directory, "reconciler", "reconciler")
	if err != nil {
		return codinglaunchjournal.Report{}, err
	}
	defer journal.Close()
	clientConfig, err := os.MkdirTemp(directory, "docker-config-")
	if err != nil {
		return codinglaunchjournal.Report{}, ErrConfig
	}
	defer os.RemoveAll(clientConfig)
	docker := codinglaunchjournal.ExecDocker{Executable: dockerExecutable, Env: []string{
		"PATH=" + filepath.Dir(dockerExecutable), "DOCKER_HOST=unix://" + dockerSocket, "DOCKER_CONFIG=" + clientConfig,
	}}
	report, err := journal.Reconcile(ctx, docker)
	if err != nil {
		return report, errors.Join(ErrCleanup, err)
	}
	return report, nil
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
