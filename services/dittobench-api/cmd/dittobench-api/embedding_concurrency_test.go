package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The whole point of this change is that hosted v7 embedding requests stop
// being serialised. Every assertion below is made against OBSERVED simultaneity
// at the upstream -- a barrier that only opens once N requests are genuinely in
// flight at the same moment -- rather than against a constant. A test that read
// the limit back would have passed just as happily while the real end-to-end
// concurrency stayed at one, which is exactly the failure this file exists to
// rule out.

// concurrencyProbe reports the greatest number of upstream requests that were
// ever in flight simultaneously, and can hold requests open until a target
// count is reached.
type concurrencyProbe struct {
	mu       sync.Mutex
	inFlight int
	peak     int
	target   int
	reached  chan struct{}
	once     sync.Once
}

func newConcurrencyProbe(target int) *concurrencyProbe {
	return &concurrencyProbe{target: target, reached: make(chan struct{})}
}

func (p *concurrencyProbe) enter() {
	p.mu.Lock()
	p.inFlight++
	if p.inFlight > p.peak {
		p.peak = p.inFlight
	}
	atTarget := p.inFlight >= p.target
	p.mu.Unlock()
	if atTarget {
		p.once.Do(func() { close(p.reached) })
	}
}

func (p *concurrencyProbe) leave() {
	p.mu.Lock()
	p.inFlight--
	p.mu.Unlock()
}

func (p *concurrencyProbe) peakSeen() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.peak
}

// hold blocks until the target simultaneity is observed, or gives up. Giving up
// is the signal that the lane is narrower than the test asked for: a serialised
// lane can never fill the barrier because request two is not admitted until
// request one has returned.
func (p *concurrencyProbe) hold(timeout time.Duration) {
	select {
	case <-p.reached:
	case <-time.After(timeout):
	}
}

// TestHostedV7EmbeddingsActuallyRunConcurrently is the empirical proof that
// this change is not a no-op.
//
// Three limits sat in series and all three were exactly one: the platform's
// embedding_per_ticket_concurrency, this broker's per-session lane, and v7 case
// concurrency. Raising any subset of them changes nothing measurable, so the
// assertion here is simultaneity at the upstream, not a configured number.
func TestHostedV7EmbeddingsActuallyRunConcurrently(t *testing.T) {
	const want = 6
	probe := newConcurrencyProbe(want)
	vector := make([]float64, embeddingDimensions)
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		probe.enter()
		// Hold every request open until `want` of them overlap. If the lane
		// serialises, this never resolves and the test fails on the peak below
		// rather than hanging forever.
		probe.hold(3 * time.Second)
		probe.leave()
		writeJSON(w, http.StatusOK, map[string]any{
			"object": "list", "model": hostedEmbeddingModel,
			"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
			"usage": map[string]int{"prompt_tokens": 4, "total_tokens": 4},
		})
	}))
	defer upstream.Close()

	originalLane := v8EmbeddingSessionConcurrency
	v8EmbeddingSessionConcurrency = want
	t.Cleanup(func() { v8EmbeddingSessionConcurrency = originalLane })

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
		"openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.150", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)

	var wg sync.WaitGroup
	codes := make([]int, want)
	for i := range codes {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			codes[i] = callEmbedding(broker, "192.0.2.150", "hosted text").Code
		}(i)
	}
	wg.Wait()

	for i, code := range codes {
		if code != http.StatusOK {
			t.Fatalf("concurrent embedding %d status=%d, want 200", i, code)
		}
	}
	if peak := probe.peakSeen(); peak != want {
		t.Fatalf("peak simultaneous hosted embeddings = %d, want %d "+
			"(the lane is still serialising; raising the platform limit alone is a no-op)", peak, want)
	}
}

// TestHostedV7EmbeddingLaneStillHasACeiling is the other half of the contract.
// Widening the lane must not remove it: a harness that floods the endpoint is
// still refused, and refused with the retryable 429 the harness already knows.
func TestHostedV7EmbeddingLaneStillHasACeiling(t *testing.T) {
	const lane = 3
	probe := newConcurrencyProbe(lane)
	vector := make([]float64, embeddingDimensions)
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		probe.enter()
		probe.hold(3 * time.Second)
		probe.leave()
		writeJSON(w, http.StatusOK, map[string]any{
			"object": "list", "model": hostedEmbeddingModel,
			"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
			"usage": map[string]int{"prompt_tokens": 4, "total_tokens": 4},
		})
	}))
	defer upstream.Close()

	originalLane := v8EmbeddingSessionConcurrency
	v8EmbeddingSessionConcurrency = lane
	t.Cleanup(func() { v8EmbeddingSessionConcurrency = originalLane })

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
		"openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.151", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)

	var wg sync.WaitGroup
	var refused atomic.Int64
	// Two more than the lane admits, all launched together.
	for i := 0; i < lane+2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if callEmbedding(broker, "192.0.2.151", "hosted text").Code == http.StatusTooManyRequests {
				refused.Add(1)
			}
		}()
	}
	wg.Wait()

	if probe.peakSeen() > lane {
		t.Fatalf("peak simultaneous embeddings = %d, exceeds the lane of %d", probe.peakSeen(), lane)
	}
	if refused.Load() == 0 {
		t.Fatal("a flood past the lane ceiling was never refused; the lane is unbounded")
	}
}

// TestLocalOllamaEmbeddingsStayStrictlySerialised is the safety direction.
//
// Bench versions 2-6 still send their embeddings to the one Ollama container
// this host runs. That is a real, scarce, single-tenant resource and the reason
// the original limits existed. Nothing in this change may widen it -- "we will
// never run v6 again" is an operational intention, not a code guarantee, and
// the v2-v6 path still exists to be exercised.
func TestLocalOllamaEmbeddingsStayStrictlySerialised(t *testing.T) {
	probe := newConcurrencyProbe(2)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		probe.enter()
		// Ask for two-way overlap. On a correctly serialised lane this always
		// times out, which is the assertion.
		probe.hold(400 * time.Millisecond)
		probe.leave()
		writeTestEmbedding(w, 1)
	}))
	defer upstream.Close()

	// Widen the v7 knob as far as it goes. A local-path bound that moved with
	// it would be the exact bug this test is here to catch.
	originalLane := v8EmbeddingSessionConcurrency
	v8EmbeddingSessionConcurrency = 32
	t.Cleanup(func() { v8EmbeddingSessionConcurrency = originalLane })

	broker := newInferenceBroker(4)
	broker.embeddingURL = upstream.URL + embeddingAPIPath
	broker.client.Transport = upstream.Client().Transport
	admittedEmbeddingBrokerSession(t, broker, "192.0.2.152")

	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			callEmbedding(broker, "192.0.2.152", "local text")
		}()
	}
	wg.Wait()

	if peak := probe.peakSeen(); peak != 1 {
		t.Fatalf("peak simultaneous LOCAL ollama embeddings = %d, want 1 "+
			"(the v2-v6 lane must not widen with the hosted one)", peak)
	}
}

// TestPlatformCapacityBackpressureIsWaitedOutNotCountedAsAFault covers the
// change that makes the platform's concurrency board safe to lower under live
// runs. Without it, the emergency brake would itself be the outage: every
// throttled run would be discarded as if its lease had been revoked.
func TestPlatformCapacityBackpressureIsWaitedOutNotCountedAsAFault(t *testing.T) {
	vector := make([]float64, embeddingDimensions)
	var attempts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) <= 2 {
			w.Header().Set("Retry-After", "1")
			http.Error(w, "embedding lane is at capacity", http.StatusServiceUnavailable)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"object": "list", "model": hostedEmbeddingModel,
			"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
			"usage": map[string]int{"prompt_tokens": 4, "total_tokens": 4},
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
		"openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.153", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}

	if response := callEmbedding(broker, "192.0.2.153", "hosted text"); response.Code != http.StatusOK {
		t.Fatalf("backpressure was not absorbed: status=%d body=%s", response.Code, response.Body.String())
	}
	if attempts.Load() != 3 {
		t.Fatalf("deliveries=%d, want 3 (two waits then success)", attempts.Load())
	}

	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if err := relayDegradedSince(start, end); err != nil {
		t.Fatalf("waiting out backpressure failed the run closed: %v", err)
	}
	execution, err := relayExecutionSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	// The distinction that matters: a queue is not a fault. Booking these as
	// retries would spend the run's three real recovery attempts on a healthy
	// platform, and would make a throttled run indistinguishable from a
	// degraded one in the published transcript.
	if execution.EmbeddingRetries != 0 {
		t.Fatalf("capacity waits were booked as fault retries: %+v", execution)
	}
	if execution.InfrastructureFailures != 0 || execution.GrantDenials != 0 {
		t.Fatalf("capacity waits were booked as failures: %+v", execution)
	}
}

// TestPlatformCapacityBackpressureUsesTheRequestDeadlineRatherThanACount
// protects long rolling provider windows. Twelve Retry-After responses used to
// terminate a healthy ticket even though the request still had time remaining.
// The HTTP request context is already the authoritative 65-second bound, so the
// broker must keep waiting until that deadline or a successful response.
func TestPlatformCapacityBackpressureUsesTheRequestDeadlineRatherThanACount(t *testing.T) {
	vector := make([]float64, embeddingDimensions)
	var attempts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) <= 13 {
			w.Header().Set("Retry-After", "1")
			http.Error(w, "embedding provider rolling window is full", http.StatusServiceUnavailable)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"object": "list", "model": hostedEmbeddingModel,
			"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
			"usage": map[string]int{"prompt_tokens": 4, "total_tokens": 4},
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
		llm.V9AggregateProfileRevision, llm.HarnessModelForVersion(protocol.BenchVersionV9))
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.155", protocol.BenchVersionV9)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v9 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)

	if response := callEmbedding(broker, "192.0.2.155", "hosted text"); response.Code != http.StatusOK {
		t.Fatalf("rolling backpressure was not absorbed: status=%d body=%s", response.Code, response.Body.String())
	}
	if attempts.Load() != 14 {
		t.Fatalf("deliveries=%d, want 14 (thirteen waits then success)", attempts.Load())
	}
}

// TestPlatformFiveHundredThreeWithoutRetryAfterIsStillAFault guards the
// boundary. Only 503 WITH Retry-After is backpressure; a bare 503 is the
// platform giving up after its own provider loop, which #103 established as the
// transient fault class and which must keep its retry ledger entry.
func TestPlatformFiveHundredThreeWithoutRetryAfterIsStillAFault(t *testing.T) {
	vector := make([]float64, embeddingDimensions)
	var attempts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) == 1 {
			http.Error(w, "provider gave up", http.StatusServiceUnavailable)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"object": "list", "model": hostedEmbeddingModel,
			"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
			"usage": map[string]int{"prompt_tokens": 4, "total_tokens": 4},
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
		"openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.154", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}

	if response := callEmbedding(broker, "192.0.2.154", "hosted text"); response.Code != http.StatusOK {
		t.Fatalf("transient fault was not absorbed: status=%d", response.Code)
	}
	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	execution, err := relayExecutionSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if execution.EmbeddingRetries != 1 {
		t.Fatalf("a bare 503 stopped being a recorded fault retry: %+v", execution)
	}
}

// TestEndEmbeddingPhaseRevokesEveryInFlightCall is the isolation invariant.
//
// The phase used to track ONE (cancel, done) pair, which was sufficient while
// the lane admitted one call. With a wide lane, tracking a single pair would
// let a hostile harness keep background embedding calls alive past the end of
// its scored request simply by opening more than one -- each new call would
// overwrite the field the phase teardown reads.
func TestEndEmbeddingPhaseRevokesEveryInFlightCall(t *testing.T) {
	const inFlight = 4
	probe := newConcurrencyProbe(inFlight)
	release := make(chan struct{})
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		probe.enter()
		defer probe.leave()
		// Never answer. The only way these calls end is cancellation.
		select {
		case <-r.Context().Done():
		case <-release:
		}
		w.WriteHeader(http.StatusGatewayTimeout)
	}))
	defer upstream.Close()
	defer close(release)

	originalLane := v8EmbeddingSessionConcurrency
	v8EmbeddingSessionConcurrency = inFlight
	t.Cleanup(func() { v8EmbeddingSessionConcurrency = originalLane })

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
		"openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	sessionID := prepared["session_id"]
	runID := claimAndBindBrokerSession(t, broker, sessionID, "192.0.2.155", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(sessionID, runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}

	var wg sync.WaitGroup
	for i := 0; i < inFlight; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			callEmbedding(broker, "192.0.2.155", "hosted text")
		}()
	}
	probe.hold(3 * time.Second)
	if peak := probe.peakSeen(); peak != inFlight {
		t.Fatalf("only %d of %d calls reached the upstream; test cannot prove the invariant", peak, inFlight)
	}

	// endEmbeddingPhase must not return until every one of them is torn down.
	done := make(chan struct{})
	go func() {
		broker.endEmbeddingPhase(sessionID, runID)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("endEmbeddingPhase did not revoke and drain every in-flight call")
	}
	wg.Wait()

	broker.mu.RLock()
	session := broker.sessions[sessionID]
	broker.mu.RUnlock()
	session.mu.Lock()
	remaining, tracked := session.embeddingInFlight, len(session.embeddingCalls)
	failures, callerCancels := session.failures, session.callerCancels
	session.mu.Unlock()
	if remaining != 0 || tracked != 0 {
		t.Fatalf("lane leaked after phase teardown: inFlight=%d tracked=%d", remaining, tracked)
	}
	if failures != 0 || callerCancels != inFlight {
		t.Fatalf(
			"phase teardown accounting: failures=%d caller_cancellations=%d, want 0 and %d",
			failures, callerCancels, inFlight,
		)
	}
}

func TestEmbeddingRequestCancellationKeepsDeadlinesFailClosed(t *testing.T) {
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if !embeddingRequestCanceled(canceled) {
		t.Fatal("caller cancellation was not recognized")
	}

	deadline, cancelDeadline := context.WithDeadline(
		context.Background(), time.Now().Add(-time.Second),
	)
	defer cancelDeadline()
	if embeddingRequestCanceled(deadline) {
		t.Fatal("broker deadline was mistaken for caller cancellation")
	}
}

// TestLegacySessionsKeepTheSingleSlotLane pins the default. A session that
// never went through claimRun -- every prepareLegacy path -- must behave
// byte-identically to the bool this replaced.
func TestLegacySessionsKeepTheSingleSlotLane(t *testing.T) {
	session := &brokerSession{}
	if got := session.embeddingLaneLocked(); got != 1 {
		t.Fatalf("unset embedding lane = %d, want 1", got)
	}
	session.embeddingConcurrency = -3
	if got := session.embeddingLaneLocked(); got != 1 {
		t.Fatalf("negative embedding lane = %d, want 1", got)
	}
}

func TestRetryAfterDurationIsClamped(t *testing.T) {
	for _, testCase := range []struct {
		header string
		want   time.Duration
	}{
		{"", 250 * time.Millisecond},
		{"garbage", 250 * time.Millisecond},
		{"0", 250 * time.Millisecond},
		{"-4", 250 * time.Millisecond},
		{"1", time.Second},
		{"3", 3 * time.Second},
		// A hostile or misconfigured value can only cost this one call its
		// bounded pause, never the run's deadline.
		{"86400", 5 * time.Second},
	} {
		if got := retryAfterDuration(testCase.header); got != testCase.want {
			t.Fatalf("retryAfterDuration(%q) = %s, want %s", testCase.header, got, testCase.want)
		}
	}
}
