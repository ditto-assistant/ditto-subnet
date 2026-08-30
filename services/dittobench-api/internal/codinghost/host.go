// Package codinghost composes the reviewed shadow coding runtime behind two
// private handlers. Construction is explicit and default-off at the command
// boundary; this package owns no scheduler, Platform signer, score, or weight.
package codinghost

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"syscall"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingartifacts"
	"github.com/ditto-assistant/dittobench-api/internal/codingattempt"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codingoutbox"
	"github.com/ditto-assistant/dittobench-api/internal/codingphase"
	"github.com/ditto-assistant/dittobench-api/internal/codingpublication"
	"github.com/ditto-assistant/dittobench-api/internal/codingseed"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/ditto-assistant/dittobench-api/internal/codingsupervisor"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

var (
	ErrInvalidConfig = errors.New("coding shadow host configuration is invalid")
	ErrClosed        = errors.New("coding shadow host is closed")
)

type Config struct {
	ControlToken           string
	PrivateRoot            string
	SourceListener         net.Listener
	SourcePublicBaseURL    string
	Policy                 codingcontract.InferencePolicy
	RuntimeImageRepository string
	Docker                 *sandbox.LocalDocker
	CandidateUID           uint32
	CandidateGID           uint32
	MaxTotalBytes          int64
	JournalMaxTotalBytes   int64
	MaxAttempts            int
	Now                    func() time.Time
}

type Host struct {
	mu sync.Mutex

	supervisor  *codingsupervisor.Service
	backend     *codingsupervisor.SessionBackend
	publication *codingpublication.Service
	router      *codingsource.Router
	outbox      *codingoutbox.Store
	sweepCancel context.CancelFunc
	sweepDone   chan struct{}
	closed      bool
}

func New(config Config) (*Host, error) {
	return newHost(config, nil)
}

func newHost(config Config, availability func(context.Context) error) (*Host, error) {
	if nilLike(config.SourceListener) || config.Docker == nil || !validToken(config.ControlToken) ||
		config.Policy.Validate() != nil || config.MaxTotalBytes < 64<<20 ||
		config.MaxTotalBytes > 1<<40 || config.MaxAttempts < 1 || config.MaxAttempts > 10_000 {
		return nil, ErrInvalidConfig
	}
	if config.JournalMaxTotalBytes == 0 {
		config.JournalMaxTotalBytes = 3 << 30
	}
	if config.JournalMaxTotalBytes < 128<<20 || config.JournalMaxTotalBytes > 16<<30 {
		return nil, ErrInvalidConfig
	}
	now := config.Now
	if now == nil {
		now = time.Now
	}
	root, err := preparePrivateRoot(config.PrivateRoot)
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	outboxRoot, err := privateChild(root, "outbox")
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	relayRoot, err := privateChild(root, "relay")
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	outbox, err := codingoutbox.Open(codingoutbox.Config{
		Root: outboxRoot, MaxTotalBytes: config.MaxTotalBytes, MaxAttempts: config.MaxAttempts,
		FinalizationGrace: 5 * time.Minute, OrphanGrace: 5 * time.Minute,
		ReleasedRetention: 24 * time.Hour, ExpiredRetention: 24 * time.Hour, Now: now,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	closeOutbox := true
	defer func() {
		if closeOutbox {
			_ = outbox.Close()
			_ = config.SourceListener.Close()
		}
	}()

	registry := codingsource.NewRegistry(now)
	router, err := codingsource.NewRouter(codingsource.RouterConfig{
		Listener: config.SourceListener, PublicBaseURL: config.SourcePublicBaseURL,
		Registry: registry, MaxRoutes: 64,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	closeRouter := true
	defer func() {
		if closeRouter {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			_ = router.Close(ctx)
			cancel()
		}
	}()

	harnessRuntime, err := codingharness.NewSandboxRuntime(config.Docker)
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	if availability == nil {
		availability = harnessRuntime.Available
	}
	availabilityContext, cancelAvailability := context.WithTimeout(context.Background(), 30*time.Second)
	availabilityErr := availability(availabilityContext)
	cancelAvailability()
	if availabilityErr != nil {
		return nil, errors.Join(ErrInvalidConfig, availabilityErr)
	}
	harnesses, err := codingharness.New(codingharness.Config{
		Runtime: harnessRuntime, Sources: registry, Now: now, MaxInstances: 1,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	fetcher, err := codingartifacts.New(codingartifacts.Config{
		RequestTimeout: 2 * time.Minute, AllowLoopback: false, Now: now,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	seeds, err := codingseed.New(codingseed.Config{
		MaxBundleBytes: codingcontract.MaxCanonicalJSONBytes,
		SeedTimeout:    2 * time.Minute, Now: now,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	executors, err := codingexecutor.NewPhaseFactory(codingexecutor.FactoryConfig{
		ImageRepository: config.RuntimeImageRepository,
		CandidateUID:    config.CandidateUID, CandidateGID: config.CandidateGID,
		RequireRootless: true, RequireIsolatedDaemon: true,
		SeccompProfile: config.Docker.SeccompProfile, AppArmorProfile: config.Docker.AppArmorProfile,
		Now: now,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	attempts, err := codingattempt.NewRuntime(codingattempt.RuntimeConfig{
		Artifacts: fetcher, Executors: executors, SeedProjector: seeds, Now: now,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	attemptAdapter, err := codingphase.NewRuntimeAdapter(attempts)
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	inference, err := codingphase.NewGatewayActivator(codingphase.GatewayActivatorConfig{
		JournalRoot: relayRoot, JournalMaxTotalBytes: config.JournalMaxTotalBytes,
		JournalMaxEntries: 4096, JournalMaxDirectories: config.MaxAttempts,
		Publisher: router.InferencePublisher(), Now: now,
		OperationTimeout: 30 * time.Second, CleanupTimeout: 30 * time.Second,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	phase, err := codingphase.New(codingphase.Config{
		Attempts: attemptAdapter, Outbox: outbox, Seeds: seeds, Harnesses: harnesses,
		WorkspaceRoutes: router.WorkspacePublisher(), Inference: inference,
		InferencePolicy: config.Policy, Now: now, CleanupTimeout: 30 * time.Second,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	backend, err := codingsupervisor.NewSessionBackend(codingsupervisor.SessionBackendConfig{
		Runner: phase, MaximumSessions: config.MaxAttempts,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	supervisor, err := codingsupervisor.New(codingsupervisor.Config{
		ControlToken: config.ControlToken, Backend: backend,
		OperationTimeout: 31 * time.Minute, Now: now,
	})
	if err != nil {
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	publication, err := codingpublication.New(codingpublication.Config{
		Store: outbox, ControlToken: config.ControlToken,
	})
	if err != nil {
		_ = supervisor.Close()
		_ = backend.Close()
		return nil, errors.Join(ErrInvalidConfig, err)
	}
	closeOutbox = false
	closeRouter = false
	host := &Host{
		supervisor: supervisor, backend: backend, publication: publication,
		router: router, outbox: outbox, sweepDone: make(chan struct{}),
	}
	sweepContext, sweepCancel := context.WithCancel(context.Background())
	host.sweepCancel = sweepCancel
	go host.sweepLoop(sweepContext, 5*time.Minute)
	return host, nil
}

func (host *Host) SupervisorHandler() http.Handler {
	if host == nil {
		return http.NotFoundHandler()
	}
	return host.supervisor.Handler()
}

func (host *Host) PublicationHandler() http.Handler {
	if host == nil {
		return http.NotFoundHandler()
	}
	return host.publication.Handler()
}

func (host *Host) Close(ctx context.Context) error {
	if host == nil {
		return nil
	}
	host.mu.Lock()
	defer host.mu.Unlock()
	if host.closed {
		return nil
	}
	if ctx == nil {
		return ErrClosed
	}
	host.sweepCancel()
	select {
	case <-host.sweepDone:
	case <-ctx.Done():
		return errors.Join(ErrClosed, ctx.Err())
	}
	if err := errors.Join(host.supervisor.Close(), host.publication.Close(), host.backend.Close()); err != nil {
		return errors.Join(ErrClosed, err)
	}
	if err := host.router.Close(ctx); err != nil {
		return errors.Join(ErrClosed, err)
	}
	if err := host.outbox.Close(); err != nil {
		return errors.Join(ErrClosed, err)
	}
	host.closed = true
	return nil
}

func (host *Host) sweepLoop(ctx context.Context, interval time.Duration) {
	defer close(host.sweepDone)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_, _ = host.outbox.Sweep(ctx)
		}
	}
}

func preparePrivateRoot(root string) (string, error) {
	if !filepath.IsAbs(root) || filepath.Clean(root) == string(filepath.Separator) {
		return "", ErrInvalidConfig
	}
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 ||
		info.Mode().Perm() != 0o700 {
		return "", ErrInvalidConfig
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || int(stat.Uid) != os.Geteuid() {
		return "", ErrInvalidConfig
	}
	return filepath.Clean(root), nil
}

func privateChild(root, name string) (string, error) {
	path := filepath.Join(root, name)
	if err := os.Mkdir(path, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return "", err
	}
	info, err := os.Lstat(path)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o700 {
		return "", ErrInvalidConfig
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || int(stat.Uid) != os.Geteuid() {
		return "", ErrInvalidConfig
	}
	return path, nil
}

func nilLike(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

func validToken(value string) bool {
	if len(value) < 32 || len(value) > 256 || !utf8.ValidString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) ||
			((character < 'a' || character > 'z') && (character < 'A' || character > 'Z') &&
				(character < '0' || character > '9') && character != '_' && character != '-') {
			return false
		}
	}
	return true
}

func (host *Host) String() string           { return "CodingShadowHost{private}" }
func (host *Host) GoString() string         { return host.String() }
func (host *Host) LogValue() slog.Value     { return slog.StringValue("coding-shadow-host") }
func (*Host) MarshalJSON() ([]byte, error)  { return nil, ErrClosed }
func (Config) String() string               { return "CodingShadowHostConfig{private}" }
func (config Config) GoString() string      { return config.String() }
func (Config) LogValue() slog.Value         { return slog.StringValue("coding-shadow-host-config") }
func (Config) MarshalJSON() ([]byte, error) { return nil, ErrClosed }

var _ json.Marshaler = (*Host)(nil)
var _ json.Marshaler = Config{}
