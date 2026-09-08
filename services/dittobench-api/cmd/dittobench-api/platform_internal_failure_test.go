package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/store"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Exercise the authenticated HTTP boundary, broker accounting, and the actual
// scorer finalization entry point. A later success must not hide Platform's 500.
func TestPlatformInternalFailureInvalidatesCompletedRun(t *testing.T) {
	var calls atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if calls.Add(1) == 1 {
			// Even a misleading generation marker cannot turn a Platform 500 into
			// miner-owned repair work. Old relays need not emit any new header.
			w.Header().Set(minerRecoverableFailureHeader, minerRecoverableGeneration)
			http.Error(w, `{"error_code":3000,"message":"internal server error"}`, http.StatusInternalServerError)
			return
		}
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":2,"completion_tokens":1},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", llm.V9AggregateProfileRevision, llm.V7HarnessModel)
	id := prepared["session_id"]
	claimAndBindBrokerSession(t, broker, id, "192.0.2.31", protocol.BenchVersionV12)
	start, err := broker.snapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []int{http.StatusBadGateway, http.StatusOK} {
		request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b","messages":[{"role":"user","content":"Reply OK"}]}`))
		request.RemoteAddr = "192.0.2.31:4321"
		request.SetPathValue("rest", "v1/chat/completions")
		recorder := httptest.NewRecorder()
		broker.handle(recorder, request)
		if recorder.Code != want {
			t.Fatalf("status=%d want=%d body=%s", recorder.Code, want, recorder.Body.String())
		}
	}
	end, err := broker.snapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 2 || end.PlatformInternalFailures != 1 || end.Successes != 1 || end.InfrastructureFailures != 1 || end.MinerRecoverableFailures != 0 {
		t.Fatalf("unexpected accounting or automatic redelivery: calls=%d snapshot=%+v", calls.Load(), end)
	}
	execution, err := relayExecutionSince(start, end)
	if err != nil || execution.PlatformInternalFailures != 1 {
		t.Fatalf("lost evidence: %+v %v", execution, err)
	}
	raw, _ := json.Marshal(execution)
	if !bytes.Contains(raw, []byte(`"platform_internal_failures":1`)) {
		t.Fatalf("transcript lost evidence: %s", raw)
	}
	s := &server{broker: broker, store: store.New()}
	s.store.Create("platform-failure", "run_size", store.StatusRunning, 1, 1)
	if _, _, ok := s.relayRunResult(context.Background(), "platform-failure", start, "", id); ok {
		t.Fatal("scorer accepted a platform-damaged run")
	}
	job, _ := s.store.Get("platform-failure")
	if job.Status != store.StatusFailed || job.Failure == nil || job.Failure.Kind != "validator_infrastructure" || job.Failure.Code != "model_relay_unavailable" || !job.Failure.Retryable {
		t.Fatalf("wrong failure ownership: %+v", job)
	}
}

func TestPlatformInternalFailureCounterGuards(t *testing.T) {
	start := relayHealthSnapshot{Requests: 10, Successes: 9, PlatformInternalFailures: 1}
	end := start
	end.Requests += 2
	end.Successes++
	if err := relayCompletedSince(start, end); err != nil {
		t.Fatalf("old failure contaminated new interval: %v", err)
	}
	end.PlatformInternalFailures++
	end.GrantDenials++
	end.GrantAgentDeclines++
	err := relayCompletedSince(start, end)
	if err == nil || !strings.Contains(err.Error(), "platform returned 1 internal error") || relayFinalizeFailure(err).Kind != "validator_infrastructure" {
		t.Fatalf("platform failure must win mixed attribution: %v", err)
	}
	end = start
	end.PlatformInternalFailures--
	if _, err := relayExecutionSince(start, end); err == nil {
		t.Fatal("counter rollback accepted")
	}
	end = start
	end.PlatformInternalFailures++
	if _, err := relayExecutionSince(start, end); err == nil {
		t.Fatal("failure count exceeding requests accepted")
	}
	raw, _ := json.Marshal(relayExecutionSummary{})
	if bytes.Contains(raw, []byte("platform_internal_failures")) {
		t.Fatalf("clean transcript bytes changed: %s", raw)
	}
}

func TestPlatformInternalFailureRequiresPlatformTransport(t *testing.T) {
	for _, route := range []string{"legacy", "trusted_reader"} {
		t.Run(route, func(t *testing.T) {
			handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				// A non-Platform handler cannot manufacture this diagnostic by copying
				// an internal-error envelope or a private header.
				w.Header().Set(minerRecoverableFailureHeader, "platform_internal_failure")
				http.Error(w, `{"error_code":3000,"message":"internal server error"}`, 500)
			})
			upstream := httptest.NewTLSServer(handler)
			defer upstream.Close()
			broker := newInferenceBroker(1)
			proxyURL := configureBrokerUpstream(broker, upstream)
			prepared := prepareBrokerSession(t, broker)
			activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", llm.V9AggregateProfileRevision, llm.V7HarnessModel)
			id := prepared["session_id"]
			claimAndBindBrokerSession(t, broker, id, "192.0.2.31", protocol.BenchVersionV12)
			session := broker.sessions[id]
			if route == "legacy" {
				session.legacyGateway = upstream.URL
			} else {
				session.trustedChatHandler = handler
			}
			request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b","messages":[{"role":"user","content":"Reply OK"}]}`))
			request.RemoteAddr = "192.0.2.31:4321"
			recorder := httptest.NewRecorder()
			broker.proxy(recorder, request, session, 0)
			got, err := broker.snapshot(id)
			if err != nil || got.PlatformInternalFailures != 0 || got.InfrastructureFailures != 1 || recorder.Code != 502 {
				t.Fatalf("transport misattributed as Platform: snapshot=%+v err=%v status=%d", got, err, recorder.Code)
			}
		})
	}
}
