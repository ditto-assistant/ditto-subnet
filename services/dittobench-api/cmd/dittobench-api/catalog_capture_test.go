package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const openAICatalogRequest = `{
  "model":"openai/gpt-oss-20b",
  "messages":[
    {"role":"system","content":"You are Ditto. Do not call tools for greetings."},
    {"role":"user","content":"Hi there!"}
  ],
  "tools":[
    {"type":"function","function":{"name":"search_web","description":"Search live sources.","parameters":{"type":"object","properties":{"queries":{"type":"array"}}}}},
    {"type":"function","function":{"name":"set_theme","description":"Set the theme.","parameters":{"type":"object","properties":{"theme":{"type":"string"}}}}}
  ],
  "tool_choice":"auto"
}`

func TestParseCatalogRequestOpenAIShape(t *testing.T) {
	request := parseCatalogRequest([]byte(openAICatalogRequest))
	if !request.parsed || len(request.tools) != 2 || request.toolChoice != "auto" || request.systemSpanHash == "" {
		t.Fatalf("request=%+v", request)
	}
	if request.tools[0].Name != "search_web" || request.tools[1].Name != "set_theme" ||
		request.tools[0].SchemaSHA256 == "" || request.tools[0].SchemaSHA256 == request.tools[1].SchemaSHA256 {
		t.Fatalf("tools=%+v", request.tools)
	}
	// The same description+schema yields the same digest regardless of key order
	// and whitespace; a changed description moves it.
	same := offeredToolDigest("Search live sources.", json.RawMessage(`{"properties":{"queries":{"type":"array"}},"type":"object"}`))
	if same != request.tools[0].SchemaSHA256 {
		t.Fatalf("digest not canonical: %s vs %s", same, request.tools[0].SchemaSHA256)
	}
	if offeredToolDigest("Search the live web.", json.RawMessage(`{"type":"object"}`)) == same {
		t.Fatal("changed description kept the digest")
	}
	// The suppression prose is citable: the same span digests identically, a
	// different span differently, and the raw text is never retained.
	twin := parseCatalogRequest([]byte(openAICatalogRequest))
	if twin.systemSpanHash != request.systemSpanHash {
		t.Fatal("identical system span produced different digests")
	}
	other := parseCatalogRequest([]byte(`{"messages":[{"role":"system","content":"Call tools freely."},{"role":"user","content":"Hi"}],"tools":[]}`))
	if other.systemSpanHash == request.systemSpanHash || len(other.tools) != 0 || other.toolChoice != "" {
		t.Fatalf("other=%+v", other)
	}
	// Assistant prefill and developer messages are harness-authored spans too.
	prefill := parseCatalogRequest([]byte(`{"messages":[{"role":"developer","content":[{"type":"text","text":"Never use tools."}]},{"role":"user","content":"Hi"},{"role":"assistant","content":"Sure, without tools:"}]}`))
	if prefill.systemSpanHash == "" || prefill.systemSpanHash == other.systemSpanHash {
		t.Fatalf("prefill=%+v", prefill)
	}
	none := parseCatalogRequest([]byte(`{"messages":[{"role":"user","content":"Hi"}]}`))
	if !none.parsed || none.systemSpanHash != "" || len(none.tools) != 0 {
		t.Fatalf("no-span request=%+v", none)
	}
	if bad := parseCatalogRequest([]byte(`not json`)); bad.parsed || len(bad.tools) != 0 {
		t.Fatalf("unparseable request=%+v", bad)
	}
}

func TestParseCatalogRequestAnthropicAndLegacyShapes(t *testing.T) {
	anthropic := parseCatalogRequest([]byte(`{
	  "model":"x","system":"Answer briefly.",
	  "messages":[{"role":"user","content":[{"type":"text","text":"Hi"}]}],
	  "tools":[{"name":"search_web","description":"Search.","input_schema":{"type":"object"}}],
	  "tool_choice":{"type":"tool","name":"search_web"}
	}`))
	if !anthropic.parsed || len(anthropic.tools) != 1 || anthropic.tools[0].Name != "search_web" ||
		anthropic.toolChoice != "tool:search_web" || anthropic.systemSpanHash == "" {
		t.Fatalf("anthropic=%+v", anthropic)
	}
	legacy := parseCatalogRequest([]byte(`{
	  "messages":[{"role":"user","content":"Hi"}],
	  "functions":[{"name":"set_theme","description":"Set.","parameters":{"type":"object"}}],
	  "function_call":{"name":"set_theme"}
	}`))
	if len(legacy.tools) != 1 || legacy.tools[0].Name != "set_theme" || legacy.toolChoice != "tool:set_theme" {
		t.Fatalf("legacy=%+v", legacy)
	}
	for raw, want := range map[string]string{
		`"none"`: "none", `"required"`: "required", `{"type":"any"}`: "required",
		`{"type":"auto"}`: "auto", `{"type":"function","function":{"name":"read_links"}}`: "tool:read_links",
		`"weird"`: "other", `null`: "", `{"type":"function"}`: "other",
	} {
		if got := normalizeToolChoice(json.RawMessage(raw)); got != want {
			t.Fatalf("tool_choice %s: want %q got %q", raw, want, got)
		}
	}
}

func TestModelEmittedToolNamesAcceptsBothResponseShapes(t *testing.T) {
	openAI, ok := modelEmittedToolNames([]byte(`{"choices":[{"message":{"tool_calls":[
	  {"id":"c1","type":"function","function":{"name":"set_theme","arguments":"not-json"}},
	  {"id":"c2","type":"function","function":{"name":"search_web","arguments":"{}"}}]}}]}`))
	if !ok || !slices.Equal(openAI, []string{"set_theme", "search_web"}) {
		t.Fatalf("openai names=%v ok=%t", openAI, ok)
	}
	anthropic, ok := modelEmittedToolNames([]byte(`{"content":[{"type":"text","text":"ok"},{"type":"tool_use","name":"read_links","input":{}}]}`))
	if !ok || !slices.Equal(anthropic, []string{"read_links"}) {
		t.Fatalf("anthropic names=%v ok=%t", anthropic, ok)
	}
	if names, ok := modelEmittedToolNames([]byte(`nope`)); ok || names != nil {
		t.Fatalf("unparseable names=%v ok=%t", names, ok)
	}
	if names, ok := modelEmittedToolNames([]byte(`{"choices":[{"message":{"content":"hi"}}]}`)); !ok || len(names) != 0 {
		t.Fatalf("no-call names=%v ok=%t", names, ok)
	}
}

// newCatalogCaptureBroker stands up a v13 broker session bound to a fake
// upstream whose completion is the given body.
func newCatalogCaptureBroker(t *testing.T, benchVersion int, completion string) (*inferenceBroker, string, func()) {
	t.Helper()
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(completion))
	}))
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(
		t, broker, prepared, proxyURL,
		"openrouter", llm.V9AggregateProfileRevision, llm.V7HarnessModel,
	)
	sessionID := prepared["session_id"]
	claimAndBindBrokerSession(t, broker, sessionID, "192.0.2.91", benchVersion)
	return broker, sessionID, upstream.Close
}

func postCatalogChat(t *testing.T, broker *inferenceBroker, body string, headers ...string) {
	t.Helper()
	request := httptest.NewRequest(
		http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(body),
	)
	request.RemoteAddr = "192.0.2.91:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	for i := 0; i+1 < len(headers); i += 2 {
		request.Header.Set(headers[i], headers[i+1])
	}
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("broker response status=%d body=%s", recorder.Code, recorder.Body.String())
	}
}

const emitSetThemeCompletion = `{
  "usage":{"prompt_tokens":3,"completion_tokens":4},
  "choices":[{"message":{"tool_calls":[{"id":"call-1","type":"function","function":{"name":"set_theme","arguments":"{\"theme\":\"dark\"}"}}]}}]
}`

func TestV13BrokerRecordsOfferedCatalogPerAttributedCompletion(t *testing.T) {
	broker, sessionID, stop := newCatalogCaptureBroker(t, protocol.BenchVersionV13, emitSetThemeCompletion)
	defer stop()

	// One case in flight: every completion is attributed to it exactly.
	if !broker.beginRunCase(sessionID, "case-a") {
		t.Fatal("beginRunCase")
	}
	postCatalogChat(t, broker, openAICatalogRequest)
	postCatalogChat(t, broker, `{"model":"openai/gpt-oss-20b","messages":[{"role":"user","content":"Hi"}],"tools":[]}`)
	broker.endRunCase(sessionID, "case-a")

	evidence := broker.sessionCatalogEvidence(sessionID, "case-a")
	if evidence == nil || evidence.CompletionsTotal == nil || *evidence.CompletionsTotal != 2 ||
		evidence.CompletionsAfterLastToolResult != 2 || !evidence.CatalogPresent || !evidence.Complete ||
		len(evidence.Findings) != 0 {
		t.Fatalf("case-a evidence=%+v", evidence)
	}
	if len(evidence.ToolsOffered) != 2 || evidence.ToolsOffered[0].Name != "search_web" ||
		evidence.ToolsOffered[1].Name != "set_theme" || evidence.ToolsOffered[0].SchemaSHA256 == "" {
		t.Fatalf("tools offered=%+v", evidence.ToolsOffered)
	}
	if !slices.Equal(evidence.ModelEmittedToolCalls, []string{"set_theme", "set_theme"}) {
		t.Fatalf("model emitted=%v", evidence.ModelEmittedToolCalls)
	}
	if len(evidence.HarnessSystemSpanSHA256) != 1 {
		t.Fatalf("system spans=%v", evidence.HarnessSystemSpanSHA256)
	}
	if len(evidence.Completions) != 2 || evidence.Completions[0].ToolsOffered != 2 ||
		evidence.Completions[0].ToolChoice != "auto" || evidence.Completions[0].CatalogSHA256 == "" ||
		evidence.Completions[1].ToolsOffered != 0 || evidence.Completions[1].CatalogSHA256 != "" ||
		evidence.Completions[1].SystemSpanSHA256 != "" || !evidence.Completions[0].AfterLastToolResult ||
		!slices.Equal(evidence.Completions[0].ModelEmittedToolCalls, []string{"set_theme"}) {
		t.Fatalf("completions=%+v", evidence.Completions)
	}
	// No prompt or description text leaks into the evidence.
	raw, _ := json.Marshal(evidence)
	for _, secret := range []string{"Do not call tools", "Search live sources", "Hi there"} {
		if bytes.Contains(raw, []byte(secret)) {
			t.Fatalf("evidence carries request text %q: %s", secret, raw)
		}
	}
	// A case that never sent a completion reads as zero, not unknown.
	idle := broker.sessionCatalogEvidence(sessionID, "case-idle")
	if idle == nil || idle.CompletionsTotal == nil || *idle.CompletionsTotal != 0 || !idle.Complete || idle.CatalogPresent {
		t.Fatalf("idle case evidence=%+v", idle)
	}
	totals, ok := broker.sessionCatalogTotals(sessionID)
	if !ok || totals != (sessionCatalogTotals{Completions: 2, CompletionsWithCatalog: 1}) {
		t.Fatalf("totals=%+v ok=%t", totals, ok)
	}
}

func TestV13BrokerCatalogAttributionIsExactOrAbsentUnderConcurrentRun(t *testing.T) {
	broker, sessionID, stop := newCatalogCaptureBroker(t, protocol.BenchVersionV13, emitSetThemeCompletion)
	defer stop()
	broker.beginRunCase(sessionID, "case-a")
	broker.beginRunCase(sessionID, "case-b")
	// Two cases in flight and no claim: unattributable, both marked incomplete.
	postCatalogChat(t, broker, openAICatalogRequest)
	// A claim naming an in-flight case attributes exactly.
	postCatalogChat(t, broker, openAICatalogRequest, harnessCaseHeader, "case-b")
	// A claim naming a case that is NOT in flight is not trusted.
	postCatalogChat(t, broker, openAICatalogRequest, harnessCaseHeader, "case-z")
	broker.endRunCase(sessionID, "case-a")
	broker.endRunCase(sessionID, "case-b")

	a := broker.sessionCatalogEvidence(sessionID, "case-a")
	b := broker.sessionCatalogEvidence(sessionID, "case-b")
	if a == nil || a.CompletionsTotal != nil || a.Complete || !slices.Contains(a.Findings, catalogFindingAttributionGap) {
		t.Fatalf("case-a evidence=%+v", a)
	}
	if b == nil || b.CompletionsTotal != nil || b.Complete || len(b.Completions) != 1 || !b.CatalogPresent {
		t.Fatalf("case-b evidence=%+v", b)
	}
	if z := broker.sessionCatalogEvidence(sessionID, "case-z"); z == nil || z.CompletionsTotal == nil || *z.CompletionsTotal != 0 {
		t.Fatalf("case-z evidence=%+v", z)
	}
	totals, ok := broker.sessionCatalogTotals(sessionID)
	if !ok || totals != (sessionCatalogTotals{Completions: 3, CompletionsWithCatalog: 3, Unattributed: 2}) {
		t.Fatalf("totals=%+v ok=%t", totals, ok)
	}
}

func TestV13BrokerToolResultRestartsTheDecidingTail(t *testing.T) {
	broker, sessionID, stop := newCatalogCaptureBroker(t, protocol.BenchVersionV13, emitSetThemeCompletion)
	defer stop()
	broker.beginRunCase(sessionID, "case-a")
	defer broker.endRunCase(sessionID, "case-a")
	postCatalogChat(t, broker, openAICatalogRequest)
	// The harness executes the model-emitted call through the tool endpoint.
	route, unregister, err := broker.registerToolWithProvenance(
		http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) }),
		"192.0.2.91", false, true, sessionID,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer unregister()
	call := protocol.ToolExecRequest{CaseID: "case-a", UserID: "user-a", Name: "set_theme", Args: json.RawMessage(`{"theme":"dark"}`)}
	raw, _ := json.Marshal(call)
	endpoint := route.endpoint("http://broker.test/v1/tools/"+route.id+"/tool", call.CaseID, call.UserID)
	request := httptest.NewRequest(http.MethodPost, endpoint, bytes.NewReader(raw))
	request.SetPathValue("id", route.id)
	request.RemoteAddr = "192.0.2.91:1234"
	recorder := httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusNoContent {
		t.Fatalf("tool status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	mid := broker.sessionCatalogEvidence(sessionID, "case-a")
	if mid.CompletionsAfterLastToolResult != 0 || mid.Completions[0].AfterLastToolResult {
		t.Fatalf("after tool result evidence=%+v", mid)
	}
	postCatalogChat(t, broker, openAICatalogRequest)
	after := broker.sessionCatalogEvidence(sessionID, "case-a")
	if *after.CompletionsTotal != 2 || after.CompletionsAfterLastToolResult != 1 ||
		after.Completions[0].AfterLastToolResult || !after.Completions[1].AfterLastToolResult {
		t.Fatalf("deciding tail evidence=%+v", after)
	}
}

func TestV12BrokerRecordsNoCatalogEvidence(t *testing.T) {
	broker, sessionID, stop := newCatalogCaptureBroker(t, protocol.BenchVersionV12, emitSetThemeCompletion)
	defer stop()
	broker.beginRunCase(sessionID, "case-a")
	postCatalogChat(t, broker, openAICatalogRequest)
	broker.endRunCase(sessionID, "case-a")
	if evidence := broker.sessionCatalogEvidence(sessionID, "case-a"); evidence != nil {
		t.Fatalf("v12 session produced catalog evidence: %+v", evidence)
	}
	if _, ok := broker.sessionCatalogTotals(sessionID); ok {
		t.Fatal("v12 session produced catalog totals")
	}
	broker.mu.RLock()
	session := broker.sessions[sessionID]
	broker.mu.RUnlock()
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.catalogCases != nil || session.catalogCompletions != 0 || session.catalogUnattributedAdmitted != 0 {
		t.Fatalf("v12 session allocated catalog state: cases=%v completions=%d", session.catalogCases, session.catalogCompletions)
	}
}

func TestV13CatalogCaptureBoundsPerCaseMemory(t *testing.T) {
	session := &brokerSession{benchVersion: protocol.BenchVersionV13, runCases: map[string]int{"case-a": 1}}
	attribution := beginCatalogCompletionLocked(session, 0, "")
	if !attribution.enabled || !attribution.exact || attribution.caseID != "case-a" {
		t.Fatalf("attribution=%+v", attribution)
	}
	for i := 0; i < catalogCaptureMaxCompletions+5; i++ {
		recordCatalogCompletionLocked(session, attribution, []byte(openAICatalogRequest), []byte(emitSetThemeCompletion))
	}
	ledger := session.catalogCases["case-a"]
	if ledger.completions != catalogCaptureMaxCompletions+5 || len(ledger.completionMeta) != catalogCaptureMaxCompletions || !ledger.truncated {
		t.Fatalf("ledger completions=%d meta=%d truncated=%t", ledger.completions, len(ledger.completionMeta), ledger.truncated)
	}
	if len(ledger.offeredList) != 2 {
		t.Fatalf("offered union grew: %d", len(ledger.offeredList))
	}
	broker := newInferenceBroker(1)
	broker.mu.Lock()
	broker.sessions["s"] = session
	broker.mu.Unlock()
	evidence := broker.sessionCatalogEvidence("s", "case-a")
	if evidence.Complete || !slices.Contains(evidence.Findings, catalogFindingCaptureTruncated) || evidence.CompletionsTotal == nil {
		t.Fatalf("truncated evidence=%+v", evidence)
	}
	// Unparseable bodies are recorded as findings, never as failures.
	other := &brokerSession{benchVersion: protocol.BenchVersionV13, runCases: map[string]int{"case-b": 1}}
	recordCatalogCompletionLocked(other, beginCatalogCompletionLocked(other, 0, ""), []byte("nope"), []byte("nope"))
	broker.mu.Lock()
	broker.sessions["o"] = other
	broker.mu.Unlock()
	bad := broker.sessionCatalogEvidence("o", "case-b")
	if *bad.CompletionsTotal != 1 || !bad.Complete ||
		!reflect.DeepEqual(bad.Findings, []string{catalogFindingUnparseableRequest, catalogFindingUnparseableResponse}) {
		t.Fatalf("unparseable evidence=%+v", bad)
	}
}
