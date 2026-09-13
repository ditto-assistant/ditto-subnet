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
	if v := EvaluateCatalogGate(c, full, nil, nil); v.Settled || v.Zero ||
		!slices.Equal(v.Findings, []string{CatalogFindingEvidenceUnavailable}) {
		t.Fatalf("nil evidence verdict=%+v", v)
	}
	incomplete := settledEvidence(1)
	incomplete.CompletionsTotal = nil
	incomplete.Complete = false
	if v := EvaluateCatalogGate(c, full, incomplete, nil); v.Settled || v.Zero ||
		!slices.Equal(v.Findings, []string{CatalogFindingEvidenceIncomplete}) {
		t.Fatalf("incomplete evidence verdict=%+v", v)
	}
	truncated := settledEvidence(1, "search_web")
	truncated.Complete = false
	if v := EvaluateCatalogGate(c, full, truncated, nil); v.Settled || v.Zero {
		t.Fatalf("truncated evidence verdict=%+v", v)
	}
}

func TestCatalogGateRestraintRequiresAnOfferUnlessSafeHarbor(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	chitchat := protocol.ToolCase{ID: "chat", Category: "no_tool", Prompt: "How is your day going so far?"}

	// Fixture: emptying tools[] on a declarative turn is restraint without offer.
	empty := EvaluateCatalogGate(chitchat, full, settledEvidence(1), nil)
	if !empty.Settled || !empty.Zero || !empty.CatalogAbsent || empty.SafeHarbor != "" ||
		!slices.Contains(empty.Findings, CatalogFindingRestraintWithoutOffer) ||
		!slices.Contains(empty.Findings, CatalogFindingCatalogAbsent) {
		t.Fatalf("empty catalog on declarative turn verdict=%+v", empty)
	}
	// No completion at all: the host answered without the model.
	none := EvaluateCatalogGate(chitchat, full, settledEvidence(0), nil)
	if !none.Settled || !none.Zero || !none.NoCompletion || none.CatalogAbsent ||
		!slices.Contains(none.Findings, CatalogFindingNoModelCompletion) ||
		!slices.Contains(none.Findings, CatalogFindingRestraintWithoutOffer) {
		t.Fatalf("no completion verdict=%+v", none)
	}
	// Safe harbor: any non-empty catalog on a declarative/chit-chat case.
	trimmed := EvaluateCatalogGate(chitchat, full, settledEvidence(1, "set_theme"), nil)
	if !trimmed.Settled || trimmed.Zero || trimmed.SafeHarbor != CatalogSafeHarborNonEmptyDeclarative ||
		!slices.Contains(trimmed.Findings, CatalogFindingSafeHarbor) ||
		slices.Contains(trimmed.Findings, CatalogFindingRestraintWithoutOffer) {
		t.Fatalf("non-empty catalog on declarative turn verdict=%+v", trimmed)
	}
	// Abstention and arg_hallucination are declarative/decline classes too.
	for _, category := range []string{"abstention", "arg_hallucination"} {
		c := protocol.ToolCase{ID: category, Category: category, Prompt: "Change my theme."}
		if v := EvaluateCatalogGate(c, full, settledEvidence(1, "search_web"), nil); v.Zero || v.SafeHarbor == "" {
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
	omitted := EvaluateCatalogGate(negation, full, settledEvidence(1, "set_theme", "set_chat_font"), nil)
	if !omitted.Zero || omitted.SafeHarbor != "" ||
		!slices.Contains(omitted.Findings, CatalogFindingRestraintWithoutOffer) {
		t.Fatalf("negation with the cued class withheld verdict=%+v", omitted)
	}
	retained := EvaluateCatalogGate(negation, full, settledEvidence(1, append(topK, "set_theme")...), nil)
	if retained.Zero || retained.SafeHarbor != CatalogSafeHarborSemanticTopK {
		t.Fatalf("negation with the semantic top-k retained verdict=%+v", retained)
	}
	// A full catalog always passes.
	names := make([]string, 0, len(full))
	for _, tool := range full {
		names = append(names, tool.Name)
	}
	if v := EvaluateCatalogGate(negation, full, settledEvidence(1, names...), nil); v.Zero || v.SafeHarbor == "" {
		t.Fatalf("full catalog verdict=%+v", v)
	}
}

func TestCatalogGateSwallowedModelCallScoresRestraintOnModelChoice(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	c := protocol.ToolCase{ID: "chat", Category: "no_tool", Prompt: "Hope you're having a nice afternoon!"}
	evidence := settledEvidence(1, "set_theme", "search_web")
	evidence.ModelEmittedToolCalls = []string{"set_theme"}
	swallowed := EvaluateCatalogGate(c, full, evidence, nil)
	if !swallowed.Zero || !slices.Contains(swallowed.Findings, CatalogFindingSwallowedModelCall) {
		t.Fatalf("swallowed call verdict=%+v", swallowed)
	}
	// Executed calls are not swallowed (the base rule already zeroes the action).
	executed := EvaluateCatalogGate(c, full, evidence, []protocol.ObservedToolCall{{Name: "set_theme"}})
	if slices.Contains(executed.Findings, CatalogFindingSwallowedModelCall) {
		t.Fatalf("executed call flagged as swallowed: %+v", executed)
	}
	// Memory retrieval is harness-internal and never served: not an action.
	evidence.ModelEmittedToolCalls = []string{"search_memories", "fetch_memories"}
	memory := EvaluateCatalogGate(c, full, evidence, nil)
	if memory.Zero || slices.Contains(memory.Findings, CatalogFindingSwallowedModelCall) {
		t.Fatalf("memory emission flagged as swallowed: %+v", memory)
	}
	// Emitted twice, executed once: one was swallowed.
	evidence.ModelEmittedToolCalls = []string{"search_web", "search_web"}
	twice := EvaluateCatalogGate(c, full, evidence, []protocol.ObservedToolCall{{Name: "search_web"}})
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
	withheld := EvaluateCatalogGate(web, full, settledEvidence(1, "set_theme"), nil)
	if !withheld.Zero || withheld.SafeHarbor != "" ||
		!slices.Contains(withheld.Findings, CatalogFindingExpectedToolNotOffered) {
		t.Fatalf("expected tool withheld verdict=%+v", withheld)
	}
	if v := EvaluateCatalogGate(web, full, settledEvidence(1, "search_web", "read_links"), nil); v.Zero || len(v.Findings) != 0 {
		t.Fatalf("expected tool offered verdict=%+v", v)
	}
	// Memory-only expectations are harness-internal and never required on the wire.
	memory := protocol.ToolCase{
		ID: "mem", Category: "memory_search", Prompt: "What did I say about my mentor last spring?",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_memories"}},
	}
	if v := EvaluateCatalogGate(memory, full, settledEvidence(1), nil); v.Zero || len(v.Findings) != 1 || v.Findings[0] != CatalogFindingCatalogAbsent {
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
	harbor := EvaluateCatalogGate(odd, full, settledEvidence(1, topK...), nil)
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
