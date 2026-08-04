package grade

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func v8Case(kind string) protocol.MemoryCase {
	return protocol.MemoryCase{BenchVersion: protocol.BenchVersionV8, AnswerKind: kind}
}

func TestV8StrictKindsDoNotChangeV7(t *testing.T) {
	chitchat := v8Case(protocol.AnswerChitchat)
	if got := Memory(chitchat, protocol.RunResponse{FinalText: "hello"}).Score; got != 0.5 {
		t.Fatalf("v8 chitchat correctness credit = %v, want .5", got)
	}
	chitchat.BenchVersion = protocol.BenchVersionV7
	if got := Memory(chitchat, protocol.RunResponse{FinalText: "hello"}).Score; got != 1 {
		t.Fatalf("v7 chitchat changed: %v", got)
	}

	ack := v8Case(protocol.AnswerAcknowledge)
	if got := Memory(ack, protocol.RunResponse{FinalText: "I don't have that."}).Score; got != 0 {
		t.Fatalf("v8 decline laundered as acknowledgement: %v", got)
	}
	ack.BenchVersion = protocol.BenchVersionV7
	if got := Memory(ack, protocol.RunResponse{FinalText: "I don't have that."}).Score; got != 1 {
		t.Fatalf("v7 acknowledgement changed: %v", got)
	}
}

func TestV8StructuredAnswerIsAuthoritative(t *testing.T) {
	mc := v8Case(protocol.AnswerValue)
	mc.ExpectedAnswer = "Lisbon"
	resp := protocol.RunResponse{Answer: "Porto", FinalText: "The answer is Lisbon."}
	if got := Memory(mc, resp).Score; got != 0 {
		t.Fatalf("v8 wrong slot was laundered by prose: %v", got)
	}
	mc.BenchVersion = protocol.BenchVersionV7
	if got := Memory(mc, resp).Score; got != 1 {
		t.Fatalf("v7 slot/prose fallback changed: %v", got)
	}
}

func TestV8PersistenceRejectsIncidentalGenericWords(t *testing.T) {
	mc := v8Case(protocol.AnswerPersistence)
	mc.AnswerItems = []string{"tennis"}
	if got := Memory(mc, protocol.RunResponse{FinalText: "I would still like to check tennis later."}).Score; got != 0 {
		t.Fatalf("generic persistence words credited: %v", got)
	}
	if got := Memory(mc, protocol.RunResponse{FinalText: "You remain a big fan of tennis."}).Score; got != 1 {
		t.Fatalf("explicit persistence stance missed: %v", got)
	}
}

func TestV8MoneyAcceptsHumanFormattingAndRejectsInternalCents(t *testing.T) {
	mc := v8Case(protocol.AnswerMoney)
	mc.ExpectedAnswer = "719076"
	for _, answer := range []string{"$7,190.76", "$7190.76", "USD 7,190.76", "7.190,76 USD"} {
		if got := Memory(mc, protocol.RunResponse{Answer: answer}).Score; got != 1 {
			t.Errorf("money answer %q scored %v", answer, got)
		}
	}
	for _, answer := range []string{"719076", "$7,190.75", "$71,907.60"} {
		if got := Memory(mc, protocol.RunResponse{Answer: answer}).Score; got != 0 {
			t.Errorf("wrong/internal money answer %q scored %v", answer, got)
		}
	}
}

func TestV8MoneyDistractorsUseTheSameParser(t *testing.T) {
	mc := v8Case(protocol.AnswerMoney)
	mc.ExpectedAnswer = "719076"
	mc.DistractorAnswers = []string{"720076"}
	resp := protocol.RunResponse{Answer: "$7,190.76 and maybe $7,200.76"}
	if got := Memory(mc, resp).Score; got != 0 {
		t.Fatalf("formatted wrong amount escaped distractor scan: %v", got)
	}
}

func TestV8DirectionUsesReviewedContextualEquivalents(t *testing.T) {
	mc := v8Case(protocol.AnswerDirection)
	mc.ExpectedAnswer = "increase"
	for _, answer := range []string{"It went up.", "Availability rose.", "The amount increased."} {
		if got := Memory(mc, protocol.RunResponse{Answer: answer}).Score; got != 1 {
			t.Errorf("direction answer %q scored %v", answer, got)
		}
	}
	for _, answer := range []string{"It went down.", "It did not increase.", "It increased and then decreased."} {
		if got := Memory(mc, protocol.RunResponse{Answer: answer}).Score; got != 0 {
			t.Errorf("wrong/ambiguous direction %q scored %v", answer, got)
		}
	}
}

func TestV8TypedListCombinesDirectionMoneyAndParaphrase(t *testing.T) {
	mc := v8Case(protocol.AnswerList)
	mc.AnswerItems = []string{"increase", "719076", "separate drafts from approvals"}
	mc.AnswerItemKinds = []string{protocol.AnswerDirection, protocol.AnswerMoney, protocol.AnswerValue}
	mc.AnswerItemAcceptAny = [][]string{nil, nil, {"keep draft figures separate from approved numbers"}}
	mc.DistractorAnswers = []string{"decrease", "720076"}
	resp := protocol.RunResponse{Answer: "It went up by $7,190.76; keep draft figures separate from approved numbers."}
	if got := Memory(mc, resp).Score; got != 1 {
		t.Fatalf("typed list scored %v", got)
	}
	resp.Answer = "It went down by $7,190.76; keep draft figures separate from approved numbers."
	if got := Memory(mc, resp).Score; got != 0 {
		t.Fatalf("direction synonym escaped distractor scan: %v", got)
	}
}
