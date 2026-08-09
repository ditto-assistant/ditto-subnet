package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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
)

type fakeConfirmationSandbox struct {
	mu           sync.Mutex
	harnessURL   string
	session      *brokerSession
	availableErr error
	buildErr     error
	runErr       error
	onRun        func()
	builds       int
	runs         int
	stops        int
	stopScoped   []bool
	released     int
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
func (fake *fakeConfirmationSandbox) Run(context.Context, string, map[string]string) (*sandbox.Handle, error) {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	fake.runs++
	if fake.runErr != nil {
		return nil, fake.runErr
	}
	if fake.onRun != nil {
		fake.onRun()
	}
	return &sandbox.Handle{
		ContainerID: fmt.Sprintf("container-%d", fake.runs), BaseURL: fake.harnessURL,
		SourceIP: "127.0.0.1", ImageRef: "screened-image",
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
		_ = json.NewEncoder(writer).Encode(embeddingResponse{
			Embeddings: [][]float64{make([]float64, embeddingDimensions)}, PromptEvalCount: 3,
		})
	}))
	defer embedding.Close()

	broker := newInferenceBroker(1, 1)
	broker.embeddingURL = embedding.URL + embeddingAPIPath
	sessionID, runID := "confirmation-runtime-test", "b881e493-7e8f-4a17-ab4e-24015fbb8c98"
	session := &brokerSession{
		id: sessionID, legacyGateway: provider.URL, expiresAt: time.Now().Add(time.Hour),
		expectedSourceIP: "127.0.0.1", provider: "test", model: model, requestModel: model,
		profileRevision: "test", boundRunID: runID, benchVersion: confirmationBenchVersion,
		confirmationSession: true, embeddingPhaseStarted: true, embeddingPhaseActive: true,
		embeddingConcurrency: 1, embeddingCalls: make(map[chan struct{}]context.CancelFunc),
		cancels: make(map[string]context.CancelFunc),
	}
	broker.sessions[sessionID] = session

	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/inference/{rest...}", broker.handle)
	mux.HandleFunc("POST /api/embed", broker.handleEmbedding)
	brokerServer := httptest.NewServer(mux)
	defer brokerServer.Close()
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
			embedResponse, err := http.Post(brokerServer.URL+"/api/embed", "application/json", strings.NewReader(
				`{"model":"embeddinggemma","input":["frozen selected content"]}`))
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
			chatResponse, err := http.Post(brokerServer.URL+"/v1/inference/chat/completions", "application/json", strings.NewReader(
				`{"model":"`+model+`","messages":[{"role":"user","content":"frozen selected content"}]}`))
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

	fake := &fakeConfirmationSandbox{harnessURL: harness.URL, session: session}
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
		current:       &sandbox.Handle{ContainerID: "stable-longmem", BaseURL: harness.URL, SourceIP: "127.0.0.1"},
		boundSourceIP: "127.0.0.1",
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
	if fake.runs != 3 || fake.stops != 4 || len(fake.stopScoped) != 4 ||
		fake.stopScoped[0] || !fake.stopScoped[1] || !fake.stopScoped[2] || !fake.stopScoped[3] {
		t.Fatalf("sandbox lifecycle runs=%d stops=%d scoped=%v", fake.runs, fake.stops, fake.stopScoped)
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
		ArtifactSHA256: strings.Repeat("a", 64), Deadline: time.Now().Add(time.Hour),
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

type fakeConfirmationSecretResolver struct{ value []byte }

func (resolver *fakeConfirmationSecretResolver) Resolve(
	context.Context,
	longmemeval.SecretManagerReference,
) ([]byte, error) {
	resolver.value = []byte("0123456789abcdef0123456789abcdef")
	return resolver.value, nil
}

func TestResolveConfirmationSecretCopiesAndZeroesResolverBuffer(t *testing.T) {
	resolver := &fakeConfirmationSecretResolver{}
	resolved, err := resolveConfirmationSecret(context.Background(), resolver, longmemeval.SecretManagerReference{
		ProjectID: "ditto-project", SecretID: "projection-key", Version: "1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if string(resolved) != "0123456789abcdef0123456789abcdef" {
		t.Fatal("resolved runtime copy changed")
	}
	for _, value := range resolver.value {
		if value != 0 {
			t.Fatal("resolver-owned secret buffer was not zeroed")
		}
	}
	longmemeval.ZeroSecretBytes(resolved)
}

func TestConfirmationActivationIsExplicitAndContentAddressed(t *testing.T) {
	getenv := func(string) string { return "" }
	if executor, err := confirmationExecutorFromEnvironment(getenv, nil, nil); err != nil || executor != nil {
		t.Fatalf("disabled installation = %v, %v", executor, err)
	}
	if executor, err := confirmationExecutorFromEnvironment(func(key string) string {
		if key == confirmationInstallationPathEnv {
			return "/immutable/config.json"
		}
		return ""
	}, nil, nil); err == nil || executor != nil {
		t.Fatal("partial confirmation opt-in was accepted")
	}
	directory := t.TempDir()
	path := filepath.Join(directory, "confirmation.json")
	raw := []byte(`{"schema_version":1,"execution_profile":{},"calibration_manifest_sha256":"` + strings.Repeat("a", 64) + `","calibration_manifest_path":"/calibration.json","longmem_dataset_path":"/dataset","ablation_dataset_path":"/ablation","longmem_projection_key_reference":{"project_id":"ditto-project","secret_id":"longmem-key","version":"1"},"ablation_selection_key_reference":{"project_id":"ditto-project","secret_id":"selection-key","version":"1"},"ablation_projection_key_reference":{"project_id":"ditto-project","secret_id":"projection-key","version":"1"},"provider_lanes":[],"sandbox_health_timeout_ms":1000}`)
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := readConfirmationActivationFile(path, strings.Repeat("b", 64)); err == nil {
		t.Fatal("wrong installation checksum accepted")
	}
	if installation, err := readConfirmationActivationFile(path, digestBytes(raw)); err != nil || installation.SchemaVersion != 1 {
		t.Fatalf("content-addressed installation = %+v, %v", installation, err)
	}
	hostile := bytes.Replace(raw, []byte(`"schema_version":1`), []byte(`"schema_version":1,"schema_version":1`), 1)
	if err := os.WriteFile(path, hostile, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := readConfirmationActivationFile(path, digestBytes(hostile)); err == nil {
		t.Fatal("duplicate installation identity accepted")
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
		t.Fatal("post-start calibration drift was not observable")
	}
}

type recordingConfirmationResolver struct {
	mu       sync.Mutex
	values   map[string][]byte
	failAt   int
	calls    int
	returned [][]byte
}

func (resolver *recordingConfirmationResolver) Resolve(
	_ context.Context,
	reference longmemeval.SecretManagerReference,
) ([]byte, error) {
	resolver.mu.Lock()
	defer resolver.mu.Unlock()
	resolver.calls++
	if resolver.failAt > 0 && resolver.calls == resolver.failAt {
		return nil, fmt.Errorf("secret unavailable")
	}
	value := append([]byte(nil), resolver.values[reference.SecretID]...)
	resolver.returned = append(resolver.returned, value)
	return value, nil
}

func (resolver *recordingConfirmationResolver) allReturnedZero() bool {
	resolver.mu.Lock()
	defer resolver.mu.Unlock()
	for _, value := range resolver.returned {
		for _, octet := range value {
			if octet != 0 {
				return false
			}
		}
	}
	return true
}

type noOpConfirmationAuthorizer struct{}

func (noOpConfirmationAuthorizer) Authorize(
	context.Context,
	longmemeval.SecretManagerReference,
	*http.Request,
) error {
	return nil
}

type confirmationFactoryFixture struct {
	factory         *screenedConfirmationRuntimeFactory
	sandbox         *fakeConfirmationSandbox
	resolver        *recordingConfirmationResolver
	identity        confirmationRuntimeIdentity
	longMemPath     string
	ablationPath    string
	calibrationPath string
	harness         *httptest.Server
	embedding       *httptest.Server
	provider        *httptest.Server
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
	calibrationRaw := []byte(`{"approved":true}`)
	calibrationPath := filepath.Join(directory, "calibration.json")
	if err := os.WriteFile(calibrationPath, calibrationRaw, 0o600); err != nil {
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
			Lane: longmemeval.JudgeLane, Provider: "openrouter",
			ProfileRevision: "longmemeval-official-gpt4o-openrouter-v1", Model: "openai/gpt-4o-2024-08-06",
			MaxRequests: 10, MaxPromptTokens: 100, MaxCompletionTokens: 100, MaxTotalTokens: 200, MaxCostUSDmicros: 10_000,
		},
		{
			Lane: longmemeval.ReaderLane, Provider: "openrouter", ProfileRevision: "fixture-reader-v1", Model: llm.HarnessModelForVersion(9),
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
		_ = json.NewEncoder(writer).Encode(embeddingResponse{Embeddings: [][]float64{make([]float64, embeddingDimensions)}})
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
	sandboxBackend := &fakeConfirmationSandbox{harnessURL: harness.URL}
	resolver := &recordingConfirmationResolver{values: map[string][]byte{
		"longmem-key": longMemKey, "selection-key": selectionKey, "projection-key": projectionKey,
	}}
	reference := func(secret string) longmemeval.SecretManagerReference {
		return longmemeval.SecretManagerReference{ProjectID: "ditto-project", SecretID: secret, Version: "1"}
	}
	providerReference := reference("provider-key")
	providerRuntime := longmemeval.ProviderRuntimeConfig{
		Authorizer: noOpConfirmationAuthorizer{},
		Lanes: []longmemeval.ProviderLaneRuntimeConfig{
			{
				Lane: longmemeval.JudgeLane, UpstreamURL: provider.URL + "/api/v1/chat/completions",
				RouteProvider: "openai", ReceiptProvider: "OpenAI", CredentialReference: providerReference, RequestTimeout: time.Second,
			},
			{
				Lane: longmemeval.ReaderLane, UpstreamURL: provider.URL + "/api/v1/chat/completions",
				RouteProvider: "test", ReceiptProvider: "Test", CredentialReference: providerReference, RequestTimeout: time.Second,
			},
		},
	}
	longMemFile, _, err := verifyConfirmationFile(longMemPath, 1024)
	if err != nil {
		t.Fatal(err)
	}
	ablationFile, _, err := verifyConfirmationFile(ablationPath, 1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	calibrationFile, _, err := verifyImmutableConfirmationFile(calibrationPath, 1024)
	if err != nil {
		t.Fatal(err)
	}
	dataset, err := decodeConfirmationAblationDataset(ablationRaw)
	if err != nil {
		t.Fatal(err)
	}
	factory := &screenedConfirmationRuntimeFactory{
		profile: profile, sandbox: sandboxBackend, broker: broker, calibrationManifest: calibrationFile,
		longMemDataset: longMemFile, longMemProjectionKeyReference: reference("longmem-key"),
		ablationFile: ablationFile, ablationDataset: dataset,
		ablationSelectionKeyReference: reference("selection-key"), ablationProjectionKeyReference: reference("projection-key"),
		secretResolver: resolver, providerRuntime: providerRuntime, healthTimeout: 5 * time.Millisecond,
	}
	return confirmationFactoryFixture{
		factory: factory, sandbox: sandboxBackend, resolver: resolver,
		identity: confirmationRuntimeIdentity{
			BundleID: "bundle", TicketID: "ticket", AgentID: "agent", SlotID: "slot-0", Deadline: time.Now().Add(time.Hour),
			ArtifactSHA256: strings.Repeat("a", 64),
			Source: sandbox.Source{
				TarballURL: "https://platform.example/artifact", TarballSHA256: strings.Repeat("a", 64),
				ScreenedImageURL: "https://platform.example/image", ScreenedImageSHA256: strings.Repeat("b", 64),
				ScreenedImageID: "sha256:" + strings.Repeat("c", 64), ScreenedImageRef: "screened@example", ScreenedImageSize: 1024,
			},
		},
		longMemPath: longMemPath, ablationPath: ablationPath, calibrationPath: calibrationPath,
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
	if fixture.resolver.calls != 0 || fixture.sandbox.builds != 0 {
		t.Fatal("production acquisition reached secrets or sandbox before official validation")
	}
	runtime, err := fixture.factory.acquireAfterInstallationValidation(context.Background(), fixture.identity)
	if err != nil {
		t.Fatal(err)
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
	if !fixture.resolver.allReturnedZero() {
		t.Fatal("resolver buffers survived acquisition")
	}
}

func TestConfirmationFactoryFailsBeforeSpendAndCleansEveryPartialLifecycle(t *testing.T) {
	t.Run("wrong key digest", func(t *testing.T) {
		fixture := newConfirmationFactoryFixture(t, true)
		fixture.resolver.values["longmem-key"] = bytes.Repeat([]byte{0x99}, 32)
		if runtime, err := fixture.factory.acquireAfterInstallationValidation(context.Background(), fixture.identity); err == nil || runtime != nil {
			t.Fatal("wrong projection key accepted")
		}
		if fixture.sandbox.builds != 0 || fixture.sandbox.runs != 0 || !fixture.resolver.allReturnedZero() {
			t.Fatal("key mismatch spent sandbox work or retained key bytes")
		}
	})
	t.Run("secret unavailable", func(t *testing.T) {
		fixture := newConfirmationFactoryFixture(t, true)
		fixture.resolver.failAt = 2
		if runtime, err := fixture.factory.acquireAfterInstallationValidation(context.Background(), fixture.identity); err == nil || runtime != nil {
			t.Fatal("missing secret accepted")
		}
		if fixture.sandbox.builds != 0 || !fixture.resolver.allReturnedZero() {
			t.Fatal("secret failure spent sandbox work or retained key bytes")
		}
	})
	for name, configure := range map[string]func(*confirmationFactoryFixture){
		"sandbox unavailable": func(fixture *confirmationFactoryFixture) { fixture.sandbox.availableErr = fmt.Errorf("unavailable") },
		"build failure":       func(fixture *confirmationFactoryFixture) { fixture.sandbox.buildErr = fmt.Errorf("build") },
		"run failure":         func(fixture *confirmationFactoryFixture) { fixture.sandbox.runErr = fmt.Errorf("run") },
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newConfirmationFactoryFixture(t, true)
			configure(&fixture)
			if runtime, err := fixture.factory.acquireAfterInstallationValidation(context.Background(), fixture.identity); err == nil || runtime != nil {
				t.Fatal("partial lifecycle succeeded")
			}
			if !fixture.resolver.allReturnedZero() || len(fixture.factory.broker.sessions) != 0 {
				t.Fatal("partial lifecycle retained keys or broker session")
			}
			if name == "sandbox unavailable" && (fixture.resolver.calls != 0 || fixture.sandbox.builds != 0) {
				t.Fatal("sandbox availability failure reached Secret Manager or build")
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
		if runtime, err := fixture.factory.acquireAfterInstallationValidation(context.Background(), fixture.identity); err == nil || runtime != nil {
			t.Fatal("missing broker binding accepted")
		}
		if fixture.sandbox.stops != 1 || fixture.sandbox.released != 1 || !fixture.resolver.allReturnedZero() {
			t.Fatal("bind failure cleanup incomplete")
		}
	})
	t.Run("health failure", func(t *testing.T) {
		fixture := newConfirmationFactoryFixture(t, false)
		if runtime, err := fixture.factory.acquireAfterInstallationValidation(context.Background(), fixture.identity); err == nil || runtime != nil {
			t.Fatal("unhealthy harness accepted")
		}
		if fixture.sandbox.stops != 1 || fixture.sandbox.released != 1 || len(fixture.factory.broker.sessions) != 0 ||
			!fixture.resolver.allReturnedZero() {
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
		"calibration manifest": func(t *testing.T, fixture *confirmationFactoryFixture) {
			t.Helper()
			if err := os.WriteFile(fixture.calibrationPath, []byte(`{"approved":false}`), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"provider runtime": func(_ *testing.T, fixture *confirmationFactoryFixture) {
			fixture.factory.providerRuntime.Lanes[0].RouteProvider = "wrong"
		},
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newConfirmationFactoryFixture(t, true)
			mutate(t, &fixture)
			if err := fixture.factory.ValidateInstallation(fixture.factory.profile); err == nil {
				t.Fatal("installation drift accepted")
			}
			if fixture.resolver.calls != 0 || fixture.sandbox.builds != 0 {
				t.Fatal("installation drift reached secrets or sandbox")
			}
		})
	}
}

func TestProductionFactoryRejectsNonProductionIsolationBeforeDatasetOrSecrets(t *testing.T) {
	resolver := &recordingConfirmationResolver{}
	if factory, err := newScreenedConfirmationRuntimeFactory(screenedConfirmationRuntimeFactoryConfig{
		Sandbox: &fakeConfirmationSandbox{}, Broker: newInferenceBroker(1), SecretResolver: resolver,
	}); err == nil || factory != nil {
		t.Fatal("non-LocalDocker confirmation sandbox accepted")
	}
	if resolver.calls != 0 {
		t.Fatal("isolation rejection reached Secret Manager")
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
				Sandbox: runtime, Broker: newInferenceBroker(1), SecretResolver: resolver,
			}); err == nil || factory != nil {
				t.Fatal("incomplete production isolation accepted")
			}
		})
	}
}

var _ sandbox.Sandbox = (*fakeConfirmationSandbox)(nil)
