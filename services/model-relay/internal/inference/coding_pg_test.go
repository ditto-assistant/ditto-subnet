package inference

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

const codingTestBearer = "coding-platform-bearer-000000000000000000000000"

type codingPolicyVector struct {
	Policy                      json.RawMessage              `json:"policy"`
	LockedRequests              []json.RawMessage            `json:"locked_requests"`
	ProviderResponses           []json.RawMessage            `json:"provider_responses"`
	NormalizedProviderResponses []json.RawMessage            `json:"normalized_provider_responses"`
	ProviderSettlements         map[string][]json.RawMessage `json:"provider_settlements"`
	Expected                    struct {
		InferenceGrantSHA256     string              `json:"inference_grant_sha256"`
		LockedRequestSHA256      []string            `json:"locked_request_sha256"`
		NormalizedResponseSHA256 []string            `json:"normalized_response_sha256"`
		ProviderSettlementSHA256 map[string][]string `json:"provider_settlement_sha256"`
	} `json:"expected"`
}

type codingPGFixture struct {
	deps         *Deps
	pool         *pgxpool.Pool
	now          time.Time
	deadline     time.Time
	grantID      uuid.UUID
	ticketID     uuid.UUID
	requestID    uuid.UUID
	brokerPriv   ed25519.PrivateKey
	vector       codingPolicyVector
	locked       codingLockedRequest
	lockedSHA256 string
}

func loadCodingPolicyVector(t *testing.T) codingPolicyVector {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata",
		"coding_inference_policy_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var vector codingPolicyVector
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	return vector
}

func codingTestConfig(t *testing.T, upstreamURL string) *config.Config {
	t.Helper()
	cfg := testConfig(t, map[string]string{
		"DITTO_CODING_INFERENCE_ENABLED":           "1",
		"DITTO_CODING_INFERENCE_ACCOUNT_GUARDRAIL": codingAccountGuardrail,
	})
	// Boot validation pins OpenRouter. Tests replace only the already-validated
	// destination with an in-process server.
	cfg.Inference.UpstreamURL = upstreamURL
	return cfg
}

func newCodingPGFixture(t *testing.T, cfg *config.Config) *codingPGFixture {
	t.Helper()
	pool := testutil.NewTestPGPool(t)
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	vector := loadCodingPolicyVector(t)
	var locked codingLockedRequest
	if err := json.Unmarshal(vector.LockedRequests[0], &locked); err != nil {
		t.Fatal(err)
	}
	locked.MaxCompletionTokens = 30_000
	lockedSHA256, err := codingCanonicalSHA256(locked)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC().Truncate(time.Microsecond)
	f := &codingPGFixture{
		pool: pool, now: now, deadline: now.Add(30 * time.Minute),
		grantID:    uuid.MustParse("44444444-4444-4444-8444-444444444444"),
		ticketID:   uuid.MustParse("33333333-3333-4333-8333-333333333333"),
		requestID:  uuid.MustParse("55555555-5555-4555-8555-555555555555"),
		brokerPriv: priv, vector: vector, locked: locked, lockedSHA256: lockedSHA256,
	}
	runID := uuid.New()
	certificationID := uuid.New()
	// The handler's target is the grant/request state machine. The selected run
	// and certification layers have their own real-Postgres suites; disable FK
	// triggers only while installing this otherwise-valid ticket fixture.
	connection, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := connection.Exec(t.Context(), `SET session_replication_role = replica`); err != nil {
		connection.Release()
		t.Fatal(err)
	}
	_, insertErr := connection.Exec(t.Context(), `
		INSERT INTO coding_shadow_tickets (
		  ticket_id, run_row_id, task_count, validator_hotkey,
		  certification_row_id, issued_at, deadline, created_at
		) VALUES ($1, $2, 1, $3, $4, $5, $6, $5);
		`,
		f.ticketID, runID, pgTestHotkey, certificationID, now.Add(-time.Minute), f.deadline)
	_, restoreErr := connection.Exec(t.Context(), `SET session_replication_role = origin`)
	connection.Release()
	if insertErr != nil || restoreErr != nil {
		t.Fatalf("seed coding ticket: insert=%v restore=%v", insertErr, restoreErr)
	}
	testutil.SeedSQL(t, pool, `
		INSERT INTO coding_inference_grants (
		  grant_id, ticket_id, run_row_id, task_count, validator_hotkey,
		  case_id, profile_capability_id, inference_grant_sha256,
		  model, provider_api, provider_route, receipt_provider,
		  provider_route_profile, provider_account_guardrail,
		  provider_pipeline_policy, provider_cache_policy, reasoning_effort,
		  status, bearer_digest, revoke_bearer_digest, broker_public_key, generation,
		  request_budget, prompt_token_budget, completion_token_budget,
		  cost_budget_usd_micros, request_count, prompt_tokens,
		  completion_tokens, cost_usd_micros, active_requests,
		  expires_at, revoked_at, weight_eligible, created_at, updated_at
		) VALUES (
		  $1, $2, $3, 1, $4, 'case-inference-001', 'profile-inference-001', $5,
		  $6, $7, $8, $9, $10, $11, $12, $13, $14,
		  'active', $15, repeat('d', 64), $16, 1, 166, 200000, 30000, 10000000,
		  0, 0, 0, 0, 0, $17, NULL, false, $18, $18
		)`,
		f.grantID, f.ticketID, runID, pgTestHotkey, codingInferenceGrantSHA256,
		codingModel, codingProviderAPI, codingProviderRoute, codingReceiptProvider,
		codingProviderRouteProfile, codingAccountGuardrail, codingPipelinePolicy,
		codingCachePolicy, codingReasoningEffort, bearerDigest(codingTestBearer),
		base64.RawURLEncoding.EncodeToString(pub), f.deadline, now.Add(-time.Minute))
	queries := postgres.New(pool)
	logger := slog.New(slog.NewTextHandler(testWriter{t}, nil))
	f.deps = &Deps{
		Cfg: cfg, Logger: logger,
		Pool: pool, Queries: queries, Upstream: &http.Client{},
		Settings: NewSettingsResolver(queries, logger),
		Now:      func() time.Time { return f.now },
	}
	return f
}

func (f *codingPGFixture) dispatchBody(t *testing.T, sequence, requestSequence, attempt int32) []byte {
	t.Helper()
	request := codingDispatchRequest{
		Schema: codingDispatchRequestSchema, CodingContractVersion: 1, WeightEligible: false,
		TicketID: f.ticketID.String(), CaseID: "case-inference-001",
		ProfileCapabilityID: "profile-inference-001", InferenceGrantSHA256: codingInferenceGrantSHA256,
		GrantID: f.grantID.String(), Generation: 1, Sequence: sequence,
		RequestSequence: requestSequence, Attempt: attempt, RequestID: f.requestID.String(),
		LockedRequestSHA256: f.lockedSHA256, LockedRequest: f.locked,
		Deadline: isoformatMicro(f.deadline),
	}
	body, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func (f *codingPGFixture) headers(body []byte) map[string]string {
	nonce := uuid.New()
	message := proxyMessage(f.grantID, 1, nonce, f.now, body)
	return map[string]string{
		"X-Ditto-Grant": f.grantID.String(), "X-Ditto-Generation": "1",
		"X-Ditto-Nonce": nonce.String(), "X-Ditto-Requested-At": isoformatMicro(f.now),
		"X-Ditto-Proof": base64.RawURLEncoding.EncodeToString(ed25519.Sign(f.brokerPriv, message)),
		"Authorization": "Bearer " + codingTestBearer,
	}
}

func (f *codingPGFixture) seedActiveSibling(t *testing.T, validatorHotkey string) {
	t.Helper()
	ticketID := uuid.New()
	runID := uuid.New()
	certificationID := uuid.New()
	grantID := uuid.New()
	requestRowID := uuid.New()
	requestID := uuid.New()
	connection, err := f.pool.Acquire(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := connection.Exec(t.Context(), `SET session_replication_role = replica`); err != nil {
		connection.Release()
		t.Fatal(err)
	}
	_, insertErr := connection.Exec(t.Context(), `
		INSERT INTO coding_shadow_tickets (
		  ticket_id, run_row_id, task_count, validator_hotkey,
		  certification_row_id, issued_at, deadline, created_at
		) VALUES ($1, $2, 1, $3, $4, $5, $6, $5)`,
		ticketID, runID, validatorHotkey, certificationID, f.now.Add(-time.Minute), f.deadline)
	_, restoreErr := connection.Exec(t.Context(), `SET session_replication_role = origin`)
	connection.Release()
	if insertErr != nil || restoreErr != nil {
		t.Fatalf("seed sibling ticket: insert=%v restore=%v", insertErr, restoreErr)
	}
	testutil.SeedSQL(t, f.pool, `
		INSERT INTO coding_inference_grants (
		  grant_id, ticket_id, run_row_id, task_count, validator_hotkey,
		  case_id, profile_capability_id, inference_grant_sha256,
		  model, provider_api, provider_route, receipt_provider,
		  provider_route_profile, provider_account_guardrail,
		  provider_pipeline_policy, provider_cache_policy, reasoning_effort,
		  status, bearer_digest, revoke_bearer_digest, broker_public_key, generation,
		  request_budget, prompt_token_budget, completion_token_budget,
		  cost_budget_usd_micros, request_count, prompt_tokens,
		  completion_tokens, cost_usd_micros, active_requests,
		  expires_at, revoked_at, weight_eligible, created_at, updated_at
		)
		SELECT $1, $2, $3, 1, $4, 'case-sibling', 'profile-sibling',
		  inference_grant_sha256, model, provider_api, provider_route,
		  receipt_provider, provider_route_profile, provider_account_guardrail,
		  provider_pipeline_policy, provider_cache_policy, reasoning_effort,
		  'active', bearer_digest, revoke_bearer_digest, broker_public_key, generation,
		  request_budget, prompt_token_budget, completion_token_budget,
		  cost_budget_usd_micros, 1, 0, 0, 0, 1,
		  $5, NULL, false, $6, $6
		FROM coding_inference_grants WHERE grant_id = $7`,
		grantID, ticketID, runID, validatorHotkey, f.deadline, f.now.Add(-time.Minute), f.grantID)
	testutil.SeedSQL(t, f.pool, `
		INSERT INTO coding_inference_requests (
		  request_row_id, grant_id, ticket_id, generation, sequence,
		  request_sequence, attempt, request_id, case_id,
		  profile_capability_id, inference_grant_sha256,
		  locked_request_sha256, status, started_at, weight_eligible
		) VALUES ($1, $2, $3, 1, 1, 1, 1, $4, 'case-sibling',
		  'profile-sibling', $5, $6, 'started', $7, false)`,
		requestRowID, grantID, ticketID, requestID, codingInferenceGrantSHA256,
		f.lockedSHA256, f.now)
}

func codingRequest(body []byte, headers map[string]string) *http.Request {
	request := httptest.NewRequest(http.MethodPost, "/api/v1/inference/coding/chat/completions", bytes.NewReader(body))
	for key, value := range headers {
		request.Header.Set(key, value)
	}
	request.Header.Set("Content-Type", "application/json")
	return request
}

func codingProviderBody(t *testing.T, vector codingPolicyVector, selected bool) []byte {
	t.Helper()
	var payload map[string]any
	if err := json.Unmarshal(vector.ProviderResponses[0], &payload); err != nil {
		t.Fatal(err)
	}
	payload["openrouter_metadata"] = map[string]any{
		"requested": codingModel, "strategy": "direct", "attempt": 1,
		"endpoints": map[string]any{
			"total": 1,
			"available": []any{map[string]any{
				"provider": codingReceiptProvider, "model": codingModel, "selected": selected,
			}},
		},
		"pipeline": []any{},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func fakeCodingProvider(t *testing.T, responseBody []byte, captured *[]byte) *httptest.Server {
	return fakeCodingProviderStatus(t, http.StatusOK, responseBody, captured)
}

func fakeCodingProviderStatus(
	t *testing.T,
	status int,
	responseBody []byte,
	captured *[]byte,
) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-openrouter-key" ||
			r.Header.Get("X-OpenRouter-Metadata") != "enabled" ||
			r.Header.Get("X-OpenRouter-Cache") != "false" ||
			r.Header.Get("HTTP-Referer") != "https://heyditto.ai/" ||
			r.Header.Get("X-OpenRouter-Title") != "DittoBench Coding" {
			t.Errorf("provider headers=%v", r.Header)
		}
		for _, forbidden := range []string{"X-Ditto-Grant", "X-Ditto-Generation", "X-Ditto-Proof"} {
			if r.Header.Get(forbidden) != "" {
				t.Errorf("provider received %s", forbidden)
			}
		}
		body, _ := io.ReadAll(r.Body)
		*captured = append((*captured)[:0], body...)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write(responseBody)
	}))
}

func TestCodingFullFlowPersistsCanonicalSettlement(t *testing.T) {
	vector := loadCodingPolicyVector(t)
	var captured []byte
	provider := fakeCodingProvider(t, codingProviderBody(t, vector, true), &captured)
	defer provider.Close()
	f := newCodingPGFixture(t, codingTestConfig(t, provider.URL))
	f.deps.Upstream = provider.Client()
	body := f.dispatchBody(t, 1, 1, 1)
	request := codingRequest(body, f.headers(body))
	request.Header.Set("HTTP-Referer", "https://attacker.invalid/")
	request.Header.Set("X-OpenRouter-Title", "attacker")
	w := serve(f.deps, request)
	if w.Code != http.StatusOK || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("response=%d %s headers=%v", w.Code, w.Body.String(), w.Header())
	}
	var result codingDispatchResult
	if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.Schema != codingDispatchResultSchema || result.WeightEligible ||
		result.Settlement.Outcome != "complete" || result.NormalizedResponseBase64 == nil ||
		result.FailureResponseProjectionBase64 != nil {
		t.Fatalf("result=%#v", result)
	}
	lockedBody, err := compactJSON(f.locked)
	if err != nil || !bytes.Equal(captured, lockedBody) {
		t.Fatalf("provider body drifted: err=%v\n got=%s\nwant=%s", err, captured, lockedBody)
	}
	if bytes.Contains(captured, []byte(f.ticketID.String())) || bytes.Contains(captured, []byte("locked_request_sha256")) {
		t.Fatal("provider received control-plane dispatch fields")
	}
	var requestStatus, settlementJSON string
	var providerGeneration *string
	if err := f.pool.QueryRow(t.Context(), `
		SELECT status, provider_settlement_json, provider_generation_id
		FROM coding_inference_requests WHERE grant_id = $1 AND sequence = 1`, f.grantID).
		Scan(&requestStatus, &settlementJSON, &providerGeneration); err != nil {
		t.Fatal(err)
	}
	if requestStatus != "complete" || providerGeneration == nil || *providerGeneration != "generation-synthetic-001" ||
		!strings.Contains(settlementJSON, `"outcome":"complete"`) {
		t.Fatalf("request=%s generation=%v settlement=%s", requestStatus, providerGeneration, settlementJSON)
	}
	var grantStatus string
	var requestCount, active int
	var prompt, completion, cost int64
	if err := f.pool.QueryRow(t.Context(), `
		SELECT status, request_count, active_requests, prompt_tokens, completion_tokens, cost_usd_micros
		FROM coding_inference_grants WHERE grant_id = $1`, f.grantID).
		Scan(&grantStatus, &requestCount, &active, &prompt, &completion, &cost); err != nil {
		t.Fatal(err)
	}
	if grantStatus != "active" || requestCount != 1 || active != 0 || prompt != 1000 || completion != 200 || cost != 1234 {
		t.Fatalf("grant=%s count=%d active=%d usage=%d/%d/%d", grantStatus, requestCount, active, prompt, completion, cost)
	}
}

func TestCodingReceiptFreeRetryDoesNotDoubleCountLogicalRequest(t *testing.T) {
	vector := loadCodingPolicyVector(t)
	var firstCaptured []byte
	preProvider := map[string]any{
		"error": map[string]any{"code": 429, "message": "capacity"},
		"openrouter_metadata": map[string]any{
			"requested": codingModel, "strategy": "direct", "attempt": 0,
			"endpoints": map[string]any{
				"total": 1, "available": []any{map[string]any{
					"provider": codingReceiptProvider, "model": codingModel, "selected": false,
				}},
			},
			"pipeline": []any{},
		},
	}
	preProviderBody, _ := json.Marshal(preProvider)
	provider := fakeCodingProviderStatus(t, http.StatusTooManyRequests, preProviderBody, &firstCaptured)
	f := newCodingPGFixture(t, codingTestConfig(t, provider.URL))
	f.deps.Upstream = provider.Client()
	firstBody := f.dispatchBody(t, 1, 1, 1)
	first := serve(f.deps, codingRequest(firstBody, f.headers(firstBody)))
	provider.Close()
	if first.Code != http.StatusOK || !strings.Contains(first.Body.String(), `"outcome":"receipt_free_retry"`) {
		t.Fatalf("first=%d %s", first.Code, first.Body.String())
	}

	var secondCaptured []byte
	success := fakeCodingProvider(t, codingProviderBody(t, vector, true), &secondCaptured)
	defer success.Close()
	f.deps.Upstream = success.Client()
	f.deps.Cfg.Inference.UpstreamURL = success.URL
	secondBody := f.dispatchBody(t, 2, 1, 2)
	second := serve(f.deps, codingRequest(secondBody, f.headers(secondBody)))
	if second.Code != http.StatusOK || !strings.Contains(second.Body.String(), `"outcome":"complete"`) {
		t.Fatalf("second=%d %s", second.Code, second.Body.String())
	}
	var count, active int
	if err := f.pool.QueryRow(t.Context(), `
		SELECT request_count, active_requests FROM coding_inference_grants WHERE grant_id = $1`, f.grantID).
		Scan(&count, &active); err != nil {
		t.Fatal(err)
	}
	if count != 1 || active != 0 {
		t.Fatalf("request_count=%d active=%d", count, active)
	}
}

type failingRoundTripper struct{ err error }

func (transport failingRoundTripper) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, transport.err
}

func TestCodingAmbiguousProviderTransportIsTerminalUnsettled(t *testing.T) {
	f := newCodingPGFixture(t, codingTestConfig(t, "http://127.0.0.1:1"))
	f.deps.Upstream = &http.Client{Transport: failingRoundTripper{err: errors.New("private provider detail")}}
	body := f.dispatchBody(t, 1, 1, 1)
	w := serve(f.deps, codingRequest(body, f.headers(body)))
	if w.Code != http.StatusBadGateway || strings.Contains(w.Body.String(), "private provider detail") {
		t.Fatalf("response=%d %s", w.Code, w.Body.String())
	}
	var requestStatus, reason, grantStatus string
	if err := f.pool.QueryRow(t.Context(), `
		SELECT r.status, r.unsettled_reason, g.status
		FROM coding_inference_requests r JOIN coding_inference_grants g USING (grant_id)
		WHERE r.grant_id = $1`, f.grantID).Scan(&requestStatus, &reason, &grantStatus); err != nil {
		t.Fatal(err)
	}
	if requestStatus != "unsettled" || reason != "provider_settlement_unavailable" || grantStatus != "revoked" {
		t.Fatalf("request=%s reason=%s grant=%s", requestStatus, reason, grantStatus)
	}
}

func TestCodingInvalidSelectedProviderResponseIsCanonicalFailure(t *testing.T) {
	invalid := map[string]any{
		"id": "generation-synthetic-invalid", "model": "openai/other-model",
		"provider": codingReceiptProvider, "choices": []any{},
		"usage": map[string]any{
			"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0,
		},
		"openrouter_metadata": map[string]any{
			"requested": codingModel, "strategy": "direct", "attempt": 1,
			"endpoints": map[string]any{
				"total": 1, "available": []any{map[string]any{
					"provider": codingReceiptProvider, "model": codingModel, "selected": true,
				}},
			},
			"pipeline": []any{},
		},
	}
	invalidBody, _ := json.Marshal(invalid)
	var captured []byte
	provider := fakeCodingProvider(t, invalidBody, &captured)
	defer provider.Close()
	f := newCodingPGFixture(t, codingTestConfig(t, provider.URL))
	f.deps.Upstream = provider.Client()
	body := f.dispatchBody(t, 1, 1, 1)
	w := serve(f.deps, codingRequest(body, f.headers(body)))
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"outcome":"provider_failure"`) ||
		!strings.Contains(w.Body.String(), `"terminal_error_code":"provider_response_invalid"`) ||
		!strings.Contains(w.Body.String(), `"failure_response_projection_base64":"`) {
		t.Fatalf("response=%d %s", w.Code, w.Body.String())
	}
	var requestStatus, grantStatus, generation string
	if err := f.pool.QueryRow(t.Context(), `
		SELECT r.status, g.status, r.provider_generation_id
		FROM coding_inference_requests r JOIN coding_inference_grants g USING (grant_id)
		WHERE r.grant_id = $1`, f.grantID).Scan(&requestStatus, &grantStatus, &generation); err != nil {
		t.Fatal(err)
	}
	if requestStatus != "provider_failure" || grantStatus != "revoked" || generation != "generation-synthetic-invalid" {
		t.Fatalf("request=%s grant=%s generation=%s", requestStatus, grantStatus, generation)
	}
}

func TestCodingConcurrentDispatchAdmitsOneProviderCall(t *testing.T) {
	vector := loadCodingPolicyVector(t)
	entered := make(chan struct{})
	release := make(chan struct{})
	var calls atomic.Int32
	providerBody := codingProviderBody(t, vector, true)
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			close(entered)
		}
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(providerBody)
	}))
	defer provider.Close()
	f := newCodingPGFixture(t, codingTestConfig(t, provider.URL))
	f.deps.Upstream = provider.Client()
	body := f.dispatchBody(t, 1, 1, 1)
	firstDone := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		firstDone <- serve(f.deps, codingRequest(body, f.headers(body)))
	}()
	select {
	case <-entered:
	case <-time.After(10 * time.Second):
		t.Fatal("first provider call did not start")
	}
	second := serve(f.deps, codingRequest(body, f.headers(body)))
	if second.Code != http.StatusConflict {
		t.Fatalf("second=%d %s", second.Code, second.Body.String())
	}
	close(release)
	first := <-firstDone
	if first.Code != http.StatusOK || calls.Load() != 1 {
		t.Fatalf("first=%d %s calls=%d", first.Code, first.Body.String(), calls.Load())
	}
}

func TestCodingSettlementAccountsProviderAfterConcurrentGrantRevocation(t *testing.T) {
	vector := loadCodingPolicyVector(t)
	entered := make(chan struct{})
	release := make(chan struct{})
	providerBody := codingProviderBody(t, vector, true)
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(entered)
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(providerBody)
	}))
	defer provider.Close()
	f := newCodingPGFixture(t, codingTestConfig(t, provider.URL))
	f.deps.Upstream = provider.Client()
	body := f.dispatchBody(t, 1, 1, 1)
	done := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		done <- serve(f.deps, codingRequest(body, f.headers(body)))
	}()
	select {
	case <-entered:
	case <-time.After(10 * time.Second):
		t.Fatal("provider call did not start")
	}
	testutil.SeedSQL(t, f.pool, `
		UPDATE coding_inference_grants
		SET status = 'revoked', bearer_digest = NULL, broker_public_key = NULL,
		    active_requests = 0, revoked_at = clock_timestamp(), updated_at = clock_timestamp()
		WHERE grant_id = $1`, f.grantID)
	close(release)
	response := <-done
	if response.Code != http.StatusOK {
		t.Fatalf("response=%d %s", response.Code, response.Body.String())
	}
	var requestStatus, grantStatus string
	var prompt, completion, cost int64
	if err := f.pool.QueryRow(t.Context(), `
		SELECT r.status, g.status, g.prompt_tokens, g.completion_tokens, g.cost_usd_micros
		FROM coding_inference_requests r JOIN coding_inference_grants g USING (grant_id)
		WHERE r.grant_id = $1`, f.grantID).
		Scan(&requestStatus, &grantStatus, &prompt, &completion, &cost); err != nil {
		t.Fatal(err)
	}
	if requestStatus != "complete" || grantStatus != "revoked" || prompt != 1000 || completion != 200 || cost != 1234 {
		t.Fatalf("request=%s grant=%s usage=%d/%d/%d", requestStatus, grantStatus, prompt, completion, cost)
	}
}

func TestCodingConcurrencyCapsDeclineBeforeProvider(t *testing.T) {
	for _, test := range []struct {
		name      string
		hotkey    string
		validator int
		global    int
	}{
		{name: "validator", hotkey: pgTestHotkey, validator: 1, global: 16},
		{name: "global", hotkey: "5" + strings.Repeat("W", 47), validator: 4, global: 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			var calls atomic.Int32
			provider := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
				calls.Add(1)
			}))
			defer provider.Close()
			f := newCodingPGFixture(t, codingTestConfig(t, provider.URL))
			f.deps.Upstream = provider.Client()
			f.deps.Cfg.Inference.CodingValidatorConcurrency = test.validator
			f.deps.Cfg.Inference.CodingGlobalConcurrency = test.global
			f.seedActiveSibling(t, test.hotkey)
			body := f.dispatchBody(t, 1, 1, 1)
			w := serve(f.deps, codingRequest(body, f.headers(body)))
			if w.Code != http.StatusTooManyRequests || w.Header().Get("Retry-After") != "1" || calls.Load() != 0 {
				t.Fatalf("response=%d %s retry=%q calls=%d", w.Code, w.Body.String(), w.Header().Get("Retry-After"), calls.Load())
			}
			var count int
			if err := f.pool.QueryRow(t.Context(), `SELECT count(*) FROM coding_inference_requests WHERE grant_id = $1`, f.grantID).Scan(&count); err != nil {
				t.Fatal(err)
			}
			if count != 0 {
				t.Fatalf("declined grant persisted %d requests", count)
			}
		})
	}
}

func TestCodingProviderGenerationReuseRevokesSecondRequest(t *testing.T) {
	vector := loadCodingPolicyVector(t)
	var captured []byte
	provider := fakeCodingProvider(t, codingProviderBody(t, vector, true), &captured)
	defer provider.Close()
	f := newCodingPGFixture(t, codingTestConfig(t, provider.URL))
	f.deps.Upstream = provider.Client()
	firstBody := f.dispatchBody(t, 1, 1, 1)
	first := serve(f.deps, codingRequest(firstBody, f.headers(firstBody)))
	if first.Code != http.StatusOK {
		t.Fatalf("first=%d %s", first.Code, first.Body.String())
	}
	f.requestID = uuid.New()
	f.locked.MaxCompletionTokens = 29_000
	var err error
	f.lockedSHA256, err = codingCanonicalSHA256(f.locked)
	if err != nil {
		t.Fatal(err)
	}
	secondBody := f.dispatchBody(t, 2, 2, 1)
	second := serve(f.deps, codingRequest(secondBody, f.headers(secondBody)))
	if second.Code != http.StatusInternalServerError {
		t.Fatalf("second=%d %s", second.Code, second.Body.String())
	}
	var requestStatus, reason, grantStatus string
	if err := f.pool.QueryRow(t.Context(), `
		SELECT r.status, r.unsettled_reason, g.status
		FROM coding_inference_requests r JOIN coding_inference_grants g USING (grant_id)
		WHERE r.grant_id = $1 AND r.sequence = 2`, f.grantID).
		Scan(&requestStatus, &reason, &grantStatus); err != nil {
		t.Fatal(err)
	}
	if requestStatus != "unsettled" || reason != "invalid_provider_settlement" || grantStatus != "revoked" {
		t.Fatalf("request=%s reason=%s grant=%s", requestStatus, reason, grantStatus)
	}
}

func TestCodingProviderMetadataFailuresAreTerminalUnsettled(t *testing.T) {
	vector := loadCodingPolicyVector(t)
	valid := codingProviderBody(t, vector, true)
	var validObject map[string]any
	if err := json.Unmarshal(valid, &validObject); err != nil {
		t.Fatal(err)
	}
	tests := map[string]func(map[string]any, http.Header){
		"missing metadata": func(value map[string]any, _ http.Header) {
			delete(value, "openrouter_metadata")
		},
		"pipeline mutation": func(value map[string]any, _ http.Header) {
			value["openrouter_metadata"].(map[string]any)["pipeline"] = []any{"compression"}
		},
		"wrong content type": func(_ map[string]any, header http.Header) {
			header.Set("Content-Type", "text/plain")
		},
		"selected route mismatch": func(value map[string]any, _ http.Header) {
			metadata := value["openrouter_metadata"].(map[string]any)
			endpoint := metadata["endpoints"].(map[string]any)["available"].([]any)[0].(map[string]any)
			endpoint["provider"] = "Other"
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			clonedBody, _ := json.Marshal(validObject)
			var payload map[string]any
			_ = json.Unmarshal(clonedBody, &payload)
			header := http.Header{"Content-Type": []string{"application/json"}}
			mutate(payload, header)
			providerBody, _ := json.Marshal(payload)
			provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				for key, values := range header {
					w.Header()[key] = values
				}
				_, _ = w.Write(providerBody)
			}))
			defer provider.Close()
			f := newCodingPGFixture(t, codingTestConfig(t, provider.URL))
			f.deps.Upstream = provider.Client()
			body := f.dispatchBody(t, 1, 1, 1)
			w := serve(f.deps, codingRequest(body, f.headers(body)))
			if w.Code != http.StatusBadGateway {
				t.Fatalf("response=%d %s", w.Code, w.Body.String())
			}
			var requestStatus, reason, grantStatus string
			if err := f.pool.QueryRow(t.Context(), `
				SELECT r.status, r.unsettled_reason, g.status
				FROM coding_inference_requests r JOIN coding_inference_grants g USING (grant_id)
				WHERE r.grant_id = $1`, f.grantID).Scan(&requestStatus, &reason, &grantStatus); err != nil {
				t.Fatal(err)
			}
			if requestStatus != "unsettled" || reason != "provider_settlement_unavailable" || grantStatus != "revoked" {
				t.Fatalf("request=%s reason=%s grant=%s", requestStatus, reason, grantStatus)
			}
		})
	}
}

type losingResponseWriter struct {
	header http.Header
	status int
}

func (writer *losingResponseWriter) Header() http.Header {
	if writer.header == nil {
		writer.header = make(http.Header)
	}
	return writer.header
}

func (writer *losingResponseWriter) WriteHeader(status int) { writer.status = status }

func (*losingResponseWriter) Write([]byte) (int, error) {
	return 0, errors.New("simulated client response loss")
}

func TestCodingSettlementCommitsBeforeClientResponseLoss(t *testing.T) {
	vector := loadCodingPolicyVector(t)
	var captured []byte
	provider := fakeCodingProvider(t, codingProviderBody(t, vector, true), &captured)
	defer provider.Close()
	f := newCodingPGFixture(t, codingTestConfig(t, provider.URL))
	f.deps.Upstream = provider.Client()
	body := f.dispatchBody(t, 1, 1, 1)
	request := codingRequest(body, f.headers(body))
	writer := &losingResponseWriter{}
	f.deps.handleCodingChatCompletions(writer, request)
	if writer.status != http.StatusOK {
		t.Fatalf("status=%d", writer.status)
	}
	var requestStatus string
	var active int
	if err := f.pool.QueryRow(t.Context(), `
		SELECT r.status, g.active_requests
		FROM coding_inference_requests r JOIN coding_inference_grants g USING (grant_id)
		WHERE r.grant_id = $1`, f.grantID).Scan(&requestStatus, &active); err != nil {
		t.Fatal(err)
	}
	if requestStatus != "complete" || active != 0 {
		t.Fatalf("request=%s active=%d", requestStatus, active)
	}
}

func TestCodingDisabledAndInvalidProofNeverReachProvider(t *testing.T) {
	disabled := newGateDeps(t, testConfig(t, nil))
	w := serve(disabled, httptest.NewRequest(http.MethodPost, "/api/v1/inference/coding/chat/completions", nil))
	expectEnvelope(t, w, 404, 3002, "coding inference proxy is disabled")

	var captured []byte
	provider := fakeCodingProvider(t, []byte(`{}`), &captured)
	defer provider.Close()
	f := newCodingPGFixture(t, codingTestConfig(t, provider.URL))
	f.deps.Upstream = provider.Client()
	body := f.dispatchBody(t, 1, 1, 1)
	headers := f.headers(body)
	headers["X-Ditto-Proof"] = base64.RawURLEncoding.EncodeToString(make([]byte, ed25519.SignatureSize))
	w = serve(f.deps, codingRequest(body, headers))
	if w.Code != http.StatusUnauthorized || len(captured) != 0 {
		t.Fatalf("response=%d provider_body=%q", w.Code, captured)
	}
}

func TestCodingPolicyConstantsMatchSharedVector(t *testing.T) {
	vector := loadCodingPolicyVector(t)
	var policy any
	decoder := json.NewDecoder(bytes.NewReader(vector.Policy))
	decoder.UseNumber()
	if err := decoder.Decode(&policy); err != nil {
		t.Fatal(err)
	}
	digest, err := codingCanonicalSHA256(policy)
	if err != nil || digest != codingInferenceGrantSHA256 || digest != vector.Expected.InferenceGrantSHA256 {
		t.Fatalf("policy digest=%s vector=%s err=%v", digest, vector.Expected.InferenceGrantSHA256, err)
	}
	dispatch := &codingPGFixture{vector: vector, locked: codingLockedRequest{}}
	if json.Unmarshal(vector.LockedRequests[0], &dispatch.locked) != nil || validateCodingLocked(dispatch.locked) != nil {
		t.Fatal("model relay rejected the shared locked-request vector")
	}
}
