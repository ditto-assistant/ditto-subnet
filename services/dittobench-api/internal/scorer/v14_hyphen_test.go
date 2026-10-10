package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV14RestraintGroundingReadsTypographicHyphens is the #2734 tool-axis
// vector: a clarifying question that quotes the record's "short-and-fast"
// with U+2011 cites the searched record at v14 and stays ungrounded at v13.
func TestV14RestraintGroundingReadsTypographicHyphens(t *testing.T) {
	ask := protocol.ToolCase{
		ID: "ask", Category: datagen.V13RestraintCategoryPrefix + "effort", MaxToolCalls: 0,
		Restraint: &protocol.RestraintClaim{
			Kind: protocol.RestraintClarifyFirst, ForbiddenTools: []string{"set_reasoning_effort"},
			Accept:    []string{"effort", "level"},
			Grounding: []string{"short-and-fast"},
		},
	}
	resp := protocol.RunResponse{FinalText: "You described it as short‑and‑fast before — which effort level should be the default?"}
	if got := ScoreToolCaseObservedForVersion(ask, resp, true, nil, ScopeScored, protocol.BenchVersionV13); got.Score != 0 {
		t.Fatalf("v13 changed: scored %v (%v)", got.Score, got.Notes)
	}
	if got := ScoreToolCaseObservedForVersion(ask, resp, true, nil, ScopeScored, protocol.BenchVersionV14); got.Score != 1 {
		t.Fatalf("v14 scored %v, want 1 (%v)", got.Score, got.Notes)
	}
}
