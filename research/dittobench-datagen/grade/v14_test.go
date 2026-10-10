package grade

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV14PolicyOnlyAddsTheHyphenFold pins the v14 floor: every v13 policy
// field carries forward and the typographic hyphen fold is the one addition.
// V13 stays a frozen scored contract.
func TestV14PolicyOnlyAddsTheHyphenFold(t *testing.T) {
	v13 := gradingPolicyForVersion(protocol.BenchVersionV13)
	v14 := gradingPolicyForVersion(protocol.BenchVersionV14)
	if v13.foldTypographicHyphens || !v14.foldTypographicHyphens {
		t.Fatalf("foldTypographicHyphens must be v14-only: v13=%+v v14=%+v", v13, v14)
	}
	v14.foldTypographicHyphens = false
	if v14 != v13 {
		t.Fatalf("v14 changed a v13 policy field: v13=%+v v14=%+v", v13, v14)
	}
}

// TestV14TypographicHyphensMatchTheASCIIRecord is the #2734 acceptance
// vector: the pinned model's U+2011/U+2010 inside an identifier copied from an
// ASCII record scores at v14 and is still a miss at v13. A distractor or a
// forbidden value written with the same hyphen is caught at v14.
func TestV14TypographicHyphensMatchTheASCIIRecord(t *testing.T) {
	handle := protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "VK-48HJXP6A63", DistractorAnswers: []string{"VK-48HJXP6A36"}}
	nbsp := protocol.RunResponse{FinalText: "The Signal handle you have for Melanie Brown is **VK‑48HJXP6A63**. Answer: VK‑48HJXP6A63"}
	hyphen := protocol.RunResponse{Answer: "VK‐48HJXP6A63", FinalText: "Your handle is VK‐48HJXP6A63."}
	for _, tt := range []struct {
		name    string
		version int
		resp    protocol.RunResponse
		want    float64
	}{
		{"v13 non-breaking hyphen stays a miss", protocol.BenchVersionV13, nbsp, 0},
		{"v13 hyphen stays a miss", protocol.BenchVersionV13, hyphen, 0},
		{"v14 non-breaking hyphen", protocol.BenchVersionV14, nbsp, 1},
		{"v14 hyphen in slot and prose", protocol.BenchVersionV14, hyphen, 1},
		{"v14 distractor with non-breaking hyphen", protocol.BenchVersionV14, protocol.RunResponse{FinalText: "Your handle is VK‑48HJXP6A36."}, 0},
	} {
		mc := handle
		mc.BenchVersion = tt.version
		if got := Memory(mc, tt.resp); got.Score != tt.want {
			t.Errorf("%s: scored %v, want %v (%v)", tt.name, got.Score, tt.want, got.Notes)
		}
	}

	leak := protocol.MemoryCase{BenchVersion: protocol.BenchVersionV14, AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "GAVOTU-8841"}
	if got := Memory(leak, protocol.RunResponse{FinalText: "GAVOTU‑8841"}); got.Score != 0 {
		t.Fatalf("v14 forbidden value behind U+2011 scored %v: %v", got.Score, got.Notes)
	}
}

// TestFoldReplyHyphensIsVersionGated pins the exported scorer hook: unchanged
// below v14, both hyphens folded from v14, hidden-value-free and dash-free.
func TestFoldReplyHyphensIsVersionGated(t *testing.T) {
	resp := protocol.RunResponse{Answer: "a‐b", FinalText: "c‑d – e — f"}
	if got := FoldReplyHyphens(protocol.BenchVersionV13, resp); got.Answer != resp.Answer || got.FinalText != resp.FinalText {
		t.Fatalf("v13 reply rewritten: %+v", got)
	}
	got := FoldReplyHyphens(protocol.BenchVersionV14, resp)
	if got.Answer != "a-b" || got.FinalText != "c-d – e — f" {
		t.Fatalf("v14 fold = %+v", got)
	}
}
