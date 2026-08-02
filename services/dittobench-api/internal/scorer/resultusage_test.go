package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestAnswerCarriesValue(t *testing.T) {
	cases := []struct {
		answer, needle string
		want           bool
	}{
		{"The figure is 3,418 points.", "3,418", true}, // exact comma form
		{"It reached 3418.", "3,418", true},            // no separator
		{"about 3 418 or so", "3,418", true},           // space separator
		{"the value was 34,180", "3,418", false},       // longer number, not a match
		{"no number here", "3,418", false},
		{"", "3,418", false},
		{"3,418", "", false}, // no needle → never credit
	}
	for _, c := range cases {
		if got := answerCarriesValue(c.answer, c.needle); got != c.want {
			t.Errorf("answerCarriesValue(%q,%q)=%v want %v", c.answer, c.needle, got, c.want)
		}
	}
}

func TestComposeResultUsage(t *testing.T) {
	base := protocol.CaseScore{ToolScore: 1.0} // called the right tool

	hit := ComposeResultUsage(base, "The report says 3,418 points.", "3,418")
	if hit.ResultUsage != 1.0 {
		t.Fatalf("answer with needle should score usage 1.0, got %v", hit.ResultUsage)
	}
	// 0.4*trajectory + 0.6*usage = 0.4 + 0.6 = 1.0
	if hit.Score != 1.0 {
		t.Fatalf("full trajectory + usage should score 1.0, got %v", hit.Score)
	}

	miss := ComposeResultUsage(base, "I searched but here's a guess: 999.", "3,418")
	if miss.ResultUsage != 0 {
		t.Fatalf("answer without needle should score usage 0, got %v", miss.ResultUsage)
	}
	// Right tool, ignored result → only the trajectory half (0.4).
	if miss.Score != 0.4 {
		t.Fatalf("ignoring the result should cap at the trajectory weight 0.4, got %v", miss.Score)
	}

	// No tool called (trajectory 0) and no needle → 0.
	none := ComposeResultUsage(protocol.CaseScore{ToolScore: 0}, "the Veltrix index is huge", "3,418")
	if none.Score != 0 {
		t.Fatalf("no trajectory, no usage → 0, got %v", none.Score)
	}
}

// TestComposeResultUsageDecoy: a harness that fished a number out of the wrong
// tool carries the served decoy, so the usage half is 0 even if the needle is
// also present — the decoy is the signature of grepping any number.
func TestComposeResultUsageDecoy(t *testing.T) {
	base := protocol.CaseScore{ToolScore: 1.0}
	// Answer carries only the decoy -> usage 0.
	d := ComposeResultUsageWithDecoy(base, "The figure is 7,902.", "3,418", "7,902")
	if d.ResultUsage != 0 {
		t.Fatalf("decoy-only answer must score usage 0, got %v", d.ResultUsage)
	}
	// Answer carries the needle and no decoy -> usage 1.
	ok := ComposeResultUsageWithDecoy(base, "It reached 3,418 points.", "3,418", "7,902")
	if ok.ResultUsage != 1 {
		t.Fatalf("needle answer must score usage 1, got %v", ok.ResultUsage)
	}
	// Carrying the decoy zeroes usage even if the needle is also present (grepping).
	both := ComposeResultUsageWithDecoy(base, "Maybe 3,418 or 7,902.", "3,418", "7,902")
	if both.ResultUsage != 0 {
		t.Fatalf("answer surfacing the decoy must score usage 0, got %v", both.ResultUsage)
	}
	// Empty decoy reproduces plain behavior.
	plain := ComposeResultUsageWithDecoy(base, "It reached 3,418.", "3,418", "")
	if plain.ResultUsage != 1 {
		t.Fatalf("empty decoy should be plain: got %v", plain.ResultUsage)
	}
}
