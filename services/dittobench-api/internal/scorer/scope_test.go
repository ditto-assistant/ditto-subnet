package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// observableToolCase is a validator-served, observable tool case (its expected
// tool is a real external-world tool the mock endpoint serves).
func observableToolCase() protocol.ToolCase {
	return protocol.ToolCase{
		ID:            "web_search-9-0",
		Category:      "web_search",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
		MaxToolCalls:  1,
	}
}

// In SCORED scope an observable tool case the validator did NOT observe execute
// scores 0 — the free 0.5 self-report ceiling is gone. Practice still caps at 0.5.
func TestCapUnobservedScope_ScoredZeroPracticeCapped(t *testing.T) {
	c := observableToolCase()
	// Perfect self-report, but nothing observed.
	self := protocol.RunResponse{FinalText: "ok", ToolCalls: []protocol.ObservedToolCall{{Name: "search_web"}}}

	// Practice: capped at the 0.5 ceiling (unchanged behavior).
	pc := FinishTool(ScoreToolCaseObservedScope(c, self, true, nil, ScopePractice))
	pc = CapUnobservedScope(pc, ScopePractice)
	if pc.Score != UnobservedCeiling {
		t.Fatalf("practice unobserved observable should cap at %v, got %v", UnobservedCeiling, pc.Score)
	}

	// Scored: the same unobserved observable case scores 0.
	sc := FinishTool(ScoreToolCaseObservedScope(c, self, true, nil, ScopeScored))
	sc = CapUnobservedScope(sc, ScopeScored)
	if sc.Score != 0 {
		t.Fatalf("scored unobserved observable must score 0, got %v", sc.Score)
	}
}

// An OBSERVED case is unaffected by scope: the ceiling only bites on the
// unobserved-observable fallback, so a scored run that actually observed the
// right call still earns full trajectory credit.
func TestCapUnobservedScope_ObservedUnaffected(t *testing.T) {
	c := observableToolCase()
	observed := []protocol.ObservedToolCall{{Name: "search_web", Hop: 0}}
	cs := FinishTool(ScoreToolCaseObservedScope(c, protocol.RunResponse{}, true, observed, ScopeScored))
	if cs.ToolScore == 0 {
		t.Fatalf("observed correct call should score > 0 even scored, got %v", cs.ToolScore)
	}
	// The cap is only applied by the caller for the UNobserved branch; an observed
	// case never routes through CapUnobservedScope, but even if it did the full
	// score would be zeroed only when unobserved — assert the ceiling helper value.
	if UnobservedCeilingFor(ScopeScored) != 0 || UnobservedCeilingFor(ScopePractice) != UnobservedCeiling {
		t.Fatalf("ceiling helper wrong: scored=%v practice=%v",
			UnobservedCeilingFor(ScopeScored), UnobservedCeilingFor(ScopePractice))
	}
}

// Memory routing is scored on OBSERVED misrouting only, identically in both
// scopes. Memory retrieval is internal and unobservable, so a legitimate
// direct-store answer with no catalog call must be credited, and a self-reported
// memory-tool call is never trusted as evidence (it is not in the observed
// trajectory). This replaces the old "require a memory-tool call when scored"
// rule, which was spoofable and zeroed correct direct-store retrieval.
func TestMemoryRoutingScoresOnObservedMisroutingOnly(t *testing.T) {
	c := protocol.ToolCase{
		ID:            "memq-1",
		Category:      "route_memory",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_memories"}},
	}
	// Direct-store answer, no catalog call: credited in BOTH scopes.
	textOnly := protocol.RunResponse{FinalText: "Your favorite color is blue."}
	for _, sc := range []Scope{ScopePractice, ScopeScored} {
		if got := ScoreToolCaseScope(c, textOnly, true, sc); got.ToolScore != 1.0 {
			t.Fatalf("direct-store memory answer must score 1.0 in %v, got %v", sc, got.ToolScore)
		}
	}
	// An observed memory-tool call is also fine in both scopes.
	withCall := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{{Name: "search_memories"}}}
	if got := ScoreToolCaseScope(c, withCall, true, ScopeScored); got.ToolScore != 1.0 {
		t.Fatalf("observed memory-tool call should score 1.0, got %v", got.ToolScore)
	}
	// A misroute to a non-memory tool zeroes in both scopes.
	misroute := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{{Name: "search_web"}}}
	for _, sc := range []Scope{ScopePractice, ScopeScored} {
		if got := ScoreToolCaseScope(c, misroute, true, sc); got.ToolScore != 0 {
			t.Fatalf("memory-routing misroute must score 0 in %v, got %v", sc, got.ToolScore)
		}
	}
	// A pure no-op (no call, no answer) still fails the routing trap.
	noop := protocol.RunResponse{}
	if got := ScoreToolCaseScope(c, noop, true, ScopeScored); got.ToolScore != 0 {
		t.Fatalf("no-op memory routing must score 0, got %v", got.ToolScore)
	}
	// A direct-store response that populates only the answer SLOT (empty
	// final_text) is substantive retrieval evidence too: the protocol grades
	// the slot with a prose fallback, so routing must credit it the same way.
	slotOnly := protocol.RunResponse{Answer: "blue"}
	for _, sc := range []Scope{ScopePractice, ScopeScored} {
		if got := ScoreToolCaseScope(c, slotOnly, true, sc); got.ToolScore != 1.0 {
			t.Fatalf("answer-slot-only memory answer must score 1.0 in %v, got %v", sc, got.ToolScore)
		}
	}
}

// The exported practice wrappers must remain identical to the scoped-practice
// path (backward-compatible defaults).
func TestPracticeWrappersUnchanged(t *testing.T) {
	c := observableToolCase()
	self := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{{Name: "search_web"}}}
	a := ScoreToolCase(c, self, true)
	b := ScoreToolCaseScope(c, self, true, ScopePractice)
	if a.ToolScore != b.ToolScore {
		t.Fatalf("ScoreToolCase must equal practice-scope: %v vs %v", a.ToolScore, b.ToolScore)
	}
	hi := protocol.CaseScore{Score: 1.0}
	if CapUnobserved(hi).Score != CapUnobservedScope(hi, ScopePractice).Score {
		t.Fatalf("CapUnobserved must equal practice-scope cap")
	}
}
