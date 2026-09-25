package grade

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV13SlotAssertionScope(t *testing.T) {
	for _, tc := range []struct{ kind, expected, honest, rejected string }{
		{protocol.AnswerNumber, "0", "The answer is zero.", "The answer is not 0."},
		{protocol.AnswerNumber, "12", "Initially 9, but now 12.", "Was it 12?"},
		{protocol.AnswerValue, "contact-abcd@fictional.example", "Use contact-abcd@fictional.example.", "Do not use contact-abcd@fictional.example."},
	} {
		mc := protocol.MemoryCase{BenchVersion: 13, AnswerKind: tc.kind, ExpectedAnswer: tc.expected}
		for _, answer := range []string{tc.expected, tc.honest} {
			if got := Memory(mc, protocol.RunResponse{Answer: answer}); got.Score != 1 {
				t.Errorf("honest %q: %+v", answer, got)
			}
		}
		for _, final := range []string{"", "The answer is " + tc.expected + "."} {
			if got := Memory(mc, protocol.RunResponse{Answer: tc.rejected, FinalText: final}); got.Score != 0 {
				t.Errorf("rejected slot %q with prose %q: %+v", tc.rejected, final, got)
			}
		}
	}
}

func TestV13SlotSiblingActionDoesNotSupersedeCurrentActor(t *testing.T) {
	mc := protocol.MemoryCase{BenchVersion: 13, AnswerKind: protocol.AnswerList,
		Claims: []protocol.Claim{
			{Kind: protocol.ClaimKindEntity, Expected: "Brendan Leon", Accept: []string{"Buddy"}, Critical: true},
			{Kind: protocol.ClaimKindAction, Expected: "cancel the old plan", Accept: []string{"cancel the old"}, Critical: true},
			{Kind: protocol.ClaimKindChannel, Expected: "phone", Accept: []string{"telephone"}, Critical: true},
		}}
	if got := Memory(mc, protocol.RunResponse{Answer: "Buddy will cancel the old by telephone."}); got.Score != 1 {
		t.Fatalf("current actor rejected because of sibling action vocabulary: %+v", got)
	}
	if got := Memory(mc, protocol.RunResponse{Answer: "Buddy will not cancel the old by telephone."}); got.Score != 0 {
		t.Fatalf("outer negation hidden by sibling vocabulary: %+v", got)
	}
}
