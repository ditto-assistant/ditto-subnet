package scorer

import (
	"encoding/json"
	"reflect"
	"slices"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func intPtr(v int) *int { return &v }

func offered(names ...string) []protocol.OfferedTool {
	out := make([]protocol.OfferedTool, 0, len(names))
	for _, name := range names {
		out = append(out, protocol.OfferedTool{Name: name, SchemaSHA256: "digest-" + name})
	}
	return out
}

func settledEvidence(completions int, names ...string) *protocol.CatalogEvidence {
	return &protocol.CatalogEvidence{
		CompletionsTotal:               intPtr(completions),
		CompletionsAfterLastToolResult: completions,
		CatalogPresent:                 len(names) > 0,
		ToolsOffered:                   offered(names...),
		Complete:                       true,
	}
}

func TestCatalogSemanticTopKIsDeterministicAndRanksTheCuedTool(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	cases := map[string]string{
		"Search the web for the latest Veltrix index figure.":      "search_web",
		"Switch my theme to dark mode, please.":                    "set_theme",
		"Read this link and summarize it: https://example.com/a":   "read_links",
		"Generate an image of a lighthouse at dusk.":               "create_image",
		"Run a background agent job to refactor the billing repo.": "execute_agent_job",
	}
	for prompt, want := range cases {
		first := CatalogSemanticTopK(prompt, full, CatalogSafeHarborTopK)
		second := CatalogSemanticTopK(prompt, full, CatalogSafeHarborTopK)
		if !reflect.DeepEqual(first, second) {
			t.Fatalf("%q: top-k not deterministic: %v vs %v", prompt, first, second)
		}
		if len(first) != CatalogSafeHarborTopK {
			t.Fatalf("%q: want %d names, got %v", prompt, CatalogSafeHarborTopK, first)
		}
		if !slices.Contains(first, want) {
			t.Fatalf("%q: top-k %v does not contain %s", prompt, first, want)
		}
	}
	if got := CatalogSemanticTopK("anything", nil, 3); got != nil {
		t.Fatalf("empty catalog: want nil, got %v", got)
	}
	if got := CatalogSemanticTopK("anything", full[:2], 3); len(got) != 2 {
		t.Fatalf("catalog smaller than k: want 2 names, got %v", got)
	}
}

func TestCatalogGateFailsOpenWithoutSettledEvidence(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	c := protocol.ToolCase{ID: "chat", Category: "no_tool", Prompt: "How is your day?"}
	if v := EvaluateCatalogGate(c, full, nil, nil, false); v.Settled || v.Zero ||
		!slices.Equal(v.Findings, []string{CatalogFindingEvidenceUnavailable}) {
		t.Fatalf("nil evidence verdict=%+v", v)
	}
	incomplete := settledEvidence(1)
	incomplete.CompletionsTotal = nil
	incomplete.Complete = false
	if v := EvaluateCatalogGate(c, full, incomplete, nil, false); v.Settled || v.Zero ||
		!slices.Equal(v.Findings, []string{CatalogFindingEvidenceIncomplete}) {
		t.Fatalf("incomplete evidence verdict=%+v", v)
	}
	truncated := settledEvidence(1, "search_web")
	truncated.Complete = false
	if v := EvaluateCatalogGate(c, full, truncated, nil, false); v.Settled || v.Zero {
		t.Fatalf("truncated evidence verdict=%+v", v)
	}
}

func TestCatalogGateRestraintRequiresAnOfferUnlessSafeHarbor(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	chitchat := protocol.ToolCase{ID: "chat", Category: "no_tool", Prompt: "How is your day going so far?"}

	// Fixture: emptying tools[] on a declarative turn is restraint without offer.
	empty := EvaluateCatalogGate(chitchat, full, settledEvidence(1), nil, false)
	if !empty.Settled || !empty.Zero || !empty.CatalogAbsent || empty.SafeHarbor != "" ||
		!slices.Contains(empty.Findings, CatalogFindingRestraintWithoutOffer) ||
		!slices.Contains(empty.Findings, CatalogFindingCatalogAbsent) {
		t.Fatalf("empty catalog on declarative turn verdict=%+v", empty)
	}
	// No completion at all: the host answered without the model.
	none := EvaluateCatalogGate(chitchat, full, settledEvidence(0), nil, false)
	if !none.Settled || !none.Zero || !none.NoCompletion || none.CatalogAbsent ||
		!slices.Contains(none.Findings, CatalogFindingNoModelCompletion) ||
		!slices.Contains(none.Findings, CatalogFindingRestraintWithoutOffer) {
		t.Fatalf("no completion verdict=%+v", none)
	}
	// Safe harbor: any non-empty catalog on a declarative/chit-chat case.
	trimmed := EvaluateCatalogGate(chitchat, full, settledEvidence(1, "set_theme"), nil, false)
	if !trimmed.Settled || trimmed.Zero || trimmed.SafeHarbor != CatalogSafeHarborNonEmptyDeclarative ||
		!slices.Contains(trimmed.Findings, CatalogFindingSafeHarbor) ||
		slices.Contains(trimmed.Findings, CatalogFindingRestraintWithoutOffer) {
		t.Fatalf("non-empty catalog on declarative turn verdict=%+v", trimmed)
	}
	// Abstention and arg_hallucination are declarative/decline classes too.
	for _, category := range []string{"abstention", "arg_hallucination"} {
		c := protocol.ToolCase{ID: category, Category: category, Prompt: "Change my theme."}
		if v := EvaluateCatalogGate(c, full, settledEvidence(1, "search_web"), nil, false); v.Zero || v.SafeHarbor == "" {
			t.Fatalf("%s with a non-empty catalog verdict=%+v", category, v)
		}
	}

	// The tempting class: a negation case NAMES the cue, so a non-empty catalog
	// that omits the cued class is not restraint the model chose.
	negation := protocol.ToolCase{
		ID: "neg", Category: "negation_no_tool",
		Prompt: "Don't search the web for this, just tell me what you already know about the Veltrix index.",
	}
	topK := CatalogSemanticTopK(negation.Prompt, full, CatalogSafeHarborTopK)
	if !slices.Contains(topK, "search_web") {
		t.Fatalf("published top-k %v lacks the cued tool", topK)
	}
	omitted := EvaluateCatalogGate(negation, full, settledEvidence(1, "set_theme", "set_chat_font"), nil, false)
	if !omitted.Zero || omitted.SafeHarbor != "" ||
		!slices.Contains(omitted.Findings, CatalogFindingRestraintWithoutOffer) {
		t.Fatalf("negation with the cued class withheld verdict=%+v", omitted)
	}
	retained := EvaluateCatalogGate(negation, full, settledEvidence(1, append(topK, "set_theme")...), nil, false)
	if retained.Zero || retained.SafeHarbor != CatalogSafeHarborSemanticTopK {
		t.Fatalf("negation with the semantic top-k retained verdict=%+v", retained)
	}
	// A full catalog always passes.
	names := make([]string, 0, len(full))
	for _, tool := range full {
		names = append(names, tool.Name)
	}
	if v := EvaluateCatalogGate(negation, full, settledEvidence(1, names...), nil, false); v.Zero || v.SafeHarbor == "" {
		t.Fatalf("full catalog verdict=%+v", v)
	}
}

func TestCatalogGateSwallowedModelCallScoresRestraintOnModelChoice(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	c := protocol.ToolCase{ID: "chat", Category: "no_tool", Prompt: "Hope you're having a nice afternoon!"}
	evidence := settledEvidence(1, "set_theme", "search_web")
	evidence.ModelEmittedToolCalls = []string{"set_theme"}
	swallowed := EvaluateCatalogGate(c, full, evidence, nil, false)
	if !swallowed.Zero || !slices.Contains(swallowed.Findings, CatalogFindingSwallowedModelCall) {
		t.Fatalf("swallowed call verdict=%+v", swallowed)
	}
	// Executed calls are not swallowed (the base rule already zeroes the action).
	executed := EvaluateCatalogGate(c, full, evidence, []protocol.ObservedToolCall{{Name: "set_theme"}}, false)
	if slices.Contains(executed.Findings, CatalogFindingSwallowedModelCall) {
		t.Fatalf("executed call flagged as swallowed: %+v", executed)
	}
	// Memory retrieval is harness-internal and never served: not an action.
	evidence.ModelEmittedToolCalls = []string{"search_memories", "fetch_memories"}
	memory := EvaluateCatalogGate(c, full, evidence, nil, false)
	if memory.Zero || slices.Contains(memory.Findings, CatalogFindingSwallowedModelCall) {
		t.Fatalf("memory emission flagged as swallowed: %+v", memory)
	}
	// Emitted twice, executed once: one was swallowed.
	evidence.ModelEmittedToolCalls = []string{"search_web", "search_web"}
	twice := EvaluateCatalogGate(c, full, evidence, []protocol.ObservedToolCall{{Name: "search_web"}}, false)
	if !slices.Contains(twice.Findings, CatalogFindingSwallowedModelCall) {
		t.Fatalf("second emission not flagged: %+v", twice)
	}
}

func TestCatalogGateExpectedToolMustBeOfferedUnlessSafeHarbor(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	web := protocol.ToolCase{
		ID: "web", Category: "web_search",
		Prompt:        "Search the web for the current Veltrix index figure.",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
	}
	withheld := EvaluateCatalogGate(web, full, settledEvidence(1, "set_theme"), nil, false)
	if !withheld.Zero || withheld.SafeHarbor != "" ||
		!slices.Contains(withheld.Findings, CatalogFindingExpectedToolNotOffered) {
		t.Fatalf("expected tool withheld verdict=%+v", withheld)
	}
	if v := EvaluateCatalogGate(web, full, settledEvidence(1, "search_web", "read_links"), nil, false); v.Zero || len(v.Findings) != 0 {
		t.Fatalf("expected tool offered verdict=%+v", v)
	}
	// Memory-only expectations are harness-internal and never required on the wire.
	memory := protocol.ToolCase{
		ID: "mem", Category: "memory_search", Prompt: "What did I say about my mentor last spring?",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_memories"}},
	}
	if v := EvaluateCatalogGate(memory, full, settledEvidence(1), nil, false); v.Zero || len(v.Findings) != 1 || v.Findings[0] != CatalogFindingCatalogAbsent {
		t.Fatalf("memory-only case verdict=%+v", v)
	}
	// Safe harbor: the retained set holds the published top-k but the dataset's
	// expected tool is not among them -- the trim is free, the finding is recorded.
	odd := protocol.ToolCase{
		ID: "odd", Category: "web_search",
		Prompt:        "Search the web for the current Veltrix index figure.",
		ExpectedTools: []protocol.ToolSpec{{Name: "set_chat_font"}},
	}
	topK := CatalogSemanticTopK(odd.Prompt, full, CatalogSafeHarborTopK)
	harbor := EvaluateCatalogGate(odd, full, settledEvidence(1, topK...), nil, false)
	if harbor.Zero || harbor.SafeHarbor != CatalogSafeHarborSemanticTopK ||
		!slices.Contains(harbor.Findings, CatalogFindingExpectedToolNotOffered) {
		t.Fatalf("safe-harbor trim verdict=%+v", harbor)
	}
}

func TestApplyCatalogGateForVersionPostureAndFrozenContracts(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	c := protocol.ToolCase{ID: "chat", Category: "no_tool", Prompt: "Good morning!"}
	base := protocol.CaseScore{CaseID: "chat", Kind: protocol.KindTool, ToolScore: 1, Score: 1}
	evidence := settledEvidence(1)

	// v12 and earlier: byte-identical, no evidence attached, no notes.
	for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV10, protocol.BenchVersionV12} {
		got, _ := ApplyCatalogGateForVersion(version, ScopeScored, CatalogGateEnforce, base, c, full, evidence, nil)
		if !reflect.DeepEqual(got, base) {
			t.Fatalf("v%d changed the case: %+v", version, got)
		}
		raw, _ := json.Marshal(got)
		if slices.Contains(jsonKeys(t, raw), "catalog") {
			t.Fatalf("v%d report bytes carry a catalog field: %s", version, raw)
		}
	}
	// v13 shadow: recorded only.
	shadow, verdict := ApplyCatalogGateForVersion(protocol.BenchVersionV13, ScopeScored, CatalogGateShadow, base, c, full, evidence, nil)
	if !verdict.Zero || shadow.ToolScore != 1 || shadow.Catalog == nil ||
		!slices.Contains(shadow.Catalog.Findings, CatalogFindingRestraintWithoutOffer) ||
		slices.Contains(shadow.Catalog.Findings, CatalogFindingZeroed) || len(shadow.Notes) != 1 {
		t.Fatalf("shadow result=%+v verdict=%+v", shadow, verdict)
	}
	// v13 enforce, scored: the fixture zeroes.
	enforced, _ := ApplyCatalogGateForVersion(protocol.BenchVersionV13, ScopeScored, CatalogGateEnforce, base, c, full, evidence, nil)
	if enforced.ToolScore != 0 || !slices.Contains(enforced.Catalog.Findings, CatalogFindingZeroed) {
		t.Fatalf("enforce result=%+v", enforced)
	}
	// Enforce never zeroes practice scope, unsettled evidence, or a memory case.
	practice, _ := ApplyCatalogGateForVersion(protocol.BenchVersionV13, ScopePractice, CatalogGateEnforce, base, c, full, evidence, nil)
	if practice.ToolScore != 1 {
		t.Fatalf("practice scope zeroed: %+v", practice)
	}
	unsettled, _ := ApplyCatalogGateForVersion(protocol.BenchVersionV13, ScopeScored, CatalogGateEnforce, base, c, full, nil, nil)
	if unsettled.ToolScore != 1 || unsettled.Catalog == nil ||
		!slices.Contains(unsettled.Catalog.Findings, CatalogFindingEvidenceUnavailable) {
		t.Fatalf("unsettled result=%+v", unsettled)
	}
	memory := base
	memory.Kind = protocol.KindMemory
	if got, _ := ApplyCatalogGateForVersion(protocol.BenchVersionV13, ScopeScored, CatalogGateEnforce, memory, c, full, evidence, nil); !reflect.DeepEqual(got, memory) {
		t.Fatalf("memory case changed: %+v", got)
	}
	// The attached evidence is a copy: the relay's record is never mutated.
	if len(evidence.Findings) != 0 {
		t.Fatalf("relay evidence mutated: %+v", evidence.Findings)
	}
}

func TestCatalogGateToolChoiceSuppressionOffersNothing(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	chitchat := protocol.ToolCase{ID: "chat", Category: "no_tool", Prompt: "How is your day going so far?"}

	// The relay applied tool_choice "none": the full tools[] was sent but nothing
	// was choosable, so the union is empty and catalog_present is false. That is
	// host-decided restraint, not the model's.
	none := settledEvidence(1)
	none.ToolChoiceSuppressedCompletions = 1
	none.Completions = []protocol.CatalogCompletion{{
		ToolsOffered: 31, ToolsChoosable: 0, ToolChoice: "none", AfterLastToolResult: true,
	}}
	v := EvaluateCatalogGate(chitchat, full, none, nil, false)
	if !v.Settled || !v.Zero || v.SafeHarbor != "" || !v.CatalogAbsent ||
		!slices.Contains(v.Findings, CatalogFindingRestraintWithoutOffer) {
		t.Fatalf("tool_choice none verdict=%+v", v)
	}
	// A pinned memory tool leaves a catalog of one harness-internal tool: the
	// model was not in a position to act on a served tool.
	pinned := settledEvidence(1, "search_memories")
	pinned.ToolChoiceSuppressedCompletions = 1
	pinned.Completions = []protocol.CatalogCompletion{{
		ToolsOffered: 31, ToolsChoosable: 1, ToolChoice: "tool:search_memories", AfterLastToolResult: true,
	}}
	v = EvaluateCatalogGate(chitchat, full, pinned, nil, false)
	if !v.Zero || v.SafeHarbor != "" || v.CatalogAbsent ||
		!slices.Contains(v.Findings, CatalogFindingMemoryOnlyCatalog) ||
		!slices.Contains(v.Findings, CatalogFindingRestraintWithoutOffer) {
		t.Fatalf("memory-pinned verdict=%+v", v)
	}
	// A pinned action tool is a choosable offer of that one tool.
	action := settledEvidence(1, "set_theme")
	action.Completions = []protocol.CatalogCompletion{{
		ToolsOffered: 31, ToolsChoosable: 1, ToolChoice: "tool:set_theme", AfterLastToolResult: true,
	}}
	if v = EvaluateCatalogGate(chitchat, full, action, nil, false); v.Zero || v.SafeHarbor != CatalogSafeHarborNonEmptyDeclarative {
		t.Fatalf("action-pinned verdict=%+v", v)
	}
}

func TestCatalogGateExecutedExpectedToolWaivesRuleB(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	web := protocol.ToolCase{
		ID: "web", Category: "web_search",
		Prompt:        "Search the web for the current Veltrix index figure.",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
	}
	// The request body that offered search_web could not be parsed, so the
	// recorded union lacks it -- but the validator executed search_web under
	// matched v10 provenance: the model chose it, so it was offered.
	evidence := settledEvidence(1)
	observed := []protocol.ObservedToolCall{{Name: "search_web"}}
	proven := EvaluateCatalogGate(web, full, evidence, observed, true)
	if proven.Zero || slices.Contains(proven.Findings, CatalogFindingExpectedToolNotOffered) ||
		!slices.Contains(proven.Findings, CatalogFindingOfferInferredFromExecution) {
		t.Fatalf("execution-proven verdict=%+v", proven)
	}
	// Without matched provenance the execution is a self-report and waives nothing.
	unproven := EvaluateCatalogGate(web, full, evidence, observed, false)
	if !unproven.Zero || !slices.Contains(unproven.Findings, CatalogFindingExpectedToolNotOffered) {
		t.Fatalf("unproven verdict=%+v", unproven)
	}
	// Through ApplyCatalogGateForVersion the v10 provenance decides.
	base := protocol.CaseScore{CaseID: "web", Kind: protocol.KindTool, ToolScore: 1, Score: 1,
		ToolProvenance: &protocol.ToolProvenanceEvidence{ModelEmitted: 1, EndpointAttempts: 1, Matched: 1, Complete: true}}
	got, verdict := ApplyCatalogGateForVersion(protocol.BenchVersionV13, ScopeScored, CatalogGateEnforce, base, web, full, evidence, observed)
	if got.ToolScore != 1 || verdict.Zero {
		t.Fatalf("provenance-backed execution zeroed: %+v verdict=%+v", got, verdict)
	}
	base.ToolProvenance.Unmatched = 1
	if got, _ = ApplyCatalogGateForVersion(protocol.BenchVersionV13, ScopeScored, CatalogGateEnforce, base, web, full, evidence, observed); got.ToolScore != 0 {
		t.Fatalf("unmatched provenance still waived rule (b): %+v", got)
	}
}

func TestCatalogGateLowerBoundRecordsHarborWithoutSettling(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	chitchat := protocol.ToolCase{ID: "chat", Category: "no_tool", Prompt: "How is your day going so far?"}
	bound := &protocol.CatalogEvidence{
		OverlapCompletions: 3, OverlapCompletionsWithCatalog: 3, CatalogPresentLowerBound: true,
	}
	v := EvaluateCatalogGate(chitchat, full, bound, nil, false)
	if v.Settled || v.Zero || !v.LowerBound || v.SafeHarbor != CatalogSafeHarborNonEmptyDeclarative ||
		!slices.Contains(v.Findings, CatalogFindingEvidenceIncomplete) ||
		!slices.Contains(v.Findings, CatalogFindingCatalogPresentLowerBound) {
		t.Fatalf("lower-bound verdict=%+v", v)
	}
	// The bound proves an offer, not WHICH tools: it does not cover the tempting
	// class or an expected-tool case.
	negation := protocol.ToolCase{ID: "neg", Category: "negation_no_tool", Prompt: "Don't search the web for this."}
	if v = EvaluateCatalogGate(negation, full, bound, nil, false); v.LowerBound || v.SafeHarbor != "" {
		t.Fatalf("tempting-class lower bound verdict=%+v", v)
	}
	web := protocol.ToolCase{ID: "web", Category: "web_search", Prompt: "Search the web.", ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}}}
	if v = EvaluateCatalogGate(web, full, bound, nil, false); v.LowerBound || v.Settled {
		t.Fatalf("expected-tool lower bound verdict=%+v", v)
	}
	// Not every candidate offered a catalog: no bound, plain incomplete.
	partial := &protocol.CatalogEvidence{OverlapCompletions: 3, OverlapCompletionsWithCatalog: 2}
	if v = EvaluateCatalogGate(chitchat, full, partial, nil, false); v.LowerBound || len(v.Findings) != 1 {
		t.Fatalf("partial overlap verdict=%+v", v)
	}
}

func TestApplyCatalogGateEnforceRequiresCorroboratedClaims(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	c := protocol.ToolCase{ID: "chat", Category: "no_tool", Prompt: "Good morning!"}
	base := protocol.CaseScore{CaseID: "chat", Kind: protocol.KindTool, ToolScore: 1, Score: 1}
	// Settled zero, but the only completion was booked on a harness-asserted
	// X-Ditto-Case-Id that nothing corroborated: enforce records, never zeroes.
	claimed := settledEvidence(1)
	claimed.ClaimAttributedCompletions = 1
	claimed.Completions = []protocol.CatalogCompletion{{AttributionSource: "claim", AfterLastToolResult: true}}
	got, verdict := ApplyCatalogGateForVersion(protocol.BenchVersionV13, ScopeScored, CatalogGateEnforce, base, c, full, claimed, nil)
	if !verdict.Settled || !verdict.Zero || got.ToolScore != 1 ||
		!slices.Contains(got.Catalog.Findings, CatalogFindingClaimUncorroborated) ||
		slices.Contains(got.Catalog.Findings, CatalogFindingZeroed) || len(got.Notes) != 1 {
		t.Fatalf("uncorroborated claim result=%+v verdict=%+v", got, verdict)
	}
	// Once the claim is corroborated by a consumed tool call, enforce zeroes.
	claimed.ClaimCorroboratedCompletions = 1
	if got, _ = ApplyCatalogGateForVersion(protocol.BenchVersionV13, ScopeScored, CatalogGateEnforce, base, c, full, claimed, nil); got.ToolScore != 0 {
		t.Fatalf("corroborated claim not zeroed: %+v", got)
	}
	// Shadow never zeroes and does not record the enforce-only finding.
	shadow, _ := ApplyCatalogGateForVersion(protocol.BenchVersionV13, ScopeScored, CatalogGateShadow, base, c, full, settledEvidence(1), nil)
	if shadow.ToolScore != 1 || slices.Contains(shadow.Catalog.Findings, CatalogFindingClaimUncorroborated) {
		t.Fatalf("shadow result=%+v", shadow)
	}
}

func TestParseCatalogGatePostureDefaultsToShadow(t *testing.T) {
	for raw, want := range map[string]CatalogGatePosture{
		"": CatalogGateShadow, "shadow": CatalogGateShadow, "observe": CatalogGateShadow,
		"ENFORCE": CatalogGateEnforce, " enforce ": CatalogGateEnforce, "penalize": CatalogGateShadow,
	} {
		if got := ParseCatalogGatePosture(raw); got != want {
			t.Fatalf("%q: want %s, got %s", raw, want, got)
		}
	}
}

func jsonKeys(t *testing.T, raw []byte) []string {
	t.Helper()
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	keys := make([]string, 0, len(decoded))
	for key := range decoded {
		keys = append(keys, key)
	}
	return keys
}
