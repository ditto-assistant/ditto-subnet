package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/store"
	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestLockScoredRelayRunSerializesSharedRelayAccounting(t *testing.T) {
	t.Parallel()

	s := &server{}
	unlockFirst := s.lockScoredRelayRun()

	started := make(chan struct{})
	acquired := make(chan struct{})
	go func() {
		close(started)
		unlockSecond := s.lockScoredRelayRun()
		close(acquired)
		unlockSecond()
	}()
	<-started

	select {
	case <-acquired:
		t.Fatal("a second scored run acquired the shared relay lease concurrently")
	case <-time.After(50 * time.Millisecond):
	}

	unlockFirst()
	select {
	case <-acquired:
	case <-time.After(time.Second):
		t.Fatal("a waiting scored run did not acquire the relay lease after release")
	}
}

func TestProbeLockedModelRelay(t *testing.T) {
	t.Run("healthy tiny completion", func(t *testing.T) {
		var got map[string]any
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/v1/chat/completions" {
				t.Fatalf("path = %q", r.URL.Path)
			}
			if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
				t.Fatal(err)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"choices": []any{map[string]any{"message": map[string]string{"content": "OK"}}}})
		}))
		defer srv.Close()

		if err := probeLockedModelRelay(context.Background(), srv.URL, protocol.BenchVersionV7); err != nil {
			t.Fatalf("probe failed: %v", err)
		}
		if got["max_tokens"] != float64(1) || got["stream"] != false {
			t.Fatalf("probe was not bounded: %#v", got)
		}
		if got["model"] != "openai/gpt-oss-20b" {
			t.Fatalf("probe model = %q", got["model"])
		}
	})

	for _, tc := range []struct {
		name   string
		status int
		body   string
	}{
		{name: "upstream failure", status: http.StatusBadGateway, body: `upstream unavailable`},
		{name: "malformed success", status: http.StatusOK, body: `{"choices":[]}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer srv.Close()
			err := probeLockedModelRelay(context.Background(), srv.URL, protocol.BenchVersionV6)
			if err == nil {
				t.Fatal("expected relay probe failure")
			}
			if strings.Contains(err.Error(), tc.body) {
				t.Fatalf("probe exposed upstream response: %v", err)
			}
		})
	}
}

func TestHandleRelayPreflight(t *testing.T) {
	t.Run("reachable relay returns ok identity", func(t *testing.T) {
		relay := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/health" {
				t.Fatalf("path = %q", r.URL.Path)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"status":             "ok",
				"accounting_version": 2,
				"provider":           "openrouter",
				"profile_revision":   "profile-v1",
				"model":              "qwen/qwen3-32b",
			})
		}))
		defer relay.Close()
		t.Setenv("HARNESS_GATEWAY_URL", relay.URL)

		rec := httptest.NewRecorder()
		(&server{}).handleRelayPreflight(rec, httptest.NewRequest(http.MethodGet, "/v1/relay-preflight", nil))
		if rec.Code != http.StatusOK {
			t.Fatalf("status = %d, body %s", rec.Code, rec.Body.String())
		}
		var body map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		if body["status"] != "ok" || body["provider"] != "openrouter" || body["model"] != "qwen/qwen3-32b" {
			t.Fatalf("unexpected body: %#v", body)
		}
	})

	t.Run("unreachable relay fails closed as validator infrastructure", func(t *testing.T) {
		// A gateway that refuses the connection stands in for host.docker.internal
		// not resolving inside a mis-wired managed-stack netns — the exact failure
		// the off-netns validator preflight cannot otherwise see.
		t.Setenv("HARNESS_GATEWAY_URL", "http://127.0.0.1:1/")
		rec := httptest.NewRecorder()
		(&server{}).handleRelayPreflight(rec, httptest.NewRequest(http.MethodGet, "/v1/relay-preflight", nil))
		if rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("status = %d, body %s", rec.Code, rec.Body.String())
		}
		var body struct {
			Status  string `json:"status"`
			Failure struct {
				Kind      string `json:"kind"`
				Code      string `json:"code"`
				Retryable bool   `json:"retryable"`
			} `json:"failure"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		if body.Status != "unavailable" || body.Failure.Kind != "validator_infrastructure" ||
			body.Failure.Code != "model_relay_unavailable" || !body.Failure.Retryable {
			t.Fatalf("unexpected failure envelope: %s", rec.Body.String())
		}
	})
}

func TestRelayFailureIsRetryableValidatorInfrastructure(t *testing.T) {
	s := &server{store: store.New()}
	s.store.Create("run", "run_size", store.StatusRunning, 1, 1)
	s.failRelayUnavailable("run", errors.New("relay returned 503"))
	job, _ := s.store.Get("run")
	if job.Status != store.StatusFailed || job.Failure == nil {
		t.Fatalf("job = %#v", job)
	}
	if job.Failure.Kind != "validator_infrastructure" || job.Failure.Code != "model_relay_unavailable" || !job.Failure.Retryable {
		t.Fatalf("failure = %#v", job.Failure)
	}
}

func TestRelayFailureForContextKeepsCancellationSeparate(t *testing.T) {
	t.Run("caller cancellation is not infrastructure", func(t *testing.T) {
		s := &server{store: store.New()}
		s.store.Create("cancelled", "run_size", store.StatusRunning, 1, 1)
		ctx, cancel := context.WithCancel(context.Background())
		cancel()

		s.failRelayUnavailableForContext(ctx, "cancelled", context.Canceled)
		job, _ := s.store.Get("cancelled")
		if job.Status != store.StatusRunning || job.Failure != nil {
			t.Fatalf("caller cancellation was classified as infrastructure: %#v", job)
		}
	})

	t.Run("deadline remains fail closed", func(t *testing.T) {
		s := &server{store: store.New()}
		s.store.Create("deadline", "run_size", store.StatusRunning, 1, 1)
		ctx, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
		defer cancel()

		s.failRelayUnavailableForContext(ctx, "deadline", context.DeadlineExceeded)
		job, _ := s.store.Get("deadline")
		if job.Status != store.StatusFailed || job.Failure == nil || job.Failure.Code != "model_relay_unavailable" {
			t.Fatalf("deadline did not fail closed: %#v", job)
		}
	})
}

func TestTrustedEmbeddingFailureIsRetryableValidatorInfrastructure(t *testing.T) {
	failure := trustedEmbeddingInfrastructureFailure(errors.New("/seed returned 502: embedding service unavailable"))
	if failure == nil || failure.Kind != "validator_infrastructure" ||
		failure.Code != "embedding_provider_unavailable" || !failure.Retryable {
		t.Fatalf("failure = %#v", failure)
	}
	if got := trustedEmbeddingInfrastructureFailure(errors.New("harness returned malformed seed output")); got != nil {
		t.Fatalf("miner failure misclassified as infrastructure: %#v", got)
	}
}

func TestV7SeedingFailurePersistsStructuredInfrastructureClassification(t *testing.T) {
	s := &server{store: store.New()}
	s.store.Create("run", "run_size", store.StatusRunning, 1, 7)
	s.failV7Seeding("run", "seeding failed: ", errors.New("embedding service unavailable"))
	job, _ := s.store.Get("run")
	if job.Status != store.StatusFailed || job.Failure == nil ||
		job.Failure.Code != "embedding_provider_unavailable" || !job.Failure.Retryable {
		t.Fatalf("job = %#v", job)
	}
}

func TestRelayDegradedSince(t *testing.T) {
	start := relayHealthSnapshot{Requests: 10, Successes: 9, InfrastructureFailures: 1, CallerCancellations: 2, UpstreamAttempts: 12}
	if err := relayDegradedSince(start, relayHealthSnapshot{Requests: 20, Successes: 19, InfrastructureFailures: 1, CallerCancellations: 3, UpstreamAttempts: 23}); err != nil {
		t.Fatalf("healthy run was rejected: %v", err)
	}
	if err := relayDegradedSince(start, relayHealthSnapshot{Requests: 20, Successes: 18, InfrastructureFailures: 2}); err == nil {
		t.Fatal("provider failure during run must reject the attempt")
	}
	if err := relayDegradedSince(start, relayHealthSnapshot{}); err == nil {
		t.Fatal("relay restart during run must reject the attempt")
	}
}

func TestRelayExecutionSinceProducesTrustedRunDelta(t *testing.T) {
	start := relayHealthSnapshot{Requests: 10, Successes: 9, InfrastructureFailures: 1, CallerCancellations: 2, UpstreamAttempts: 12}
	end := relayHealthSnapshot{Requests: 14, Successes: 12, InfrastructureFailures: 1, CallerCancellations: 3, UpstreamAttempts: 18}

	got, err := relayExecutionSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if got.Requests != 4 || got.Successes != 3 || got.InfrastructureFailures != 0 ||
		got.CallerCancellations != 1 || got.UpstreamAttempts != 6 || got.Retries != 2 {
		t.Fatalf("execution = %#v", got)
	}

	end.UpstreamAttempts = 1
	if _, err := relayExecutionSince(start, end); err == nil {
		t.Fatal("relay restart must invalidate execution attribution")
	}
}

func TestRelayUsageSinceProducesTrustedRunDelta(t *testing.T) {
	start := relayHealthSnapshot{
		AccountingVersion: 2, Provider: "openrouter", ProfileRevision: "profile-v1",
		Model: "qwen/qwen3-32b", Requests: 10, Successes: 9, UsageAvailable: 9,
		PromptTokens: 1000, CompletionTokens: 200, ProviderLatencyMs: 3000,
		PromptBytes: 4000,
		TTFTStatus:  "unavailable_non_streaming",
	}
	end := start
	end.Requests += 4
	end.Successes += 4
	end.UsageAvailable += 4
	end.PromptTokens += 700
	end.PromptBytes += 2800
	end.CompletionTokens += 100
	end.ProviderLatencyMs += 900

	got, err := relayUsageSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != "complete" || got.Source != "model_proxy_provider_response" ||
		got.PromptTokens != 700 || got.PromptBytes != 2800 || got.CompletionTokens != 100 || got.TotalTokens != 800 ||
		got.ProviderLatencyMs != 900 || got.UsageAvailable != 4 || got.UsageUnavailable != 0 {
		t.Fatalf("usage = %#v", got)
	}

	end.UsageUnavailable++
	got, err = relayUsageSince(start, end)
	if err != nil || got.Status != "unavailable" {
		t.Fatalf("missing usage must be explicit and neutral: usage=%#v err=%v", got, err)
	}

	end.ProfileRevision = "profile-v2"
	if _, err := relayUsageSince(start, end); err == nil {
		t.Fatal("provider profile change must invalidate attribution")
	}
}

func TestReadRelayHealthRejectsStaticLegacyHealth(t *testing.T) {
	relay := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}))
	defer relay.Close()
	if _, err := readRelayHealth(context.Background(), relay.URL); err == nil || !strings.Contains(err.Error(), "run accounting") {
		t.Fatalf("legacy static health must fail closed, got %v", err)
	}
}

func TestV5RequiresTrustedTokenAccountingWhileLegacyAccountingRemainsReadable(t *testing.T) {
	legacy := relayHealthSnapshot{AccountingVersion: 1, Status: "ok"}
	if err := requireTokenAccounting(legacy, protocol.BenchVersionV5, "full"); err == nil {
		t.Fatal("v5 accepted relay accounting v1")
	}
	metered := relayHealthSnapshot{
		AccountingVersion: 2, Status: "ok", Provider: "p",
		ProfileRevision: "r", Model: "m",
	}
	if err := requireTokenAccounting(metered, protocol.BenchVersionV5, "full"); err != nil {
		t.Fatalf("v5 rejected trusted accounting: %v", err)
	}

	relay := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(legacy)
	}))
	defer relay.Close()
	if _, err := readRelayHealth(context.Background(), relay.URL); err != nil {
		t.Fatalf("v2-v4 rollout compatibility rejected accounting v1: %v", err)
	}
}

// Under the v7 QUALITY-ONLY contract the admission gate still fail-closes on
// the locked model identity and a well-formed versioned route profile, but it
// no longer requires a reviewed token baseline: token usage never moves the v7
// composite, so any correct route with trusted metered accounting is admitted
// and scores quality-only.
func TestV7RequiresExactModelProfileAndAdmitsQualityOnly(t *testing.T) {
	snapshot := relayHealthSnapshot{
		AccountingVersion: 2,
		Status:            "ok",
		Provider:          "groq",
		ProfileRevision:   "openrouter-route-0123456789abcdef-v1",
		Model:             llm.V7HarnessModel,
	}

	wrongModel := snapshot
	wrongModel.Model = llm.LockedHarnessModel
	if err := requireTokenAccounting(wrongModel, protocol.BenchVersionV7, "full"); err == nil || !strings.Contains(err.Error(), "model") {
		t.Fatalf("v7 accepted wrong model: %v", err)
	}

	wrongProfile := snapshot
	wrongProfile.ProfileRevision = "profile-v1"
	if err := requireTokenAccounting(wrongProfile, protocol.BenchVersionV7, "full"); err == nil || !strings.Contains(err.Error(), "profile") {
		t.Fatalf("v7 accepted unversioned profile: %v", err)
	}

	// No reviewed baseline exists for this route; quality-only admits it anyway.
	if err := requireTokenAccounting(snapshot, protocol.BenchVersionV7, "full"); err != nil {
		t.Fatalf("v7 quality-only admission rejected a correct route identity: %v", err)
	}

	// Untrusted accounting and missing provider identity still fail closed.
	untrusted := snapshot
	untrusted.AccountingVersion = 1
	if err := requireTokenAccounting(untrusted, protocol.BenchVersionV7, "full"); err == nil || !strings.Contains(err.Error(), "accounting") {
		t.Fatalf("v7 accepted untrusted accounting: %v", err)
	}
}

func TestV7RequiresCompleteProviderUsage(t *testing.T) {
	incomplete := protocol.TokenUsage{
		Provider:        "groq",
		ProfileRevision: "openrouter-route-0123456789abcdef-v1",
		Model:           llm.V7HarnessModel,
		PromptTokens:    1,
	}
	if err := requireCompleteV7Usage(protocol.BenchVersionV6, incomplete, relayExecutionSummary{}); err != nil {
		t.Fatalf("v6 compatibility rejected incomplete usage: %v", err)
	}
	if err := requireCompleteV7Usage(protocol.BenchVersionV7, incomplete, relayExecutionSummary{}); err == nil {
		t.Fatal("v7 accepted incomplete provider usage")
	}
	incomplete.CompletionTokens = 1
	incomplete.TotalTokens = 2
	incomplete.Requests = 1
	incomplete.Successes = 1
	incomplete.UsageAvailable = 1
	incomplete.Status = "complete"
	if err := requireCompleteV7Usage(protocol.BenchVersionV7, incomplete, relayExecutionSummary{}); err != nil {
		t.Fatalf("v7 rejected complete provider usage: %v", err)
	}
	// Caller cancellations may make requests exceed successful provider
	// responses, as observed in the reviewed campaign. They remain acceptable
	// when every successful response has usage and no response is unaccounted.
	incomplete.Requests = 2
	if err := requireCompleteV7Usage(protocol.BenchVersionV7, incomplete, relayExecutionSummary{}); err != nil {
		t.Fatalf("v7 rejected complete usage after a caller cancellation: %v", err)
	}
}

func TestV8RejectsACompleteUnusedChatLaneAsAgentFailure(t *testing.T) {
	unused := protocol.TokenUsage{
		Status:            "complete",
		AccountingVersion: 2,
		Provider:          "openrouter",
		ProfileRevision:   "openrouter-route-0123456789abcdef-v1",
		Model:             llm.V7HarnessModel,
	}
	err := requireCompleteV7Usage(protocol.BenchVersionV8, unused, relayExecutionSummary{})
	if !errors.Is(err, errAgentModelUseMissing) {
		t.Fatalf("v8 zero-chat interval error = %v, want model-use sentinel", err)
	}
	if err := requireCompleteV7Usage(protocol.BenchVersionV7, unused, relayExecutionSummary{}); err == nil {
		t.Fatal("the retired v7 contract was widened along with v8")
	}
	failure := relayFinalizeFailure(err)
	if failure.Kind != "sandbox_failure" || failure.Code != "model_inference_required" || failure.Retryable {
		t.Fatalf("zero-chat failure classification = %+v", failure)
	}
}

func TestV8ZeroUsageStillFailsClosedAfterAChatAttempt(t *testing.T) {
	unused := protocol.TokenUsage{
		Status:            "complete",
		AccountingVersion: 2,
		Provider:          "openrouter",
		ProfileRevision:   "openrouter-route-0123456789abcdef-v1",
		Model:             llm.V7HarnessModel,
	}
	for _, execution := range []relayExecutionSummary{
		{Requests: 1, CallerCancellations: 1},
		{Requests: 1, InfrastructureFailures: 1},
		{Requests: 1, AgentRequestRejections: 1},
	} {
		if err := requireCompleteV7Usage(protocol.BenchVersionV8, unused, execution); err == nil {
			t.Fatalf("v8 accepted inconsistent zero usage after a chat attempt: %+v", execution)
		}
	}
}

func TestRelayCountersCatchMaskedTwoHundredEmptyHarnessResponse(t *testing.T) {
	var failures uint64
	relay := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/health":
			_ = json.NewEncoder(w).Encode(relayHealthSnapshot{AccountingVersion: 2, Status: "ok", InfrastructureFailures: failures, Provider: "provider", ProfileRevision: "revision", Model: "model", TTFTStatus: "unavailable_non_streaming"})
		case "/v1/chat/completions":
			failures++
			http.Error(w, "provider unavailable", http.StatusBadGateway)
		default:
			http.NotFound(w, r)
		}
	}))
	defer relay.Close()

	harness := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/run" {
			http.NotFound(w, r)
			return
		}
		// Reproduce the production incident exactly: the harness observes a relay
		// failure but masks it behind a valid HTTP 200 empty RunResponse.
		_, _ = http.Post(relay.URL+"/v1/chat/completions", "application/json", strings.NewReader(`{"messages":[]}`))
		_ = json.NewEncoder(w).Encode(protocol.RunResponse{})
	}))
	defer harness.Close()

	start, err := readRelayHealth(context.Background(), relay.URL)
	if err != nil {
		t.Fatal(err)
	}
	ctx := runner.TrustSandbox(context.Background())
	resp, runErr := runner.RunCase(ctx, harness.URL, "case", "prompt", catalog.Catalog(), runner.CaseOptions{})
	if runErr != nil {
		t.Fatalf("incident shape must look delivered to the runner: %v", runErr)
	}
	if resp.FinalText != "" || len(resp.ToolCalls) != 0 {
		t.Fatalf("expected empty delivered response, got %#v", resp)
	}
	end, err := readRelayHealth(context.Background(), relay.URL)
	if err != nil {
		t.Fatal(err)
	}
	if err := relayDegradedSince(start, end); err == nil {
		t.Fatal("masked 200-empty response must not survive relay health validation")
	}
}
