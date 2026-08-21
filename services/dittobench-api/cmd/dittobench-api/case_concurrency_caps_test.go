package main

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestSourceConcurrencyForFloorsAtSerialCap(t *testing.T) {
	if got := sourceConcurrencyFor(0); got != brokerPerSourceConcurrency {
		t.Fatalf("zero = %d, want floor %d", got, brokerPerSourceConcurrency)
	}
	if got := sourceConcurrencyFor(brokerPerSourceConcurrency - 1); got != brokerPerSourceConcurrency {
		t.Fatalf("below floor = %d, want %d", got, brokerPerSourceConcurrency)
	}
	if got := sourceConcurrencyFor(maxBenchmarkCaseConcurrency); got != maxBenchmarkCaseConcurrency {
		t.Fatalf("max = %d, want %d", got, maxBenchmarkCaseConcurrency)
	}
}

func TestRegisterToolRouteSizesSlotsToCaseConcurrency(t *testing.T) {
	broker := newInferenceBroker(1)
	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	wide, stopWide, err := broker.registerToolRoute(handler, "192.0.2.20", false, true, "", 16)
	if err != nil {
		t.Fatal(err)
	}
	defer stopWide()
	legacy, stopLegacy, err := broker.registerToolWithProvenance(handler, "192.0.2.21", false, true, "")
	if err != nil {
		t.Fatal(err)
	}
	defer stopLegacy()
	broker.mu.RLock()
	wideSlots, legacySlots := cap(broker.tools[wide.id].slots), cap(broker.tools[legacy.id].slots)
	broker.mu.RUnlock()
	if wideSlots != 16 {
		t.Fatalf("case_concurrency 16 tool slots = %d", wideSlots)
	}
	if legacySlots != brokerPerSourceConcurrency {
		t.Fatalf("default tool slots = %d, want %d", legacySlots, brokerPerSourceConcurrency)
	}
}

func TestConfigureCaseConcurrencyRaisesChatAdmission(t *testing.T) {
	broker := newInferenceBroker(1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"role":"assistant","content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}`))
	}))
	defer upstream.Close()
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSession(t, broker, prepared, proxyURL)
	sessionID := prepared["session_id"]
	runID := claimAndBindBrokerSession(t, broker, sessionID, "192.0.2.70", protocol.BenchVersionV6)

	if broker.configureCaseConcurrency(sessionID, "not-the-bound-run", 8) {
		t.Fatal("foreign run must not reconfigure the session")
	}
	if !broker.configureCaseConcurrency(sessionID, runID, 8) {
		t.Fatal("bound run should reconfigure the session")
	}
	broker.mu.RLock()
	session := broker.sessions[sessionID]
	broker.mu.RUnlock()
	if got := session.chatConcurrencyLimit(); got != 8 {
		t.Fatalf("chat limit = %d, want 8", got)
	}

	chat := func(inFlight int) int {
		session.mu.Lock()
		session.inFlight = inFlight
		session.mu.Unlock()
		request := httptest.NewRequest(http.MethodPost, "/v1/inference/v1/chat/completions",
			http.NoBody)
		request.RemoteAddr = "192.0.2.70:4321"
		request.SetPathValue("rest", "v1/chat/completions")
		recorder := httptest.NewRecorder()
		broker.handle(recorder, request)
		return recorder.Code
	}
	// The serial-era cap (4 in flight) no longer rejects an 8-way run.
	if code := chat(brokerPerSourceConcurrency); code == http.StatusTooManyRequests {
		t.Fatalf("in-flight %d under limit 8 was rejected for capacity", brokerPerSourceConcurrency)
	}
	if code := chat(8); code != http.StatusTooManyRequests {
		t.Fatalf("in-flight 8 at limit 8 status = %d, want 429", code)
	}
	// Lowering below the floor never shrinks admission under the serial cap.
	if !broker.configureCaseConcurrency(sessionID, runID, 1) {
		t.Fatal("reconfigure to 1 failed")
	}
	if got := session.chatConcurrencyLimit(); got != brokerPerSourceConcurrency {
		t.Fatalf("chat limit after 1 = %d, want floor %d", got, brokerPerSourceConcurrency)
	}
}
