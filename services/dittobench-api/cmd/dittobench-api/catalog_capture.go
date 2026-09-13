package main

// Bench v13 catalog CAPTURE: the broker-side record of what the harness OFFERED
// the controlled model on every successful chat completion, paired with what
// the model chose. Bench v10 provenance only decodes the RESPONSE tool_calls,
// so a zero-tool deciding turn -- a harness that withholds the catalog on a
// request-keyed family, or offers it and swallows the call -- was invisible to
// scoring. This file records the REQUEST side: tools[] names plus a digest of
// each tool's description and parameter schema, tool_choice, and a digest over
// the harness-authored system/prefill spans, for OpenAI- and Anthropic-shaped
// bodies alike.
//
// Metadata only. No prompt text, no completion text, no description text, and
// no argument bodies are retained (PG ledger stays metadata: see
// sn118-trace-capture-design). A body that cannot be parsed records an empty
// catalog and a finding rather than failing the completion, and the capture
// is bounded per case so a hostile harness cannot grow broker memory.
//
// Attribution is exact or absent. A completion is booked on the case whose
// exclusive window, verified X-Ditto-Case-Id claim, or sole in-flight /run
// admitted it. Under concurrent /run with several cases in flight and no
// verified claim, the completion is booked run-wide and every case in flight at
// admission is marked incomplete; the scorer fails OPEN on those cases.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const (
	// catalogCaptureMaxCompletions bounds the per-completion metadata kept per
	// case. A real case makes a handful of completions; the ceiling exists so a
	// pathological harness cannot grow the ledger without bound. Once hit the
	// case is marked incomplete and counts keep increasing.
	catalogCaptureMaxCompletions = 256
	// catalogCaptureMaxTools bounds the distinct (name, schema digest) pairs kept
	// per completion and per case. The published catalog has 31 tools.
	catalogCaptureMaxTools = 512
	// catalogCaptureMaxSpans bounds the distinct system-span digests kept per case.
	catalogCaptureMaxSpans = 64
	// catalogCaptureMaxEmissions bounds the model-emitted tool names kept per case.
	catalogCaptureMaxEmissions = 1024
)

// Catalog-capture finding names (CatalogEvidence.Findings).
const (
	catalogFindingUnparseableRequest  = "catalog_request_unparseable"
	catalogFindingCaptureTruncated    = "catalog_capture_truncated"
	catalogFindingAttributionGap      = "catalog_attribution_incomplete"
	catalogFindingUnparseableResponse = "catalog_response_unparseable"
)

// catalogRequest is the request-side metadata of one chat completion.
type catalogRequest struct {
	tools          []protocol.OfferedTool
	toolChoice     string
	systemSpanHash string
	parsed         bool
}

// brokerCatalogLedger is one wire case's catalog evidence under construction.
type brokerCatalogLedger struct {
	completions            int
	afterLastToolResult    int
	catalogPresent         bool
	offered                map[string]struct{} // "name\x00schema" keys
	offeredList            []protocol.OfferedTool
	completionMeta         []protocol.CatalogCompletion
	emitted                []string
	spanHashes             map[string]struct{}
	unattributedOverlap    int
	truncated              bool
	unparseableRequests    int
	unparseableResponses   int
	offeredOverflowDropped int
}

// catalogCaptureEnabled reports whether this session records catalog evidence.
// Gated on the shared floor so v12 and earlier sessions allocate nothing.
func catalogCaptureEnabled(session *brokerSession) bool {
	return session.benchVersion >= protocol.BenchVersionV13
}

// ensureCatalogLedgerLocked returns the case's ledger, creating it. Caller
// holds session.mu and has checked catalogCaptureEnabled.
func ensureCatalogLedgerLocked(session *brokerSession, caseID string) *brokerCatalogLedger {
	if session.catalogCases == nil {
		session.catalogCases = make(map[string]*brokerCatalogLedger)
	}
	ledger := session.catalogCases[caseID]
	if ledger == nil {
		ledger = &brokerCatalogLedger{
			offered:    make(map[string]struct{}),
			spanHashes: make(map[string]struct{}),
		}
		session.catalogCases[caseID] = ledger
	}
	return ledger
}

// attributedCaseLocked resolves the one case a chat completion serves, using
// the same evidence order as the trace context: an exclusive case window, then
// a harness claim that names an in-flight case, then a sole in-flight case.
// ok=false means the completion cannot be attributed to exactly one case.
// Caller holds session.mu.
func attributedCaseLocked(session *brokerSession, caseGeneration uint64, claimed string) (string, bool) {
	if caseGeneration != 0 && session.activeCaseGeneration == caseGeneration && session.activeCaseID != "" {
		return session.activeCaseID, true
	}
	claimed = strings.TrimSpace(claimed)
	if claimed != "" && session.runCases[claimed] > 0 {
		return claimed, true
	}
	if len(session.runCases) == 1 {
		for caseID := range session.runCases {
			return caseID, true
		}
	}
	return "", false
}

// catalogAttribution is the admission-time attribution of one chat completion,
// resolved before the upstream call so the in-flight set that existed when the
// harness SENT the request decides the booking, not the set at response time.
type catalogAttribution struct {
	enabled bool
	caseID  string
	exact   bool
	// inFlight is the set of /run cases in flight at admission; when the
	// completion is unattributable and SUCCEEDS, each of them is marked
	// incomplete (a failed completion informed no case and marks nothing).
	inFlight []string
}

// beginCatalogCompletionLocked resolves attribution for one admitted chat
// request. An unattributable request is booked run-wide and marks every case
// in flight at admission incomplete. Caller holds session.mu.
func beginCatalogCompletionLocked(session *brokerSession, caseGeneration uint64, claimed string) catalogAttribution {
	if !catalogCaptureEnabled(session) {
		return catalogAttribution{}
	}
	caseID, ok := attributedCaseLocked(session, caseGeneration, claimed)
	if ok {
		return catalogAttribution{enabled: true, caseID: caseID, exact: true}
	}
	session.catalogUnattributedAdmitted++
	attribution := catalogAttribution{enabled: true}
	for inFlight := range session.runCases {
		attribution.inFlight = append(attribution.inFlight, inFlight)
	}
	return attribution
}

// recordCatalogCompletionLocked books one SUCCESSFUL chat completion's request
// and response metadata on the attributed case (or run-wide). Caller holds
// session.mu.
func recordCatalogCompletionLocked(session *brokerSession, attribution catalogAttribution, requestBody, responseBody []byte) {
	if !attribution.enabled || !catalogCaptureEnabled(session) {
		return
	}
	request := parseCatalogRequest(requestBody)
	emitted, responseOK := modelEmittedToolNames(responseBody)
	session.catalogCompletions++
	if len(request.tools) > 0 {
		session.catalogCompletionsWithCatalog++
	}
	if !attribution.exact {
		session.catalogUnattributedCompletions++
		for _, inFlight := range attribution.inFlight {
			ensureCatalogLedgerLocked(session, inFlight).unattributedOverlap++
		}
		return
	}
	ledger := ensureCatalogLedgerLocked(session, attribution.caseID)
	ledger.completions++
	ledger.afterLastToolResult++
	if !request.parsed {
		ledger.unparseableRequests++
	}
	if !responseOK {
		ledger.unparseableResponses++
	}
	if len(request.tools) > 0 {
		ledger.catalogPresent = true
	}
	for _, tool := range request.tools {
		key := tool.Name + "\x00" + tool.SchemaSHA256
		if _, seen := ledger.offered[key]; seen {
			continue
		}
		if len(ledger.offeredList) >= catalogCaptureMaxTools {
			ledger.offeredOverflowDropped++
			ledger.truncated = true
			continue
		}
		ledger.offered[key] = struct{}{}
		ledger.offeredList = append(ledger.offeredList, tool)
	}
	if request.systemSpanHash != "" {
		if _, seen := ledger.spanHashes[request.systemSpanHash]; !seen {
			if len(ledger.spanHashes) < catalogCaptureMaxSpans {
				ledger.spanHashes[request.systemSpanHash] = struct{}{}
			} else {
				ledger.truncated = true
			}
		}
	}
	for _, name := range emitted {
		if len(ledger.emitted) >= catalogCaptureMaxEmissions {
			ledger.truncated = true
			break
		}
		ledger.emitted = append(ledger.emitted, name)
	}
	if len(ledger.completionMeta) >= catalogCaptureMaxCompletions {
		ledger.truncated = true
		return
	}
	ledger.completionMeta = append(ledger.completionMeta, protocol.CatalogCompletion{
		ToolsOffered:          len(request.tools),
		CatalogSHA256:         catalogDigest(request.tools),
		ToolChoice:            request.toolChoice,
		ModelEmittedToolCalls: emitted,
		SystemSpanSHA256:      request.systemSpanHash,
		AfterLastToolResult:   true,
	})
}

// recordCatalogToolResultLocked notes that the validator served this case a
// tool_endpoint result: the deciding tail restarts after it. Caller holds
// session.mu.
func recordCatalogToolResultLocked(session *brokerSession, caseID string) {
	if !catalogCaptureEnabled(session) || caseID == "" {
		return
	}
	ledger := ensureCatalogLedgerLocked(session, caseID)
	ledger.afterLastToolResult = 0
	for index := range ledger.completionMeta {
		ledger.completionMeta[index].AfterLastToolResult = false
	}
}

// catalogDigest is the SHA-256 over the sorted "name:schema" lines of a catalog,
// or "" for an empty catalog.
func catalogDigest(tools []protocol.OfferedTool) string {
	if len(tools) == 0 {
		return ""
	}
	lines := make([]string, 0, len(tools))
	for _, tool := range tools {
		lines = append(lines, tool.Name+":"+tool.SchemaSHA256)
	}
	sort.Strings(lines)
	sum := sha256.Sum256([]byte(strings.Join(lines, "\n")))
	return hex.EncodeToString(sum[:])
}

// canonicalJSONDigest hashes the canonical (sorted-key, compact) JSON encoding
// of a raw value; "" when the value is absent or not JSON.
func canonicalJSONDigest(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var value any
	if json.Unmarshal(raw, &value) != nil {
		return ""
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])
}

// offeredToolDigest is the SHA-256 of the canonical JSON of
// {"description": ..., "parameters": ...}, so a renamed description or a
// changed schema moves the digest while the name stays comparable.
func offeredToolDigest(description string, parameters json.RawMessage) string {
	var params any
	if len(parameters) > 0 {
		if json.Unmarshal(parameters, &params) != nil {
			params = string(parameters)
		}
	}
	canonical, err := json.Marshal(map[string]any{"description": description, "parameters": params})
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])
}

// parseCatalogRequest extracts the request-side catalog metadata from an
// OpenAI-shaped (tools[]/functions[], tool_choice/function_call, messages[])
// or Anthropic-shaped (tools[] with input_schema, tool_choice object, system)
// chat body. parsed=false records an unparseable body; the returned metadata is
// then empty.
func parseCatalogRequest(body []byte) catalogRequest {
	var decoded struct {
		Tools []struct {
			Type     string `json:"type"`
			Function *struct {
				Name        string          `json:"name"`
				Description string          `json:"description"`
				Parameters  json.RawMessage `json:"parameters"`
			} `json:"function"`
			// Anthropic shape.
			Name        string          `json:"name"`
			Description string          `json:"description"`
			InputSchema json.RawMessage `json:"input_schema"`
		} `json:"tools"`
		// Legacy OpenAI functions shape.
		Functions []struct {
			Name        string          `json:"name"`
			Description string          `json:"description"`
			Parameters  json.RawMessage `json:"parameters"`
		} `json:"functions"`
		ToolChoice   json.RawMessage `json:"tool_choice"`
		FunctionCall json.RawMessage `json:"function_call"`
		System       json.RawMessage `json:"system"`
		Messages     []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if json.Unmarshal(body, &decoded) != nil {
		return catalogRequest{}
	}
	request := catalogRequest{parsed: true}
	for _, tool := range decoded.Tools {
		switch {
		case tool.Function != nil && tool.Function.Name != "":
			request.tools = append(request.tools, protocol.OfferedTool{
				Name:         tool.Function.Name,
				SchemaSHA256: offeredToolDigest(tool.Function.Description, tool.Function.Parameters),
			})
		case tool.Name != "":
			request.tools = append(request.tools, protocol.OfferedTool{
				Name:         tool.Name,
				SchemaSHA256: offeredToolDigest(tool.Description, tool.InputSchema),
			})
		}
		if len(request.tools) >= catalogCaptureMaxTools {
			break
		}
	}
	for _, function := range decoded.Functions {
		if function.Name == "" || len(request.tools) >= catalogCaptureMaxTools {
			continue
		}
		request.tools = append(request.tools, protocol.OfferedTool{
			Name:         function.Name,
			SchemaSHA256: offeredToolDigest(function.Description, function.Parameters),
		})
	}
	request.toolChoice = normalizeToolChoice(decoded.ToolChoice)
	if request.toolChoice == "" {
		request.toolChoice = normalizeToolChoice(decoded.FunctionCall)
	}
	// Harness-authored spans: the Anthropic top-level system, every OpenAI
	// system/developer message, and a trailing assistant prefill. Each span is
	// hashed individually; the per-completion digest covers the ordered span
	// digests so the same suppression prose yields the same citable value.
	var spans []string
	if text, ok := rawContentText(decoded.System); ok && text != "" {
		spans = append(spans, spanDigest(text))
	}
	for _, message := range decoded.Messages {
		if message.Role != "system" && message.Role != "developer" {
			continue
		}
		if text, ok := rawContentText(message.Content); ok && text != "" {
			spans = append(spans, spanDigest(text))
		}
	}
	if n := len(decoded.Messages); n > 0 && decoded.Messages[n-1].Role == "assistant" {
		if text, ok := rawContentText(decoded.Messages[n-1].Content); ok && text != "" {
			spans = append(spans, spanDigest("prefill:"+text))
		}
	}
	if len(spans) > 0 {
		request.systemSpanHash = spanDigest(strings.Join(spans, "\n"))
	}
	return request
}

// spanDigest is the hex SHA-256 of one span's text.
func spanDigest(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

// rawContentText renders a message content field -- a string, or an array of
// typed parts with "text" -- as plain text. ok=false when it is neither.
func rawContentText(raw json.RawMessage) (string, bool) {
	if len(raw) == 0 || string(raw) == "null" {
		return "", false
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s, true
	}
	var parts []struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &parts) == nil {
		var b strings.Builder
		for _, part := range parts {
			b.WriteString(part.Text)
		}
		return b.String(), true
	}
	return "", false
}

// normalizeToolChoice renders an OpenAI or Anthropic tool_choice as one of "",
// "auto", "none", "required", or "tool:<name>".
func normalizeToolChoice(raw json.RawMessage) string {
	if len(raw) == 0 || string(raw) == "null" {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		switch strings.ToLower(strings.TrimSpace(s)) {
		case "auto":
			return "auto"
		case "none":
			return "none"
		case "required", "any":
			return "required"
		}
		return "other"
	}
	var object struct {
		Type     string `json:"type"`
		Name     string `json:"name"`
		Function *struct {
			Name string `json:"name"`
		} `json:"function"`
	}
	if json.Unmarshal(raw, &object) != nil {
		return "other"
	}
	switch strings.ToLower(object.Type) {
	case "auto":
		return "auto"
	case "none":
		return "none"
	case "any", "required":
		return "required"
	case "function", "tool", "":
		// Legacy function_call carries a bare {"name": ...} with no type.
		name := object.Name
		if object.Function != nil && object.Function.Name != "" {
			name = object.Function.Name
		}
		if name != "" {
			return "tool:" + name
		}
	}
	return "other"
}

// modelEmittedToolNames lists the tool names a chat completion response
// selected: OpenAI choices[].message.tool_calls[].function.name (every choice,
// in order) and Anthropic content[] blocks of type tool_use. Unlike
// decodeModelToolCalls it is lenient -- an invalid call still names its tool
// so restraint can be scored on what the model chose -- and it reports whether
// the body was parseable at all.
func modelEmittedToolNames(responseBody []byte) ([]string, bool) {
	var response struct {
		Choices []struct {
			Message struct {
				ToolCalls []struct {
					Function struct {
						Name string `json:"name"`
					} `json:"function"`
				} `json:"tool_calls"`
			} `json:"message"`
		} `json:"choices"`
		Content []struct {
			Type string `json:"type"`
			Name string `json:"name"`
		} `json:"content"`
	}
	if json.Unmarshal(responseBody, &response) != nil {
		return nil, false
	}
	var names []string
	for _, choice := range response.Choices {
		for _, call := range choice.Message.ToolCalls {
			if call.Function.Name != "" {
				names = append(names, call.Function.Name)
			}
		}
	}
	for _, block := range response.Content {
		if block.Type == "tool_use" && block.Name != "" {
			names = append(names, block.Name)
		}
	}
	return names, true
}

// sessionCatalogEvidence returns one wire case's catalog evidence, or nil when
// the session is unknown or below Bench v13 (the scorer then records
// unavailability and fails open). A case with no ledger and no overlap is a
// case the harness never sent a completion for: CompletionsTotal is 0, not nil.
func (b *inferenceBroker) sessionCatalogEvidence(id, caseID string) *protocol.CatalogEvidence {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return nil
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if !catalogCaptureEnabled(session) {
		return nil
	}
	ledger := session.catalogCases[caseID]
	if ledger == nil {
		ledger = &brokerCatalogLedger{}
	}
	evidence := &protocol.CatalogEvidence{
		CompletionsAfterLastToolResult: ledger.afterLastToolResult,
		CatalogPresent:                 ledger.catalogPresent,
		Complete:                       ledger.unattributedOverlap == 0 && !ledger.truncated,
	}
	if ledger.unattributedOverlap == 0 {
		completions := ledger.completions
		evidence.CompletionsTotal = &completions
	}
	if len(ledger.offeredList) > 0 {
		evidence.ToolsOffered = append([]protocol.OfferedTool(nil), ledger.offeredList...)
		sort.Slice(evidence.ToolsOffered, func(i, j int) bool {
			if evidence.ToolsOffered[i].Name != evidence.ToolsOffered[j].Name {
				return evidence.ToolsOffered[i].Name < evidence.ToolsOffered[j].Name
			}
			return evidence.ToolsOffered[i].SchemaSHA256 < evidence.ToolsOffered[j].SchemaSHA256
		})
	}
	if len(ledger.completionMeta) > 0 {
		evidence.Completions = make([]protocol.CatalogCompletion, len(ledger.completionMeta))
		for index, meta := range ledger.completionMeta {
			meta.ModelEmittedToolCalls = append([]string(nil), meta.ModelEmittedToolCalls...)
			evidence.Completions[index] = meta
		}
	}
	if len(ledger.emitted) > 0 {
		evidence.ModelEmittedToolCalls = append([]string(nil), ledger.emitted...)
	}
	if len(ledger.spanHashes) > 0 {
		evidence.HarnessSystemSpanSHA256 = make([]string, 0, len(ledger.spanHashes))
		for digest := range ledger.spanHashes {
			evidence.HarnessSystemSpanSHA256 = append(evidence.HarnessSystemSpanSHA256, digest)
		}
		sort.Strings(evidence.HarnessSystemSpanSHA256)
	}
	if ledger.unattributedOverlap > 0 {
		evidence.Findings = append(evidence.Findings, catalogFindingAttributionGap)
	}
	if ledger.truncated {
		evidence.Findings = append(evidence.Findings, catalogFindingCaptureTruncated)
	}
	if ledger.unparseableRequests > 0 {
		evidence.Findings = append(evidence.Findings, catalogFindingUnparseableRequest)
	}
	if ledger.unparseableResponses > 0 {
		evidence.Findings = append(evidence.Findings, catalogFindingUnparseableResponse)
	}
	return evidence
}

// sessionCatalogTotals is the run-level view of the catalog capture.
type sessionCatalogTotals struct {
	Completions            uint64
	CompletionsWithCatalog uint64
	Unattributed           uint64
}

// sessionCatalogTotals reads the session-wide catalog counters for the run
// summary. ok=false when no v13+ session exists.
func (b *inferenceBroker) sessionCatalogTotals(id string) (sessionCatalogTotals, bool) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return sessionCatalogTotals{}, false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if !catalogCaptureEnabled(session) {
		return sessionCatalogTotals{}, false
	}
	return sessionCatalogTotals{
		Completions:            session.catalogCompletions,
		CompletionsWithCatalog: session.catalogCompletionsWithCatalog,
		Unattributed:           session.catalogUnattributedCompletions,
	}, true
}
