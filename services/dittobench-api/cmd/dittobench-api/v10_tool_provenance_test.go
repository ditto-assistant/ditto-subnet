package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func addV10ProvenanceSession(broker *inferenceBroker, id string) *brokerSession {
	session := &brokerSession{benchVersion: protocol.BenchVersionV10}
	broker.mu.Lock()
	broker.sessions[id] = session
	broker.mu.Unlock()
	return session
}

func recordV10ModelToolResponse(t *testing.T, session *brokerSession, generation uint64, body string) {
	t.Helper()
	session.mu.Lock()
	recordModelToolCallsLocked(session, generation, []byte(body))
	session.mu.Unlock()
}

func postProvenanceTool(
	t *testing.T,
	broker *inferenceBroker,
	route registeredToolRoute,
	caseID string,
	call protocol.ToolExecRequest,
) *httptest.ResponseRecorder {
	t.Helper()
	raw, err := json.Marshal(call)
	if err != nil {
		t.Fatal(err)
	}
	endpoint := route.endpoint(
		"http://broker.test/v1/tools/"+route.id+"/tool", caseID, call.UserID,
	)
	request := httptest.NewRequest(http.MethodPost, endpoint, bytes.NewReader(raw))
	request.SetPathValue("id", route.id)
	request.RemoteAddr = "192.0.2.20:1234"
	recorder := httptest.NewRecorder()
	broker.handleTool(recorder, request)
	return recorder
}

func TestV10ToolRouteForwardsWithoutCaseWindowWhenProvenanceUnbound(t *testing.T) {
	broker := newInferenceBroker(1)
	called := 0
	route, stop, err := broker.registerToolWithProvenance(
		http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			called++
			w.WriteHeader(http.StatusNoContent)
		}),
		"192.0.2.20", false, true, "",
	)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	call := protocol.ToolExecRequest{
		CaseID: "case-a", UserID: "user-a", Name: "search_web",
		Args: json.RawMessage(`{"query":"veltrix"}`),
	}
	if recorder := postProvenanceTool(t, broker, route, "case-a", call); recorder.Code != http.StatusNoContent {
		t.Fatalf("unbound provenance status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if called != 1 {
		t.Fatalf("forwarded calls=%d want=1", called)
	}
}

func TestV10ToolRouteRequiresAndConsumesMatchingModelEmission(t *testing.T) {
	broker := newInferenceBroker(1)
	const sessionID = "v10-provenance"
	session := addV10ProvenanceSession(broker, sessionID)
	generation, before, err := broker.beginCaseSnapshot(sessionID, "case-a")
	if err != nil || !before.ToolEvidenceComplete {
		t.Fatalf("begin v10 case = %+v, %v", before, err)
	}
	recordV10ModelToolResponse(t, session, generation, `{
		"choices":[{"message":{"tool_calls":[{
			"id":"call-1","type":"function","function":{
				"name":"search_web","arguments":"{\"limit\":2,\"query\":\"veltrix\"}"
			}
		}]}}]
	}`)

	called := 0
	route, stop, err := broker.registerToolWithProvenance(
		http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			called++
			w.WriteHeader(http.StatusNoContent)
		}),
		"192.0.2.20", false, true, sessionID,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	call := protocol.ToolExecRequest{
		CaseID: "case-a", UserID: "user-a", Name: "search_web",
		Args: json.RawMessage(`{"query":"veltrix","limit":2}`),
	}
	if recorder := postProvenanceTool(t, broker, route, "case-a", call); recorder.Code != http.StatusNoContent {
		t.Fatalf("matched execution status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if recorder := postProvenanceTool(t, broker, route, "case-a", call); recorder.Code != http.StatusConflict {
		t.Fatalf("duplicate execution status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if called != 1 {
		t.Fatalf("forwarded calls=%d want=1", called)
	}
	after, err := broker.endCaseSnapshot(sessionID, generation)
	if err != nil {
		t.Fatal(err)
	}
	evidence := toolProvenanceEvidence(after)
	if evidence == nil || evidence.ModelEmitted != 1 || evidence.EndpointAttempts != 2 ||
		evidence.Matched != 1 || evidence.Unmatched != 1 || !evidence.Complete ||
		!slices.Contains(evidence.Findings, "duplicate_tool_execution") {
		t.Fatalf("provenance evidence=%+v", evidence)
	}
}

func TestV10BrokerResponseRecordsModelToolCallsForActiveCase(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"usage":{"prompt_tokens":3,"completion_tokens":4},
			"choices":[{"message":{"tool_calls":[{
				"id":"call-1","type":"function","function":{
					"name":"search_web","arguments":"{\"query\":\"veltrix\"}"
				}
			}]}}]
		}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(
		t, broker, prepared, proxyURL,
		"openrouter", llm.V9AggregateProfileRevision, llm.V7HarnessModel,
	)
	claimAndBindBrokerSession(
		t, broker, prepared["session_id"], "192.0.2.91", protocol.BenchVersionV10,
	)
	generation, _, err := broker.beginCaseSnapshot(prepared["session_id"], "case-a")
	if err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(
		http.MethodPost, "/v1/inference/id/v1/chat/completions",
		bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`),
	)
	request.RemoteAddr = "192.0.2.91:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("broker response status=%d body=%s", recorder.Code, recorder.Body.String())
	}

	after, err := broker.endCaseSnapshot(prepared["session_id"], generation)
	if err != nil {
		t.Fatal(err)
	}
	evidence := toolProvenanceEvidence(after)
	if evidence == nil || evidence.ModelEmitted != 1 || evidence.ModelSelectedNotExecuted != 1 ||
		!evidence.Complete || !slices.Contains(evidence.Findings, "model_selected_not_executed") {
		t.Fatalf("broker provenance evidence=%+v", evidence)
	}
}

func TestV10InvalidModelToolEmissionFailsEvidenceClosed(t *testing.T) {
	broker := newInferenceBroker(1)
	const sessionID = "v10-invalid-emission"
	session := addV10ProvenanceSession(broker, sessionID)
	generation, _, err := broker.beginCaseSnapshot(sessionID, "case-a")
	if err != nil {
		t.Fatal(err)
	}
	recordV10ModelToolResponse(t, session, generation, `{
		"choices":[{"message":{"tool_calls":[{
			"id":"call-1","type":"function","function":{
				"name":"search_web","arguments":"not-json"
			}
		}]}}]
	}`)
	after, err := broker.endCaseSnapshot(sessionID, generation)
	if err != nil {
		t.Fatal(err)
	}
	evidence := toolProvenanceEvidence(after)
	if evidence == nil || evidence.Complete ||
		!slices.Contains(evidence.Findings, "invalid_model_tool_emission") ||
		!slices.Contains(evidence.Findings, "tool_provenance_incomplete") {
		t.Fatalf("invalid-emission evidence=%+v", evidence)
	}
}

func TestV10ToolRouteFailsClosedOnUnbackedMismatchAndCrossCaseCalls(t *testing.T) {
	tests := []struct {
		name        string
		activeCase  string
		requestCase string
		modelCalls  string
		call        protocol.ToolExecRequest
		finding     string
	}{
		{
			name: "unbacked", activeCase: "case-a", requestCase: "case-a",
			modelCalls: `{"choices":[{"message":{}}]}`,
			call:       protocol.ToolExecRequest{CaseID: "case-a", UserID: "user-a", Name: "search_web", Args: json.RawMessage(`{"query":"x"}`)},
			finding:    "unbacked_harness_execution",
		},
		{
			name: "argument mismatch", activeCase: "case-a", requestCase: "case-a",
			modelCalls: `{"choices":[{"message":{"tool_calls":[{"id":"call-1","type":"function","function":{"name":"search_web","arguments":"{\"query\":\"expected\"}"}}]}}]}`,
			call:       protocol.ToolExecRequest{CaseID: "case-a", UserID: "user-a", Name: "search_web", Args: json.RawMessage(`{"query":"different"}`)},
			finding:    "name_argument_mismatch",
		},
		{
			name: "cross case replay", activeCase: "case-b", requestCase: "case-a",
			modelCalls: `{"choices":[{"message":{"tool_calls":[{"id":"call-1","type":"function","function":{"name":"search_web","arguments":"{\"query\":\"x\"}"}}]}}]}`,
			call:       protocol.ToolExecRequest{CaseID: "case-a", UserID: "user-a", Name: "search_web", Args: json.RawMessage(`{"query":"x"}`)},
			finding:    "cross_case_replay",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			broker := newInferenceBroker(1)
			const sessionID = "v10-provenance"
			session := addV10ProvenanceSession(broker, sessionID)
			generation, _, err := broker.beginCaseSnapshot(sessionID, test.activeCase)
			if err != nil {
				t.Fatal(err)
			}
			recordV10ModelToolResponse(t, session, generation, test.modelCalls)
			called := false
			route, stop, err := broker.registerToolWithProvenance(
				http.HandlerFunc(func(http.ResponseWriter, *http.Request) { called = true }),
				"192.0.2.20", false, true, sessionID,
			)
			if err != nil {
				t.Fatal(err)
			}
			defer stop()
			recorder := postProvenanceTool(t, broker, route, test.requestCase, test.call)
			if recorder.Code != http.StatusConflict || called {
				t.Fatalf("status=%d forwarded=%t body=%s", recorder.Code, called, recorder.Body.String())
			}
			after, err := broker.endCaseSnapshot(sessionID, generation)
			if err != nil {
				t.Fatal(err)
			}
			evidence := toolProvenanceEvidence(after)
			if evidence == nil || evidence.EndpointAttempts != 1 || evidence.Unmatched != 1 ||
				!slices.Contains(evidence.Findings, test.finding) {
				t.Fatalf("evidence=%+v", evidence)
			}
		})
	}
}

func TestV10ToolProvenanceReportsModelSelectionWithoutExecution(t *testing.T) {
	broker := newInferenceBroker(1)
	const sessionID = "v10-provenance"
	session := addV10ProvenanceSession(broker, sessionID)
	generation, _, err := broker.beginCaseSnapshot(sessionID, "case-a")
	if err != nil {
		t.Fatal(err)
	}
	recordV10ModelToolResponse(t, session, generation, `{"choices":[{"message":{"tool_calls":[{"id":"call-1","type":"function","function":{"name":"search_web","arguments":"{}"}}]}}]}`)
	after, err := broker.endCaseSnapshot(sessionID, generation)
	if err != nil {
		t.Fatal(err)
	}
	evidence := toolProvenanceEvidence(after)
	if evidence == nil || evidence.ModelSelectedNotExecuted != 1 ||
		!slices.Contains(evidence.Findings, "model_selected_not_executed") {
		t.Fatalf("evidence=%+v", evidence)
	}
}

func TestV10ToolProvenanceControlsScoredCreditAndLeavesV9Frozen(t *testing.T) {
	base := protocol.CaseScore{Kind: protocol.KindTool, ToolScore: 1}
	observed := []protocol.ObservedToolCall{{Name: "search_web"}}
	response := protocol.RunResponse{ToolCalls: observed}
	validExecution := runner.CaseExecution{ToolProvenance: &protocol.ToolProvenanceEvidence{
		ModelEmitted: 1, EndpointAttempts: 1, Matched: 1, Complete: true,
	}}
	valid := applyV10ToolProvenance(
		protocol.BenchVersionV10, scorer.ScopeScored, base, response, observed, validExecution,
	)
	if valid.ToolScore != 1 || valid.ToolProvenance == nil {
		t.Fatalf("valid v10 score=%+v", valid)
	}

	unbackedExecution := runner.CaseExecution{ToolProvenance: &protocol.ToolProvenanceEvidence{
		EndpointAttempts: 1, Unmatched: 1, Complete: true,
		Findings: []string{"unbacked_harness_execution"},
	}}
	unbacked := applyV10ToolProvenance(
		protocol.BenchVersionV10, scorer.ScopeScored, base, response, nil, unbackedExecution,
	)
	if unbacked.ToolScore != 0 || !slices.Contains(unbacked.ToolProvenance.Findings, "untrusted_self_report_only") {
		t.Fatalf("unbacked v10 score=%+v", unbacked)
	}

	v9 := applyV10ToolProvenance(
		protocol.BenchVersionV9, scorer.ScopeScored, base, response, observed, runner.CaseExecution{},
	)
	if v9.ToolScore != base.ToolScore || v9.ToolProvenance != nil || len(v9.Notes) != 0 {
		t.Fatalf("v9 score changed: got=%+v want=%+v", v9, base)
	}

	missing := applyV10ToolProvenance(
		protocol.BenchVersionV10, scorer.ScopeScored, base, response, observed, runner.CaseExecution{},
	)
	if missing.ToolScore != 1 || missing.ToolProvenance == nil ||
		!slices.Contains(missing.ToolProvenance.Findings, "tool_provenance_unavailable") {
		t.Fatalf("missing provenance should keep observed score: %+v", missing)
	}
}

func TestV10ToolProvenanceSummaryAggregatesTrustedCounts(t *testing.T) {
	perCase := []protocol.CaseScore{
		{ToolProvenance: &protocol.ToolProvenanceEvidence{ModelEmitted: 2, EndpointAttempts: 1, Matched: 1, ModelSelectedNotExecuted: 1, Complete: true}},
		{ToolProvenance: &protocol.ToolProvenanceEvidence{EndpointAttempts: 1, Unmatched: 1}},
		{},
	}
	summary := summarizeV10ToolProvenance(perCase)
	if summary == nil || summary.Cases != 2 || summary.IncompleteCases != 1 ||
		summary.ModelEmitted != 2 || summary.EndpointAttempts != 2 ||
		summary.Matched != 1 || summary.Unmatched != 1 || summary.ModelSelectedNotExecuted != 1 {
		t.Fatalf("summary=%+v", summary)
	}
}
