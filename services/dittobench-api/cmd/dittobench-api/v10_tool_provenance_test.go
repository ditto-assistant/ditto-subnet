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
	if after.ModelToolCalls != 1 || after.EndpointAttempts != 2 ||
		after.MatchedToolCalls != 1 || after.UnmatchedToolCalls != 1 ||
		!toolEvidenceComplete(after) || after.ToolFindings&toolFindingDuplicateExecution == 0 {
		t.Fatalf("provenance counters=%+v", after)
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
	if after.ModelToolCalls != 1 || toolSelectedNotExecuted(after) != 1 || !toolEvidenceComplete(after) {
		t.Fatalf("broker provenance counters=%+v", after)
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
	if toolEvidenceComplete(after) || after.ToolFindings&toolFindingInvalidModelEmission == 0 {
		t.Fatalf("invalid-emission counters=%+v", after)
	}
}

func TestV10ToolRouteFailsClosedOnUnbackedMismatchAndCrossCaseCalls(t *testing.T) {
	tests := []struct {
		name        string
		activeCase  string
		requestCase string
		modelCalls  string
		call        protocol.ToolExecRequest
		finding     uint64
	}{
		{
			name: "unbacked", activeCase: "case-a", requestCase: "case-a",
			modelCalls: `{"choices":[{"message":{}}]}`,
			call:       protocol.ToolExecRequest{CaseID: "case-a", UserID: "user-a", Name: "search_web", Args: json.RawMessage(`{"query":"x"}`)},
			finding:    toolFindingUnbacked,
		},
		{
			name: "argument mismatch", activeCase: "case-a", requestCase: "case-a",
			modelCalls: `{"choices":[{"message":{"tool_calls":[{"id":"call-1","type":"function","function":{"name":"search_web","arguments":"{\"query\":\"expected\"}"}}]}}]}`,
			call:       protocol.ToolExecRequest{CaseID: "case-a", UserID: "user-a", Name: "search_web", Args: json.RawMessage(`{"query":"different"}`)},
			finding:    toolFindingNameArgumentMismatch,
		},
		{
			name: "cross case replay", activeCase: "case-b", requestCase: "case-a",
			modelCalls: `{"choices":[{"message":{"tool_calls":[{"id":"call-1","type":"function","function":{"name":"search_web","arguments":"{\"query\":\"x\"}"}}]}}]}`,
			call:       protocol.ToolExecRequest{CaseID: "case-a", UserID: "user-a", Name: "search_web", Args: json.RawMessage(`{"query":"x"}`)},
			finding:    toolFindingCrossCaseReplay,
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
			if after.EndpointAttempts != 1 || after.UnmatchedToolCalls != 1 ||
				after.ToolFindings&test.finding == 0 {
				t.Fatalf("counters=%+v", after)
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
	if toolSelectedNotExecuted(after) != 1 {
		t.Fatalf("counters=%+v", after)
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
	if missing.ToolScore != 0 || missing.ToolProvenance == nil ||
		!slices.Contains(missing.ToolProvenance.Findings, "tool_provenance_unavailable") ||
		len(missing.Notes) != 1 {
		t.Fatalf("missing provenance must fail closed on scored tool credit: %+v", missing)
	}
	missingPractice := applyV10ToolProvenance(
		protocol.BenchVersionV10, scorer.ScopePractice, base, response, observed, runner.CaseExecution{},
	)
	if missingPractice.ToolScore != 1 || missingPractice.ToolProvenance == nil {
		t.Fatalf("missing provenance must not touch practice credit: %+v", missingPractice)
	}
	missingMemory := applyV10ToolProvenance(
		protocol.BenchVersionV10, scorer.ScopeScored,
		protocol.CaseScore{Kind: protocol.KindMemory, ToolScore: 1, Score: 1},
		response, observed, runner.CaseExecution{},
	)
	if missingMemory.ToolScore != 1 || missingMemory.Score != 1 {
		t.Fatalf("missing provenance must not touch memory credit: %+v", missingMemory)
	}

	// Session-scoped evidence: an unmatched endpoint attempt zeroes the case; a
	// clean consumption keeps the observed score.
	sessionUnbacked := runner.CaseExecution{ToolProvenance: &protocol.ToolProvenanceEvidence{
		EndpointAttempts: 2, Matched: 1, ModelEmitted: 1, Unmatched: 1, Complete: true,
		Findings: []string{"unbacked_harness_execution"},
	}}
	zeroed := applyV10ToolProvenance(
		protocol.BenchVersionV10, scorer.ScopeScored, base, response, observed, sessionUnbacked,
	)
	if zeroed.ToolScore != 0 {
		t.Fatalf("session-level unmatched must zero scored tool credit: %+v", zeroed)
	}
	sessionClean := runner.CaseExecution{ToolProvenance: &protocol.ToolProvenanceEvidence{
		EndpointAttempts: 1, Matched: 1, ModelEmitted: 1, Complete: true,
	}}
	kept := applyV10ToolProvenance(
		protocol.BenchVersionV11, scorer.ScopeScored, base, response, observed, sessionClean,
	)
	if kept.ToolScore != 1 || len(kept.Notes) != 0 {
		t.Fatalf("session-level clean match must keep observed score: %+v", kept)
	}
}

func TestV10ToolProvenanceSummaryAggregatesTrustedCounts(t *testing.T) {
	perCase := []protocol.CaseScore{
		{ToolProvenance: &protocol.ToolProvenanceEvidence{ModelEmitted: 2, EndpointAttempts: 1, Matched: 1, ModelSelectedNotExecuted: 1, Complete: true}},
		{ToolProvenance: &protocol.ToolProvenanceEvidence{EndpointAttempts: 1, Unmatched: 1}},
		{},
	}
	summary := summarizeV10ToolProvenance(perCase, nil)
	if summary == nil || summary.Cases != 2 || summary.IncompleteCases != 1 ||
		summary.ModelEmitted != 2 || summary.EndpointAttempts != 2 ||
		summary.Matched != 1 || summary.Unmatched != 1 || summary.ModelSelectedNotExecuted != 1 {
		t.Fatalf("summary=%+v", summary)
	}
	// Session-wide totals replace the per-case emission sum and settle the
	// unconsumed remainder as model_selected_not_executed.
	withTotals := summarizeV10ToolProvenance(perCase, &sessionToolProvenanceTotals{ModelEmitted: 5, Consumed: 1})
	if withTotals == nil || withTotals.Cases != 2 || withTotals.ModelEmitted != 5 ||
		withTotals.Matched != 1 || withTotals.Unmatched != 1 || withTotals.ModelSelectedNotExecuted != 4 {
		t.Fatalf("summary with totals=%+v", withTotals)
	}
	if summarizeV10ToolProvenance(nil, &sessionToolProvenanceTotals{ModelEmitted: 5}) != nil {
		t.Fatal("summary without per-case evidence must stay nil")
	}
}

func TestV10SessionToolProvenanceConsumesEmissionRegardlessOfCaseOnce(t *testing.T) {
	broker := newInferenceBroker(1)
	const sessionID = "v10-session-provenance"
	session := addV10ProvenanceSession(broker, sessionID)
	// No case window is open: concurrent /run admits every chat at generation 0.
	recordV10ModelToolResponse(t, session, 0, `{
		"choices":[{"message":{"tool_calls":[{
			"id":"call-1","type":"function","function":{
				"name":"search_web","arguments":"{\"limit\":2,\"query\":\"veltrix\"}"
			}
		},{
			"id":"call-2","type":"function","function":{
				"name":"get_weather","arguments":"{\"city\":\"Oslo\"}"
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
	search := protocol.ToolExecRequest{
		CaseID: "case-b", UserID: "user-a", Name: "search_web",
		Args: json.RawMessage(`{"query":"veltrix","limit":2}`),
	}
	// The emission is consumed from case-b even though no window attributes it
	// to a case; the same (name, args) cannot be spent twice.
	if recorder := postProvenanceTool(t, broker, route, "case-b", search); recorder.Code != http.StatusNoContent {
		t.Fatalf("cross-case match status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	replay := search
	replay.CaseID = "case-a"
	if recorder := postProvenanceTool(t, broker, route, "case-a", replay); recorder.Code != http.StatusConflict {
		t.Fatalf("double-spend status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	// Changed arguments and unbacked names are rejected before the mock runs.
	mismatch := protocol.ToolExecRequest{
		CaseID: "case-c", UserID: "user-a", Name: "get_weather",
		Args: json.RawMessage(`{"city":"Bergen"}`),
	}
	if recorder := postProvenanceTool(t, broker, route, "case-c", mismatch); recorder.Code != http.StatusConflict {
		t.Fatalf("argument mismatch status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	unbacked := protocol.ToolExecRequest{
		CaseID: "case-c", UserID: "user-a", Name: "send_email",
		Args: json.RawMessage(`{}`),
	}
	if recorder := postProvenanceTool(t, broker, route, "case-c", unbacked); recorder.Code != http.StatusConflict {
		t.Fatalf("unbacked status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if called != 1 {
		t.Fatalf("forwarded calls=%d want=1", called)
	}

	caseB := broker.sessionToolProvenance(sessionID, "case-b")
	if caseB == nil || caseB.EndpointAttempts != 1 || caseB.Matched != 1 || caseB.ModelEmitted != 1 ||
		caseB.Unmatched != 0 || !caseB.Complete || len(caseB.Findings) != 0 {
		t.Fatalf("case-b evidence=%+v", caseB)
	}
	caseA := broker.sessionToolProvenance(sessionID, "case-a")
	if caseA == nil || caseA.EndpointAttempts != 1 || caseA.Matched != 0 || caseA.Unmatched != 1 ||
		!caseA.Complete || !slices.Contains(caseA.Findings, "duplicate_tool_execution") {
		t.Fatalf("case-a evidence=%+v", caseA)
	}
	caseC := broker.sessionToolProvenance(sessionID, "case-c")
	if caseC == nil || caseC.EndpointAttempts != 2 || caseC.Unmatched != 2 ||
		!slices.Contains(caseC.Findings, "name_argument_mismatch") ||
		!slices.Contains(caseC.Findings, "unbacked_harness_execution") {
		t.Fatalf("case-c evidence=%+v", caseC)
	}
	// A case that never touched the endpoint settles clean and complete.
	untouched := broker.sessionToolProvenance(sessionID, "case-z")
	if untouched == nil || !untouched.Complete || untouched.EndpointAttempts != 0 ||
		untouched.Matched != 0 || untouched.Unmatched != 0 || untouched.ModelEmitted != 0 ||
		untouched.ModelSelectedNotExecuted != 0 || untouched.Findings != nil {
		t.Fatalf("untouched evidence=%+v", untouched)
	}
	totals, ok := broker.sessionToolProvenanceTotals(sessionID)
	if !ok || totals != (sessionToolProvenanceTotals{ModelEmitted: 2, Consumed: 1}) {
		t.Fatalf("totals=%+v ok=%t", totals, ok)
	}
	if broker.sessionToolProvenance("unknown-session", "case-b") != nil {
		t.Fatal("unknown session must yield nil evidence")
	}
	if _, ok := broker.sessionToolProvenanceTotals("unknown-session"); ok {
		t.Fatal("unknown session must yield no totals")
	}
}

func TestV10SessionToolProvenanceIsV10Only(t *testing.T) {
	broker := newInferenceBroker(1)
	const sessionID = "v9-session"
	session := &brokerSession{benchVersion: protocol.BenchVersionV9}
	broker.mu.Lock()
	broker.sessions[sessionID] = session
	broker.mu.Unlock()
	recordV10ModelToolResponse(t, session, 0, `{"choices":[{"message":{"tool_calls":[{"id":"call-1","type":"function","function":{"name":"search_web","arguments":"{}"}}]}}]}`)
	session.mu.Lock()
	emitted := session.sessionToolEmitted
	session.mu.Unlock()
	if emitted != 0 {
		t.Fatalf("v9 session recorded %d emissions", emitted)
	}
	if broker.sessionToolProvenance(sessionID, "case-a") != nil {
		t.Fatal("v9 session must not settle session-scoped tool evidence")
	}
	if _, ok := broker.sessionToolProvenanceTotals(sessionID); ok {
		t.Fatal("v9 session must not expose session-scoped totals")
	}
}

func TestV10SessionInvalidModelEmissionIsUnconsumableAndSurfaced(t *testing.T) {
	broker := newInferenceBroker(1)
	const sessionID = "v10-session-invalid"
	session := addV10ProvenanceSession(broker, sessionID)
	recordV10ModelToolResponse(t, session, 0, `{
		"choices":[{"message":{"tool_calls":[{
			"id":"call-1","type":"function","function":{"name":"search_web","arguments":"not-json"}
		}]}}]
	}`)
	route, stop, err := broker.registerToolWithProvenance(
		http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) }),
		"192.0.2.20", false, true, sessionID,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	call := protocol.ToolExecRequest{CaseID: "case-a", UserID: "user-a", Name: "search_web", Args: json.RawMessage(`{}`)}
	if recorder := postProvenanceTool(t, broker, route, "case-a", call); recorder.Code != http.StatusConflict {
		t.Fatalf("execution from an undecodable emission status=%d", recorder.Code)
	}
	evidence := broker.sessionToolProvenance(sessionID, "case-a")
	if evidence == nil || !evidence.Complete || evidence.Unmatched != 1 ||
		!slices.Contains(evidence.Findings, "unbacked_harness_execution") ||
		!slices.Contains(evidence.Findings, "invalid_model_tool_emission") {
		t.Fatalf("evidence=%+v", evidence)
	}
	totals, ok := broker.sessionToolProvenanceTotals(sessionID)
	if !ok || totals != (sessionToolProvenanceTotals{InvalidEmissions: 1}) {
		t.Fatalf("totals=%+v", totals)
	}
}

func TestV10BrokerResponseRecordsModelToolCallsAtSessionLevelUnderConcurrentRun(t *testing.T) {
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
	sessionID := prepared["session_id"]
	claimAndBindBrokerSession(t, broker, sessionID, "192.0.2.91", protocol.BenchVersionV10)
	// No beginCaseSnapshot / case capability: the chat is admitted at generation 0.
	for i := 0; i < 2; i++ {
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
	}
	totals, ok := broker.sessionToolProvenanceTotals(sessionID)
	if !ok || totals != (sessionToolProvenanceTotals{ModelEmitted: 2}) {
		t.Fatalf("totals after two emissions=%+v ok=%t", totals, ok)
	}

	called := 0
	route, stop, err := broker.registerToolWithProvenance(
		http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			called++
			w.WriteHeader(http.StatusNoContent)
		}),
		"192.0.2.91", false, true, sessionID,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	call := protocol.ToolExecRequest{
		CaseID: "case-x", UserID: "user-a", Name: "search_web",
		Args: json.RawMessage(`{"query":"veltrix"}`),
	}
	post := func(caseID string) int {
		raw, _ := json.Marshal(protocol.ToolExecRequest{CaseID: caseID, UserID: call.UserID, Name: call.Name, Args: call.Args})
		endpoint := route.endpoint("http://broker.test/v1/tools/"+route.id+"/tool", caseID, call.UserID)
		request := httptest.NewRequest(http.MethodPost, endpoint, bytes.NewReader(raw))
		request.SetPathValue("id", route.id)
		request.RemoteAddr = "192.0.2.91:1234"
		recorder := httptest.NewRecorder()
		broker.handleTool(recorder, request)
		return recorder.Code
	}
	// Two overlapping cases each consume one of the two identical emissions; a
	// third identical execution has nothing left to consume.
	if code := post("case-x"); code != http.StatusNoContent {
		t.Fatalf("first consumption status=%d", code)
	}
	if code := post("case-y"); code != http.StatusNoContent {
		t.Fatalf("second consumption status=%d", code)
	}
	if code := post("case-y"); code != http.StatusConflict {
		t.Fatalf("third execution status=%d", code)
	}
	if called != 2 {
		t.Fatalf("forwarded calls=%d want=2", called)
	}
	x := broker.sessionToolProvenance(sessionID, "case-x")
	y := broker.sessionToolProvenance(sessionID, "case-y")
	if x == nil || x.Matched != 1 || x.Unmatched != 0 || !x.Complete ||
		y == nil || y.Matched != 1 || y.Unmatched != 1 || !y.Complete ||
		!slices.Contains(y.Findings, "duplicate_tool_execution") {
		t.Fatalf("case-x=%+v case-y=%+v", x, y)
	}
	totals, ok = broker.sessionToolProvenanceTotals(sessionID)
	if !ok || totals != (sessionToolProvenanceTotals{ModelEmitted: 2, Consumed: 2}) {
		t.Fatalf("totals after consumption=%+v ok=%t", totals, ok)
	}
}

// toolEvidenceComplete and toolSelectedNotExecuted mirror how the broker's
// per-case tool counters were published. The production publisher was removed
// with the exclusive per-case windows that fed it; the counters themselves are
// still recorded on the session's success path, so these tests assert them
// directly.
func toolEvidenceComplete(snapshot brokerCaseSnapshot) bool {
	return snapshot.ToolEvidenceComplete && snapshot.InFlight == 0
}

func toolSelectedNotExecuted(snapshot brokerCaseSnapshot) uint64 {
	if snapshot.ModelToolCalls <= snapshot.MatchedToolCalls {
		return 0
	}
	return snapshot.ModelToolCalls - snapshot.MatchedToolCalls
}
