package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV13GraderContractMatchesGenerator pins the string constants the v13
// grader keys on that the generator owns. grade cannot import gen, so the two
// are checked here.
func TestV13GraderContractMatchesGenerator(t *testing.T) {
	if grade.DeclarativeAckQuestionType != QTDeclarativeAck {
		t.Fatalf("grade.DeclarativeAckQuestionType=%q, gen.QTDeclarativeAck=%q", grade.DeclarativeAckQuestionType, QTDeclarativeAck)
	}
	// The v13 declarative-acknowledgement credit applies to exactly the case the
	// generator emits: an AnswerValue case of QTDeclarativeAck whose AcceptAny
	// holds the acknowledgement forms.
	mc := protocol.MemoryCase{
		BenchVersion:   protocol.BenchVersionV13,
		QuestionType:   QTDeclarativeAck,
		Question:       "By the way, the codeword for our chats is GAVOTU-8841 — use it when you check in with me.",
		ExpectedAnswer: "GAVOTU-8841",
		AnswerKind:     protocol.AnswerValue,
		AcceptAny:      declarativeAckForms,
	}
	if got := grade.Memory(mc, protocol.RunResponse{FinalText: "Got it, noted."}).Score; got != 0.25 {
		t.Fatalf("canned declarative acknowledgement scored %v at v13, want 0.25", got)
	}
	if got := grade.Memory(mc, protocol.RunResponse{FinalText: "Noted — GAVOTU-8841 is the codeword."}).Score; got != 1 {
		t.Fatalf("reflected declarative acknowledgement scored %v at v13, want 1", got)
	}
	// Frozen: the same case at v12 keeps the v6 lenient acknowledgement.
	mc.BenchVersion = protocol.BenchVersionV12
	if got := grade.Memory(mc, protocol.RunResponse{FinalText: "Got it, noted."}).Score; got != 1 {
		t.Fatalf("v12 declarative acknowledgement moved: %v", got)
	}
}
