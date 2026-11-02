package grade

import (
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"testing"
)

func TestV13ClaimExplanationsPreserveCurrentAnswer(t *testing.T) {
	channel := protocol.MemoryCase{BenchVersion: 13, AnswerKind: protocol.AnswerValue, ExpectedAnswer: "video meeting", DistractorAnswers: []string{"ticket portal"}, Claims: []protocol.Claim{{Kind: protocol.ClaimKindChannel, Expected: "video meeting", Critical: true}}}
	set := protocol.MemoryCase{BenchVersion: 13, AnswerKind: protocol.AnswerList, DistractorAnswers: []string{"art smock"}}
	for _, value := range []string{"swim goggles", "spare socks", "bus pass", "signed permission slip"} {
		set.Claims = append(set.Claims, protocol.Claim{Kind: protocol.ClaimKindSetMember, Expected: value})
		set.AnswerItems = append(set.AnswerItems, value)
	}
	list := "The updated packing list is swim goggles, spare socks, bus pass, and signed permission slip. "
	for _, tc := range []struct {
		name string
		mc   protocol.MemoryCase
		text string
		want float64
	}{
		{"channel-history", channel, "The agreed channel is video meeting, as this superseded the earlier agreement to use ticket portal.", 1},
		{"channel-parenthesis", channel, "The agreed channel is video meeting (previously ticket portal).", 1},
		{"channel-shotgun", channel, "The agreed channel is video meeting and ticket portal.", 0},
		{"channel-comma-shotgun", channel, "The agreed channel is video meeting, ticket portal.", 0},
		{"wrong-current", channel, "The agreed channel is ticket portal, replacing video meeting.", 0},
		{"set-completed-removal", set, list + "Art smock has been removed.", 1},
		{"set-past-removal", set, list + "Art smock was removed.", 1},
		{"set-negated-removal", set, list + "Art smock has not been removed.", 0},
		{"set-reasserted", set, list + "Art smock has been removed. Include art smock too.", 0},
		{"set-future-removal", set, list + "Art smock will be removed.", 0},
		{"set-uncertain-removal", set, list + "Art smock might have been removed.", 0},
		{"set-false-removal", set, list + "The claim that art smock has been removed is false.", 0},
		{"set-extra-current", set, list + "Art smock is also required.", 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v := Memory(tc.mc, protocol.RunResponse{FinalText: tc.text})
			if v.Score != tc.want {
				t.Fatalf("score=%v want=%v notes=%v", v.Score, tc.want, v.Notes)
			}
		})
	}
}

func TestV13CompletedRemovalIsOnlySetMembership(t *testing.T) {
	for _, kind := range []string{protocol.ClaimKindPerson, protocol.ClaimKindValue} {
		mc := protocol.MemoryCase{BenchVersion: 13, Claims: []protocol.Claim{{Kind: kind, Expected: "Alex Morgan"}}}
		if got := Memory(mc, protocol.RunResponse{FinalText: "Alex Morgan has been removed."}); got.Score != 1 {
			t.Fatalf("removal relation leaked into %s: %v", kind, got)
		}
	}
}
