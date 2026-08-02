package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func webToolCase() protocol.ToolCase {
	return protocol.ToolCase{
		ID:            "web_search-1-0",
		Category:      "web_search",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
		MaxToolCalls:  1,
	}
}

// When the validator observed the trajectory, it is authoritative: a harness
// that self-reports NOTHING but was observed calling the right tool gets credit.
func TestScoreToolCaseObserved_AuthoritativeOverSelfReport(t *testing.T) {
	c := webToolCase()
	resp := protocol.RunResponse{FinalText: "here you go", ToolCalls: nil} // self-report: no calls
	observed := []protocol.ObservedToolCall{{Name: "search_web", Hop: 0}}

	cs := ScoreToolCaseObserved(c, resp, true, observed)
	if cs.ToolScore == 0 {
		t.Fatalf("observed correct call should score > 0, got %v", cs.ToolScore)
	}
	if len(cs.Called) != 1 || cs.Called[0] != "search_web" {
		t.Fatalf("Called should reflect observed trajectory, got %v", cs.Called)
	}
	// Conversely: a harness that self-reports the right call but was NOT observed
	// falls back to self-report (no observation to override it).
	self := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{{Name: "search_web"}}}
	if base := ScoreToolCaseObserved(c, self, true, nil); base.ToolScore == 0 {
		t.Fatalf("self-report fallback should score the reported call, got %v", base.ToolScore)
	}
}

// A harness observed calling the WRONG tool cannot hide it by self-reporting the
// right one — the observation overrides the (untrusted) self-report.
func TestScoreToolCaseObserved_WrongObservedBeatsRightSelfReport(t *testing.T) {
	c := webToolCase()
	resp := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{{Name: "search_web"}}} // claims right
	observed := []protocol.ObservedToolCall{{Name: "search_memories"}}                         // actually wrong

	cs := ScoreToolCaseObserved(c, resp, true, observed)
	if cs.ToolScore != 0 {
		t.Fatalf("wrong observed call should score 0 regardless of self-report, got %v", cs.ToolScore)
	}
}

func TestCapUnobserved(t *testing.T) {
	high := protocol.CaseScore{Score: 1.0}
	if got := CapUnobserved(high); got.Score != UnobservedCeiling {
		t.Fatalf("high score should cap to %v, got %v", UnobservedCeiling, got.Score)
	}
	low := protocol.CaseScore{Score: 0.3}
	if got := CapUnobserved(low); got.Score != 0.3 {
		t.Fatalf("below-ceiling score must be unchanged, got %v", got.Score)
	}
	// The cap is applied to the final score, so an unobserved observable case
	// (perfect self-reported trajectory) cannot beat an observed one.
	capped := CapUnobserved(FinishTool(protocol.CaseScore{ToolScore: 1.0}))
	if capped.Score > UnobservedCeiling {
		t.Fatalf("capped composite should not exceed ceiling, got %v", capped.Score)
	}
}
