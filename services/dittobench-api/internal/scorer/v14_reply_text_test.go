package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV14ResultUsageReadsTheReplyAsRendered covers the result-usage needle
// check under the v14 reply reading: grade.ReplyAsRead renders markdown and
// folds non-ASCII spaces, and answerCarriesValueV14 lets sentence punctuation
// end the needle. Every v13 result is unchanged and every guard still holds.
func TestV14ResultUsageReadsTheReplyAsRendered(t *testing.T) {
	base := protocol.CaseScore{ToolScore: 1}
	for _, tt := range []struct {
		name     string
		answer   string
		v13, v14 float64
	}{
		{"needle then comma", "The Veltrix index closed at 3,418, up two points.", 0.1, 1},
		{"bold needle then comma", "The Veltrix index closed at **3,418**, up two points.", 1, 1},
		{"needle behind a no-break space", "The Veltrix index closed at 3\u00a0418 points.", 1, 1},
		{"needle in a table", "| Index | Close |\n|---|---|\n| Veltrix | 3,418 |", 1, 1},
		{"plain needle", "It reached 3,418 points.", 1, 1},
		{"final period", "It reached 3,418.", 1, 1},
		// Guards: decoy, numbers inside numbers, signs and decimals.
		{"decoy then comma is now caught", "It reached 7,902, a record.", 0.1, 0},
		{"needle and decoy", "Either 3,418, or 7,902.", 0, 0},
		{"decimal tail", "It reached 3,418.5 points.", 0.1, 0.1},
		{"longer number", "It reached 3,4180 points.", 0.1, 0.1},
		{"negated", "It moved by -3,418.", 0.1, 0.1},
		{"miss", "No figure was returned.", 0.1, 0.1},
	} {
		for _, v := range []struct {
			version int
			want    float64
		}{{protocol.BenchVersionV13, tt.v13}, {protocol.BenchVersionV14, tt.v14}} {
			read := grade.ReplyTextAsRead(v.version, tt.answer)
			got := ComposeResultUsageForVersion(v.version, base, read, "3,418", "7,902")
			if got.Score != v.want {
				t.Errorf("%s at v%d: scored %v, want %v (%v)", tt.name, v.version, got.Score, v.want, got.Notes)
			}
		}
	}
}

// TestAnswerCarriesValueV14OnlyRelaxesTrailingPunctuation pins that the v14
// rule differs from the v13 one only where a "." or "," ends a sentence.
func TestAnswerCarriesValueV14OnlyRelaxesTrailingPunctuation(t *testing.T) {
	for _, tt := range []struct {
		answer   string
		v13, v14 bool
	}{
		{"3,418", true, true},
		{"value 3418 here", true, true},
		{"3,418, then", false, true},
		{"3,418. Then", false, true},
		{"(3,418), then", true, true},
		{"3,418.5", false, false},
		{"3,418,000", false, false},
		{"13,418", false, false},
		{"-3,418", false, false},
		{"+3,418", false, false},
		{"3,4185", false, false},
	} {
		if got := answerCarriesValue(tt.answer, "3,418"); got != tt.v13 {
			t.Errorf("v13 %q = %v, want %v", tt.answer, got, tt.v13)
		}
		if got := answerCarriesValueV14(tt.answer, "3,418"); got != tt.v14 {
			t.Errorf("v14 %q = %v, want %v", tt.answer, got, tt.v14)
		}
	}
}

// TestV14RestraintGroundingReadsRenderedMarkdown is the tool-axis vector for
// the reply reading: a clarifying question that bolds part of the searched
// record's phrase cites it at v14 and stays ungrounded at v13.
func TestV14RestraintGroundingReadsRenderedMarkdown(t *testing.T) {
	ask := protocol.ToolCase{
		ID: "ask", Category: datagen.V13RestraintCategoryPrefix + "effort", MaxToolCalls: 0,
		Restraint: &protocol.RestraintClaim{
			Kind: protocol.RestraintClarifyFirst, ForbiddenTools: []string{"set_reasoning_effort"},
			Accept:    []string{"effort", "level"},
			Grounding: []string{"short and fast"},
		},
	}
	resp := protocol.RunResponse{FinalText: "You described it as **short** and fast before — which effort level should be the default?"}
	if got := ScoreToolCaseObservedForVersion(ask, resp, true, nil, ScopeScored, protocol.BenchVersionV13); got.Score != 0 {
		t.Fatalf("v13 changed: scored %v (%v)", got.Score, got.Notes)
	}
	if got := ScoreToolCaseObservedForVersion(ask, resp, true, nil, ScopeScored, protocol.BenchVersionV14); got.Score != 1 {
		t.Fatalf("v14 scored %v, want 1 (%v)", got.Score, got.Notes)
	}
}
