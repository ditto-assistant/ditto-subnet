package grade

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV14PolicyOnlyAddsIssue2734Matchers pins the v14 floor: every v13 policy
// field carries forward and the #2734 matcher fields are the only additions.
// V13 stays a frozen scored contract.
func TestV14PolicyOnlyAddsIssue2734Matchers(t *testing.T) {
	v13 := gradingPolicyForVersion(protocol.BenchVersionV13)
	v14 := gradingPolicyForVersion(protocol.BenchVersionV14)
	if v13.foldTypographicHyphens || v13.hyphenJoinedValues || v13.setRemovalPlace || v13.progressiveActions {
		t.Fatalf("a #2734 matcher reached v13: %+v", v13)
	}
	if !v14.foldTypographicHyphens || !v14.hyphenJoinedValues || !v14.setRemovalPlace || !v14.progressiveActions {
		t.Fatalf("v14 is missing a #2734 matcher: %+v", v14)
	}
	v14.foldTypographicHyphens, v14.hyphenJoinedValues, v14.setRemovalPlace, v14.progressiveActions = false, false, false, false
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

type versionVector struct {
	name    string
	version int
	resp    protocol.RunResponse
	want    float64
}

func checkVersionVectors(t *testing.T, base protocol.MemoryCase, vectors []versionVector) {
	t.Helper()
	for _, tt := range vectors {
		mc := base
		mc.BenchVersion = tt.version
		if got := Memory(mc, tt.resp); got.Score != tt.want {
			t.Errorf("%s: scored %v, want %v (%v)", tt.name, got.Score, tt.want, got.Notes)
		}
	}
}

// TestV14HyphenJoinedMultiWordValues is the #2734 point-2 acceptance vector:
// the pinned model joins a stored two-word value with a (typographic) hyphen
// when it uses it as a modifier. V14 reads that hyphen as the space it
// replaces; v13 keeps the miss. A changed word stays a miss and a
// hyphen-joined distractor or forbidden value is still caught.
func TestV14HyphenJoinedMultiWordValues(t *testing.T) {
	behavior := protocol.MemoryCase{QuestionType: "declarative-behavior", AnswerKind: protocol.AnswerValue,
		ExpectedAnswer: "true blue", DistractorAnswers: []string{"leafy green", "bluey green"}}
	checkVersionVectors(t, behavior, []versionVector{
		{"v13 non-breaking hyphen join stays a miss", protocol.BenchVersionV13, protocol.RunResponse{FinalText: "Answer: true‑blue accent"}, 0},
		{"v13 ASCII hyphen join stays a miss", protocol.BenchVersionV13, protocol.RunResponse{FinalText: "Answer: true-blue accent"}, 0},
		{"v14 non-breaking hyphen join", protocol.BenchVersionV14, protocol.RunResponse{FinalText: "Answer: true‑blue accent"}, 1},
		{"v14 ASCII hyphen join in slot and prose", protocol.BenchVersionV14, protocol.RunResponse{Answer: "True-Blue", FinalText: "I'd set the accent to True-Blue."}, 1},
		{"v14 hyphen-joined distractor is still caught", protocol.BenchVersionV14, protocol.RunResponse{FinalText: "Answer: true‑blue or leafy‑green"}, 0},
		{"v14 hyphen-joined distractor alone", protocol.BenchVersionV14, protocol.RunResponse{FinalText: "Answer: leafy‑green"}, 0},
		{"v14 joined inside a longer word is not the value", protocol.BenchVersionV14, protocol.RunResponse{FinalText: "Answer: true-blueish accent"}, 0},
	})

	leafy := protocol.MemoryCase{QuestionType: "declarative-behavior", AnswerKind: protocol.AnswerValue, ExpectedAnswer: "leafy green"}
	checkVersionVectors(t, leafy, []versionVector{
		{"v13 leafy-green stays a miss", protocol.BenchVersionV13, protocol.RunResponse{FinalText: "Answer: leafy‑green"}, 0},
		{"v14 leafy-green", protocol.BenchVersionV14, protocol.RunResponse{FinalText: "Answer: leafy‑green"}, 1},
	})

	bluey := protocol.MemoryCase{QuestionType: "declarative-behavior", AnswerKind: protocol.AnswerValue, ExpectedAnswer: "bluey green"}
	checkVersionVectors(t, bluey, []versionVector{
		{"v14 changed word stays a miss", protocol.BenchVersionV14, protocol.RunResponse{FinalText: "Answer: blue‑green"}, 0},
		{"v14 same words joined", protocol.BenchVersionV14, protocol.RunResponse{FinalText: "Answer: bluey‑green"}, 1},
	})

	ack := protocol.MemoryCase{QuestionType: DeclarativeAckQuestionType, AnswerKind: protocol.AnswerValue,
		Question:       "Please keep my workspace accent blue purple from now on.",
		ExpectedAnswer: "blue purple", AcceptAny: []string{"got it", "noted", "understood"}}
	reply := protocol.RunResponse{FinalText: "Got it—your workspace accent remains blue‑purple across sessions."}
	checkVersionVectors(t, ack, []versionVector{
		{"v13 declarative ack without the spaced value", protocol.BenchVersionV13, reply, declarativeAckCredit},
		{"v14 declarative ack credits the joined value", protocol.BenchVersionV14, reply, 1},
	})

	leak := protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "amber rose"}
	checkVersionVectors(t, leak, []versionVector{
		{"v13 joined forbidden value keeps chitchat credit", protocol.BenchVersionV13, protocol.RunResponse{FinalText: "Hi! Your code word is amber‑rose."}, 0.5},
		{"v14 joined forbidden value is a leak", protocol.BenchVersionV14, protocol.RunResponse{FinalText: "Hi! Your code word is amber‑rose."}, 0},
	})
}

// TestJoinHyphenatedValuesRewrite pins the rewrite: letter-letter gaps of a
// known value only, bounded, byte-for-byte, and never at the cost of a known
// value that itself carries a hyphen.
func TestJoinHyphenatedValuesRewrite(t *testing.T) {
	for _, tt := range []struct {
		name string
		mc   protocol.MemoryCase
		in   string
		want string
	}{
		{"joins a two-word value", protocol.MemoryCase{ExpectedAnswer: "blue purple"}, "Accent: Blue-Purple.", "Accent: Blue Purple."},
		{"joins a distractor", protocol.MemoryCase{DistractorAnswers: []string{"blue purple"}}, "blue-purple", "blue purple"},
		{"joins a claim accept form", protocol.MemoryCase{Claims: []protocol.Claim{{Kind: protocol.ClaimKindValue, Expected: "x", Accept: []string{"dark mode"}}}}, "dark-mode on", "dark mode on"},
		{"joins each letter gap", protocol.MemoryCase{ExpectedAnswer: "dark blue grey"}, "dark-blue grey and dark-blue-grey", "dark blue grey and dark blue grey"},
		{"digit gap is not joinable", protocol.MemoryCase{ExpectedAnswer: "4 pm"}, "at 4-pm", "at 4-pm"},
		{"identifier without a space", protocol.MemoryCase{ExpectedAnswer: "VK-48HJXP6A63"}, "VK-48HJXP6A63", "VK-48HJXP6A63"},
		{"unbounded occurrence", protocol.MemoryCase{ExpectedAnswer: "blue purple"}, "blue-purpled", "blue-purpled"},
		{"changed word", protocol.MemoryCase{ExpectedAnswer: "bluey green"}, "blue-green", "blue-green"},
		{"spaced dash is not a join", protocol.MemoryCase{ExpectedAnswer: "blue purple"}, "blue - purple", "blue - purple"},
		{"hyphenated grounding kept", protocol.MemoryCase{ExpectedAnswer: "short and", GroundingTokens: []string{"short-and-fast"}}, "short-and-fast", "short-and-fast"},
	} {
		got := joinHyphenatedValuesV14(tt.mc, protocol.RunResponse{Answer: tt.in, FinalText: tt.in})
		if got.Answer != tt.want || got.FinalText != tt.want {
			t.Errorf("%s: %q -> %+v, want %q", tt.name, tt.in, got, tt.want)
		}
	}
}

// TestV14SetRemovalWithPlacePhrase is the #2734 point-3 vector: a set reply
// that lists the current items and adds "PE kit has been removed from the
// list" asserts the current set at v14; v13 reads PE kit as asserted. The
// place phrase is bounded and cannot excuse an item asserted elsewhere.
func TestV14SetRemovalWithPlacePhrase(t *testing.T) {
	items := []string{"swim goggles", "packed lunch", "sunscreen", "torch"}
	set := protocol.MemoryCase{AnswerKind: protocol.AnswerList, AnswerItems: items, AnswerItemKinds: make([]string, len(items)),
		ExpectedAnswer: "swim goggles; packed lunch; sunscreen; torch", DistractorAnswers: []string{"PE kit", "raincoat"}}
	for _, item := range items {
		set.Claims = append(set.Claims, protocol.Claim{Kind: protocol.ClaimKindSetMember, Expected: item, Accept: []string{item}, Weight: 0.25})
	}
	list := "The list is now swim goggles, packed lunch, sunscreen and torch. "
	at := func(s string) protocol.RunResponse { return protocol.RunResponse{FinalText: list + s} }
	checkVersionVectors(t, set, []versionVector{
		{"v13 bare removal still excused", protocol.BenchVersionV13, at("PE kit has been removed."), 1},
		{"v13 removal from the list stays asserted", protocol.BenchVersionV13, at("PE kit has been removed from the list."), 0},
		{"v14 removal from the list", protocol.BenchVersionV14, at("PE kit has been removed from the list."), 1},
		{"v14 removal from it", protocol.BenchVersionV14, at("PE kit was removed from it."), 1},
		{"v14 removal from your list", protocol.BenchVersionV14, at("PE kit is removed from your list."), 1},
		{"v14 removal from your packing list", protocol.BenchVersionV14, at("PE kit has been removed from your packing list."), 1},
		{"v14 removal off the list", protocol.BenchVersionV14, at("PE kit has been removed off the list."), 1},
		{"v14 removed item asserted elsewhere", protocol.BenchVersionV14, protocol.RunResponse{FinalText: "The list is now swim goggles, PE kit, sunscreen and torch. Packed lunch has been removed from the list."}, 0},
		{"v14 place phrase too long", protocol.BenchVersionV14, at("PE kit has been removed from the list the school sent home."), 0},
		{"v14 place phrase names a value", protocol.BenchVersionV14, at("PE kit has been removed from sunscreen."), 0},
		{"v14 not a place phrase", protocol.BenchVersionV14, at("PE kit is removed later today."), 0},
		{"v14 hedged removal", protocol.BenchVersionV14, at("Maybe PE kit has been removed from the list."), 0},
	})
}

// TestRemovedThenPlace pins the clause rule directly.
func TestRemovedThenPlace(t *testing.T) {
	for in, want := range map[string]bool{
		"pe kit has been removed":                      true,
		"(pe kit has been removed)":                    true,
		"pe kit has been removed from the list":        true,
		"pe kit was removed from it.":                  true,
		"pe kit is removed in the update":              true,
		"pe kit was removed on monday":                 true,
		"pe kit has been removed from":                 false,
		"pe kit has been removed by the school":        false,
		"pe kit has been removed from a b c d":         false,
		"pe kit has been removed from ________":        false,
		"pe kit has been removed from the 2026 list":   false,
		"pe kit has been removed from the list is new": false,
	} {
		if got := removedThenPlaceV14(in); got != want {
			t.Errorf("removedThenPlaceV14(%q) = %v, want %v", in, got, want)
		}
	}
}

// TestV14ProgressiveActionClaims is the #2734 point-4 vector: an action claim
// stated in the progressive form ("We are pausing the gym membership") states
// "pause it" at v14 and stays a miss at v13. Past -ed forms, another verb's
// -ing form, and a distractor action in the -ing form do not score.
func TestV14ProgressiveActionClaims(t *testing.T) {
	pause := protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "pause it", DistractorAnswers: []string{"cancel it"},
		Claims: []protocol.Claim{{Kind: protocol.ClaimKindAction, Expected: "pause it", Accept: []string{"pause it", "pause", "put it on pause", "freeze it for a few months"}, Weight: 1}}}
	say := func(s string) protocol.RunResponse { return protocol.RunResponse{FinalText: s} }
	checkVersionVectors(t, pause, []versionVector{
		{"v13 progressive stays a miss", protocol.BenchVersionV13, say("We are pausing the gym membership."), 0},
		{"v13 base form", protocol.BenchVersionV13, say("We will pause the gym membership."), 1},
		{"v14 progressive", protocol.BenchVersionV14, say("We are pausing the gym membership."), 1},
		{"v14 progressive of an accept phrase", protocol.BenchVersionV14, say("We're freezing it for a few months."), 1},
		{"v14 past tense stays a miss", protocol.BenchVersionV14, say("We paused the gym membership."), 0},
		{"v14 another verb's -ing form", protocol.BenchVersionV14, say("We are passing on the gym membership."), 0},
		{"v14 progressive distractor is caught", protocol.BenchVersionV14, say("We are pausing it, or cancelling it."), 0},
	})
	upgrade := protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "upgrade it",
		Claims: []protocol.Claim{{Kind: protocol.ClaimKindAction, Expected: "upgrade it", Accept: []string{"upgrade it", "upgrade", "go up a tier"}, Weight: 1}}}
	checkVersionVectors(t, upgrade, []versionVector{
		{"v13 upgrading stays a miss", protocol.BenchVersionV13, say("Upgrading it."), 0},
		{"v14 upgrading", protocol.BenchVersionV14, say("Upgrading it."), 1},
		{"v14 going up a tier", protocol.BenchVersionV14, say("We're going up a tier."), 1},
	})
	// Non-action claims never take the progressive form.
	status := protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "pause",
		Claims: []protocol.Claim{{Kind: protocol.ClaimKindValue, Expected: "pause", Weight: 1}}}
	checkVersionVectors(t, status, []versionVector{
		{"v14 value claim keeps the exact form", protocol.BenchVersionV14, say("We are pausing it."), 0},
	})
}

// TestProgressiveVerbSpelling pins the conservative -ing speller.
func TestProgressiveVerbSpelling(t *testing.T) {
	for in, want := range map[string]string{
		"pause": "pausing", "upgrade": "upgrading", "stop": "stopping", "cancel": "canceling cancelling",
		"hop": "hopping", "hope": "hoping", "see": "seeing", "go": "going", "pay": "paying",
		"freeze": "freezing", "tie": "tying", "fix": "fixing", "put": "putting", "send": "sending",
		"book": "booking", "paused": "", "billing": "", "nda": "ndaing", "café": "",
	} {
		if got := strings.Join(progressiveVerbV14(in), " "); got != want {
			t.Errorf("progressiveVerbV14(%q) = %q, want %q", in, got, want)
		}
	}
}
