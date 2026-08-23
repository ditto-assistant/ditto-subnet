package codingharness

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingphase"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

type fakeRuntime struct {
	mu sync.Mutex

	availableErr error
	loadErr      error
	startErr     error
	stopErr      error
	baseURL      string
	loadHook     func()
	loaded       int
	started      int
	stopped      int
	released     int
}

type fakeRunning struct {
	container string
	baseURL   string
	sourceIP  string
	image     string
}

func (runtime *fakeRuntime) Available(context.Context) error { return runtime.availableErr }
func (runtime *fakeRuntime) Load(_ context.Context, source ImageSource) (string, error) {
	runtime.mu.Lock()
	defer runtime.mu.Unlock()
	runtime.loaded++
	if runtime.loadHook != nil {
		runtime.loadHook()
	}
	if source.URL == "" || source.SHA256 == "" || source.ArtifactSHA == "" {
		return "", ErrInvalid
	}
	return "dittobench-sub:fixture", runtime.loadErr
}
func (runtime *fakeRuntime) Start(_ context.Context, image string) (Running, error) {
	runtime.mu.Lock()
	defer runtime.mu.Unlock()
	runtime.started++
	if runtime.startErr != nil {
		return nil, runtime.startErr
	}
	return &fakeRunning{
		container: strings.Repeat("1", 64), baseURL: runtime.baseURL,
		sourceIP: "172.21.0.5", image: image,
	}, nil
}
func (runtime *fakeRuntime) Stop(_ context.Context, running Running) error {
	runtime.mu.Lock()
	defer runtime.mu.Unlock()
	runtime.stopped++
	if running == nil {
		return ErrInvalid
	}
	return runtime.stopErr
}
func (runtime *fakeRuntime) Release(_ context.Context, image string) {
	runtime.mu.Lock()
	defer runtime.mu.Unlock()
	if image != "" {
		runtime.released++
	}
}

func (running *fakeRunning) ContainerID() string { return running.container }
func (running *fakeRunning) BaseURL() string     { return running.baseURL }
func (running *fakeRunning) SourceIP() string    { return running.sourceIP }
func (running *fakeRunning) ImageRef() string    { return running.image }

func fixtureHarnessBinding(now time.Time) codingphase.HarnessBinding {
	agentID := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	return codingphase.HarnessBinding{
		ExecutionID: "33333333-3333-4333-8333-333333333333",
		AgentID:     agentID, RunRowID: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
		AgentArtifactSHA256: strings.Repeat("a", 64),
		TicketID:            "33333333-3333-4333-8333-333333333333",
		CaseID:              "case-harness-001", ProfileCapabilityID: "profile-harness-001",
		Deadline: now.Add(time.Hour), BenchVersion: 12,
		ScreenedImageSHA256: strings.Repeat("b", 64), ScreenedImageSize: 1024,
		ScreenedImageID:  "sha256:" + strings.Repeat("c", 64),
		ScreenedImageRef: "ditto-screen/" + agentID + ":latest", ScreeningPolicyVersion: 9,
		ImageURL:       "https://storage.invalid/image.tar?signature=synthetic",
		ImageExpiresAt: now.Add(5 * time.Minute),
	}
}

func newHarnessFixture(t *testing.T) (*Factory, *fakeRuntime, *codingsource.Registry, time.Time) {
	t.Helper()
	now := time.Date(2026, 8, 23, 18, 0, 0, 0, time.UTC)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		if request.URL.Path != "/coding/health" {
			http.NotFound(response, request)
			return
		}
		_, _ = response.Write([]byte(`{"status":"ok","supported_coding_contract_versions":[1],"capabilities":["case_scoped_inference_v1","coding_runner_tools_v1","scoped_memory_seed_v1"]}`))
	}))
	t.Cleanup(server.Close)
	runtime := &fakeRuntime{baseURL: server.URL}
	registry := codingsource.NewRegistry(func() time.Time { return now })
	factory, err := New(Config{
		Runtime: runtime, Sources: registry, Transport: server.Client().Transport,
		Now: func() time.Time { return now }, NewInstance: func() string { return "coding-harness-fixture-001" },
	})
	if err != nil {
		t.Fatal(err)
	}
	return factory, runtime, registry, now
}

func TestAcquireIsDormantThenActivateRegistersExactSource(t *testing.T) {
	factory, runtime, registry, now := newHarnessFixture(t)
	if factory.client.Timeout != 0 {
		t.Fatalf("hidden harness HTTP timeout=%s", factory.client.Timeout)
	}
	harness, err := factory.Acquire(t.Context(), fixtureHarnessBinding(now))
	if err != nil {
		t.Fatal(err)
	}
	if runtime.loaded != 1 || runtime.started != 0 {
		t.Fatalf("loaded/started=%d/%d", runtime.loaded, runtime.started)
	}
	owned := harness.(*Handle)
	if owned.binding.ImageURL != "" || !owned.binding.ImageExpiresAt.IsZero() {
		t.Fatal("dormant handle retained the screened-image capability")
	}
	if _, err := harness.Client().Health(t.Context()); !errors.Is(err, ErrInactive) {
		t.Fatalf("dormant health err=%v", err)
	}
	if err := harness.Activate(t.Context()); err != nil {
		t.Fatal(err)
	}
	health, err := harness.Client().Health(t.Context())
	if err != nil || !health.SupportsCodingV1() {
		t.Fatalf("health=%#v err=%v", health, err)
	}
	binding := fixtureHarnessBinding(now)
	published, err := newWorkspaceRouteForTest(registry, binding, harness.InstanceID())
	if err != nil {
		t.Fatal(err)
	}
	if err := published.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := published.Close(); err != nil {
		t.Fatal(err)
	}
	if err := harness.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
	if runtime.started != 1 || runtime.stopped != 1 {
		t.Fatalf("started/stopped=%d/%d", runtime.started, runtime.stopped)
	}
	if err := harness.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
}

func TestHarnessOperationContextUsesSignedBudgetAndTicketMinimum(t *testing.T) {
	ctx, cancel := boundedHarnessContext(t.Context(), time.Hour, 10*time.Minute)
	defer cancel()
	deadline, ok := ctx.Deadline()
	if !ok || deadline.After(time.Now().UTC().Add(11*time.Minute)) {
		t.Fatalf("bounded deadline=%s", deadline)
	}
}

func newWorkspaceRouteForTest(
	registry *codingsource.Registry,
	binding codingphase.HarnessBinding,
	instanceID string,
) (codingcertifier.PublishedCapability, error) {
	listener, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		return nil, err
	}
	_, port, _ := net.SplitHostPort(listener.Addr().String())
	router, err := codingsource.NewRouter(codingsource.RouterConfig{
		Listener: listener, PublicBaseURL: "http://host.docker.internal:" + port,
		Registry: registry, NewToken: func() string { return strings.Repeat("z", 43) },
	})
	if err != nil {
		return nil, err
	}
	published, err := router.WorkspacePublisher().Publish(context.Background(), codingcertifier.CapabilityBinding{
		HarnessInstanceID: instanceID, AgentArtifactSHA256: binding.AgentArtifactSHA256,
		TicketID: binding.TicketID, CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
	}, http.NotFoundHandler())
	if err != nil {
		_ = router.Close(context.Background())
		return nil, err
	}
	return &routeWithRouter{PublishedCapability: published, router: router}, nil
}

type routeWithRouter struct {
	codingcertifier.PublishedCapability
	router *codingsource.Router
}

func (route *routeWithRouter) Close() error {
	err := route.PublishedCapability.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	return errors.Join(err, route.router.Close(ctx))
}

func TestActivationFailureIsTerminalAndCleanupIsRetryable(t *testing.T) {
	factory, runtime, _, now := newHarnessFixture(t)
	runtime.startErr = errors.New("start failed")
	harness, err := factory.Acquire(t.Context(), fixtureHarnessBinding(now))
	if err != nil {
		t.Fatal(err)
	}
	if err := harness.Activate(t.Context()); !errors.Is(err, ErrLifecycle) {
		t.Fatalf("activate err=%v", err)
	}
	if err := harness.Activate(t.Context()); !errors.Is(err, ErrClosed) {
		t.Fatalf("repeat activate err=%v", err)
	}
	if err := harness.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
	if runtime.started != 1 || runtime.released == 0 {
		t.Fatalf("started/released=%d/%d", runtime.started, runtime.released)
	}
}

func TestDestroyRetriesAuthoritativeSandboxStop(t *testing.T) {
	factory, runtime, _, now := newHarnessFixture(t)
	harness, err := factory.Acquire(t.Context(), fixtureHarnessBinding(now))
	if err != nil {
		t.Fatal(err)
	}
	if err := harness.Activate(t.Context()); err != nil {
		t.Fatal(err)
	}
	runtime.stopErr = errors.New("ambiguous stop")
	if err := harness.Destroy(t.Context()); !errors.Is(err, ErrLifecycle) {
		t.Fatalf("first destroy err=%v", err)
	}
	runtime.stopErr = nil
	if err := harness.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
	if runtime.stopped != 2 {
		t.Fatalf("stop attempts=%d", runtime.stopped)
	}
}

func TestTicketDeadlinePreventsPostLoadOrDelayedActivation(t *testing.T) {
	now := time.Date(2026, 8, 23, 18, 0, 0, 0, time.UTC)
	clock := now
	runtime := &fakeRuntime{baseURL: "http://127.0.0.1:8080"}
	registry := codingsource.NewRegistry(func() time.Time { return clock })
	factory, err := New(Config{
		Runtime: runtime, Sources: registry, Now: func() time.Time { return clock },
		NewInstance: func() string { return "coding-harness-deadline-001" },
	})
	if err != nil {
		t.Fatal(err)
	}
	binding := fixtureHarnessBinding(now)
	runtime.loadHook = func() { clock = binding.Deadline }
	if _, err := factory.Acquire(t.Context(), binding); !errors.Is(err, ErrClosed) {
		t.Fatalf("post-load err=%v", err)
	}
	if runtime.released != 1 || runtime.started != 0 {
		t.Fatalf("released/started=%d/%d", runtime.released, runtime.started)
	}

	clock = now
	runtime.loadHook = nil
	harness, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal(err)
	}
	clock = binding.Deadline
	if err := harness.Activate(t.Context()); !errors.Is(err, ErrClosed) {
		t.Fatalf("delayed activate err=%v", err)
	}
	if runtime.started != 0 {
		t.Fatal("candidate started after ticket deadline")
	}
	if err := harness.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
}

func TestFactoryRejectsAuthorityDriftAndDuplicateInstance(t *testing.T) {
	factory, runtime, _, now := newHarnessFixture(t)
	for name, mutate := range map[string]func(*codingphase.HarnessBinding){
		"artifact":  func(binding *codingphase.HarnessBinding) { binding.AgentArtifactSHA256 = "bad" },
		"image ref": func(binding *codingphase.HarnessBinding) { binding.ScreenedImageRef = "other:latest" },
		"image URL": func(binding *codingphase.HarnessBinding) { binding.ImageURL = "http://storage.invalid/image" },
		"profile":   func(binding *codingphase.HarnessBinding) { binding.ProfileCapabilityID = "" },
		"expiry":    func(binding *codingphase.HarnessBinding) { binding.ImageExpiresAt = now.Add(7 * time.Minute) },
	} {
		t.Run(name, func(t *testing.T) {
			binding := fixtureHarnessBinding(now)
			mutate(&binding)
			if _, err := factory.Acquire(t.Context(), binding); !errors.Is(err, ErrInvalid) {
				t.Fatalf("err=%v", err)
			}
		})
	}
	first, err := factory.Acquire(t.Context(), fixtureHarnessBinding(now))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := factory.Acquire(t.Context(), fixtureHarnessBinding(now)); !errors.Is(err, ErrLifecycle) {
		t.Fatalf("duplicate instance err=%v", err)
	}
	if runtime.loaded != 1 || runtime.started != 0 {
		t.Fatalf("loaded/started during duplicate check=%d/%d", runtime.loaded, runtime.started)
	}
	if err := first.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
}

func TestSandboxRuntimeRequiresDedicatedRootlessCapabilityEgress(t *testing.T) {
	docker := sandbox.NewLocalDocker()
	for name, configure := range map[string]func(*sandbox.LocalDocker){
		"rootless": func(value *sandbox.LocalDocker) { value.RequireRootless = false },
		"isolated": func(value *sandbox.LocalDocker) { value.RequireIsolatedDaemon = false },
		"hardened": func(value *sandbox.LocalDocker) { value.Harden = false },
		"network":  func(value *sandbox.LocalDocker) { value.EgressNetwork = "" },
		"proxy":    func(value *sandbox.LocalDocker) { value.EgressProxy = "" },
	} {
		t.Run(name, func(t *testing.T) {
			copy := *docker
			copy.RequireRootless = true
			copy.RequireIsolatedDaemon = true
			copy.Harden = true
			copy.EgressNetwork = "required"
			copy.EgressProxy = "http://proxy.internal:8080"
			configure(&copy)
			if _, err := NewSandboxRuntime(&copy); !errors.Is(err, ErrInvalidConfig) {
				t.Fatalf("err=%v", err)
			}
		})
	}
	docker.RequireRootless = true
	docker.RequireIsolatedDaemon = true
	docker.Harden = true
	docker.EgressNetwork = "required"
	docker.EgressProxy = "http://proxy.internal:8080"
	if _, err := NewSandboxRuntime(docker); err != nil {
		t.Fatal(err)
	}
}

func TestHarnessPrivateStateRejectsSerialization(t *testing.T) {
	factory, _, _, now := newHarnessFixture(t)
	harness, err := factory.Acquire(t.Context(), fixtureHarnessBinding(now))
	if err != nil {
		t.Fatal(err)
	}
	for name, value := range map[string]any{"factory": factory, "harness": harness, "source": ImageSource{URL: "secret"}} {
		if _, err := json.Marshal(value); !errors.Is(err, ErrInvalid) {
			t.Fatalf("%s marshal err=%v", name, err)
		}
	}
	source := ImageSource{URL: "https://storage.invalid/image?signature=secret"}
	if strings.Contains(source.String(), "signature") || strings.Contains(source.GoString(), "storage") {
		t.Fatal("screened image capability leaked through diagnostics")
	}
	if err := harness.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
}
