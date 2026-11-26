package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// costCeilingBudgets is armed request/token evidence plus a cost ceiling.
func costCeilingBudgets(ceiling uint64, mode string) brokerActivation {
	return brokerActivation{
		RequestBudget:          8192,
		TokenBudget:            75_000_000,
		EmbeddingRequestBudget: 100_000,
		EmbeddingTokenBudget:   1_000_000_000,
		MaxOutputTokens:        8192,
		CostCeilingMicrousd:    ceiling,
		CostCeilingMode:        mode,
	}
}

// costCeilingUpstream answers the first chat call 200 with the given ledger
// total (omitted when empty) and every later call 429/4110.
func costCeilingUpstream(t *testing.T, ledgerCost string) *httptest.Server {
	t.Helper()
	var calls atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if ledgerCost != "" {
			w.Header().Set(ledgerCostHeader, ledgerCost)
		}
		if calls.Add(1) == 1 {
			writeJSON(w, http.StatusOK, map[string]any{
				"usage":   map[string]int{"prompt_tokens": 1000, "completion_tokens": 100},
				"choices": []map[string]any{{"message": map[string]string{"content": "OK"}}},
			})
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"error_code":4110,"message":"cost ceiling exhausted"}`))
	}))
	t.Cleanup(server.Close)
	return server
}

func sendBrokerChat(t *testing.T, broker *inferenceBroker, sourceIP string, port int) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b","max_tokens":64}`))
	request.RemoteAddr = fmt.Sprintf("%s:%d", sourceIP, port)
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	return recorder
}

func TestCostCeilingDeclineConfirmedByLedgerEvidenceIsAgentFault(t *testing.T) {
	upstream := costCeilingUpstream(t, "250000")
	broker := newInferenceBroker(1)
	var walls []allowanceWall
	broker.terminalAgentFailure = func(_ string, wall allowanceWall) { walls = append(walls, wall) }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel,
		costCeilingBudgets(250_000, costCeilingModeEnforce))
	sourceIP := "192.0.2.210"
	_ = claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV8)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}

	first := sendBrokerChat(t, broker, sourceIP, 4321)
	if first.Code != http.StatusOK {
		t.Fatalf("crossing call status=%d, want 200", first.Code)
	}
	// The harness can pace itself against the ceiling.
	if got := first.Header().Get(harnessCostSpentHeader); got != "250000" {
		t.Fatalf("%s=%q", harnessCostSpentHeader, got)
	}
	if got := first.Header().Get(harnessCostCeilingHeader); got != "250000" {
		t.Fatalf("%s=%q", harnessCostCeilingHeader, got)
	}

	second := sendBrokerChat(t, broker, sourceIP, 4322)
	if second.Code != http.StatusBadGateway {
		t.Fatalf("declined call status=%d, want the same 502 a token wall produces", second.Code)
	}
	if len(walls) != 1 || walls[0] != allowanceWallCost {
		t.Fatalf("terminal callbacks=%v, want one cost wall", walls)
	}
	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if end.GrantDenials != 1 || end.GrantAgentDeclines != 1 || end.CostCeilingAgentDeclines != 1 {
		t.Fatalf("confirmed 4110 was not charged to the agent: %+v", end)
	}
	degraded := relayDegradedSince(start, end)
	if !errors.Is(degraded, errAgentInferenceDeclined) {
		t.Fatalf("degraded=%v, want agent decline", degraded)
	}
	failure := relayFinalizeFailure(degraded)
	if failure.Kind != "sandbox_failure" || failure.Code != "inference_allowance_exhausted" || failure.Retryable {
		t.Fatalf("confirmed 4110 run outcome: %+v", failure)
	}
	if failure.Diagnostics["wall"] != "cost" {
		t.Fatalf("diagnostics=%+v, want wall=cost", failure.Diagnostics)
	}
	summary, err := relayExecutionSince(start, end)
	if err != nil || summary.CostCeilingAgentDeclines != 1 {
		t.Fatalf("summary=%+v err=%v", summary, err)
	}
}

func TestUnconfirmedCostCeilingDeclineKeepsTheMinerAttempt(t *testing.T) {
	for _, testCase := range []struct {
		name       string
		ledgerCost string
		budgets    brokerActivation
		mismatch   bool
	}{
		{
			// Spent below the ceiling: the 4110 is impossible.
			name: "ledger below ceiling", ledgerCost: "100000",
			budgets: costCeilingBudgets(250_000, costCeilingModeEnforce), mismatch: true,
		},
		{
			// An old relay sends no ledger header: nothing confirms the spend.
			name: "no ledger header", ledgerCost: "",
			budgets: costCeilingBudgets(250_000, costCeilingModeEnforce), mismatch: true,
		},
		{
			// Shadow never declines, so a 4110 under it is not the agent's.
			name: "shadow mode", ledgerCost: "900000",
			budgets: costCeilingBudgets(250_000, costCeilingModeShadow),
		},
		{
			// An activation without a ceiling cannot confirm one.
			name: "no ceiling stamped", ledgerCost: "900000",
			budgets: costCeilingBudgets(0, ""),
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			upstream := costCeilingUpstream(t, testCase.ledgerCost)
			broker := newInferenceBroker(1)
			terminalFailures := 0
			broker.terminalAgentFailure = func(string, allowanceWall) { terminalFailures++ }
			proxyURL := configureBrokerUpstream(broker, upstream)
			prepared := prepareBrokerSession(t, broker)
			activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel, testCase.budgets)
			sourceIP := "192.0.2.211"
			_ = claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV8)
			start, err := broker.snapshot(prepared["session_id"])
			if err != nil {
				t.Fatal(err)
			}
			if first := sendBrokerChat(t, broker, sourceIP, 4321); first.Code != http.StatusOK {
				t.Fatalf("first call status=%d", first.Code)
			}
			if second := sendBrokerChat(t, broker, sourceIP, 4322); second.Code != http.StatusBadGateway {
				t.Fatalf("declined call status=%d", second.Code)
			}
			end, err := broker.snapshot(prepared["session_id"])
			if err != nil {
				t.Fatal(err)
			}
			if terminalFailures != 0 || end.GrantAgentDeclines != 0 || end.CostCeilingAgentDeclines != 0 {
				t.Fatalf("unconfirmed 4110 charged the miner: callbacks=%d %+v", terminalFailures, end)
			}
			if testCase.mismatch && end.DeclineEvidenceMismatches != 1 {
				t.Fatalf("want an evidence mismatch: %+v", end)
			}
			// A missing ceiling is its own gap, never a missing request/token
			// budget.
			if !testCase.mismatch && (end.CostCeilingAbsences != 1 || end.BudgetEvidenceAbsences != 0) {
				t.Fatalf("want a cost ceiling evidence absence: %+v", end)
			}
			failure := relayFinalizeFailure(relayDegradedSince(start, end))
			if failure.Kind != "validator_infrastructure" || !failure.Retryable {
				t.Fatalf("unconfirmed 4110 did not keep the attempt: %+v", failure)
			}
			if !testCase.mismatch {
				if failure.Diagnostics["relay_cause"] != "budget_evidence_absent" || failure.Diagnostics["wall"] != "cost" ||
					failure.Diagnostics["cost_ceiling_evidence_armed"] != false {
					t.Fatalf("absent ceiling diagnostics=%+v", failure.Diagnostics)
				}
				if _, ok := failure.Diagnostics["budget_evidence_armed"]; ok {
					t.Fatalf("absent ceiling reported on the request/token budgets: %+v", failure.Diagnostics)
				}
			}
		})
	}
}

func TestNoCostHeadersWithoutAnArmedCeiling(t *testing.T) {
	// No ceiling at all, and a stamped ceiling switched off.
	for ceiling, mode := range map[uint64]string{0: "", 250_000: costCeilingModeOff} {
		upstream := costCeilingUpstream(t, "")
		broker := newInferenceBroker(1)
		proxyURL := configureBrokerUpstream(broker, upstream)
		prepared := prepareBrokerSession(t, broker)
		activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel,
			costCeilingBudgets(ceiling, mode))
		sourceIP := "192.0.2.212"
		_ = claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV8)
		recorder := sendBrokerChat(t, broker, sourceIP, 4321)
		if recorder.Code != http.StatusOK {
			t.Fatalf("mode %q status=%d", mode, recorder.Code)
		}
		for _, header := range []string{harnessCostSpentHeader, harnessCostCeilingHeader} {
			if got := recorder.Header().Get(header); got != "" {
				t.Fatalf("mode %q leaked %s=%q", mode, header, got)
			}
		}
	}
}

func TestActivationRefusesAnUnknownCostCeilingMode(t *testing.T) {
	for _, testCase := range []struct {
		name    string
		ceiling uint64
		mode    string
		status  int
	}{
		{name: "unknown mode", ceiling: 1000, mode: "enforced", status: http.StatusUnauthorized},
		// Half a ceiling can be neither armed nor confirmed.
		{name: "ceiling without mode", ceiling: 1000, mode: "", status: http.StatusUnauthorized},
		{name: "mode without ceiling", ceiling: 0, mode: costCeilingModeEnforce, status: http.StatusUnauthorized},
		{name: "full pair", ceiling: 1000, mode: costCeilingModeEnforce, status: http.StatusOK},
		{name: "no ceiling", ceiling: 0, mode: "", status: http.StatusOK},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			broker := newInferenceBroker(1)
			broker.platformProxyURL = "https://platform.example" + platformInferenceAPIPath
			prepared := prepareBrokerSession(t, broker)
			activation := brokerActivation{
				ActivationSecret:    prepared["activation_secret"],
				GrantID:             "00000000-0000-0000-0000-000000000001",
				Bearer:              "platform-bearer-never-given-to-harness",
				ProxyURL:            broker.platformProxyURL,
				Generation:          1,
				ExpiresAt:           time.Now().Add(time.Minute),
				CostCeilingMicrousd: testCase.ceiling,
				CostCeilingMode:     testCase.mode,
			}
			body, _ := json.Marshal(activation)
			request := httptest.NewRequest(http.MethodPost, "/v1/inference/session/id/activate", bytes.NewReader(body))
			request.RemoteAddr = "127.0.0.1:4321"
			request.SetPathValue("id", prepared["session_id"])
			recorder := httptest.NewRecorder()
			broker.activate(recorder, request)
			if recorder.Code != testCase.status {
				t.Fatalf("status=%d, want %d: %s", recorder.Code, testCase.status, recorder.Body.String())
			}
		})
	}
}

func TestCostCeilingConfirmsOnTheEmbeddingLane(t *testing.T) {
	session := &brokerSession{
		requestBudget: 8192, tokenBudget: 75_000_000, maxOutputTokens: 8192,
		embeddingRequestBudget: 100_000, embeddingTokenBudget: 1_000_000_000,
		costCeilingMicrousd: 1000, costCeilingMode: costCeilingModeEnforce,
	}
	header := http.Header{}
	header.Set(ledgerCostHeader, "1000")
	observeLedgerCostLocked(session, header)
	// A lower total settled out of order never lowers the evidence.
	header.Set(ledgerCostHeader, "400")
	observeLedgerCostLocked(session, header)
	agent, attribution := attributePlatformDeclineLocked(session, platformDeclineCostCeilingExhausted, true, 0)
	if !agent || session.agentDeclineWall != allowanceWallCost {
		t.Fatalf("embedding 4110 not confirmed: %q wall=%q", attribution, session.agentDeclineWall)
	}
	// The spent ceiling exhausts the shared grant, so a 4102 on the
	// embedding lane after it is the agent's too.
	if agent, _ := attributePlatformDeclineLocked(session, platformDeclineBudgetExhausted, true, 0); !agent {
		t.Fatal("embedding 4102 after a spent cost ceiling was not confirmed")
	}
	if session.agentDeclineWall != allowanceWallCost {
		t.Fatalf("first wall overwritten: %q", session.agentDeclineWall)
	}
}

func TestAllowanceWallDiagnostics(t *testing.T) {
	token := relayFinalizeFailure(fmt.Errorf("%w: %w", errAgentInferenceDeclined, allowanceWallToken))
	if token.Code != "inference_allowance_exhausted" || token.Diagnostics["wall"] != "token" ||
		token.Diagnostics["budget_evidence_armed"] != true {
		t.Fatalf("token wall: %+v", token)
	}
	// A cost wall is confirmed by the ceiling, not the request/token budgets.
	cost := relayFinalizeFailure(fmt.Errorf("%w: %w", errAgentInferenceDeclined, allowanceWallCost))
	if cost.Diagnostics["wall"] != "cost" || cost.Diagnostics["cost_ceiling_evidence_armed"] != true {
		t.Fatalf("cost wall: %+v", cost)
	}
	if _, ok := cost.Diagnostics["budget_evidence_armed"]; ok {
		t.Fatalf("cost wall claimed request/token budget evidence: %+v", cost.Diagnostics)
	}
	plain := relayFinalizeFailure(errAgentInferenceDeclined)
	if _, ok := plain.Diagnostics["wall"]; ok {
		t.Fatalf("an unnamed decline invented a wall: %+v", plain.Diagnostics)
	}
	if !strings.Contains(allowanceWallCost.Error(), "cost") {
		t.Fatal("wall error text")
	}
}

func TestDecisionLaneCarriesLedgerEvidenceAndHarnessCostHeaders(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set(ledgerCostHeader, "777")
		writeJSON(w, http.StatusOK, map[string]any{
			"model":   "typesafe/jev-1.14",
			"answers": map[string]any{},
			"usage":   map[string]int{"input_tokens": 120, "output_tokens": 0},
		})
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", llm.V9AggregateProfileRevision,
		llm.HarnessModelForVersion(protocol.BenchVersionV15), costCeilingBudgets(1000, costCeilingModeShadow))
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.72", protocol.BenchVersionV15)

	response := callDecision(broker, "192.0.2.72", `{"state":"s","questions":{"q":{"type":"choice"}}}`)
	if response.Code != http.StatusOK {
		t.Fatalf("decision status=%d body=%s", response.Code, response.Body.String())
	}
	// Decision spend is ledger cost under the run's ceiling: the harness
	// sees it, and the broker keeps it as evidence for a later 4110.
	if got := response.Header().Get(harnessCostSpentHeader); got != "777" {
		t.Fatalf("%s=%q", harnessCostSpentHeader, got)
	}
	if got := response.Header().Get(harnessCostCeilingHeader); got != "1000" {
		t.Fatalf("%s=%q", harnessCostCeilingHeader, got)
	}
}

func TestOnlyAnAllCostRunNamesTheCostWall(t *testing.T) {
	start := relayHealthSnapshot{}
	allCost := relayHealthSnapshot{GrantDenials: 2, GrantAgentDeclines: 2, CostCeilingAgentDeclines: 2}
	err := relayDegradedSince(start, allCost)
	var wall allowanceWall
	if !errors.Is(err, errAgentInferenceDeclined) || !errors.As(err, &wall) || wall != allowanceWallCost {
		t.Fatalf("all-cost run: %v", err)
	}
	// One token wall and one cost wall: still the agent's, but no single
	// wall describes the run.
	mixed := relayHealthSnapshot{GrantDenials: 2, GrantAgentDeclines: 2, CostCeilingAgentDeclines: 1}
	err = relayDegradedSince(start, mixed)
	wall = ""
	if !errors.Is(err, errAgentInferenceDeclined) || errors.As(err, &wall) {
		t.Fatalf("mixed run named wall %q: %v", wall, err)
	}
	if _, ok := relayFinalizeFailure(err).Diagnostics["wall"]; ok {
		t.Fatalf("mixed run diagnostics named a wall: %+v", relayFinalizeFailure(err).Diagnostics)
	}
}

// heldSettlementUpstream holds the first chat call until release closes, then
// answers 200 with settledLedger; every later call is 4110 with no ledger
// header, the way a hop that strips X-Ditto-* would deliver it. arrived closes
// once the held call has reached the Platform.
func heldSettlementUpstream(t *testing.T, settledLedger string) (server *httptest.Server, arrived, release chan struct{}) {
	t.Helper()
	arrived, release = make(chan struct{}), make(chan struct{})
	var calls atomic.Int32
	server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if calls.Add(1) == 1 {
			close(arrived)
			<-release
			w.Header().Set(ledgerCostHeader, settledLedger)
			writeJSON(w, http.StatusOK, map[string]any{
				"usage":   map[string]int{"prompt_tokens": 1000, "completion_tokens": 100},
				"choices": []map[string]any{{"message": map[string]string{"content": "OK"}}},
			})
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"error_code":4110,"message":"cost ceiling exhausted"}`))
	}))
	t.Cleanup(server.Close)
	return server, arrived, release
}

func TestCostCeilingDeclineWaitsForAnInFlightSettlement(t *testing.T) {
	for _, testCase := range []struct {
		name          string
		settledLedger string
		agent         bool
	}{
		// The sibling settled past the ceiling at the Platform before the
		// 4110, but its ledger header reaches the broker after it.
		{name: "sibling proves the ceiling spent", settledLedger: "250000", agent: true},
		// Every sibling answered and none proves it: the 4110 was impossible.
		{name: "sibling does not prove it", settledLedger: "100000", agent: false},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			upstream, arrived, release := heldSettlementUpstream(t, testCase.settledLedger)
			broker := newInferenceBroker(1)
			var mu sync.Mutex
			var walls []allowanceWall
			broker.terminalAgentFailure = func(_ string, wall allowanceWall) {
				mu.Lock()
				walls = append(walls, wall)
				mu.Unlock()
			}
			proxyURL := configureBrokerUpstream(broker, upstream)
			prepared := prepareBrokerSession(t, broker)
			activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel,
				costCeilingBudgets(250_000, costCeilingModeEnforce))
			sourceIP := "192.0.2.213"
			_ = claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV8)
			start, err := broker.snapshot(prepared["session_id"])
			if err != nil {
				t.Fatal(err)
			}

			held := make(chan int, 1)
			go func() { held <- sendBrokerChat(t, broker, sourceIP, 4321).Code }()
			<-arrived
			if declined := sendBrokerChat(t, broker, sourceIP, 4322); declined.Code != http.StatusBadGateway {
				t.Fatalf("declined call status=%d", declined.Code)
			}
			// Judged on the stale total this would be a mismatch: it waits.
			waiting, err := broker.snapshot(prepared["session_id"])
			if err != nil {
				t.Fatal(err)
			}
			if waiting.GrantDenials != 1 || waiting.GrantAgentDeclines != 0 || waiting.DeclineEvidenceMismatches != 0 {
				t.Fatalf("4110 judged before its sibling settled: %+v", waiting)
			}

			close(release)
			if code := <-held; code != http.StatusOK {
				t.Fatalf("held call status=%d", code)
			}
			end, err := broker.snapshot(prepared["session_id"])
			if err != nil {
				t.Fatal(err)
			}
			mu.Lock()
			defer mu.Unlock()
			degraded := relayDegradedSince(start, end)
			if testCase.agent {
				if end.GrantAgentDeclines != 1 || end.CostCeilingAgentDeclines != 1 || end.DeclineEvidenceMismatches != 0 {
					t.Fatalf("settled sibling did not confirm the 4110: %+v", end)
				}
				if len(walls) != 1 || walls[0] != allowanceWallCost {
					t.Fatalf("terminal callbacks=%v, want one cost wall", walls)
				}
				if !errors.Is(degraded, errAgentInferenceDeclined) {
					t.Fatalf("degraded=%v, want agent decline", degraded)
				}
				return
			}
			if end.GrantAgentDeclines != 0 || end.DeclineEvidenceMismatches != 1 || len(walls) != 0 {
				t.Fatalf("unproven 4110 was not a mismatch: callbacks=%v %+v", walls, end)
			}
			if !errors.Is(degraded, errGrantDeclineEvidenceMismatch) {
				t.Fatalf("degraded=%v, want evidence mismatch", degraded)
			}
		})
	}
}

func TestCostCeilingDeclineCarryingTheLedgerIsConfirmedAtOnce(t *testing.T) {
	session := &brokerSession{costCeilingMicrousd: 1000, costCeilingMode: costCeilingModeEnforce}
	// A sibling is still in flight, but the decline's own header carries the
	// settled total the Platform declined on.
	beginLedgerDispatchLocked(session)
	beginLedgerDispatchLocked(session)
	header := http.Header{}
	header.Set(ledgerCostHeader, "1000")
	if endLedgerDispatchLocked(session, header) {
		t.Fatal("no decline was pending")
	}
	agent, attribution := attributePlatformDeclineLocked(session, platformDeclineCostCeilingExhausted, false, 0)
	if !agent || session.costCeilingPendingDeclines != 0 {
		t.Fatalf("4110 with its own ledger evidence waited: %q pending=%d", attribution, session.costCeilingPendingDeclines)
	}
	// A transport failure ends a dispatch without evidence.
	if endLedgerDispatchLocked(session, nil) || session.ledgerDispatchesInFlight != 0 {
		t.Fatalf("in flight=%d", session.ledgerDispatchesInFlight)
	}
}

func TestTrustedChatAdvertisesNoCostSpend(t *testing.T) {
	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set(ledgerCostHeader, "999999")
		writeJSON(w, http.StatusOK, map[string]any{
			"usage":   map[string]int{"prompt_tokens": 10, "completion_tokens": 1},
			"choices": []map[string]any{{"message": map[string]string{"content": "OK"}}},
		})
	})
	upstream := httptest.NewTLSServer(handler)
	defer upstream.Close()
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel,
		costCeilingBudgets(250_000, costCeilingModeEnforce))
	sourceIP := "192.0.2.214"
	id := prepared["session_id"]
	_ = claimAndBindBrokerSession(t, broker, id, sourceIP, protocol.BenchVersionV8)
	session := broker.sessions[id]
	session.trustedChatHandler = handler
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b","messages":[{"role":"user","content":"Reply OK"}]}`))
	request.RemoteAddr = sourceIP + ":4321"
	recorder := httptest.NewRecorder()
	broker.proxy(recorder, request, session, 0, "")
	if recorder.Code != http.StatusOK {
		t.Fatalf("trusted chat status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	for _, header := range []string{harnessCostSpentHeader, harnessCostCeilingHeader} {
		if got := recorder.Header().Get(header); got != "" {
			t.Fatalf("trusted chat advertised %s=%q from a ledger it never read", header, got)
		}
	}
	if session.ledgerCostMicrousd != 0 {
		t.Fatalf("trusted reader's header became ledger evidence: %d", session.ledgerCostMicrousd)
	}
}

func TestMixedEvidenceAbsencesReportEachGap(t *testing.T) {
	start := relayHealthSnapshot{}
	end := relayHealthSnapshot{GrantDenials: 3, BudgetEvidenceAbsences: 2, CostCeilingAbsences: 1}
	err := relayDegradedSince(start, end)
	if !errors.Is(err, errBudgetEvidenceAbsent) || !errors.Is(err, errCostCeilingEvidenceAbsent) {
		t.Fatalf("mixed absences: %v", err)
	}
	if !strings.Contains(err.Error(), "sent 2 terminal allowance decline(s)") ||
		!strings.Contains(err.Error(), "and 1 cost ceiling decline(s)") {
		t.Fatalf("mixed absences conflated the counts: %v", err)
	}
	var wall allowanceWall
	if errors.As(err, &wall) {
		t.Fatalf("mixed absences named wall %q", wall)
	}
	failure := relayFinalizeFailure(err)
	if failure.Kind != "validator_infrastructure" || !failure.Retryable ||
		failure.Diagnostics["relay_cause"] != "budget_evidence_absent" ||
		failure.Diagnostics["budget_evidence_armed"] != false ||
		failure.Diagnostics["cost_ceiling_evidence_armed"] != false {
		t.Fatalf("mixed absence failure=%+v", failure)
	}
	if _, ok := failure.Diagnostics["wall"]; ok {
		t.Fatalf("mixed absences diagnostics named a wall: %+v", failure.Diagnostics)
	}
	// Budget gaps alone keep their own count and never mention the ceiling.
	budgetOnly := relayDegradedSince(start, relayHealthSnapshot{GrantDenials: 2, BudgetEvidenceAbsences: 2})
	if errors.Is(budgetOnly, errCostCeilingEvidenceAbsent) || !strings.Contains(budgetOnly.Error(), "sent 2 terminal") {
		t.Fatalf("budget-only absences: %v", budgetOnly)
	}
}
