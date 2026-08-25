package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/longmemeval"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
	"github.com/google/uuid"
)

func localConfirmationToolAdvertiser(t *testing.T) confirmationToolEndpointAdvertiser {
	t.Helper()
	toolSrv := toolexec.NewServer()
	server := httptest.NewServer(toolSrv)
	t.Cleanup(server.Close)
	return func(request confirmationToolEndpointRequest) (string, func(), error) {
		if strings.TrimSpace(request.CaseID) == "" || strings.TrimSpace(request.UserID) == "" {
			return "", nil, errors.New("confirmation tool_endpoint identity is unavailable")
		}
		toolSrv.Register(request.CaseID, toolexec.BuildFixture(0, protocol.ToolCase{ID: request.CaseID}))
		return server.URL, func() {}, nil
	}
}

type fakeConfirmationSandbox struct {
	mu           sync.Mutex
	harnessURL   string
	session      *brokerSession
	availableErr error
	buildErr     error
	runErr       error
	runHandle    *sandbox.Handle
	stopErr      error
	onRun        func()
	builds       int
	runs         int
	stops        int
	stopScoped   []bool
	released     int
	lastEnv      map[string]string
	envHistory   []map[string]string
}

func assertDistinctSandboxCapabilities(t *testing.T, history []map[string]string, want int) {
	t.Helper()
	if len(history) != want {
		t.Fatalf("sandbox capability history=%d want=%d", len(history), want)
	}
	seen := make(map[string]struct{}, len(history))
	for index, env := range history {
		capability := env["OPENROUTER_API_KEY"]
		if !canonicalBrokerSourceCapability(capability) {
			t.Fatalf("sandbox %d capability is not canonical: %q", index, capability)
		}
		if _, duplicate := seen[capability]; duplicate {
			t.Fatalf("sandbox capability reused at index %d", index)
		}
		seen[capability] = struct{}{}
	}
}

func TestConfirmationLongMemHarnessSealsOneRunGeneration(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	sessionID := "confirmation-longmem-generation"
	session := &brokerSession{
		confirmationSession: true,
		caseSnapshots:       make(map[uint64]brokerCaseSnapshot),
		boundRunID:          "confirmation-run",
		expiresAt:           time.Now().Add(time.Hour),
		trustedChatHandler:  http.NotFoundHandler(),
	}
	broker.sessions[sessionID] = session
	harnessServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/health" {
			writer.WriteHeader(http.StatusOK)
			return
		}
		if request.URL.Path == "/seed" {
			var seed protocol.SeedRequest
			if err := json.NewDecoder(request.Body).Decode(&seed); err != nil {
				http.Error(writer, "bad seed", http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(writer).Encode(protocol.SeedResponse{
				Pairs: len(seed.Pairs), Subjects: len(seed.Subjects), Links: len(seed.Links),
			})
			return
		}
		if request.URL.Path != "/run" {
			http.NotFound(writer, request)
			return
		}
		session.mu.Lock()
		generation := session.activeCaseGeneration
		snapshot := session.caseSnapshots[generation]
		snapshot.EmbeddingAttempts++
		snapshot.EmbeddingDispatches++
		snapshot.EmbeddingDelivered++
		session.caseSnapshots[generation] = snapshot
		session.mu.Unlock()
		http.Error(writer, "private submitted detail", http.StatusInternalServerError)
	}))
	defer harnessServer.Close()
	rawHarness, err := longmemeval.NewHTTPHarness(harnessServer.URL, harnessServer.Client())
	if err != nil {
		t.Fatal(err)
	}
	_ = rawHarness
	fake := &fakeConfirmationSandbox{harnessURL: harnessServer.URL, session: session}
	harness := &confirmationLongMemHarness{
		sandbox: fake, broker: broker, image: "screened-image", sessionID: sessionID,
		runID: "confirmation-run", healthTimeout: time.Second,
		advertiseToolEndpoint: localConfirmationToolAdvertiser(t),
		binding:               &confirmationSourceBinding{},
	}
	if _, err = harness.Seed(context.Background(), protocol.SeedRequest{UserID: "projected-user"}); err != nil {
		t.Fatal(err)
	}
	_, err = harness.Run(context.Background(), protocol.RunRequest{CaseID: "case-1", UserID: "projected-user"})
	diagnostic, ok := longmemeval.FailureDiagnostic(err)
	if !ok || diagnostic != (longmemeval.HarnessFailureDiagnostic{
		Operation: "run", Kind: "http_status", StatusCode: http.StatusInternalServerError,
	}) {
		t.Fatalf("diagnostic=%#v, %v; err=%v", diagnostic, ok, err)
	}
	if strings.Contains(err.Error(), "private submitted detail") {
		t.Fatalf("submitted response body leaked: %v", err)
	}
	session.mu.Lock()
	active := session.activeCaseGeneration
	snapshot := session.caseSnapshots[session.caseGeneration]
	session.mu.Unlock()
	if active != 0 || snapshot.EmbeddingAttempts != 1 || snapshot.EmbeddingDispatches != 1 ||
		snapshot.EmbeddingDelivered != 1 {
		t.Fatalf("sealed generation active=%d snapshot=%+v", active, snapshot)
	}
}

func (fake *fakeConfirmationSandbox) Available(context.Context) error { return fake.availableErr }
func (fake *fakeConfirmationSandbox) Build(context.Context, sandbox.Source) (string, string, *protocol.CodeFingerprint, error) {
	fake.mu.Lock()
	fake.builds++
	fake.mu.Unlock()
	if fake.buildErr != nil {
		return "", "", nil, fake.buildErr
	}
	return "screened-image", "", nil, nil
}
func (fake *fakeConfirmationSandbox) Run(_ context.Context, _ string, env map[string]string) (*sandbox.Handle, error) {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	fake.runs++
	fake.lastEnv = make(map[string]string, len(env))
	for key, value := range env {
		fake.lastEnv[key] = value
	}
	fake.envHistory = append(fake.envHistory, fake.lastEnv)
	if fake.runErr != nil {
		return fake.runHandle, fake.runErr
	}
	if fake.onRun != nil {
		fake.onRun()
	}
	if fake.runHandle != nil {
		return fake.runHandle, nil
	}
	return &sandbox.Handle{
		ContainerID: fmt.Sprintf("container-%d", fake.runs), BaseURL: fake.harnessURL,
		SourceIP: "127.0.0.1", ImageRef: "screened-image", NetworkName: fmt.Sprintf("ditto-job-%d", fake.runs),
	}, nil
}
func (fake *fakeConfirmationSandbox) Release(context.Context, string) {
	fake.mu.Lock()
	fake.released++
	fake.mu.Unlock()
}
func (fake *fakeConfirmationSandbox) Stop(context.Context, *sandbox.Handle) {
	scoped := false
	if fake.session != nil {
		fake.session.mu.Lock()
		scoped = fake.session.ablation != nil
		fake.session.mu.Unlock()
	}
	fake.mu.Lock()
	fake.stops++
	fake.stopScoped = append(fake.stopScoped, scoped)
	fake.mu.Unlock()
}
func (fake *fakeConfirmationSandbox) StopRetainingImage(ctx context.Context, handle *sandbox.Handle) error {
	fake.Stop(ctx, handle)
	return fake.stopErr
}

func TestConfirmationLongMemHarnessRejectsUserDriftAndStopFailurePreventsNextCase(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	sessionID, runID := "confirmation-longmem-isolation", "confirmation-run"
	session := &brokerSession{
		confirmationSession: true, caseSnapshots: make(map[uint64]brokerCaseSnapshot),
		boundRunID: runID, expiresAt: time.Now().Add(time.Hour), trustedChatHandler: http.NotFoundHandler(),
	}
	broker.sessions[sessionID] = session
	harnessServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/health":
			writer.WriteHeader(http.StatusOK)
		case "/seed":
			var seed protocol.SeedRequest
			if json.NewDecoder(request.Body).Decode(&seed) != nil {
				http.Error(writer, "bad seed", http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(writer).Encode(protocol.SeedResponse{Pairs: len(seed.Pairs)})
		case "/run":
			http.Error(writer, "private", http.StatusInternalServerError)
		default:
			http.NotFound(writer, request)
		}
	}))
	defer harnessServer.Close()
	fake := &fakeConfirmationSandbox{harnessURL: harnessServer.URL, session: session}
	harness := &confirmationLongMemHarness{
		sandbox: fake, broker: broker, image: "screened-image", sessionID: sessionID, runID: runID,
		healthTimeout: time.Second, advertiseToolEndpoint: localConfirmationToolAdvertiser(t),
		binding: &confirmationSourceBinding{},
	}
	if _, err := harness.Seed(context.Background(), protocol.SeedRequest{UserID: "user-a"}); err != nil {
		t.Fatal(err)
	}
	if _, err := harness.Seed(context.Background(), protocol.SeedRequest{UserID: "user-b"}); err == nil {
		t.Fatal("mismatched seed user was accepted")
	}
	if _, err := harness.Run(context.Background(), protocol.RunRequest{CaseID: "case-b", UserID: "user-b"}); err == nil {
		t.Fatal("mismatched run user was accepted")
	}
	fake.stopErr = errors.New("stop not verified")
	if _, err := harness.Run(context.Background(), protocol.RunRequest{CaseID: "case-a", UserID: "user-a"}); err == nil ||
		!strings.Contains(err.Error(), "isolation") {
		t.Fatalf("stop failure = %v", err)
	}
	if _, err := harness.Seed(context.Background(), protocol.SeedRequest{UserID: "user-c"}); err == nil {
		t.Fatal("new case started after unverified stop")
	}
	fake.mu.Lock()
	runs, stops := fake.runs, fake.stops
	fake.mu.Unlock()
	if runs != 1 || stops != 1 {
		t.Fatalf("sandbox lifecycle after stop failure runs=%d stops=%d", runs, stops)
	}
	session.mu.Lock()
	capabilityRequired := session.sourceCapabilityRequired
	capabilityActive := session.sourceCapabilityActive
	boundSource := session.expectedSourceIP
	activeGeneration := session.activeCaseGeneration
	session.mu.Unlock()
	if !capabilityRequired || capabilityActive || boundSource == "" || activeGeneration == 0 {
		t.Fatalf(
			"failed isolation cleanup required=%v active=%v source=%q generation=%d",
			capabilityRequired, capabilityActive, boundSource, activeGeneration,
		)
	}
	if broker.bindSource(sessionID, runID, boundSource) {
		t.Fatal("revoked capability session reopened legacy source-only admission")
	}

	// A failed strict stop must leave enough lifecycle identity for Close to
	// retry removal. Revocation is intentionally idempotent; it must not rotate
	// or reactivate the capability while cleanup is retried.
	fake.stopErr = nil
	harness.Close()
	fake.mu.Lock()
	runs, stops = fake.runs, fake.stops
	fake.mu.Unlock()
	if runs != 1 || stops != 2 {
		t.Fatalf("sandbox lifecycle after close retry runs=%d stops=%d", runs, stops)
	}
	session.mu.Lock()
	capabilityRequired = session.sourceCapabilityRequired
	capabilityActive = session.sourceCapabilityActive
	boundSource = session.expectedSourceIP
	activeGeneration = session.activeCaseGeneration
	session.mu.Unlock()
	if !capabilityRequired || capabilityActive || boundSource != "" || activeGeneration != 0 {
		t.Fatalf(
			"close retry cleanup required=%v active=%v source=%q generation=%d",
			capabilityRequired, capabilityActive, boundSource, activeGeneration,
		)
	}
	if harness.current != nil || harness.harness != nil || harness.generation != 0 ||
		harness.binding.sourceIP != "" || harness.binding.capability != "" {
		t.Fatalf("close retry retained runtime identity: harness=%+v binding=%+v", harness, harness.binding)
	}
}

func TestConfirmationLongMemHarnessRetainsPartialStartForCloseRetry(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	sessionID, runID := "confirmation-longmem-partial-start", "confirmation-run"
	session := &brokerSession{
		confirmationSession: true, caseSnapshots: make(map[uint64]brokerCaseSnapshot),
		boundRunID: runID, expiresAt: time.Now().Add(time.Hour), trustedChatHandler: http.NotFoundHandler(),
	}
	broker.sessions[sessionID] = session
	fake := &fakeConfirmationSandbox{
		runErr: errors.New("runtime returned a partial handle"),
		runHandle: &sandbox.Handle{
			ContainerID: "partial-container", BaseURL: "http://127.0.0.1:1",
			SourceIP: "127.0.0.1", ImageRef: "screened-image", NetworkName: "ditto-partial",
		},
		stopErr: errors.New("strict removal not yet verified"),
		session: session,
	}
	harness := &confirmationLongMemHarness{
		sandbox: fake, broker: broker, image: "screened-image", sessionID: sessionID, runID: runID,
		healthTimeout: time.Second, binding: &confirmationSourceBinding{},
	}
	if _, err := harness.Seed(context.Background(), protocol.SeedRequest{UserID: "projected-user"}); err == nil ||
		!strings.Contains(err.Error(), "isolation") {
		t.Fatalf("partial start error=%v", err)
	}
	if harness.current == nil || harness.generation == 0 || harness.currentCapability != "" {
		t.Fatalf("partial lifecycle was discarded: current=%v generation=%d capability=%q", harness.current, harness.generation, harness.currentCapability)
	}
	session.mu.Lock()
	activeCapability, source, generation := session.sourceCapabilityActive, session.expectedSourceIP, session.activeCaseGeneration
	session.mu.Unlock()
	if activeCapability || source != "" || generation == 0 {
		t.Fatalf("partial start remained admissible: active=%v source=%q generation=%d", activeCapability, source, generation)
	}
	fake.stopErr = nil
	if err := harness.Close(); err != nil {
		t.Fatal(err)
	}
	if harness.current != nil || harness.generation != 0 {
		t.Fatalf("close retry retained partial lifecycle: current=%v generation=%d", harness.current, harness.generation)
	}
	fake.mu.Lock()
	stops := fake.stops
	fake.mu.Unlock()
	if stops != 2 {
		t.Fatalf("partial start strict stop attempts=%d", stops)
	}
}

func TestConfirmationLongMemHarnessUsesFreshProcessAndEpochForEveryCase(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	sessionID, runID := "confirmation-longmem-fresh-cases", "confirmation-run"
	session := &brokerSession{
		confirmationSession: true, caseSnapshots: make(map[uint64]brokerCaseSnapshot),
		boundRunID: runID, expiresAt: time.Now().Add(time.Hour), trustedChatHandler: http.NotFoundHandler(),
	}
	broker.sessions[sessionID] = session
	var seedUsers, runUsers []string
	harnessServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/health":
			writer.WriteHeader(http.StatusOK)
		case "/seed":
			var seed protocol.SeedRequest
			if err := json.NewDecoder(request.Body).Decode(&seed); err != nil {
				http.Error(writer, "bad seed", http.StatusBadRequest)
				return
			}
			seedUsers = append(seedUsers, seed.UserID)
			_ = json.NewEncoder(writer).Encode(protocol.SeedResponse{Pairs: len(seed.Pairs)})
		case "/run":
			var run protocol.RunRequest
			if err := json.NewDecoder(request.Body).Decode(&run); err != nil {
				http.Error(writer, "bad run", http.StatusBadRequest)
				return
			}
			runUsers = append(runUsers, run.UserID)
			_ = json.NewEncoder(writer).Encode(protocol.RunResponse{FinalText: "ok"})
		default:
			http.NotFound(writer, request)
		}
	}))
	defer harnessServer.Close()
	fake := &fakeConfirmationSandbox{harnessURL: harnessServer.URL, session: session}
	binding := &confirmationSourceBinding{}
	harness := &confirmationLongMemHarness{
		sandbox: fake, broker: broker, image: "screened-image", sessionID: sessionID, runID: runID,
		healthTimeout: time.Second, advertiseToolEndpoint: localConfirmationToolAdvertiser(t), binding: binding,
	}
	for _, userID := range []string{"projected-user-a", "projected-user-b"} {
		if _, err := harness.Seed(context.Background(), protocol.SeedRequest{UserID: userID}); err != nil {
			t.Fatal(err)
		}
		if _, err := harness.Seed(context.Background(), protocol.SeedRequest{UserID: userID}); err != nil {
			t.Fatal(err)
		}
		if _, err := harness.Run(context.Background(), protocol.RunRequest{CaseID: "case-" + userID, UserID: userID}); err != nil {
			t.Fatal(err)
		}
	}
	if !reflect.DeepEqual(seedUsers, []string{"projected-user-a", "projected-user-a", "projected-user-b", "projected-user-b"}) ||
		!reflect.DeepEqual(runUsers, []string{"projected-user-a", "projected-user-b"}) {
		t.Fatalf("projected lifecycle seed=%v run=%v", seedUsers, runUsers)
	}
	fake.mu.Lock()
	runs, stops, releases := fake.runs, fake.stops, fake.released
	envHistory := append([]map[string]string(nil), fake.envHistory...)
	fake.mu.Unlock()
	if runs != 2 || stops != 2 || releases != 0 {
		t.Fatalf("sandbox lifecycle runs=%d stops=%d releases=%d", runs, stops, releases)
	}
	assertDistinctSandboxCapabilities(t, envHistory, 2)
	session.mu.Lock()
	active, epoch, source := session.activeCaseGeneration, session.sourceEpoch, session.expectedSourceIP
	session.mu.Unlock()
	if active != 0 || epoch != 6 || source != "" || binding.sourceIP != "" {
		t.Fatalf("case boundary active=%d epoch=%d session_source=%q binding_source=%q", active, epoch, source, binding.sourceIP)
	}
}

func TestConfirmationLongMemRunAdvertisesDittoBenchToolEndpoint(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	sessionID, runID := "confirmation-longmem-tool-endpoint", "confirmation-run"
	session := &brokerSession{
		confirmationSession: true, caseSnapshots: make(map[uint64]brokerCaseSnapshot),
		boundRunID: runID, expiresAt: time.Now().Add(time.Hour), trustedChatHandler: http.NotFoundHandler(),
	}
	broker.sessions[sessionID] = session
	var got protocol.RunRequest
	harnessServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/health":
			writer.WriteHeader(http.StatusOK)
		case "/seed":
			_ = json.NewEncoder(writer).Encode(protocol.SeedResponse{})
		case "/run":
			if err := json.NewDecoder(request.Body).Decode(&got); err != nil {
				http.Error(writer, "bad run", http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(writer).Encode(protocol.RunResponse{FinalText: "ok"})
		default:
			http.NotFound(writer, request)
		}
	}))
	defer harnessServer.Close()
	advertiser := localConfirmationToolAdvertiser(t)
	fake := &fakeConfirmationSandbox{harnessURL: harnessServer.URL, session: session}
	harness := &confirmationLongMemHarness{
		sandbox: fake, broker: broker, image: "screened-image", sessionID: sessionID, runID: runID,
		healthTimeout: time.Second, advertiseToolEndpoint: advertiser, binding: &confirmationSourceBinding{},
	}
	if _, err := harness.Seed(context.Background(), protocol.SeedRequest{UserID: "projected-user"}); err != nil {
		t.Fatal(err)
	}
	if _, err := harness.Run(context.Background(), protocol.RunRequest{
		CaseID: "case-1", UserID: "projected-user", Tools: longmemeval.NativeMemoryTools(),
	}); err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(got.ToolEndpoint) == "" {
		t.Fatal("confirmation LongMem /run omitted tool_endpoint")
	}
	body, err := json.Marshal(protocol.ToolExecRequest{
		CaseID: "case-1", UserID: "projected-user", Name: "search_memories",
		Args: json.RawMessage(`{"queries":["launch"]}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(got.ToolEndpoint, "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var executed protocol.ToolExecResponse
	if err := json.NewDecoder(resp.Body).Decode(&executed); err != nil {
		t.Fatal(err)
	}
	if executed.Result != "" || !strings.Contains(executed.Error, "tool not available via this endpoint: search_memories") {
		t.Fatalf("memory tool must stay unserved: %+v", executed)
	}
}

func TestConfirmationLongMemRunFailsClosedWithoutToolEndpoint(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	sessionID, runID := "confirmation-longmem-tool-endpoint-missing", "confirmation-run"
	session := &brokerSession{
		confirmationSession: true, caseSnapshots: make(map[uint64]brokerCaseSnapshot),
		boundRunID: runID, expiresAt: time.Now().Add(time.Hour), trustedChatHandler: http.NotFoundHandler(),
	}
	broker.sessions[sessionID] = session
	var runs atomic.Int64
	harnessServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/health":
			writer.WriteHeader(http.StatusOK)
		case "/seed":
			_ = json.NewEncoder(writer).Encode(protocol.SeedResponse{})
		case "/run":
			runs.Add(1)
			_ = json.NewEncoder(writer).Encode(protocol.RunResponse{FinalText: "should-not-run"})
		default:
			http.NotFound(writer, request)
		}
	}))
	defer harnessServer.Close()
	fake := &fakeConfirmationSandbox{harnessURL: harnessServer.URL, session: session}
	harness := &confirmationLongMemHarness{
		sandbox: fake, broker: broker, image: "screened-image", sessionID: sessionID, runID: runID,
		healthTimeout: time.Second, binding: &confirmationSourceBinding{},
	}
	if _, err := harness.Seed(context.Background(), protocol.SeedRequest{UserID: "projected-user"}); err != nil {
		t.Fatal(err)
	}
	_, err := harness.Run(context.Background(), protocol.RunRequest{CaseID: "case-1", UserID: "projected-user"})
	if err == nil || !strings.Contains(err.Error(), "tool_endpoint") {
		t.Fatalf("missing tool_endpoint error=%v", err)
	}
	if _, ok := longmemeval.FailureDiagnostic(err); ok {
		t.Fatalf("infrastructure tool_endpoint failure was classified as harness case failure: %v", err)
	}
	if runs.Load() != 0 {
		t.Fatalf("harness /run was invoked without a tool_endpoint")
	}
}

func TestBindConfirmationToolEndpointRejectsIncompleteIdentity(t *testing.T) {
	advertiser := localConfirmationToolAdvertiser(t)
	request := protocol.RunRequest{CaseID: "case-1", UserID: "user-1"}
	if _, err := bindConfirmationToolEndpoint(nil, "127.0.0.1", &request, confirmationBenchVersion, "session"); err == nil {
		t.Fatal("nil advertiser was accepted")
	}
	if _, err := bindConfirmationToolEndpoint(advertiser, "not-an-ip", &request, confirmationBenchVersion, "session"); err == nil {
		t.Fatal("invalid source IP was accepted")
	}
	if _, err := bindConfirmationToolEndpoint(advertiser, "127.0.0.1", &protocol.RunRequest{UserID: "user-1"}, confirmationBenchVersion, "session"); err == nil {
		t.Fatal("missing case id was accepted")
	}
}

func (*fakeConfirmationSandbox) Diagnostics(context.Context, *sandbox.Handle) sandbox.RuntimeDiagnostics {
	return sandbox.RuntimeDiagnostics{}
}
func (*fakeConfirmationSandbox) Logs(context.Context, *sandbox.Handle) string { return "" }

func TestScreenedAblationCaseRunnerUsesFreshScopedContainersAndZeroInterventionUpstream(t *testing.T) {
	model := llm.HarnessModelForVersion(confirmationBenchVersion)
	var providerCalls atomic.Int64
	provider := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		providerCalls.Add(1)
		writer.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(writer, `{"choices":[{"message":{"role":"assistant","content":"launch answer"}}],"usage":{"prompt_tokens":3,"completion_tokens":2}}`)
	}))
	defer provider.Close()
	var embeddingCalls atomic.Int64
	embedding := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		embeddingCalls.Add(1)
		writer.Header().Set("Content-Type", "application/json")
		response := platformEmbeddingResponse{}
		response.Model = "perplexity/pplx-embed-v1-0.6b"
		response.Data = append(response.Data, struct {
			Index     int       `json:"index"`
			Embedding []float64 `json:"embedding"`
		}{Index: 0, Embedding: make([]float64, embeddingDimensions)})
		response.Usage.PromptTokens, response.Usage.TotalTokens = 3, 3
		_ = json.NewEncoder(writer).Encode(response)
	}))
	defer embedding.Close()

	broker := newInferenceBroker(1, 1)
	broker.embeddingURL = embedding.URL + embeddingAPIPath
	sessionID, runID := "confirmation-runtime-test", "b881e493-7e8f-4a17-ab4e-24015fbb8c98"
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Hour)
	session := &brokerSession{
		id: sessionID, privateKey: privateKey, legacyGateway: provider.URL, expiresAt: deadline,
		provider: "test", model: model, requestModel: model,
		profileRevision: "test", boundRunID: runID, benchVersion: confirmationBenchVersion,
		confirmationSession: true, confirmationGrants: map[string]brokerConfirmationGrant{
			"embedding": {
				Lane: "embedding", GrantID: "b881e493-7e8f-4a17-ab4e-24015fbb8c98", Bearer: "embedding-capability",
				ProxyURL: embedding.URL, Generation: 1, ExpiresAt: deadline, Provider: "perplexity",
				RouteProvider: "perplexity", ReceiptProvider: "Perplexity", ProfileRevision: "fixture-embedding-v1",
				Model: "perplexity/pplx-embed-v1-0.6b", RequestBudget: 10, TokenBudget: 10_000, CostBudgetMicrousd: 10_000,
			},
		}, embeddingPhaseStarted: true, embeddingPhaseActive: true,
		embeddingConcurrency: 1, embeddingCalls: make(map[chan struct{}]context.CancelFunc),
		cancels: make(map[string]context.CancelFunc),
	}
	broker.sessions[sessionID] = session

	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/inference/{rest...}", broker.handle)
	mux.HandleFunc("POST /api/embed", broker.handleEmbedding)
	brokerServer := httptest.NewServer(mux)
	defer brokerServer.Close()
	var fake *fakeConfirmationSandbox
	harness := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/health":
			writer.WriteHeader(http.StatusNoContent)
		case "/seed":
			var seed protocol.SeedRequest
			if err := json.NewDecoder(request.Body).Decode(&seed); err != nil {
				t.Errorf("decode seed: %v", err)
			}
			_ = json.NewEncoder(writer).Encode(protocol.SeedResponse{Pairs: len(seed.Pairs), Subjects: len(seed.Subjects), Links: len(seed.Links)})
		case "/run":
			fake.mu.Lock()
			capability := fake.lastEnv["OPENROUTER_API_KEY"]
			capabilityHost, _ := brokerSourceCapabilityHost(capability)
			fake.mu.Unlock()
			embedRequest, _ := http.NewRequest(http.MethodPost, brokerServer.URL+"/api/embed", strings.NewReader(
				`{"model":"embeddinggemma","input":["frozen selected content"]}`))
			embedRequest.Header.Set("Content-Type", "application/json")
			embedRequest.Host = capabilityHost
			embedResponse, err := http.DefaultClient.Do(embedRequest)
			if err != nil {
				t.Errorf("embedding request: %v", err)
				writer.WriteHeader(http.StatusInternalServerError)
				return
			}
			_, _ = io.Copy(io.Discard, embedResponse.Body)
			_ = embedResponse.Body.Close()
			if embedResponse.StatusCode != http.StatusOK {
				t.Errorf("embedding status = %d", embedResponse.StatusCode)
			}
			chatRequest, _ := http.NewRequest(http.MethodPost, brokerServer.URL+"/v1/inference/chat/completions", strings.NewReader(
				`{"model":"`+model+`","messages":[{"role":"user","content":"frozen selected content"}]}`))
			chatRequest.Header.Set("Content-Type", "application/json")
			chatRequest.Host = capabilityHost
			chatResponse, err := http.DefaultClient.Do(chatRequest)
			if err != nil {
				t.Errorf("chat request: %v", err)
				writer.WriteHeader(http.StatusInternalServerError)
				return
			}
			defer chatResponse.Body.Close()
			var chat struct {
				Choices []struct {
					Message struct {
						Content string `json:"content"`
					} `json:"message"`
				} `json:"choices"`
			}
			if err := json.NewDecoder(chatResponse.Body).Decode(&chat); err != nil || len(chat.Choices) != 1 {
				t.Errorf("decode chat: %v", err)
				writer.WriteHeader(http.StatusInternalServerError)
				return
			}
			_ = json.NewEncoder(writer).Encode(map[string]any{
				"final_text": chat.Choices[0].Message.Content, "tool_calls": []any{},
				"prompt_tokens": 0, "output_tokens": 0, "latency_ms": 0,
			})
		default:
			writer.WriteHeader(http.StatusNotFound)
		}
	}))
	defer harness.Close()

	fake = &fakeConfirmationSandbox{harnessURL: harness.URL, session: session}
	dataset := confirmationAblationDataset{
		SchemaVersion: 1, Revision: "ablation-fixture-v1",
		Cases: []confirmationAblationCase{{
			CaseID: "case-a", UserID: "population-user-a", SystemPrompt: "Answer from memory.",
			Question: "What is the launch answer?", QuestionType: "single-session-user", ExpectedAnswer: "launch answer",
			SeedBatches: [][]protocol.MemoryPair{{{
				PairID: "pair-a", SessionID: "session-a", Timestamp: "2026-08-08T12:00:00Z",
				Prompt: "Remember the launch answer.", Response: "The launch answer is launch answer.",
			}}},
		}},
	}
	dataset.byID = map[string]confirmationAblationCase{"case-a": dataset.Cases[0]}
	runnerAdapter := &screenedAblationCaseRunner{
		sandbox: fake, broker: broker, image: "screened-image", sessionID: sessionID, runID: runID,
		healthTimeout: time.Second, dataset: dataset,
		advertiseToolEndpoint: localConfirmationToolAdvertiser(t),
		binding:               &confirmationSourceBinding{},
	}
	ordinary, err := runnerAdapter.RunCase(context.Background(), ablation.RunRequest{
		Lane: ablation.LaneOrdinary, CaseID: "case-a", OpaqueUserNamespace: "ordinary-namespace",
	})
	if err != nil || ordinary.Score != 1 {
		t.Fatalf("ordinary = %+v, %v", ordinary, err)
	}
	inferenceResponder, err := ablation.NewResponder(ablation.InterventionInference, ablation.Budget{
		MaxChatRequests: 1, MaxChatInputBytes: 4096,
	})
	if err != nil {
		t.Fatal(err)
	}
	inference, err := runnerAdapter.RunCase(context.Background(), ablation.RunRequest{
		Lane: ablation.LaneInference, CaseID: "case-a", OpaqueUserNamespace: "inference-namespace", Responder: inferenceResponder,
	})
	if err != nil || inference.Score != 0 {
		t.Fatalf("inference = %+v, %v", inference, err)
	}
	embeddingResponder, err := ablation.NewResponder(ablation.InterventionEmbedding, ablation.Budget{
		MaxEmbeddingRequests: 1, MaxEmbeddingInputs: 1, MaxEmbeddingInputBytes: 4096,
	})
	if err != nil {
		t.Fatal(err)
	}
	embeddingResult, err := runnerAdapter.RunCase(context.Background(), ablation.RunRequest{
		Lane: ablation.LaneEmbedding, CaseID: "case-a", OpaqueUserNamespace: "embedding-namespace", Responder: embeddingResponder,
	})
	if err != nil || embeddingResult.Score != 1 {
		t.Fatalf("embedding = %+v, %v", embeddingResult, err)
	}
	if got := providerCalls.Load(); got != 1 {
		t.Fatalf("provider calls = %d, want ordinary-only 1", got)
	}
	if got := embeddingCalls.Load(); got != 1 {
		t.Fatalf("embedding upstream calls = %d, want ordinary-only 1", got)
	}
	if usage := inferenceResponder.Snapshot(); usage.ChatApplied != 1 || usage.UpstreamRequests != 0 {
		t.Fatalf("inference usage = %+v", usage)
	}
	if usage := embeddingResponder.Snapshot(); usage.EmbeddingApplied != 1 || usage.UpstreamRequests != 0 {
		t.Fatalf("embedding usage = %+v", usage)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if fake.runs != 3 || fake.stops != 3 || len(fake.stopScoped) != 3 ||
		!fake.stopScoped[0] || !fake.stopScoped[1] || !fake.stopScoped[2] {
		t.Fatalf("sandbox lifecycle runs=%d stops=%d scoped=%v", fake.runs, fake.stops, fake.stopScoped)
	}
	assertDistinctSandboxCapabilities(t, fake.envHistory, 3)
}

func TestScreenedAblationRunnerRetainsPartialStartForCleanupRetry(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	sessionID, runID := "confirmation-ablation-partial-start", "confirmation-run"
	session := &brokerSession{
		confirmationSession: true, benchVersion: confirmationBenchVersion,
		boundRunID: runID, expiresAt: time.Now().Add(time.Hour), legacyGateway: "active",
	}
	broker.sessions[sessionID] = session
	fake := &fakeConfirmationSandbox{
		runErr: errors.New("runtime returned a partial handle"),
		runHandle: &sandbox.Handle{
			ContainerID: "partial-ablation", BaseURL: "http://127.0.0.1:1",
			SourceIP: "127.0.0.1", ImageRef: "screened-image", NetworkName: "ditto-partial",
		},
		stopErr: errors.New("strict removal not yet verified"),
		session: session,
	}
	item := confirmationAblationCase{CaseID: "case-a", UserID: "population-user-a"}
	runnerAdapter := &screenedAblationCaseRunner{
		sandbox: fake, broker: broker, image: "screened-image", sessionID: sessionID, runID: runID,
		healthTimeout: time.Second,
		dataset: confirmationAblationDataset{
			Cases: []confirmationAblationCase{item}, byID: map[string]confirmationAblationCase{"case-a": item},
		},
		binding: &confirmationSourceBinding{},
	}
	if _, err := runnerAdapter.RunCase(context.Background(), ablation.RunRequest{
		Lane: ablation.LaneOrdinary, CaseID: "case-a", OpaqueUserNamespace: "ordinary-namespace",
	}); err == nil || !strings.Contains(err.Error(), "isolation") {
		t.Fatalf("partial start error=%v", err)
	}
	if runnerAdapter.current == nil || runnerAdapter.currentLease == nil || runnerAdapter.currentCapability != "" || !runnerAdapter.closed {
		t.Fatalf(
			"partial ablation lifecycle discarded current=%v lease=%v capability=%q closed=%v",
			runnerAdapter.current, runnerAdapter.currentLease, runnerAdapter.currentCapability, runnerAdapter.closed,
		)
	}
	session.mu.Lock()
	activeCapability, source, scope := session.sourceCapabilityActive, session.expectedSourceIP, session.ablation
	session.mu.Unlock()
	if activeCapability || source != "" || scope == nil || !scope.draining {
		t.Fatalf("partial ablation remained admissible: active=%v source=%q scope=%+v", activeCapability, source, scope)
	}
	fake.stopErr = nil
	if err := runnerAdapter.stopCurrentLocked(); err != nil {
		t.Fatal(err)
	}
	if runnerAdapter.current != nil || runnerAdapter.currentLease != nil || runnerAdapter.currentCapability != "" {
		t.Fatalf("cleanup retry retained partial ablation lifecycle: %+v", runnerAdapter)
	}
	session.mu.Lock()
	activeCapability, source, scope = session.sourceCapabilityActive, session.expectedSourceIP, session.ablation
	required := session.sourceCapabilityRequired
	session.mu.Unlock()
	if !required || activeCapability || source != "" || scope != nil {
		t.Fatalf("cleanup retry state required=%v active=%v source=%q scope=%+v", required, activeCapability, source, scope)
	}
}

func TestDecodeConfirmationAblationDatasetRejectsDuplicateAndIncompleteCases(t *testing.T) {
	valid := `{"schema_version":1,"revision":"fixture-v1","cases":[{"case_id":"a","user_id":"u","system_prompt":"answer","question":"q","question_type":"single-session-user","expected_answer":"a","seed_batches":[[{"pair_id":"p","session_id":"s","timestamp":"2026-08-08T12:00:00Z","prompt":"q","response":"a"}]]}]}`
	if _, err := decodeConfirmationAblationDataset([]byte(valid)); err != nil {
		t.Fatal(err)
	}
	for name, raw := range map[string]string{
		"duplicate root": strings.Replace(valid, `"schema_version":1`, `"schema_version":1,"schema_version":1`, 1),
		"duplicate case": strings.Replace(valid, `"case_id":"a"`, `"case_id":"a","case_id":"b"`, 1),
		"empty batches":  strings.Replace(valid, `[[{"pair_id":"p","session_id":"s","timestamp":"2026-08-08T12:00:00Z","prompt":"q","response":"a"}]]`, `[]`, 1),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := decodeConfirmationAblationDataset([]byte(raw)); err == nil {
				t.Fatal("invalid dataset accepted")
			}
		})
	}
}

func TestValidateScreenedConfirmationSourceRejectsSourceBuildFallback(t *testing.T) {
	valid := confirmationRuntimeIdentity{
		BundleID: "bundle", TicketID: "ticket", AgentID: "agent", SlotID: "slot-0",
		ArtifactSHA256: strings.Repeat("a", 64), ProfileChecksum: strings.Repeat("c", 64),
		SettingsChecksum: strings.Repeat("d", 64), SettingsRevision: 1, Deadline: time.Now().Add(time.Hour),
		Source: sandbox.Source{
			TarballURL: "https://platform.example/artifact", TarballSHA256: strings.Repeat("a", 64),
			ScreenedImageURL: "https://platform.example/image", ScreenedImageSHA256: strings.Repeat("b", 64),
			ScreenedImageID: "sha256:" + strings.Repeat("b", 64), ScreenedImageRef: "screened@example", ScreenedImageSize: 1024,
		},
	}
	if err := validateScreenedConfirmationSource(valid); err != nil {
		t.Fatal(err)
	}
	valid.Source.ScreenedImageURL = ""
	if err := validateScreenedConfirmationSource(valid); err == nil {
		t.Fatal("missing screened image accepted")
	}
	valid.Source.ScreenedImageURL = "https://platform.example/image"
	valid.Source.GitURL = "https://github.example/source"
	if err := validateScreenedConfirmationSource(valid); err == nil {
		t.Fatal("source-build fallback accepted")
	}
}

func TestConfirmationActivationIsExplicitAndContentAddressed(t *testing.T) {
	getenv := func(string) string { return "" }
	if executor, err := confirmationExecutorFromEnvironment(getenv, nil, nil, false); err != nil || executor != nil {
		t.Fatalf("disabled installation = %v, %v", executor, err)
	}
	if executor, err := confirmationExecutorFromEnvironment(func(key string) string {
		if key == confirmationInstallationPathEnv {
			return "/immutable/config.json"
		}
		return ""
	}, nil, nil, false); err == nil || executor != nil {
		t.Fatal("partial confirmation opt-in was accepted")
	}
	directory := t.TempDir()
	path := filepath.Join(directory, "confirmation.json")
	raw := []byte(`{"schema_version":1,"execution_profile":{},"launch_manifest_sha256":"` + strings.Repeat("a", 64) + `","launch_manifest_path":"/launch.json","longmem_dataset_path":"/dataset","ablation_dataset_path":"/ablation","sandbox_health_timeout_ms":1000}`)
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := readConfirmationActivationFile(path, strings.Repeat("b", 64)); err == nil {
		t.Fatal("wrong installation checksum accepted")
	}
	if installation, err := readConfirmationActivationFile(path, digestBytes(raw)); err != nil || installation.SchemaVersion != 1 {
		t.Fatalf("content-addressed installation = %+v, %v", installation, err)
	}
	legacySecretMount := bytes.Replace(
		raw,
		[]byte(`"sandbox_health_timeout_ms":1000`),
		[]byte(`"longmem_projection_key_path":"/run/secrets/longmem","sandbox_health_timeout_ms":1000`),
		1,
	)
	if err := os.WriteFile(path, legacySecretMount, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := readConfirmationActivationFile(path, digestBytes(legacySecretMount)); err == nil {
		t.Fatal("legacy confirmation secret mount was accepted")
	}
	hostile := bytes.Replace(raw, []byte(`"schema_version":1`), []byte(`"schema_version":1,"schema_version":1`), 1)
	if err := os.WriteFile(path, hostile, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := readConfirmationActivationFile(path, digestBytes(hostile)); err == nil {
		t.Fatal("duplicate installation identity accepted")
	}
}

func TestProductionConfirmationInstallationIsExactBoundedAndCredentialFree(t *testing.T) {
	t.Helper()
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("could not locate confirmation test source")
	}
	repository := filepath.Clean(filepath.Join(filepath.Dir(source), "../../../.."))
	data := filepath.Join(
		repository,
		"packages/ditto-screening-protocol/ditto_screening_protocol/data",
	)
	installationPath := filepath.Join(data, "confirmation_installation_v9_shadow.json")
	installationRaw, err := os.ReadFile(installationPath)
	if err != nil {
		t.Fatal(err)
	}
	const installationSHA = "1b8f966e4c62c4c74888cb4b2d671e3b03e2775b003f63383d80ca4827da2c88"
	if digestBytes(installationRaw) != installationSHA {
		t.Fatalf("installation digest = %s", digestBytes(installationRaw))
	}
	composeRaw, err := os.ReadFile(filepath.Join(repository, "docker-compose.yml"))
	if err != nil || !bytes.Contains(composeRaw, []byte(installationSHA)) {
		t.Fatalf("compose installation pin missing %s: %v", installationSHA, err)
	}
	installation, err := readConfirmationActivationFile(installationPath, installationSHA)
	if err != nil {
		t.Fatal(err)
	}
	profile, _, err := decodeAndValidateConfirmationProfile(installation.ExecutionProfile)
	if err != nil {
		t.Fatal(err)
	}
	if profile.Revision != "v9-confirmation-shadow-bounded-2026-08-25-zdr-v7" ||
		profile.LongMemCasesPerCapability != 8 || profile.AblationCoordinatorPolicy.SampleSize != 4 ||
		profile.Composite.BaseWeightBPS != 7000 || profile.Composite.LongMemWeightBPS != 3000 {
		t.Fatalf("unexpected bounded profile: %+v", profile)
	}
	if len(profile.ProviderLanes) != 2 ||
		profile.ProviderLanes[0].Model != "openai/gpt-4o-2024-08-06" ||
		profile.ProviderLanes[0].RouteProvider != "azure" ||
		profile.ProviderLanes[1].Model != "openai/gpt-oss-20b" ||
		profile.ProviderLanes[1].RouteProvider != "throughput" {
		t.Fatalf("unexpected provider lanes: %+v", profile.ProviderLanes)
	}
	if profile.EmbeddingLane.Provider != "perplexity" ||
		profile.EmbeddingLane.Model != "perplexity/pplx-embed-v1-0.6b" ||
		profile.EmbeddingLane.Dimensions != 768 || profile.EmbeddingLane.MaxRequests != 20000 {
		t.Fatalf("unexpected embedding lane: %+v", profile.EmbeddingLane)
	}
	if installation.LaunchManifestPath != "/opt/ditto/confirmation/confirmation_launch_manifest_v9_shadow.json" ||
		installation.LongMemDatasetPath != "/opt/ditto/confirmation/longmemeval_s_cleaned.json" ||
		installation.AblationDatasetPath != "/opt/ditto/confirmation/confirmation_ablation_v9_shadow.json" {
		t.Fatalf("unexpected installed paths: %+v", installation)
	}
	launchRaw, err := os.ReadFile(filepath.Join(data, "confirmation_launch_manifest_v9_shadow.json"))
	if err != nil || digestBytes(launchRaw) != installation.LaunchManifestSHA256 {
		t.Fatalf("launch manifest identity = %s, %v", digestBytes(launchRaw), err)
	}
	ablationRaw, err := os.ReadFile(filepath.Join(data, "confirmation_ablation_v9_shadow.json"))
	if err != nil || digestBytes(ablationRaw) != profile.AblationDatasetSHA256 {
		t.Fatalf("ablation dataset identity = %s, %v", digestBytes(ablationRaw), err)
	}
	for _, forbidden := range []string{
		`"api_key"`, `"credential_path"`, `"credential_ref"`, `"gcp"`, `"gcloud"`,
		`"google_application_credentials"`, `"secret_manager"`, `"service_account"`, `"sm://"`,
	} {
		if bytes.Contains(bytes.ToLower(installationRaw), []byte(forbidden)) ||
			bytes.Contains(bytes.ToLower(launchRaw), []byte(forbidden)) {
			t.Fatalf("production installation contains forbidden authority field %s", forbidden)
		}
	}
}

func TestVerifyImmutableConfirmationFileRejectsUnsafeIdentityAndDrift(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "manifest.json")
	if err := os.WriteFile(path, []byte(`{"approved":true}`), 0o600); err != nil {
		t.Fatal(err)
	}
	verified, _, err := verifyImmutableConfirmationFile(path, 1024)
	if err != nil {
		t.Fatal(err)
	}
	if verified.sha256 != digestBytes([]byte(`{"approved":true}`)) {
		t.Fatal("immutable file digest mismatch")
	}
	symlink := filepath.Join(directory, "manifest-link.json")
	if err := os.Symlink(path, symlink); err != nil {
		t.Fatal(err)
	}
	for name, candidate := range map[string]string{
		"relative": "manifest.json", "symlink": symlink, "directory": directory,
		"missing": filepath.Join(directory, "missing.json"),
	} {
		t.Run(name, func(t *testing.T) {
			if _, _, err := verifyImmutableConfirmationFile(candidate, 1024); err == nil {
				t.Fatal("unsafe immutable file accepted")
			}
		})
	}
	if err := os.WriteFile(path, []byte(`{"approved":false}`), 0o600); err != nil {
		t.Fatal(err)
	}
	changed, _, err := verifyImmutableConfirmationFile(path, 1024)
	if err != nil {
		t.Fatal(err)
	}
	if changed.sha256 == verified.sha256 {
		t.Fatal("post-start launch manifest drift was not observable")
	}
}

type noOpConfirmationAuthorizer struct{}

func (noOpConfirmationAuthorizer) Authorize(
	context.Context,
	string,
	*http.Request,
) error {
	return nil
}

type confirmationFactoryFixture struct {
	factory      *screenedConfirmationRuntimeFactory
	sandbox      *fakeConfirmationSandbox
	identity     confirmationRuntimeIdentity
	longMemPath  string
	ablationPath string
	launchPath   string
	harness      *httptest.Server
	embedding    *httptest.Server
	provider     *httptest.Server
}

func newConfirmationFactoryFixture(t *testing.T, healthy bool) confirmationFactoryFixture {
	t.Helper()
	directory := t.TempDir()
	longMemPath := filepath.Join(directory, "longmem.json")
	longMemRaw := []byte(`[]`)
	if err := os.WriteFile(longMemPath, longMemRaw, 0o600); err != nil {
		t.Fatal(err)
	}
	cases := []confirmationAblationCase{
		{
			CaseID: "case-a", UserID: "user-a", SystemPrompt: "Answer from memory.", Question: "What is A?",
			QuestionType: "single-session-user", ExpectedAnswer: "A",
			SeedBatches: [][]protocol.MemoryPair{{{
				PairID: "pair-a", SessionID: "session-a", Timestamp: "2026-08-08T12:00:00Z", Prompt: "Remember A", Response: "A",
			}}},
		},
		{
			CaseID: "case-b", UserID: "user-b", SystemPrompt: "Answer from memory.", Question: "What is B?",
			QuestionType: "single-session-user", ExpectedAnswer: "B",
			SeedBatches: [][]protocol.MemoryPair{{{
				PairID: "pair-b", SessionID: "session-b", Timestamp: "2026-08-08T12:00:00Z", Prompt: "Remember B", Response: "B",
			}}},
		},
	}
	ablationRaw, err := json.Marshal(confirmationAblationDataset{
		SchemaVersion: confirmationAblationDatasetSchemaVersion, Revision: "fixture-ablation-v1", Cases: cases,
	})
	if err != nil {
		t.Fatal(err)
	}
	ablationPath := filepath.Join(directory, "ablation.json")
	if err := os.WriteFile(ablationPath, ablationRaw, 0o600); err != nil {
		t.Fatal(err)
	}
	launchRaw := []byte(`{"mode":"shadow"}`)
	launchPath := filepath.Join(directory, "launch.json")
	if err := os.WriteFile(launchPath, launchRaw, 0o600); err != nil {
		t.Fatal(err)
	}
	longMemKey := bytes.Repeat([]byte{0x43}, 32)
	selectionKey := bytes.Repeat([]byte{0x41}, 32)
	projectionKey := bytes.Repeat([]byte{0x42}, 32)
	profile, _ := validInstalledConfirmationProfile(t)
	profile.LongMemDatasetRevision = "fixture-longmem-v1"
	profile.LongMemDatasetSHA256 = digestBytes(longMemRaw)
	profile.LongMemProjectionKeySHA256 = digestBytes(longMemKey)
	profile.ProviderLanes = []confirmationProviderLaneProfile{
		{
			Lane: longmemeval.JudgeLane, Provider: "openrouter", RouteProvider: "azure", ReceiptProvider: "Azure",
			ProfileRevision: "longmemeval-official-gpt4o-azure-zdr-v2", Model: "openai/gpt-4o-2024-08-06",
			MaxRequests: 10, MaxPromptTokens: 100, MaxCompletionTokens: 100, MaxTotalTokens: 200, MaxCostUSDmicros: 10_000,
		},
		{
			Lane: longmemeval.ReaderLane, Provider: "openrouter", RouteProvider: "test", ReceiptProvider: "Test",
			ProfileRevision: "fixture-reader-v1", Model: llm.HarnessModelForVersion(9),
			MaxRequests: 10, MaxPromptTokens: 100, MaxCompletionTokens: 100, MaxTotalTokens: 200, MaxCostUSDmicros: 10_000,
		},
	}
	profile.AblationDatasetSHA256 = digestBytes(ablationRaw)
	profile.AblationSelectionKeySHA256 = digestBytes(selectionKey)
	profile.AblationProjectionKeySHA256 = digestBytes(projectionKey)
	refreshConfirmationProfileChecksums(t, &profile)

	provider := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.WriteHeader(http.StatusInternalServerError)
	}))
	t.Cleanup(provider.Close)
	embedding := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(writer).Encode(map[string]any{
			"model": profile.EmbeddingLane.Model,
			"data":  []map[string]any{{"index": 0, "embedding": make([]float64, embeddingDimensions)}},
			"usage": map[string]int{"prompt_tokens": 1, "total_tokens": 1},
		})
	}))
	t.Cleanup(embedding.Close)
	harness := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/health" && healthy {
			writer.WriteHeader(http.StatusNoContent)
			return
		}
		writer.WriteHeader(http.StatusServiceUnavailable)
	}))
	t.Cleanup(harness.Close)
	broker := newInferenceBroker(2, 1)
	broker.embeddingURL = embedding.URL + embeddingAPIPath
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Hour)
	sessionID := "confirmation-session-fixture"
	grant := func(lane, proxyURL, providerName, routeProvider, receiptProvider, revision, model string) brokerConfirmationGrant {
		return brokerConfirmationGrant{
			Lane: lane, GrantID: uuid.NewString(), Bearer: lane + "-capability", ProxyURL: proxyURL,
			Generation: 1, ExpiresAt: deadline, Provider: providerName, RouteProvider: routeProvider,
			ReceiptProvider: receiptProvider, ProfileRevision: revision, Model: model,
			RequestBudget: 10_000, TokenBudget: 10_000_000, CostBudgetMicrousd: 10_000_000,
		}
	}
	broker.sessions[sessionID] = &brokerSession{
		id: sessionID, privateKey: privateKey, expiresAt: deadline, confirmationSession: true,
		ticketAgentID: "agent", ticketSlotID: "slot-0", ticketDeadline: deadline,
		confirmationGrants: map[string]brokerConfirmationGrant{
			longmemeval.ReaderLane: grant(
				longmemeval.ReaderLane, provider.URL+"/api/v1/chat/completions", "openrouter", "test", "Test",
				"fixture-reader-v1", llm.HarnessModelForVersion(9),
			),
			longmemeval.JudgeLane: grant(
				longmemeval.JudgeLane, provider.URL+"/api/v1/chat/completions", "openrouter", "azure", "Azure",
				"longmemeval-official-gpt4o-azure-zdr-v2", "openai/gpt-4o-2024-08-06",
			),
			"embedding": grant(
				"embedding", embedding.URL, profile.EmbeddingLane.Provider, profile.EmbeddingLane.Provider,
				profile.EmbeddingLane.Provider, profile.EmbeddingLane.ProfileRevision, profile.EmbeddingLane.Model,
			),
		},
		embeddingCalls: make(map[chan struct{}]context.CancelFunc), cancels: make(map[string]context.CancelFunc),
	}
	sandboxBackend := &fakeConfirmationSandbox{harnessURL: harness.URL}
	longMemFile, _, err := verifyConfirmationFile(longMemPath, 1024)
	if err != nil {
		t.Fatal(err)
	}
	ablationFile, _, err := verifyConfirmationFile(ablationPath, 1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	launchFile, _, err := verifyImmutableConfirmationFile(launchPath, 1024)
	if err != nil {
		t.Fatal(err)
	}
	dataset, err := decodeConfirmationAblationDataset(ablationRaw)
	if err != nil {
		t.Fatal(err)
	}
	factory := &screenedConfirmationRuntimeFactory{
		profile: profile, sandbox: sandboxBackend, broker: broker,
		advertiseToolEndpoint: localConfirmationToolAdvertiser(t),
		launchManifest:        launchFile,
		longMemDataset:        longMemFile, ablationFile: ablationFile, ablationDataset: dataset,
		healthTimeout: 5 * time.Millisecond,
	}
	identity := confirmationRuntimeIdentity{
		BundleID: "bundle", TicketID: "ticket", AgentID: "agent", SlotID: "slot-0",
		InferenceSessionID: sessionID, Deadline: deadline, ArtifactSHA256: strings.Repeat("a", 64),
		ProfileChecksum: profile.Checksum, SettingsChecksum: strings.Repeat("d", 64), SettingsRevision: 1,
		Source: sandbox.Source{
			TarballURL: "https://platform.example/artifact", TarballSHA256: strings.Repeat("a", 64),
			ScreenedImageURL: "https://platform.example/image", ScreenedImageSHA256: strings.Repeat("b", 64),
			ScreenedImageID: "sha256:" + strings.Repeat("c", 64), ScreenedImageRef: "screened@example", ScreenedImageSize: 1024,
		},
	}
	return confirmationFactoryFixture{
		factory: factory, sandbox: sandboxBackend,
		identity:    identity,
		longMemPath: longMemPath, ablationPath: ablationPath, launchPath: launchPath,
		harness: harness, embedding: embedding, provider: provider,
	}
}

func TestConfirmationFactoryAcquireAndCloseOwnEveryResource(t *testing.T) {
	fixture := newConfirmationFactoryFixture(t, true)
	if err := fixture.factory.ValidateInstallation(fixture.factory.profile); err == nil ||
		!strings.Contains(err.Error(), "official cleaned condition") {
		t.Fatalf("production validation accepted synthetic dataset: %v", err)
	}
	if runtime, err := fixture.factory.Acquire(context.Background(), fixture.identity); err == nil || runtime != nil {
		t.Fatal("production acquisition accepted synthetic dataset")
	}
	if fixture.sandbox.builds != 0 {
		t.Fatal("production acquisition reached sandbox before official validation")
	}
	runtime, err := fixture.factory.acquireAfterInstallationValidation(context.Background(), fixture.identity)
	if err != nil {
		t.Fatal(err)
	}
	fixture.sandbox.mu.Lock()
	preflightHistory := append([]map[string]string(nil), fixture.sandbox.envHistory...)
	fixture.sandbox.mu.Unlock()
	assertDistinctSandboxCapabilities(t, preflightHistory, 1)
	fixture.factory.broker.sessions[fixture.identity.InferenceSessionID].mu.Lock()
	preflightRequired := fixture.factory.broker.sessions[fixture.identity.InferenceSessionID].sourceCapabilityRequired
	fixture.factory.broker.sessions[fixture.identity.InferenceSessionID].mu.Unlock()
	if preflightRequired {
		t.Fatal("health-only preflight capability was installed into broker")
	}
	longMemKey, selectionKey, projectionKey := runtime.LongMemProjectionKey, runtime.AblationSelectionKey, runtime.AblationProjectionKey
	if err := runtime.Close(); err != nil {
		t.Fatal(err)
	}
	if err := runtime.Close(); err != nil {
		t.Fatal(err)
	}
	fixture.sandbox.mu.Lock()
	builds, runs, stops, released := fixture.sandbox.builds, fixture.sandbox.runs, fixture.sandbox.stops, fixture.sandbox.released
	fixture.sandbox.mu.Unlock()
	if builds != 1 || runs != 1 || stops != 1 || released != 1 {
		t.Fatalf("lifecycle builds=%d runs=%d stops=%d releases=%d", builds, runs, stops, released)
	}
	if len(fixture.factory.broker.sessions) != 0 {
		t.Fatal("confirmation broker session survived close")
	}
	for _, key := range [][]byte{longMemKey, selectionKey, projectionKey} {
		for _, value := range key {
			if value != 0 {
				t.Fatal("runtime key survived close")
			}
		}
	}
}

func TestConfirmationProviderRuntimeAcceptsTicketScopedPlatformProxy(t *testing.T) {
	fixture := newConfirmationFactoryFixture(t, true)
	const proxyURL = "https://platform.test/api/v1/inference/confirmation/chat/completions"
	session := fixture.factory.broker.sessions[fixture.identity.InferenceSessionID]
	session.mu.Lock()
	for _, lane := range []string{longmemeval.ReaderLane, longmemeval.JudgeLane} {
		grant := session.confirmationGrants[lane]
		grant.ProxyURL = proxyURL
		session.confirmationGrants[lane] = grant
	}
	session.mu.Unlock()

	config, err := fixture.factory.broker.confirmationProviderRuntime(
		fixture.identity.InferenceSessionID,
		fixture.factory.profile,
	)
	if err != nil {
		t.Fatal(err)
	}
	providerSession, err := longmemeval.NewProviderSession(
		context.Background(),
		fixture.factory.profile.longMemProfile(),
		config,
	)
	if err != nil || providerSession == nil {
		t.Fatalf("ticket-scoped Platform runtime rejected: session=%#v err=%v", providerSession, err)
	}
	if err := providerSession.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestConfirmationFactoryFailsBeforeSpendAndCleansEveryPartialLifecycle(t *testing.T) {
	for name, test := range map[string]struct {
		configure func(*confirmationFactoryFixture)
		stage     string
	}{
		"sandbox unavailable": {
			configure: func(fixture *confirmationFactoryFixture) { fixture.sandbox.availableErr = fmt.Errorf("unavailable") },
			stage:     "sandbox_availability",
		},
		"build failure": {
			configure: func(fixture *confirmationFactoryFixture) { fixture.sandbox.buildErr = fmt.Errorf("build") },
			stage:     "screened_image",
		},
		"run failure": {
			configure: func(fixture *confirmationFactoryFixture) { fixture.sandbox.runErr = fmt.Errorf("run") },
			stage:     "sandbox_start",
		},
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newConfirmationFactoryFixture(t, true)
			test.configure(&fixture)
			runtime, err := fixture.factory.acquireAfterInstallationValidation(context.Background(), fixture.identity)
			if err == nil || runtime != nil {
				t.Fatal("partial lifecycle succeeded")
			}
			if got := confirmationExecutionStage(err); got != test.stage {
				t.Fatalf("failure stage = %q, want %q; err=%v", got, test.stage, err)
			}
			wantSessions := 1
			if name == "run failure" {
				wantSessions = 0
			}
			if len(fixture.factory.broker.sessions) != wantSessions {
				t.Fatalf("partial lifecycle retained unexpected broker session count: got %d want %d",
					len(fixture.factory.broker.sessions), wantSessions)
			}
			if name == "sandbox unavailable" && fixture.sandbox.builds != 0 {
				t.Fatal("sandbox availability failure reached build")
			}
			if name == "run failure" && fixture.sandbox.released != 1 {
				t.Fatalf("run failure releases = %d", fixture.sandbox.released)
			}
		})
	}
	t.Run("bind failure", func(t *testing.T) {
		fixture := newConfirmationFactoryFixture(t, true)
		fixture.sandbox.onRun = func() {
			fixture.factory.broker.mu.Lock()
			clear(fixture.factory.broker.sessions)
			fixture.factory.broker.mu.Unlock()
		}
		runtime, err := fixture.factory.acquireAfterInstallationValidation(context.Background(), fixture.identity)
		if err == nil || runtime != nil {
			t.Fatal("missing broker binding accepted")
		}
		if got := confirmationExecutionStage(err); got != "embedding_phase" {
			t.Fatalf("failure stage = %q, want embedding_phase; err=%v", got, err)
		}
		if fixture.sandbox.stops != 1 || fixture.sandbox.released != 1 {
			t.Fatal("bind failure cleanup incomplete")
		}
	})
	t.Run("health failure", func(t *testing.T) {
		fixture := newConfirmationFactoryFixture(t, false)
		runtime, err := fixture.factory.acquireAfterInstallationValidation(context.Background(), fixture.identity)
		if err == nil || runtime != nil {
			t.Fatal("unhealthy harness accepted")
		}
		if got := confirmationExecutionStage(err); got != "harness_health" {
			t.Fatalf("failure stage = %q, want harness_health; err=%v", got, err)
		}
		if fixture.sandbox.stops != 1 || fixture.sandbox.released != 1 || len(fixture.factory.broker.sessions) != 0 {
			t.Fatal("health failure cleanup incomplete")
		}
	})
}

func TestConfirmationFactoryRejectsFileAndProviderDriftBeforeSecrets(t *testing.T) {
	for name, mutate := range map[string]func(*testing.T, *confirmationFactoryFixture){
		"longmem dataset": func(t *testing.T, fixture *confirmationFactoryFixture) {
			t.Helper()
			if err := os.WriteFile(fixture.longMemPath, []byte(`[1]`), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"ablation dataset": func(t *testing.T, fixture *confirmationFactoryFixture) {
			t.Helper()
			if err := os.WriteFile(fixture.ablationPath, []byte(`{}`), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"launch manifest": func(t *testing.T, fixture *confirmationFactoryFixture) {
			t.Helper()
			if err := os.WriteFile(fixture.launchPath, []byte(`{"mode":"enforce"}`), 0o600); err != nil {
				t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newConfirmationFactoryFixture(t, true)
			mutate(t, &fixture)
			if err := fixture.factory.ValidateInstallation(fixture.factory.profile); err == nil {
				t.Fatal("installation drift accepted")
			}
			if fixture.sandbox.builds != 0 {
				t.Fatal("installation drift reached sandbox")
			}
		})
	}
}

func TestProductionFactoryRejectsNonProductionIsolationBeforeDataset(t *testing.T) {
	if factory, err := newScreenedConfirmationRuntimeFactory(screenedConfirmationRuntimeFactoryConfig{
		Sandbox: &fakeConfirmationSandbox{}, Broker: newInferenceBroker(1),
	}); err == nil || factory != nil {
		t.Fatal("non-LocalDocker confirmation sandbox accepted")
	}
	for name, mutate := range map[string]func(*sandbox.LocalDocker){
		"not hardened": func(runtime *sandbox.LocalDocker) { runtime.Harden = false },
		"not rootless": func(runtime *sandbox.LocalDocker) { runtime.RequireRootless = false },
		"no network":   func(runtime *sandbox.LocalDocker) { runtime.EgressNetwork = "" },
		"bad proxy":    func(runtime *sandbox.LocalDocker) { runtime.EgressProxy = "http://user:password@proxy.invalid" },
	} {
		t.Run(name, func(t *testing.T) {
			runtime := sandbox.NewLocalDocker()
			runtime.Harden, runtime.RequireRootless = true, true
			runtime.EgressNetwork, runtime.EgressProxy = "confirmation-egress", "http://proxy.invalid:8080"
			mutate(runtime)
			if factory, err := newScreenedConfirmationRuntimeFactory(screenedConfirmationRuntimeFactoryConfig{
				Sandbox: runtime, Broker: newInferenceBroker(1),
			}); err == nil || factory != nil {
				t.Fatal("incomplete production isolation accepted")
			}
		})
	}
	t.Run("managed isolated daemon", func(t *testing.T) {
		runtime := sandbox.NewLocalDocker()
		runtime.Harden = true
		runtime.RequireIsolatedDaemon = true
		runtime.EgressNetwork = "ditto-sandbox"
		if factory, err := newScreenedConfirmationRuntimeFactory(screenedConfirmationRuntimeFactoryConfig{
			Sandbox: runtime, Broker: newInferenceBroker(1), SandboxHealthTimeout: time.Second,
		}); err == nil || factory != nil || !strings.Contains(err.Error(), "manifest") {
			t.Fatalf("managed isolated daemon was not accepted through the isolation boundary: %v", err)
		}
	})
}

var _ sandbox.Sandbox = (*fakeConfirmationSandbox)(nil)
