package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
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
			if !testCase.mismatch && end.BudgetEvidenceAbsences != 1 {
				t.Fatalf("want an evidence absence: %+v", end)
			}
			failure := relayFinalizeFailure(relayDegradedSince(start, end))
			if failure.Kind != "validator_infrastructure" || !failure.Retryable {
				t.Fatalf("unconfirmed 4110 did not keep the attempt: %+v", failure)
			}
		})
	}
}

func TestNoCostHeadersWithoutAnArmedCeiling(t *testing.T) {
	for _, mode := range []string{"", costCeilingModeOff} {
		upstream := costCeilingUpstream(t, "")
		broker := newInferenceBroker(1)
		proxyURL := configureBrokerUpstream(broker, upstream)
		prepared := prepareBrokerSession(t, broker)
		activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel,
			costCeilingBudgets(250_000, mode))
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
	broker := newInferenceBroker(1)
	broker.platformProxyURL = "https://platform.example" + platformInferenceAPIPath
	prepared := prepareBrokerSession(t, broker)
	activation := brokerActivation{
		ActivationSecret: prepared["activation_secret"],
		GrantID:          "00000000-0000-0000-0000-000000000001",
		Bearer:           "platform-bearer-never-given-to-harness",
		ProxyURL:         broker.platformProxyURL,
		Generation:       1,
		ExpiresAt:        time.Now().Add(time.Minute),
		CostCeilingMode:  "enforced",
	}
	body, _ := json.Marshal(activation)
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/session/id/activate", bytes.NewReader(body))
	request.RemoteAddr = "127.0.0.1:4321"
	request.SetPathValue("id", prepared["session_id"])
	recorder := httptest.NewRecorder()
	broker.activate(recorder, request)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("status=%d, want 401: %s", recorder.Code, recorder.Body.String())
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
	if token.Code != "inference_allowance_exhausted" || token.Diagnostics["wall"] != "token" {
		t.Fatalf("token wall: %+v", token)
	}
	plain := relayFinalizeFailure(errAgentInferenceDeclined)
	if _, ok := plain.Diagnostics["wall"]; ok {
		t.Fatalf("an unnamed decline invented a wall: %+v", plain.Diagnostics)
	}
	if !strings.Contains(allowanceWallCost.Error(), "cost") {
		t.Fatal("wall error text")
	}
}
