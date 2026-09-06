package codingharness

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
)

type startStoreFunc func(context.Context, HostedBinding, string) (bool, error)

func (f startStoreFunc) CommitStart(ctx context.Context, b HostedBinding, instance string) (bool, error) {
	return f(ctx, b, instance)
}

func hostedFixture(t *testing.T, store HostedStartStore) (*HostedFactory, *fakeRuntime, HostedBinding) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/coding/health":
			fmt.Fprint(w, `{"status":"ok","supported_coding_contract_versions":[2],"capabilities":["scoped_memory_seed_v2","coding_runner_tools_v2","case_scoped_inference_v2"]}`)
		case "/coding/seed":
			var seed codingcontract.HostedSeedRequest
			if json.NewDecoder(r.Body).Decode(&seed) != nil || seed.Validate() != nil {
				t.Error("invalid native request")
			}
			json.NewEncoder(w).Encode(map[string]any{"case_id": seed.CaseID, "profile_capability_id": seed.ProfileCapabilityID, "memory_bundle_sha256": seed.MemoryBundleSHA256, "memory_count": len(seed.Memories), "idempotent_replay": false})
		case "/coding/run":
			var run codingcontract.HostedRunRequest
			if json.NewDecoder(r.Body).Decode(&run) != nil || run.Validate() != nil {
				t.Error("invalid native run")
			}
			json.NewEncoder(w).Encode(map[string]any{"case_id": run.CaseID, "final_report": map[string]any{"summary": "done", "remaining_risks": []string{}}})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	runtime := &fakeRuntime{baseURL: server.URL}
	factory, err := NewHosted(HostedConfig{Config: Config{Runtime: runtime, Sources: codingsource.NewRegistry(nil), Transport: server.Client().Transport}, Starts: store})
	if err != nil {
		t.Fatal(err)
	}
	old := fixtureHarnessBinding(time.Now().UTC())
	binding := HostedBinding{
		EvaluationID: old.TicketID, AttemptID: old.RunRowID, WorkerID: "10000000-0000-4000-8000-000000000001", AssignmentSHA256: strings.Repeat("d", 64),
		AgentID: old.AgentID, AgentArtifactSHA256: old.AgentArtifactSHA256, ProfileCapabilityID: old.ProfileCapabilityID, Deadline: old.Deadline,
		ScreenedImageSHA256: old.ScreenedImageSHA256, ScreenedImageID: old.ScreenedImageID, ScreenedImageRef: old.ScreenedImageRef,
		ScreenedImageSize: old.ScreenedImageSize, ScreeningPolicyVersion: old.ScreeningPolicyVersion, ImageURL: old.ImageURL, ImageExpiresAt: old.ImageExpiresAt,
	}
	return factory, runtime, binding
}

func cleanupHosted(t *testing.T, handle *HostedHandle) {
	t.Helper()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		if err := handle.Destroy(ctx); err != nil {
			t.Error(err)
		}
	})
}

func TestHostedRequiresCommittedStartAndUsesNativeClient(t *testing.T) {
	var commits atomic.Int32
	factory, runtime, binding := hostedFixture(t, startStoreFunc(func(_ context.Context, b HostedBinding, instance string) (bool, error) {
		if b.ImageURL != "" || instance == "" || b.AssignmentSHA256 == "" {
			t.Error("invalid start projection")
		}
		return commits.Add(1) == 1, nil
	}))
	handle, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal(err)
	}
	cleanupHosted(t, handle)
	if runtime.started != 0 || commits.Load() != 0 {
		t.Fatal("acquire executed candidate")
	}
	if _, err := handle.Health(t.Context()); err == nil {
		t.Fatal("dormant client usable")
	}
	if _, err := factory.Acquire(t.Context(), binding); err == nil {
		t.Fatal("duplicate acquisition")
	}
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			if err := handle.Activate(t.Context()); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if runtime.started != 1 || commits.Load() != 1 {
		t.Fatal("start replay")
	}
	if health, err := handle.Health(t.Context()); err != nil || !health.SupportsCodingV2() {
		t.Fatal("native health failed", err)
	}
	seed := codingcontract.HostedSeedRequest{CodingContractVersion: 2, TicketID: binding.EvaluationID, CaseID: binding.AttemptID, ProfileCapabilityID: binding.ProfileCapabilityID, Memories: []codingcontract.VisibleMemory{}, MemoryBundleSHA256: ""}
	// The public empty memory artifact has canonical bytes independent of identity.
	seed.MemoryBundleSHA256 = fmt.Sprintf("%x", sha256.Sum256([]byte("{\"memories\":[]}\n")))
	if _, err := handle.Seed(t.Context(), seed); err != nil {
		t.Fatal(err)
	}
	run := codingcontract.HostedRunRequest{
		CodingContractVersion: 2, TicketID: binding.EvaluationID, CaseID: binding.AttemptID, ProfileCapabilityID: binding.ProfileCapabilityID,
		RepositoryEpoch: "epoch-1", VisibleBundleSHA256: strings.Repeat("a", 64),
		Issue:                  codingcontract.Issue{Title: "Repair", Description: "Preserve identifiers", Constraints: []string{}},
		RuntimePolicy:          codingcontract.RuntimePolicy{EditablePaths: []string{"app.py"}, TestCommandIDs: []string{"visible"}, BuildCommandIDs: []string{}},
		WorkspaceCapabilityURL: "http://host.docker.internal:9000/v2/coding/workspace/synthetic/tool",
		InferenceBaseURL:       "http://host.docker.internal:9000/v2/coding/inference/synthetic",
		Budgets:                codingcontract.Budgets{ModelInputTokens: 1000, ModelOutputTokens: 100, WorkspaceToolCalls: 10, WallTimeSeconds: 60},
	}
	if _, err := handle.Run(t.Context(), run); err != nil {
		t.Fatal(err)
	}
	if _, err := handle.Run(t.Context(), run); err == nil {
		t.Fatal("run replay accepted")
	}
	run.TicketID = binding.WorkerID
	if _, err := handle.Run(t.Context(), run); err == nil {
		t.Fatal("foreign run accepted")
	}
	seed.CaseID = binding.WorkerID
	if _, err := handle.Seed(t.Context(), seed); err == nil {
		t.Fatal("cross-attempt request accepted")
	}
	if err := handle.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := handle.Activate(t.Context()); err == nil {
		t.Fatal("destroyed handle restarted")
	}
	replay, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal(err)
	}
	cleanupHosted(t, replay)
	if err := replay.Activate(t.Context()); err == nil {
		t.Fatal("persisted start replay accepted")
	}
}

func TestHostedStartDenialAndLostAcknowledgementNeverLaunch(t *testing.T) {
	for _, failed := range []bool{false, true} {
		t.Run(fmt.Sprint(failed), func(t *testing.T) {
			var calls atomic.Int32
			factory, runtime, binding := hostedFixture(t, startStoreFunc(func(context.Context, HostedBinding, string) (bool, error) {
				calls.Add(1)
				if failed {
					return true, errors.New("private secret")
				}
				return false, nil
			}))
			h, err := factory.Acquire(t.Context(), binding)
			if err != nil {
				t.Fatal(err)
			}
			cleanupHosted(t, h)
			if err := h.Activate(t.Context()); err == nil || strings.Contains(err.Error(), "secret") {
				t.Fatal("bad gate failure")
			}
			if err := h.Activate(t.Context()); err == nil {
				t.Fatal("gate retried")
			}
			if runtime.started != 0 || calls.Load() != 1 {
				t.Fatal("start boundary bypass")
			}
		})
	}
}

func TestHostedStopFailureRetainsContainerAndDeniesRestart(t *testing.T) {
	factory, runtime, binding := hostedFixture(t, startStoreFunc(func(context.Context, HostedBinding, string) (bool, error) { return true, nil }))
	h, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal(err)
	}
	cleanupHosted(t, h)
	if err := h.Activate(t.Context()); err != nil {
		t.Fatal(err)
	}
	runtime.mu.Lock()
	runtime.stopErr = errors.New("stop failed")
	runtime.mu.Unlock()
	if err := h.Destroy(t.Context()); err == nil {
		t.Fatal("failed stop reported success")
	}
	if _, err := h.Health(t.Context()); err == nil {
		t.Fatal("client survives failed cleanup")
	}
	if err := h.Activate(t.Context()); err == nil {
		t.Fatal("cleanup failure permitted restart")
	}
	if _, err := factory.Acquire(t.Context(), binding); err == nil {
		t.Fatal("lost cleanup reservation")
	}
	runtime.mu.Lock()
	runtime.stopErr = nil
	runtime.mu.Unlock()
	if err := h.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
}

func TestHostedExpiryStopsContainerAndInvalidAuthorityDoesNotLoad(t *testing.T) {
	factory, runtime, binding := hostedFixture(t, startStoreFunc(func(context.Context, HostedBinding, string) (bool, error) { return true, nil }))
	bad := binding
	bad.WorkerID = "bad"
	if _, err := factory.Acquire(t.Context(), bad); err == nil {
		t.Fatal("bad worker accepted")
	}
	if runtime.loaded != 0 {
		t.Fatal("invalid authority loaded bytes")
	}
	h, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal(err)
	}
	cleanupHosted(t, h)
	if err := h.Activate(t.Context()); err != nil {
		t.Fatal(err)
	}
	// Exercise the same cancellation used by the lifetime timer without a sleep.
	h.cancel()
	if _, err := h.Health(t.Context()); err == nil {
		t.Fatal("expired client active")
	}
	if err := h.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
	for _, v := range []any{h, factory, binding, h.SourceBinding()} {
		if _, err := json.Marshal(v); err == nil {
			t.Fatal("private lifecycle JSON exported")
		}
		if strings.Contains(fmt.Sprintf("%+v", v), binding.ImageURL) {
			t.Fatal("private URL leaked")
		}
	}
}

type partialRuntime struct{ *fakeRuntime }

func (runtime partialRuntime) Start(ctx context.Context, image string) (Running, error) {
	running, err := runtime.fakeRuntime.Start(ctx, image)
	if err != nil {
		return running, err
	}
	return running, errors.New("partial launch")
}

func TestHostedPartialStartRetainsCleanupAndDoesNotRetry(t *testing.T) {
	factory, runtime, binding := hostedFixture(t, startStoreFunc(func(context.Context, HostedBinding, string) (bool, error) { return true, nil }))
	factory.base.runtime = partialRuntime{runtime}
	runtime.stopErr = errors.New("not stopped")
	h, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal(err)
	}
	cleanupHosted(t, h)
	if err := h.Activate(t.Context()); err == nil {
		t.Fatal("partial start succeeded")
	}
	if err := h.Destroy(t.Context()); err == nil {
		t.Fatal("failed partial cleanup accepted")
	}
	if err := h.Activate(t.Context()); err == nil {
		t.Fatal("partial start retried")
	}
	runtime.mu.Lock()
	runtime.stopErr = nil
	runtime.mu.Unlock()
	if err := h.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
	runtime.mu.Lock()
	defer runtime.mu.Unlock()
	if runtime.started != 1 || runtime.stopped < 2 {
		t.Fatal("lost partially running container")
	}
}

func TestHostedCancellationWhileCommittingPreventsExecution(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	factory, runtime, binding := hostedFixture(t, startStoreFunc(func(context.Context, HostedBinding, string) (bool, error) {
		close(entered)
		<-release
		return true, nil
	}))
	h, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal(err)
	}
	cleanupHosted(t, h)
	ctx, cancel := context.WithCancel(t.Context())
	result := make(chan error, 1)
	go func() { result <- h.Activate(ctx) }()
	<-entered
	cancel()
	close(release)
	if err := <-result; err == nil {
		t.Fatal("late commit acknowledgement accepted")
	}
	if err := h.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
	if runtime.started != 0 {
		t.Fatal("candidate launched after cancellation")
	}
}

func TestHostedDestroyCancelsInflightHTTP(t *testing.T) {
	entered := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { close(entered); <-r.Context().Done() }))
	defer server.Close()
	factory, runtime, binding := hostedFixture(t, startStoreFunc(func(context.Context, HostedBinding, string) (bool, error) { return true, nil }))
	runtime.baseURL = server.URL
	h, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal(err)
	}
	cleanupHosted(t, h)
	if err := h.Activate(t.Context()); err != nil {
		t.Fatal(err)
	}
	result := make(chan error, 1)
	go func() { _, err := h.Health(t.Context()); result <- err }()
	<-entered
	if err := h.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("inflight response survived destroy")
		}
	case <-time.After(time.Second):
		t.Fatal("HTTP did not cancel")
	}
}

func TestHostedActualDeadlineAutomaticallyCleansUp(t *testing.T) {
	factory, runtime, binding := hostedFixture(t, startStoreFunc(func(context.Context, HostedBinding, string) (bool, error) { return true, nil }))
	binding.Deadline = time.Now().Add(150 * time.Millisecond)
	binding.ImageExpiresAt = binding.Deadline
	h, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal(err)
	}
	cleanupHosted(t, h)
	if err := h.Activate(t.Context()); err != nil {
		t.Fatal(err)
	}
	<-h.life.Done()
	deadline := time.After(3 * time.Second)
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		factory.mu.Lock()
		remaining := len(factory.instances)
		factory.mu.Unlock()
		if remaining == 0 {
			break
		}
		select {
		case <-ticker.C:
		case <-deadline:
			t.Fatal("expired container was not cleaned up")
		}
	}
	runtime.mu.Lock()
	defer runtime.mu.Unlock()
	if runtime.stopped != 1 {
		t.Fatal("expiry failed to stop container")
	}
}

func TestHostedRequiresTrustedGateAndRejectsProxy(t *testing.T) {
	if _, err := NewHosted(HostedConfig{}); err == nil {
		t.Fatal("missing dependencies accepted")
	}
	var missing startStoreFunc
	if _, err := NewHosted(HostedConfig{Starts: missing}); err == nil {
		t.Fatal("typed nil gate accepted")
	}
	factory, runtime, _ := hostedFixture(t, startStoreFunc(func(context.Context, HostedBinding, string) (bool, error) { return true, nil }))
	if _, err := NewHosted(HostedConfig{Config: Config{Runtime: runtime, Sources: factory.base.sources, Transport: http.DefaultTransport}, Starts: factory.starts}); err == nil {
		t.Fatal("ambient proxy accepted")
	}
	if _, err := NewHosted(HostedConfig{Config: Config{Runtime: &SandboxRuntime{}, Sources: factory.base.sources}, Starts: factory.starts}); err == nil {
		t.Fatal("legacy best-effort runtime accepted")
	}
	if err := (&HostedHandle{}).Destroy(t.Context()); err == nil {
		t.Fatal("zero handle accepted")
	}
}
