package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	testConfirmationProfileRevision  = "v9-confirmation-profile-2026-08-08"
	testConfirmationProfileChecksum  = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
	testConfirmationSettingsChecksum = "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
	testConfirmationArtifactChecksum = "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"
	testConfirmationScreenedChecksum = "d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4"
	testConfirmationEvidenceChecksum = "e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5"
)

type recordingConfirmationExecutor struct {
	mu        sync.Mutex
	readiness confirmationReadiness
	execute   func(context.Context, confirmationExecutionRequest) (confirmationExecutionResult, error)
	calls     int
	requests  []confirmationExecutionRequest
}

func (executor *recordingConfirmationExecutor) Readiness() confirmationReadiness {
	return executor.readiness
}

func (executor *recordingConfirmationExecutor) Execute(
	ctx context.Context,
	request confirmationExecutionRequest,
) (confirmationExecutionResult, error) {
	executor.mu.Lock()
	executor.calls++
	executor.requests = append(executor.requests, request)
	execute := executor.execute
	executor.mu.Unlock()
	if execute == nil {
		return validConfirmationResult(), nil
	}
	return execute(ctx, request)
}

func (executor *recordingConfirmationExecutor) callCount() int {
	executor.mu.Lock()
	defer executor.mu.Unlock()
	return executor.calls
}

func (executor *recordingConfirmationExecutor) onlyRequest(t *testing.T) confirmationExecutionRequest {
	t.Helper()
	executor.mu.Lock()
	defer executor.mu.Unlock()
	if len(executor.requests) != 1 {
		t.Fatalf("executor requests = %d, want 1", len(executor.requests))
	}
	return executor.requests[0]
}

func readyConfirmationExecutor() *recordingConfirmationExecutor {
	return &recordingConfirmationExecutor{
		readiness: confirmationReadiness{
			Ready:           true,
			ProfileRevision: testConfirmationProfileRevision,
			ProfileChecksum: testConfirmationProfileChecksum,
		},
	}
}

func validConfirmationRequest() confirmationExecutionRequest {
	return confirmationExecutionRequest{
		Purpose:                confirmationPurpose,
		BundleID:               "bundle-00000001",
		TicketID:               "ticket-00000001",
		AgentID:                "agent-00000001",
		SlotID:                 "slot-0",
		Mode:                   "shadow",
		ArtifactURL:            "https://artifacts.invalid/v9/agent.tar.gz",
		ArtifactSHA256:         testConfirmationArtifactChecksum,
		ScreenedImageURL:       "https://artifacts.invalid/v9/screened-image.tar",
		ScreenedImageSHA256:    testConfirmationScreenedChecksum,
		ScreenedImageSizeBytes: 4096,
		ScreenedImageID:        "sha256:screened-image",
		ScreenedImageRef:       "registry.invalid/screened@sha256:abcd",
		BenchVersion:           confirmationBenchVersion,
		Deadline:               time.Now().Add(time.Hour).UTC().Round(time.Microsecond),
		ProfileRevision:        testConfirmationProfileRevision,
		ProfileChecksum:        testConfirmationProfileChecksum,
		SettingsRevision:       7,
		SettingsChecksum:       testConfirmationSettingsChecksum,
		RetestGeneration:       2,
		PerBundleRequestCap:    37,
		PerBundleTokenCap:      131_072,
		ExecutionProfile:       json.RawMessage(`{"longmemeval":{"model":"openai/gpt-oss-20b"},"ablations":["inference","embedding"]}`),
	}
}

func validConfirmationResult() confirmationExecutionResult {
	return confirmationExecutionResult{
		LongMemEval:                  json.RawMessage(`{"score":0.81,"requests":9,"tokens":1234}`),
		InferenceAblation:            json.RawMessage(`{"intervention":"inference-disabled","score":0.42,"observed":true}`),
		EmbeddingAblation:            json.RawMessage(`{"intervention":"embedding-disabled","score":0.57,"observed":true}`),
		AblationCoordinatorLatencyMS: 321,
		EvidenceSHA256:               testConfirmationEvidenceChecksum,
	}
}

func confirmationRequestBody(t *testing.T, request confirmationExecutionRequest) string {
	t.Helper()
	body, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	return string(body)
}

func executeConfirmationRequest(
	t *testing.T,
	s *server,
	ctx context.Context,
	body string,
) *httptest.ResponseRecorder {
	t.Helper()
	if ctx == nil {
		ctx = context.Background()
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/confirmation/execute", strings.NewReader(body)).WithContext(ctx)
	recorder := httptest.NewRecorder()
	s.handleConfirmationExecute(recorder, req)
	return recorder
}

func assertConfirmationError(t *testing.T, recorder *httptest.ResponseRecorder, status int, message string) {
	t.Helper()
	if recorder.Code != status {
		t.Fatalf("status = %d, want %d; body=%s", recorder.Code, status, recorder.Body.String())
	}
	var body map[string]string
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode error response: %v; body=%s", err, recorder.Body.String())
	}
	if body["error"] != message {
		t.Fatalf("error = %q, want %q", body["error"], message)
	}
	if got := recorder.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("Cache-Control = %q, want no-store", got)
	}
}

func TestConfirmationReadinessFailsClosedWithoutExecutor(t *testing.T) {
	recorder := httptest.NewRecorder()
	(&server{}).handleConfirmationReadiness(
		recorder,
		httptest.NewRequest(http.MethodGet, "/v1/confirmation/readiness", nil),
	)
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want %d; body=%s", recorder.Code, http.StatusServiceUnavailable, recorder.Body.String())
	}
	if got := strings.TrimSpace(recorder.Body.String()); got != `{"ready":false}` {
		t.Fatalf("body = %s, want readiness=false without profile metadata", got)
	}
	if got := recorder.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("Cache-Control = %q, want no-store", got)
	}
}

func TestConfirmationReadinessRequiresExactFrozenIdentity(t *testing.T) {
	cases := []struct {
		name      string
		readiness confirmationReadiness
		want      int
	}{
		{
			name: "exact ready profile",
			readiness: confirmationReadiness{
				Ready:           true,
				ProfileRevision: testConfirmationProfileRevision,
				ProfileChecksum: testConfirmationProfileChecksum,
			},
			want: http.StatusOK,
		},
		{
			name: "executor not ready",
			readiness: confirmationReadiness{
				Ready:           false,
				ProfileRevision: testConfirmationProfileRevision,
				ProfileChecksum: testConfirmationProfileChecksum,
			},
			want: http.StatusServiceUnavailable,
		},
		{
			name: "blank revision",
			readiness: confirmationReadiness{
				Ready:           true,
				ProfileRevision: "  ",
				ProfileChecksum: testConfirmationProfileChecksum,
			},
			want: http.StatusServiceUnavailable,
		},
		{
			name: "short checksum",
			readiness: confirmationReadiness{
				Ready:           true,
				ProfileRevision: testConfirmationProfileRevision,
				ProfileChecksum: "deadbeef",
			},
			want: http.StatusServiceUnavailable,
		},
		{
			name: "uppercase checksum",
			readiness: confirmationReadiness{
				Ready:           true,
				ProfileRevision: testConfirmationProfileRevision,
				ProfileChecksum: strings.ToUpper(testConfirmationProfileChecksum),
			},
			want: http.StatusServiceUnavailable,
		},
		{
			name: "non hexadecimal checksum",
			readiness: confirmationReadiness{
				Ready:           true,
				ProfileRevision: testConfirmationProfileRevision,
				ProfileChecksum: strings.Repeat("g", 64),
			},
			want: http.StatusServiceUnavailable,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			(&server{confirmation: &recordingConfirmationExecutor{readiness: tc.readiness}}).
				handleConfirmationReadiness(
					recorder,
					httptest.NewRequest(http.MethodGet, "/v1/confirmation/readiness", nil),
				)
			if recorder.Code != tc.want {
				t.Fatalf("status = %d, want %d; body=%s", recorder.Code, tc.want, recorder.Body.String())
			}
			if got := recorder.Header().Get("Cache-Control"); got != "no-store" {
				t.Fatalf("Cache-Control = %q, want no-store", got)
			}
			var got confirmationReadiness
			if err := json.Unmarshal(recorder.Body.Bytes(), &got); err != nil {
				t.Fatal(err)
			}
			if tc.want == http.StatusOK {
				if !reflect.DeepEqual(got, tc.readiness) {
					t.Fatalf("readiness = %+v, want %+v", got, tc.readiness)
				}
			} else if got.Ready || got.ProfileRevision != "" || got.ProfileChecksum != "" {
				t.Fatalf("unready response leaked profile identity: %+v", got)
			}
		})
	}
}

func TestConfirmationExecuteFailsClosedWithoutExecutor(t *testing.T) {
	recorder := executeConfirmationRequest(t, &server{}, nil, confirmationRequestBody(t, validConfirmationRequest()))
	assertConfirmationError(
		t,
		recorder,
		http.StatusServiceUnavailable,
		"v9 confirmation executor is unconfigured",
	)
}

func TestConfirmationExecuteRejectsInvalidFrozenContractBeforeExecution(t *testing.T) {
	testCases := []struct {
		name    string
		mutate  func(*confirmationExecutionRequest)
		message string
	}{
		{
			name: "wrong purpose",
			mutate: func(request *confirmationExecutionRequest) {
				request.Purpose = "canonical_quorum"
			},
			message: "confirmation execution requires the internal bench v9 purpose",
		},
		{
			name: "empty purpose",
			mutate: func(request *confirmationExecutionRequest) {
				request.Purpose = ""
			},
			message: "confirmation execution requires the internal bench v9 purpose",
		},
		{
			name: "v8 version",
			mutate: func(request *confirmationExecutionRequest) {
				request.BenchVersion = 8
			},
			message: "confirmation execution requires the internal bench v9 purpose",
		},
		{
			name: "future version",
			mutate: func(request *confirmationExecutionRequest) {
				request.BenchVersion = 10
			},
			message: "confirmation execution requires the internal bench v9 purpose",
		},
		{
			name: "missing bundle identity",
			mutate: func(request *confirmationExecutionRequest) {
				request.BundleID = " "
			},
			message: "confirmation bundle, ticket, agent, and slot identities are required",
		},
		{
			name: "missing ticket identity",
			mutate: func(request *confirmationExecutionRequest) {
				request.TicketID = ""
			},
			message: "confirmation bundle, ticket, agent, and slot identities are required",
		},
		{
			name: "zero deadline",
			mutate: func(request *confirmationExecutionRequest) {
				request.Deadline = time.Time{}
			},
			message: "confirmation ticket deadline is not live",
		},
		{
			name: "expired deadline",
			mutate: func(request *confirmationExecutionRequest) {
				request.Deadline = time.Now().Add(-time.Second)
			},
			message: "confirmation ticket deadline is not live",
		},
		{
			name: "zero request cap",
			mutate: func(request *confirmationExecutionRequest) {
				request.PerBundleRequestCap = 0
			},
			message: "confirmation execution caps are out of range",
		},
		{
			name: "zero token cap",
			mutate: func(request *confirmationExecutionRequest) {
				request.PerBundleTokenCap = 0
			},
			message: "confirmation execution caps are out of range",
		},
		{
			name: "request cap above platform maximum",
			mutate: func(request *confirmationExecutionRequest) {
				request.PerBundleRequestCap = confirmationMaxRequests + 1
			},
			message: "confirmation execution caps are out of range",
		},
		{
			name: "token cap above platform maximum",
			mutate: func(request *confirmationExecutionRequest) {
				request.PerBundleTokenCap = confirmationMaxTokens + 1
			},
			message: "confirmation execution caps are out of range",
		},
		{
			name: "off mode",
			mutate: func(request *confirmationExecutionRequest) {
				request.Mode = "off"
			},
			message: "confirmation mode must authorize execution",
		},
		{
			name: "missing agent identity",
			mutate: func(request *confirmationExecutionRequest) {
				request.AgentID = ""
			},
			message: "confirmation bundle, ticket, agent, and slot identities are required",
		},
		{
			name: "invalid slot identity",
			mutate: func(request *confirmationExecutionRequest) {
				request.SlotID = "worker-0"
			},
			message: "confirmation bundle, ticket, agent, and slot identities are required",
		},
		{
			name: "zero settings revision",
			mutate: func(request *confirmationExecutionRequest) {
				request.SettingsRevision = 0
			},
			message: "confirmation frozen identity is malformed",
		},
		{
			name: "negative settings revision",
			mutate: func(request *confirmationExecutionRequest) {
				request.SettingsRevision = -1
			},
			message: "confirmation frozen identity is malformed",
		},
		{
			name: "negative retest generation",
			mutate: func(request *confirmationExecutionRequest) {
				request.RetestGeneration = -1
			},
			message: "confirmation frozen identity is malformed",
		},
		{
			name: "malformed settings checksum",
			mutate: func(request *confirmationExecutionRequest) {
				request.SettingsChecksum = "not-a-checksum"
			},
			message: "confirmation frozen identity is malformed",
		},
		{
			name: "uppercase settings checksum",
			mutate: func(request *confirmationExecutionRequest) {
				request.SettingsChecksum = strings.ToUpper(testConfirmationSettingsChecksum)
			},
			message: "confirmation frozen identity is malformed",
		},
		{
			name: "malformed artifact checksum",
			mutate: func(request *confirmationExecutionRequest) {
				request.ArtifactSHA256 = strings.Repeat("z", 64)
			},
			message: "confirmation frozen identity is malformed",
		},
		{
			name: "uppercase artifact checksum",
			mutate: func(request *confirmationExecutionRequest) {
				request.ArtifactSHA256 = strings.ToUpper(testConfirmationArtifactChecksum)
			},
			message: "confirmation frozen identity is malformed",
		},
		{
			name: "wrong profile revision",
			mutate: func(request *confirmationExecutionRequest) {
				request.ProfileRevision = testConfirmationProfileRevision + "-drift"
			},
			message: "confirmation execution profile is not exactly ready",
		},
		{
			name: "wrong profile checksum",
			mutate: func(request *confirmationExecutionRequest) {
				request.ProfileChecksum = strings.Repeat("5", 64)
			},
			message: "confirmation execution profile is not exactly ready",
		},
		{
			name: "uppercase profile checksum",
			mutate: func(request *confirmationExecutionRequest) {
				request.ProfileChecksum = strings.ToUpper(testConfirmationProfileChecksum)
			},
			message: "confirmation execution profile is not exactly ready",
		},
		{
			name:    "unready executor",
			mutate:  func(request *confirmationExecutionRequest) {},
			message: "confirmation execution profile is not exactly ready",
		},
		{
			name: "missing execution profile",
			mutate: func(request *confirmationExecutionRequest) {
				request.ExecutionProfile = nil
			},
			message: "confirmation execution profile is missing",
		},
		{
			name: "null execution profile",
			mutate: func(request *confirmationExecutionRequest) {
				request.ExecutionProfile = json.RawMessage("null")
			},
			message: "confirmation execution profile is missing",
		},
		{
			name: "missing artifact URL",
			mutate: func(request *confirmationExecutionRequest) {
				request.ArtifactURL = " "
			},
			message: "confirmation artifact URL is missing",
		},
		{
			name: "missing screened image URL",
			mutate: func(request *confirmationExecutionRequest) {
				request.ScreenedImageURL = ""
			},
			message: "confirmation screened image identity is incomplete",
		},
		{
			name: "malformed screened image checksum",
			mutate: func(request *confirmationExecutionRequest) {
				request.ScreenedImageSHA256 = "not-a-digest"
			},
			message: "confirmation screened image identity is incomplete",
		},
		{
			name: "zero screened image size",
			mutate: func(request *confirmationExecutionRequest) {
				request.ScreenedImageSizeBytes = 0
			},
			message: "confirmation screened image identity is incomplete",
		},
		{
			name: "missing screened image id",
			mutate: func(request *confirmationExecutionRequest) {
				request.ScreenedImageID = ""
			},
			message: "confirmation screened image identity is incomplete",
		},
		{
			name: "missing screened image ref",
			mutate: func(request *confirmationExecutionRequest) {
				request.ScreenedImageRef = ""
			},
			message: "confirmation screened image identity is incomplete",
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			executor := readyConfirmationExecutor()
			if tc.name == "unready executor" {
				executor.readiness.Ready = false
			}
			request := validConfirmationRequest()
			tc.mutate(&request)
			recorder := executeConfirmationRequest(
				t,
				&server{confirmation: executor},
				nil,
				confirmationRequestBody(t, request),
			)
			assertConfirmationError(t, recorder, http.StatusConflict, tc.message)
			if got := executor.callCount(); got != 0 {
				t.Fatalf("executor called %d times for rejected request", got)
			}
		})
	}
}

func TestConfirmationExecuteRejectsMalformedWireBodiesBeforeExecution(t *testing.T) {
	validBody := confirmationRequestBody(t, validConfirmationRequest())
	var withUnknown map[string]any
	if err := json.Unmarshal([]byte(validBody), &withUnknown); err != nil {
		t.Fatal(err)
	}
	withUnknown["miner_supplied_override"] = true
	unknownBody, err := json.Marshal(withUnknown)
	if err != nil {
		t.Fatal(err)
	}

	testCases := []struct {
		name string
		body string
	}{
		{name: "empty", body: ""},
		{name: "malformed", body: `{"purpose":`},
		{name: "unknown field", body: string(unknownBody)},
		{name: "trailing object", body: validBody + ` {}`},
		{name: "trailing scalar", body: validBody + ` true`},
		{
			name: "larger than one mebibyte",
			body: `{"miner_supplied_override":"` + strings.Repeat("a", confirmationBodyLimit) + `"}`,
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			executor := readyConfirmationExecutor()
			recorder := executeConfirmationRequest(t, &server{confirmation: executor}, nil, tc.body)
			assertConfirmationError(t, recorder, http.StatusBadRequest, "invalid confirmation request")
			if got := executor.callCount(); got != 0 {
				t.Fatalf("executor called %d times for malformed body", got)
			}
		})
	}
}

func TestConfirmationExecutePropagatesTicketDeadlineToExecutor(t *testing.T) {
	deadline := time.Now().Add(time.Hour).UTC().Round(time.Millisecond)
	executor := readyConfirmationExecutor()
	executor.execute = func(ctx context.Context, _ confirmationExecutionRequest) (confirmationExecutionResult, error) {
		got, ok := ctx.Deadline()
		if !ok {
			t.Fatal("executor context has no deadline")
		}
		if !got.Equal(deadline) {
			t.Fatalf("executor deadline = %s, want %s", got, deadline)
		}
		return validConfirmationResult(), nil
	}
	request := validConfirmationRequest()
	request.Deadline = deadline
	recorder := executeConfirmationRequest(
		t,
		&server{confirmation: executor},
		nil,
		confirmationRequestBody(t, request),
	)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestConfirmationExecuteHonorsRequestCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	observed := make(chan error, 1)
	executor := readyConfirmationExecutor()
	executor.execute = func(ctx context.Context, _ confirmationExecutionRequest) (confirmationExecutionResult, error) {
		observed <- ctx.Err()
		return confirmationExecutionResult{}, ctx.Err()
	}
	recorder := executeConfirmationRequest(
		t,
		&server{confirmation: executor},
		ctx,
		confirmationRequestBody(t, validConfirmationRequest()),
	)
	assertConfirmationError(t, recorder, http.StatusUnprocessableEntity, "confirmation execution failed")
	if got := <-observed; !errors.Is(got, context.Canceled) {
		t.Fatalf("executor context error = %v, want context.Canceled", got)
	}
}

func TestConfirmationExecuteEnforcesTicketDeadlineDuringExecution(t *testing.T) {
	observed := make(chan error, 1)
	executor := readyConfirmationExecutor()
	executor.execute = func(ctx context.Context, _ confirmationExecutionRequest) (confirmationExecutionResult, error) {
		<-ctx.Done()
		observed <- ctx.Err()
		return confirmationExecutionResult{}, ctx.Err()
	}
	request := validConfirmationRequest()
	request.Deadline = time.Now().Add(20 * time.Millisecond)
	recorder := executeConfirmationRequest(
		t,
		&server{confirmation: executor},
		nil,
		confirmationRequestBody(t, request),
	)
	assertConfirmationError(t, recorder, http.StatusUnprocessableEntity, "confirmation execution failed")
	if got := <-observed; !errors.Is(got, context.DeadlineExceeded) {
		t.Fatalf("executor context error = %v, want context.DeadlineExceeded", got)
	}
}

func TestConfirmationExecuteMasksExecutorErrors(t *testing.T) {
	executor := readyConfirmationExecutor()
	executor.execute = func(context.Context, confirmationExecutionRequest) (confirmationExecutionResult, error) {
		return confirmationExecutionResult{}, errors.New("provider response included internal credential material")
	}
	recorder := executeConfirmationRequest(
		t,
		&server{confirmation: executor},
		nil,
		confirmationRequestBody(t, validConfirmationRequest()),
	)
	assertConfirmationError(t, recorder, http.StatusUnprocessableEntity, "confirmation execution failed")
	if strings.Contains(recorder.Body.String(), "credential material") {
		t.Fatalf("executor error leaked through response: %s", recorder.Body.String())
	}
}

func TestConfirmationExecuteRejectsMalformedEvidence(t *testing.T) {
	testCases := []struct {
		name    string
		mutate  func(*confirmationExecutionResult)
		message string
	}{
		{
			name: "missing LongMemEval",
			mutate: func(result *confirmationExecutionResult) {
				result.LongMemEval = nil
			},
			message: "confirmation longmemeval evidence is invalid",
		},
		{
			name: "null LongMemEval",
			mutate: func(result *confirmationExecutionResult) {
				result.LongMemEval = json.RawMessage("null")
			},
			message: "confirmation longmemeval evidence is invalid",
		},
		{
			name: "invalid LongMemEval JSON",
			mutate: func(result *confirmationExecutionResult) {
				result.LongMemEval = json.RawMessage(`{"score":`)
			},
			message: "confirmation longmemeval evidence is invalid",
		},
		{
			name: "missing inference ablation",
			mutate: func(result *confirmationExecutionResult) {
				result.InferenceAblation = nil
			},
			message: "confirmation inference_ablation evidence is invalid",
		},
		{
			name: "null inference ablation",
			mutate: func(result *confirmationExecutionResult) {
				result.InferenceAblation = json.RawMessage("null")
			},
			message: "confirmation inference_ablation evidence is invalid",
		},
		{
			name: "invalid inference ablation JSON",
			mutate: func(result *confirmationExecutionResult) {
				result.InferenceAblation = json.RawMessage("{")
			},
			message: "confirmation inference_ablation evidence is invalid",
		},
		{
			name: "missing embedding ablation",
			mutate: func(result *confirmationExecutionResult) {
				result.EmbeddingAblation = nil
			},
			message: "confirmation embedding_ablation evidence is invalid",
		},
		{
			name: "null embedding ablation",
			mutate: func(result *confirmationExecutionResult) {
				result.EmbeddingAblation = json.RawMessage("null")
			},
			message: "confirmation embedding_ablation evidence is invalid",
		},
		{
			name: "invalid embedding ablation JSON",
			mutate: func(result *confirmationExecutionResult) {
				result.EmbeddingAblation = json.RawMessage("[")
			},
			message: "confirmation embedding_ablation evidence is invalid",
		},
		{
			name: "missing coordinator latency",
			mutate: func(result *confirmationExecutionResult) {
				result.AblationCoordinatorLatencyMS = 0
			},
			message: "confirmation coordinator latency is missing",
		},
		{
			name: "invalid native wire digest",
			mutate: func(result *confirmationExecutionResult) {
				result.EvidenceSHA256 = "deadbeef"
			},
			message: "confirmation native wire digest is invalid",
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			executor := readyConfirmationExecutor()
			executor.execute = func(context.Context, confirmationExecutionRequest) (confirmationExecutionResult, error) {
				result := validConfirmationResult()
				tc.mutate(&result)
				return result, nil
			}
			recorder := executeConfirmationRequest(
				t,
				&server{confirmation: executor},
				nil,
				confirmationRequestBody(t, validConfirmationRequest()),
			)
			assertConfirmationError(t, recorder, http.StatusUnprocessableEntity, tc.message)
			if got := executor.callCount(); got != 1 {
				t.Fatalf("executor calls = %d, want 1", got)
			}
		})
	}
}

func TestConfirmationExecuteReturnsCompleteValidatedEvidence(t *testing.T) {
	executor := readyConfirmationExecutor()
	wantRequest := validConfirmationRequest()
	wantResult := validConfirmationResult()
	recorder := executeConfirmationRequest(
		t,
		&server{confirmation: executor},
		nil,
		confirmationRequestBody(t, wantRequest),
	)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", recorder.Code, http.StatusOK, recorder.Body.String())
	}
	if got := recorder.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("Cache-Control = %q, want no-store", got)
	}
	if got := recorder.Header().Get("Content-Type"); got != "application/json" {
		t.Fatalf("Content-Type = %q, want application/json", got)
	}
	var gotResult confirmationExecutionResult
	if err := json.Unmarshal(recorder.Body.Bytes(), &gotResult); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(gotResult, wantResult) {
		t.Fatalf("result = %+v, want %+v", gotResult, wantResult)
	}
	if gotRequest := executor.onlyRequest(t); !reflect.DeepEqual(gotRequest, wantRequest) {
		t.Fatalf("executor request = %+v, want %+v", gotRequest, wantRequest)
	}
}

func TestConfirmationRoutesRequireControlCredential(t *testing.T) {
	executor := readyConfirmationExecutor()
	s := &server{
		broker:       newInferenceBroker(1, 1),
		confirmation: executor,
	}
	handler := testControlAuth(controlAuthEnforce, testControlToken).wrap(s.newControlPlaneMux())

	requests := []struct {
		name   string
		method string
		path   string
		body   string
	}{
		{
			name:   "readiness",
			method: http.MethodGet,
			path:   "/v1/confirmation/readiness",
		},
		{
			name:   "execute",
			method: http.MethodPost,
			path:   "/v1/confirmation/execute",
			body:   confirmationRequestBody(t, validConfirmationRequest()),
		},
	}

	for _, tc := range requests {
		t.Run(tc.name, func(t *testing.T) {
			unauthorized := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
			unauthorizedRecorder := httptest.NewRecorder()
			handler.ServeHTTP(unauthorizedRecorder, unauthorized)
			if unauthorizedRecorder.Code != http.StatusUnauthorized {
				t.Fatalf("missing credential status = %d, want %d", unauthorizedRecorder.Code, http.StatusUnauthorized)
			}

			authorized := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
			authorized.Header.Set("Authorization", "Bearer "+testControlToken)
			authorizedRecorder := httptest.NewRecorder()
			handler.ServeHTTP(authorizedRecorder, authorized)
			if authorizedRecorder.Code != http.StatusOK {
				t.Fatalf("valid credential status = %d, want %d; body=%s", authorizedRecorder.Code, http.StatusOK, authorizedRecorder.Body.String())
			}
		})
	}
	if got := executor.callCount(); got != 1 {
		t.Fatalf("executor calls = %d, want exactly the authorized execute request", got)
	}
}

func TestConfirmationExecutorDoesNotControlOrdinaryV9Capability(t *testing.T) {
	base := &server{
		softwareVersion: "0.10.0",
		sourceRevision:  testSourceRevision,
	}
	qualified := &server{
		softwareVersion: "0.10.0",
		sourceRevision:  testSourceRevision,
		confirmation:    readyConfirmationExecutor(),
	}

	baseRecorder := httptest.NewRecorder()
	base.handleCapabilities(baseRecorder, httptest.NewRequest(http.MethodGet, "/v1/capabilities", nil))
	qualifiedRecorder := httptest.NewRecorder()
	qualified.handleCapabilities(qualifiedRecorder, httptest.NewRequest(http.MethodGet, "/v1/capabilities", nil))
	if baseRecorder.Code != http.StatusOK || qualifiedRecorder.Code != http.StatusOK {
		t.Fatalf(
			"capabilities statuses base=%d qualified=%d; base=%s qualified=%s",
			baseRecorder.Code,
			qualifiedRecorder.Code,
			baseRecorder.Body.String(),
			qualifiedRecorder.Body.String(),
		)
	}
	if baseRecorder.Body.String() != qualifiedRecorder.Body.String() {
		t.Fatalf(
			"installing internal v9 executor changed public capabilities:\nbase=%s\nqualified=%s",
			baseRecorder.Body.String(),
			qualifiedRecorder.Body.String(),
		)
	}

	var capabilities capabilitiesResponse
	if err := json.Unmarshal(qualifiedRecorder.Body.Bytes(), &capabilities); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(capabilities.SupportedBenchVersions, []int{8, 9}) {
		t.Fatalf("supported versions = %v, want ordinary v8 and v9", capabilities.SupportedBenchVersions)
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(qualifiedRecorder.Body.Bytes(), &raw); err != nil {
		t.Fatal(err)
	}
	for key := range raw {
		if strings.Contains(strings.ToLower(key), "confirmation") || strings.Contains(strings.ToLower(key), "v9") {
			t.Fatalf("internal v9 readiness leaked into public capabilities as %q", key)
		}
	}
	if got, message := requestedBenchVersion(9); got != 9 || message != "" {
		t.Fatalf("ordinary v9 score request rejected: (%d, %q)", got, message)
	}
}

func TestCanonicalConfirmationSHA256RejectsNonCanonicalValues(t *testing.T) {
	testCases := []struct {
		name  string
		value string
		want  bool
	}{
		{name: "canonical", value: testConfirmationArtifactChecksum, want: true},
		{name: "empty", value: ""},
		{name: "short", value: "00"},
		{name: "long", value: strings.Repeat("0", 65)},
		{name: "uppercase", value: strings.ToUpper(strings.Repeat("a", 64))},
		{name: "non hexadecimal", value: strings.Repeat("g", 64)},
	}
	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			if got := canonicalConfirmationSHA256(tc.value); got != tc.want {
				t.Fatalf("canonicalConfirmationSHA256(%q) = %t, want %t", tc.value, got, tc.want)
			}
		})
	}
}
