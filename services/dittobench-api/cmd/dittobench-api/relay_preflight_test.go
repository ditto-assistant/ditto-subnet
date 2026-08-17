package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
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

func TestRelayRunBaselineExcludesTrustedPreflightFromUsage(t *testing.T) {
	var requests, successes, usageAvailable, promptTokens, completionTokens uint64
	relay := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/chat/completions":
			requests++
			successes++
			usageAvailable++
			promptTokens += 3
			completionTokens++
			_ = json.NewEncoder(w).Encode(map[string]any{
				"choices": []any{map[string]any{"message": map[string]string{"content": "OK"}}},
			})
		case "/health":
			_ = json.NewEncoder(w).Encode(relayHealthSnapshot{
				AccountingVersion: 2,
				Status:            "ok",
				Requests:          requests,
				Successes:         successes,
				UsageAvailable:    usageAvailable,
				PromptTokens:      promptTokens,
				CompletionTokens:  completionTokens,
				Provider:          "openrouter",
				ProfileRevision:   "openrouter-route-0123456789abcdef-v1",
				Model:             llm.V7HarnessModel,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer relay.Close()

	s := &server{}
	start, ok := s.relayRunStart(
		context.Background(), "run", protocol.BenchVersionV8, "full", relay.URL, "",
	)
	if !ok {
		t.Fatal("healthy trusted preflight failed")
	}
	if start.Requests != 1 || start.Successes != 1 {
		t.Fatalf("run baseline was not taken after preflight: %+v", start)
	}

	// With no harness request after the baseline, the scored interval is exactly
	// zero even though the lease-level relay counters include the trusted probe.
	end, err := readRelayHealth(context.Background(), relay.URL)
	if err != nil {
		t.Fatal(err)
	}
	usage, err := relayUsageSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	execution, err := relayExecutionSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if usage.Status != "complete" || usage.Requests != 0 || execution.Requests != 0 {
		t.Fatalf("trusted preflight entered run evidence: usage=%+v execution=%+v", usage, execution)
	}
	if err := requireCompleteV7Usage(protocol.BenchVersionV8, usage, execution); !errors.Is(err, errAgentModelUseMissing) {
		t.Fatalf("post-preflight zero interval = %v, want model-use sentinel", err)
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

func TestSeedStoreLockTimeoutPersistsStructuredInfrastructureClassification(t *testing.T) {
	s := &server{store: store.New()}
	s.store.Create("run", "run_size", store.StatusRunning, 1, 8)
	s.failV7Seeding("run", "seeding failed: ", fmt.Errorf("%w: exact boundary", runner.ErrSeedStoreLockTimeout))
	job, _ := s.store.Get("run")
	if job.Status != store.StatusFailed || job.Failure == nil ||
		job.Failure.Kind != "validator_infrastructure" ||
		job.Failure.Code != "seed_store_lock_timeout" || !job.Failure.Retryable {
		t.Fatalf("job = %#v", job)
	}
	if got := trustedSeedingInfrastructureFailure(errors.New("seed exceeded 600s, aborted to release the store lock")); got != nil {
		t.Fatalf("untyped prose minted a retry: %#v", got)
	}
	_, filename, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("resolve test source path")
	}
	validatorSource, err := os.ReadFile(filepath.Join(
		filepath.Dir(filename), "../../../../ditto/validator/dittobench.py",
	))
	if err != nil {
		t.Fatalf("read validator classifier: %v", err)
	}
	if !strings.Contains(string(validatorSource), `"seed_store_lock_timeout"`) {
		t.Fatal("Go emitted code is absent from the validator infrastructure allowlist")
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

func TestV9AndV10RequireReviewedVariableReasoningProfile(t *testing.T) {
	snapshot := relayHealthSnapshot{
		AccountingVersion: 2,
		Status:            "ok",
		Provider:          "openrouter",
		ProfileRevision:   llm.V9AggregateProfileRevision,
		Model:             llm.V7HarnessModel,
	}
	for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV10} {
		if err := requireTokenAccounting(snapshot, version, "full"); err != nil {
			t.Fatalf("v%d rejected reviewed variable-reasoning profile: %v", version, err)
		}
	}

	for _, profile := range []string{
		"openrouter-route-a471cd87ae7df5b9-v1", // fixed-medium v7/v8
		"openrouter-route-0123456789abcdef-v1", // merely well formed
		"local-v9-variable-reasoning",
	} {
		wrong := snapshot
		wrong.ProfileRevision = profile
		for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV10} {
			if err := requireTokenAccounting(wrong, version, "full"); err == nil || !strings.Contains(err.Error(), "profile") {
				t.Fatalf("v%d accepted unreviewed profile %q: %v", version, profile, err)
			}
		}
	}

	// Historical contracts deliberately keep their old acceptance rules.
	fixed := snapshot
	fixed.ProfileRevision = "openrouter-route-a471cd87ae7df5b9-v1"
	for _, version := range []int{protocol.BenchVersionV7, protocol.BenchVersionV8} {
		if err := requireTokenAccounting(fixed, version, "full"); err != nil {
			t.Fatalf("v%d fixed-medium compatibility rejected: %v", version, err)
		}
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

// gatedZeroUseVersions are the bench versions whose zero-use interval is
// settled by the signed route proof rather than rejected outright.
var gatedZeroUseVersions = []int{
	protocol.BenchVersionV9,
	protocol.BenchVersionV10,
	protocol.BenchVersionV11,
}

func TestV9RequiresSignedRouteProofForACompleteUnusedChatLane(t *testing.T) {
	unused := protocol.TokenUsage{
		Status:            "complete",
		AccountingVersion: 2,
		Provider:          "openrouter",
		ProfileRevision:   "openrouter-route-0123456789abcdef-v1",
		Model:             llm.V7HarnessModel,
	}
	// The proof requirement itself is unchanged: an unproven zero-use interval
	// is still never scored 0.00. What changed is WHO it is charged to.
	for _, version := range gatedZeroUseVersions {
		err := requireCompleteV7Usage(version, unused, relayExecutionSummary{})
		if !errors.Is(err, errRouteProofUnavailable) {
			t.Fatalf("v%d unproven zero use error = %v, want route-proof sentinel", version, err)
		}
		if errors.Is(err, errAgentModelUseMissing) {
			t.Fatalf("v%d blamed the agent for an unfinished platform probe", version)
		}
	}
	if err := requireCompleteV7Usage(protocol.BenchVersionV8, unused, relayExecutionSummary{}); !errors.Is(err, errAgentModelUseMissing) {
		t.Fatalf("v8 behavior changed: %v", err)
	}
}

// TestUnprovenZeroUseIsAlwaysAnUnfinishedChallenge pins the contrapositive that
// makes the reclassification safe: on a gated version there is no zero-use
// interval that a COMPLETED route challenge leaves ambiguous. Every unproven
// case has routed == 0 AND attempts < 2, i.e. the platform stopped asking. If
// this ever fails, some genuinely ambiguous state exists and the blame split
// has to be revisited rather than the assertion relaxed.
func TestUnprovenZeroUseIsAlwaysAnUnfinishedChallenge(t *testing.T) {
	for attempts := uint64(0); attempts <= 4; attempts++ {
		for routed := uint64(0); routed <= attempts+1; routed++ {
			execution := relayExecutionSummary{
				RouteProbeAttempts: attempts,
				RouteProbeRouted:   routed,
			}
			if execution.provesV9RouteDisposition() {
				continue
			}
			// Every unproven state is either an unfinished challenge (ours)
			// or incoherent counters (untrustworthy). There is no third kind,
			// and in particular no state where a COMPLETED, coherent challenge
			// left the interval ambiguous.
			coherent := routed <= attempts
			if coherent && !execution.routeChallengeUnfinished() {
				t.Fatalf(
					"unproven with attempts=%d routed=%d: a completed challenge was ambiguous",
					attempts, routed,
				)
			}
		}
	}
}

// TestRouteProofGapRetriesWithoutMintingAGrant keeps both halves of the fix
// honest: the gap must be recoverable (retryable) but must NOT reuse the
// no-fault sandbox-infrastructure code set, which raises the attempt cap and
// re-leases without bound.
func TestRouteProofGapRetriesWithoutMintingAGrant(t *testing.T) {
	failure := relayFinalizeFailure(errRouteProofUnavailable)
	if failure.Kind != "validator_infrastructure" || !failure.Retryable {
		t.Fatalf("route proof gap classification = %+v, want retryable infrastructure", failure)
	}
	if failure.Code == "model_inference_required" {
		t.Fatal("route proof gap still lands in the terminal agent bin")
	}
	agent := relayFinalizeFailure(errAgentModelUseMissing)
	if agent.Retryable || agent.Code != "model_inference_required" {
		t.Fatalf("v8 agent classification regressed: %+v", agent)
	}
}

func TestV9SignedRouteProofDistinguishesGenuineZeroUse(t *testing.T) {
	unused := protocol.TokenUsage{
		Status:            "complete",
		AccountingVersion: 2,
		Provider:          "openrouter",
		ProfileRevision:   "openrouter-route-0123456789abcdef-v1",
		Model:             llm.V7HarnessModel,
	}
	// wantPlatformGap marks the unproven cases the platform caused by not
	// finishing its own challenge. Incoherent counters stay on the agent bin:
	// self-contradicting evidence must fail closed, or forging it would buy
	// retries.
	tests := []struct {
		name            string
		execution       relayExecutionSummary
		wantProof       bool
		wantPlatformGap bool
	}{
		{name: "never probed", execution: relayExecutionSummary{}, wantPlatformGap: true},
		{name: "one selector missed", execution: relayExecutionSummary{RouteProbeAttempts: 1}, wantPlatformGap: true},
		{name: "preferred selector routed", execution: relayExecutionSummary{RouteProbeAttempts: 1, RouteProbeRouted: 1}, wantProof: true},
		{name: "both selectors missed", execution: relayExecutionSummary{RouteProbeAttempts: 2}, wantProof: true},
		{name: "compatibility selector routed", execution: relayExecutionSummary{RouteProbeAttempts: 2, RouteProbeRouted: 1}, wantProof: true},
		{name: "impossible routed count", execution: relayExecutionSummary{RouteProbeRouted: 1}},
		{name: "routed exceeds attempts", execution: relayExecutionSummary{RouteProbeAttempts: 1, RouteProbeRouted: 2}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := test.execution.provesV9RouteDisposition(); got != test.wantProof {
				t.Fatalf("provesV9RouteDisposition() = %t, want %t", got, test.wantProof)
			}
			for _, version := range gatedZeroUseVersions {
				err := requireCompleteV7Usage(version, unused, test.execution)
				if test.wantProof {
					if err != nil {
						t.Fatalf("proved v%d zero rejected: %v", version, err)
					}
					continue
				}
				// Unproven either way: the zero is still never scored 0.00.
				if err == nil {
					t.Fatalf("unproved v%d zero was accepted", version)
				}
				if test.wantPlatformGap {
					if !errors.Is(err, errRouteProofUnavailable) {
						t.Fatalf("v%d unfinished challenge = %v, want route-proof sentinel", version, err)
					}
					continue
				}
				if !errors.Is(err, errAgentModelUseMissing) {
					t.Fatalf("v%d incoherent counters = %v, want model-use sentinel", version, err)
				}
				if failure := relayFinalizeFailure(err); failure.Retryable {
					t.Fatalf("v%d incoherent counters became retryable: %+v", version, failure)
				}
			}
		})
	}

	if err := requireCompleteV7Usage(
		protocol.BenchVersionV8,
		unused,
		relayExecutionSummary{RouteProbeAttempts: 2},
	); !errors.Is(err, errAgentModelUseMissing) {
		t.Fatalf("v8 zero unexpectedly accepted by v9 proof: %v", err)
	}
}

// A `== BenchVersionV9` pin on the proven-zero branch meant every v10/v11
// zero-inference run ended as sandbox_failure / model_inference_required /
// retryable:false instead of an accepted 0.00. The submission then never
// finalized a v11 row, so the single-version ledger kept ranking the agent on
// its saturated v10 composite and the anti-template-fitting bench never
// measured it. Observed 2026-08-16 on three unrelated hotkeys, all holding an
// identical 0.997012 v10 score.
func TestProvenZeroInferenceIsAcceptedOnEveryGatedBenchVersion(t *testing.T) {
	unused := protocol.TokenUsage{
		Status:            "complete",
		AccountingVersion: 2,
		Provider:          "openrouter",
		ProfileRevision:   "openrouter-route-0123456789abcdef-v1",
		Model:             llm.V7HarnessModel,
	}
	proven := relayExecutionSummary{RouteProbeAttempts: 2, RouteProbeRouted: 1}
	for _, version := range gatedZeroUseVersions {
		if err := requireCompleteV7Usage(version, unused, proven); err != nil {
			t.Fatalf("v%d proven zero inference not accepted for scoring: %v", version, err)
		}
	}

	// The proof requirement itself must not have been dropped along the way: an
	// unproven zero is an adapter mismatch we cannot rule out, and scoring it
	// 0.00 would charge the miner for a platform fault. That guard is about
	// whether the interval is ACCEPTED, which is what this asserts. Who the
	// rejection is charged to is a separate question, covered by
	// TestV9SignedRouteProofDistinguishesGenuineZeroUse: a single missed probe
	// is an unfinished platform challenge, so it is rejected as a route-proof
	// gap rather than blamed on the agent.
	for _, version := range gatedZeroUseVersions {
		err := requireCompleteV7Usage(version, unused, relayExecutionSummary{RouteProbeAttempts: 1})
		if err == nil {
			t.Fatalf("v%d unproven zero was widened into an accepted score", version)
		}
		if !errors.Is(err, errRouteProofUnavailable) {
			t.Fatalf("v%d unfinished challenge = %v, want route-proof sentinel", version, err)
		}
		if failure := relayFinalizeFailure(err); failure.Code == "model_inference_required" {
			t.Fatalf("v%d unfinished challenge still blamed the agent: %+v", version, failure)
		}
	}

	// And an interval whose counters contradict themselves stays terminal on
	// the agent bin: corrupt evidence must never buy a retry.
	for _, version := range gatedZeroUseVersions {
		incoherent := relayExecutionSummary{RouteProbeAttempts: 1, RouteProbeRouted: 2}
		err := requireCompleteV7Usage(version, unused, incoherent)
		if !errors.Is(err, errAgentModelUseMissing) {
			t.Fatalf("v%d incoherent counters = %v, want model-use sentinel", version, err)
		}
		failure := relayFinalizeFailure(err)
		if failure.Kind != "sandbox_failure" || failure.Code != "model_inference_required" || failure.Retryable {
			t.Fatalf("v%d incoherent counter classification = %+v", version, failure)
		}
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

type postRunRouteFixture struct {
	server          *server
	harnessURL      string
	sessionID       string
	start           relayHealthSnapshot
	cooperate       *atomic.Bool
	upstreamHealthy *atomic.Bool
	harnessRuns     *atomic.Int64
}

func newPostRunRouteFixture(t *testing.T) postRunRouteFixture {
	t.Helper()
	upstreamHealthy := &atomic.Bool{}
	upstreamHealthy.Store(true)
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if !upstreamHealthy.Load() {
			http.Error(w, "provider unavailable", http.StatusBadGateway)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"probe","object":"chat.completion","created":1,"model":"openai/gpt-oss-20b","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"OK"}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}`))
	}))
	t.Cleanup(upstream.Close)

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(
		t,
		broker,
		prepared,
		proxyURL,
		"openrouter",
		"openrouter-route-0123456789abcdef-v1",
		llm.V7HarnessModel,
	)
	claimAndBindBrokerSession(
		t,
		broker,
		prepared["session_id"],
		"127.0.0.1",
		protocol.BenchVersionV8,
	)

	brokerMux := http.NewServeMux()
	brokerMux.HandleFunc("POST /v1/inference/{rest...}", broker.handle)
	brokerServer := httptest.NewServer(brokerMux)
	t.Cleanup(brokerServer.Close)

	cooperate := &atomic.Bool{}
	cooperate.Store(true)
	harnessRuns := &atomic.Int64{}
	harness := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/run" {
			http.NotFound(w, r)
			return
		}
		harnessRuns.Add(1)
		if cooperate.Load() {
			response, err := http.Post(
				brokerServer.URL+"/v1/inference/v1/chat/completions",
				"application/json",
				strings.NewReader(`{"model":"ignored","messages":[{"role":"user","content":"probe"}],"max_tokens":1}`),
			)
			if err != nil {
				t.Errorf("route probe could not call broker: %v", err)
			} else {
				_ = response.Body.Close()
				if response.StatusCode != http.StatusOK && upstreamHealthy.Load() {
					t.Errorf("route probe broker status = %d", response.StatusCode)
				}
			}
		}
		writeJSON(w, http.StatusOK, protocol.RunResponse{FinalText: "OK"})
	}))
	t.Cleanup(harness.Close)

	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	return postRunRouteFixture{
		server: &server{broker: broker}, harnessURL: harness.URL,
		sessionID: prepared["session_id"], start: start,
		cooperate: cooperate, upstreamHealthy: upstreamHealthy,
		harnessRuns: harnessRuns,
	}
}

func TestPostRunRouteReprobeSkipsSessionlessDirectHarness(t *testing.T) {
	err := (&server{}).requireCompleteModelUsageAfterRun(
		context.Background(),
		"http://unused.invalid",
		"",
		relayHealthSnapshot{},
		catalog.CatalogForVersion(protocol.BenchVersionV8),
		protocol.BenchVersionV8,
		protocol.TokenUsage{Status: "complete"},
		relayExecutionSummary{},
	)
	if !errors.Is(err, errAgentModelUseMissing) {
		t.Fatalf("sessionless all-zero route error = %v, want original model-use sentinel", err)
	}
	failure := relayFinalizeFailure(err)
	if failure.Kind != "sandbox_failure" || failure.Code != "model_inference_required" || failure.Retryable {
		t.Fatalf("sessionless all-zero route failure = %+v", failure)
	}
}

func TestPostRunRouteReprobeKeepsHealthyAllZeroV8AgentAttributable(t *testing.T) {
	fixture := newPostRunRouteFixture(t)
	ctx := runner.TrustSandbox(context.Background())
	tools := catalog.CatalogForVersion(protocol.BenchVersionV8)
	afterPreflight, routed, err := fixture.server.probeHarnessModelRoute(
		ctx, fixture.harnessURL, fixture.sessionID, fixture.start, tools, protocol.BenchVersionV8,
	)
	if err != nil || !routed {
		t.Fatalf("pre-run route probe = routed %t, error %v", routed, err)
	}

	err = fixture.server.requireCompleteModelUsageAfterRun(
		ctx,
		fixture.harnessURL,
		fixture.sessionID,
		afterPreflight,
		tools,
		protocol.BenchVersionV8,
		protocol.TokenUsage{Status: "complete"},
		relayExecutionSummary{},
	)
	if !errors.Is(err, errAgentModelUseMissing) {
		t.Fatalf("healthy all-zero route error = %v", err)
	}
	failure := relayFinalizeFailure(err)
	if failure.Kind != "sandbox_failure" || failure.Code != "model_inference_required" || failure.Retryable {
		t.Fatalf("healthy all-zero route failure = %+v", failure)
	}
	if fixture.harnessRuns.Load() != 2 {
		t.Fatalf("route probes = %d, want pre-run + post-run", fixture.harnessRuns.Load())
	}
}

func TestPostRunRouteReprobeDoesNotRewardSelectiveHarnessCooperation(t *testing.T) {
	fixture := newPostRunRouteFixture(t)
	ctx := runner.TrustSandbox(context.Background())
	tools := catalog.CatalogForVersion(protocol.BenchVersionV8)
	afterPreflight, routed, err := fixture.server.probeHarnessModelRoute(
		ctx, fixture.harnessURL, fixture.sessionID, fixture.start, tools, protocol.BenchVersionV8,
	)
	if err != nil || !routed {
		t.Fatalf("pre-run route probe = routed %t, error %v", routed, err)
	}
	fixture.cooperate.Store(false)

	err = fixture.server.requireCompleteModelUsageAfterRun(
		ctx,
		fixture.harnessURL,
		fixture.sessionID,
		afterPreflight,
		tools,
		protocol.BenchVersionV8,
		protocol.TokenUsage{Status: "complete"},
		relayExecutionSummary{},
	)
	if !errors.Is(err, errAgentModelUseMissing) {
		t.Fatalf("withheld post-run route error = %v, want model-use sentinel", err)
	}
	failure := relayFinalizeFailure(err)
	if failure.Kind != "sandbox_failure" || failure.Code != "model_inference_required" || failure.Retryable {
		t.Fatalf("withheld post-run route failure = %+v", failure)
	}
	afterWithholding, snapshotErr := fixture.server.broker.snapshot(fixture.sessionID)
	if snapshotErr != nil {
		t.Fatal(snapshotErr)
	}
	if afterWithholding.Requests != afterPreflight.Requests ||
		afterWithholding.InfrastructureFailures != afterPreflight.InfrastructureFailures {
		t.Fatalf("withheld probe changed broker health: before=%+v after=%+v", afterPreflight, afterWithholding)
	}
	if fixture.harnessRuns.Load() != 2 {
		t.Fatalf("route probes = %d, want pre-run + post-run", fixture.harnessRuns.Load())
	}
}

func TestPostRunRouteReprobePreservesInfrastructureRetryForObservedDegradation(t *testing.T) {
	fixture := newPostRunRouteFixture(t)
	ctx := runner.TrustSandbox(context.Background())
	tools := catalog.CatalogForVersion(protocol.BenchVersionV8)
	afterPreflight, routed, err := fixture.server.probeHarnessModelRoute(
		ctx, fixture.harnessURL, fixture.sessionID, fixture.start, tools, protocol.BenchVersionV8,
	)
	if err != nil || !routed {
		t.Fatalf("pre-run route probe = routed %t, error %v", routed, err)
	}
	fixture.upstreamHealthy.Store(false)

	err = fixture.server.requireCompleteModelUsageAfterRun(
		ctx,
		fixture.harnessURL,
		fixture.sessionID,
		afterPreflight,
		tools,
		protocol.BenchVersionV8,
		protocol.TokenUsage{Status: "complete"},
		relayExecutionSummary{},
	)
	if err == nil || errors.Is(err, errAgentModelUseMissing) {
		t.Fatalf("degraded post-run route error = %v", err)
	}
	failure := relayFinalizeFailure(err)
	if failure.Kind != "validator_infrastructure" || failure.Code != "model_relay_unavailable" || !failure.Retryable {
		t.Fatalf("degraded post-run route failure = %+v", failure)
	}
	afterDegradation, snapshotErr := fixture.server.broker.snapshot(fixture.sessionID)
	if snapshotErr != nil {
		t.Fatal(snapshotErr)
	}
	if afterDegradation.Requests != afterPreflight.Requests+1 ||
		afterDegradation.InfrastructureFailures != afterPreflight.InfrastructureFailures+1 {
		t.Fatalf("degraded probe was not recorded by broker: before=%+v after=%+v", afterPreflight, afterDegradation)
	}
	if fixture.harnessRuns.Load() != 2 {
		t.Fatalf("route probes = %d, want pre-run + post-run", fixture.harnessRuns.Load())
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
