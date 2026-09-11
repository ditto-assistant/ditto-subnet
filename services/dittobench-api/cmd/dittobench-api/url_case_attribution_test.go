package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

type caseURLRoute struct {
	name     string
	base     func(token string) string
	host     func(token string) string
	protect  bool
	sourceIP string
}

var caseURLRoutes = []caseURLRoute{
	{
		name:     "legacy source-only",
		base:     func(string) string { return "http://host.docker.internal:11436/v1/inference" },
		host:     func(string) string { return "host.docker.internal:11436" },
		sourceIP: "192.0.2.10",
	},
	{
		name: "capability host",
		base: func(token string) string {
			return "http://" + brokerCapabilityHostPrefix + token + brokerCapabilityHostSuffix + ":11436/v1/inference"
		},
		host: func(token string) string {
			return brokerCapabilityHostPrefix + token + brokerCapabilityHostSuffix + ":11436"
		},
		protect:  true,
		sourceIP: "192.0.2.11",
	},
}

type barrierRelay struct {
	arrived  chan traceContext
	mu       sync.Mutex
	gates    map[string]chan struct{}
	opened   map[string]bool
	seen     map[string]int
	shutdown chan struct{}
	server   *httptest.Server
}

func newBarrierRelay(t *testing.T, n int) *barrierRelay {
	t.Helper()
	relay := &barrierRelay{arrived: make(chan traceContext, n*4), gates: map[string]chan struct{}{}, opened: map[string]bool{},
		seen: map[string]int{}, shutdown: make(chan struct{})}
	relay.server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var tc traceContext
		if err := json.Unmarshal([]byte(r.Header.Get(traceContextHeader)), &tc); err != nil {
			t.Errorf("trace context is not JSON: %v", err)
		}
		if relay.first(tc.CaseID) {
			relay.arrived <- tc
			select {
			case <-relay.gate(tc.CaseID):
			case <-relay.shutdown:
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[]}`))
	}))
	t.Cleanup(relay.server.Close)
	return relay
}

func (b *barrierRelay) gate(caseID string) chan struct{} {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.gates[caseID] == nil {
		b.gates[caseID] = make(chan struct{})
	}
	return b.gates[caseID]
}

func (b *barrierRelay) first(caseID string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.seen[caseID]++
	return b.seen[caseID] == 1
}

func (b *barrierRelay) open(caseID string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.openLocked(caseID)
}

func (b *barrierRelay) release() {
	b.mu.Lock()
	defer b.mu.Unlock()
	for caseID := range b.gates {
		b.openLocked(caseID)
	}
	select {
	case <-b.shutdown:
	default:
		close(b.shutdown)
	}
}

func (b *barrierRelay) openLocked(caseID string) {
	if b.gates[caseID] == nil {
		b.gates[caseID] = make(chan struct{})
	}
	if !b.opened[caseID] {
		close(b.gates[caseID])
		b.opened[caseID] = true
	}
}

func (b *barrierRelay) await(n int) ([]traceContext, error) {
	var got []traceContext
	for i := 0; i < n; i++ {
		select {
		case tc := <-b.arrived:
			got = append(got, tc)
		case <-time.After(5 * time.Second):
			return got, fmt.Errorf("only %d of %d inference calls reached the relay; requests are not overlapping", len(got), n)
		}
	}
	return got, nil
}

func TestCaseURLsAttributeGenuinelyOverlappingRuns(t *testing.T) {
	for _, route := range caseURLRoutes {
		t.Run(route.name, func(t *testing.T) {
			if err := runOverlapScenario(t, route, false); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestOverlapGateReportsPromptlyWhenRequestsSerialize(t *testing.T) {
	started := time.Now()
	err := runOverlapScenario(t, caseURLRoutes[0], true)
	if err == nil {
		t.Fatal("serialized inference must be reported as a missing overlap")
	}
	if !strings.Contains(err.Error(), "not overlapping") {
		t.Fatalf("serialized inference reported as %q, want a missing-overlap report", err)
	}
	if elapsed := time.Since(started); elapsed > 30*time.Second {
		t.Fatalf("the gate took %v to report; it must not hang when it fires", elapsed)
	}
}

func runOverlapScenario(t *testing.T, route caseURLRoute, serialize bool) error {
	{
		const n = 4
		cases := []string{"case-A", "case-B", "case-C", "case-D"}
		relay := newBarrierRelay(t, n)

		broker := newInferenceBroker(n * 2)
		proxyURL := configureBrokerUpstream(broker, relay.server)
		prepared := prepareBrokerSession(t, broker)
		activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
			llm.V9AggregateProfileRevision, llm.HarnessModelForVersion(protocol.BenchVersionV10))
		sessionID := prepared["session_id"]
		capability := ""
		if route.protect {
			runID := claimBrokerSession(t, broker, sessionID, protocol.BenchVersionV10)
			token, err := broker.installSourceCapability(sessionID, runID)
			if err != nil {
				return err
			}
			if !broker.bindSourceCapability(sessionID, runID, route.sourceIP, token) {
				return errors.New("bind protected source")
			}
			capability = token
		} else {
			claimAndBindBrokerSession(t, broker, sessionID, route.sourceIP, protocol.BenchVersionV10)
		}
		base := route.base(capability)
		broker.setHarnessBase(sessionID, base)

		callBroker := func(caseURL, claim string) (int, error) {
			if !strings.HasPrefix(caseURL, base+"/cases/") {
				return 0, fmt.Errorf("case url %q is not under the session base %q", caseURL, base)
			}
			rest := strings.TrimPrefix(caseURL, base+"/") + "/v1/chat/completions"
			request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/"+rest,
				bytes.NewBufferString(`{"model":"qwen/qwen3-32b","max_tokens":32}`))
			request.RemoteAddr = route.sourceIP + ":4321"
			request.Host = route.host(capability)
			request.SetPathValue("rest", rest)
			if claim != "" {
				request.Header.Set(harnessCaseHeader, claim)
			}
			recorder := httptest.NewRecorder()
			broker.handle(recorder, request)
			return recorder.Code, nil
		}

		var urlMu sync.Mutex
		var serialMu sync.Mutex
		urls := map[string]string{}
		harness := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/run" {
				http.NotFound(w, r)
				return
			}
			var req protocol.RunRequest
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				t.Errorf("decode run request: %v", err)
			}
			urlMu.Lock()
			urls[req.CaseID] = req.InferenceBaseURL
			urlMu.Unlock()
			if serialize {
				serialMu.Lock()
				defer serialMu.Unlock()
			}
			if code, err := callBroker(req.InferenceBaseURL, ""); err != nil || code != http.StatusOK {
				t.Errorf("case %s inference through its url: code=%d err=%v", req.CaseID, code, err)
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"final_text":"ok"}`))
		}))
		defer harness.Close()
		defer relay.release()

		srv := &server{broker: broker}
		done := make(chan error, n)
		for _, caseID := range cases {
			go func(caseID string) {
				_, execution, err := srv.runCaseWithModelAttribution(
					runner.TrustSandbox(context.Background()), sessionID, harness.URL,
					caseID, "question", nil, runner.CaseOptions{BenchVersion: protocol.BenchVersionV10},
				)
				if err != nil {
					done <- fmt.Errorf("%s: %w", caseID, err)
					return
				}
				if execution.ModelAttributionComplete || execution.ModelInferenceObserved {
					done <- fmt.Errorf("%s opened per-case attribution: %+v", caseID, execution)
					return
				}
				done <- nil
			}(caseID)
		}

		seen, awaitErr := relay.await(n)
		if awaitErr != nil {
			return awaitErr
		}
		byCase := map[string]traceContext{}
		for _, tc := range seen {
			if tc.CaseSource != "url" || !tc.CaseVerified {
				return fmt.Errorf("case %q attributed as %q verified=%v, want url/true", tc.CaseID, tc.CaseSource, tc.CaseVerified)
			}
			if tc.CaseGeneration != 0 {
				return fmt.Errorf("case %q ran at generation %d, want 0", tc.CaseID, tc.CaseGeneration)
			}
			if !slices.Contains(tc.CasesInFlight, tc.CaseID) {
				return fmt.Errorf("case %q is missing from its own in-flight set %v", tc.CaseID, tc.CasesInFlight)
			}
			byCase[tc.CaseID] = tc
		}
		if len(byCase) != n {
			return fmt.Errorf("overlapping runs resolved to %d distinct cases, want %d", len(byCase), n)
		}

		broker.mu.RLock()
		session := broker.sessions[sessionID]
		broker.mu.RUnlock()
		session.mu.Lock()
		generation, activeCase, snapshots := session.activeCaseGeneration, session.activeCaseID, len(session.caseSnapshots)
		inFlight := len(session.runCases)
		session.mu.Unlock()
		if inFlight != n {
			return fmt.Errorf("session holds %d cases in flight, want %d", inFlight, n)
		}
		if generation != 0 || activeCase != "" || snapshots != 0 {
			return fmt.Errorf("url attribution opened an exclusive window: generation=%d case=%q snapshots=%d",
				generation, activeCase, snapshots)
		}

		relay.open("case-A")
		if err := <-done; err != nil {
			return err
		}
		urlMu.Lock()
		urlB := urls["case-B"]
		urlMu.Unlock()
		code, err := callBroker(urlB, "")
		if err != nil {
			return err
		}
		if code != http.StatusOK {
			return fmt.Errorf("case-B url returned %d after case-A completed, want 200", code)
		}

		for _, caseID := range cases[1:] {
			relay.open(caseID)
		}
		for i := 1; i < n; i++ {
			if err := <-done; err != nil {
				return err
			}
		}

		urlMu.Lock()
		finished := urls["case-C"]
		urlMu.Unlock()
		if code, err := callBroker(finished, ""); err != nil || code != http.StatusUnauthorized {
			return fmt.Errorf("a finished case url returned %d (err=%v), want 401", code, err)
		}
	}
	return nil
}

func claimBrokerSession(t *testing.T, broker *inferenceBroker, sessionID string, benchVersion int) string {
	t.Helper()
	broker.mu.RLock()
	session := broker.sessions[sessionID]
	broker.mu.RUnlock()
	if session == nil {
		t.Fatal("prepared broker session disappeared")
	}
	session.mu.Lock()
	identity := brokerTicketIdentity{
		GrantID: session.grantID, AgentID: session.ticketAgentID,
		SlotID: session.ticketSlotID, TicketDeadline: session.ticketDeadline,
	}
	session.mu.Unlock()
	runID := uuid.NewString()
	if !broker.claimRun(sessionID, runID, identity, benchVersion) {
		t.Fatal("failed to claim prepared broker session")
	}
	return runID
}
