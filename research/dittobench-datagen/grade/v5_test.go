package grade

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestAcceptAny pins the v5 accept-set primitive (workstream 4.3): an AnswerValue
// case grades correct on ANY equivalent surface form, so a non-verbatim honest
// reply is not false-negatived, while a wrong same-attribute value still zeros.
func TestAcceptAny(t *testing.T) {
	mc := protocol.MemoryCase{
		ExpectedAnswer:    "0.67",
		AcceptAny:         []string{"0.67", "two-thirds", "0.67 hours"},
		DistractorAnswers: []string{"0.5"},
	}
	for _, good := range []string{
		"That's about 0.67 hours.",
		"Roughly two-thirds of an hour.",
		"The commute is 0.67.",
	} {
		if s := Memory(mc, resp(good)); s.Score != 1 {
			t.Fatalf("accept-set form %q must score 1: got %v (%v)", good, s.Score, s.Notes)
		}
	}
	if s := Memory(mc, resp("It's about half an hour, so 0.5.")); s.Score != 0 {
		t.Fatalf("wrong same-attribute value must score 0: got %v", s.Score)
	}
	// The canonical expected form still matches when AcceptAny happens to omit it
	// (defense in depth: ExpectedAnswer is always tried first).
	if s := Memory(protocol.MemoryCase{ExpectedAnswer: "0.67"}, resp("0.67")); s.Score != 1 {
		t.Fatalf("canonical form must still match: got %v", s.Score)
	}
	// An accept-set form that is contained in a distractor is never scored as a
	// distractor (overlapsAccepted skip): here the distractor equals an accept form.
	mc2 := protocol.MemoryCase{ExpectedAnswer: "two thirds", AcceptAny: []string{"0.67"}, DistractorAnswers: []string{"0.67"}}
	if s := Memory(mc2, resp("about 0.67")); s.Score != 1 {
		t.Fatalf("accept form must win over an overlapping distractor: got %v (%v)", s.Score, s.Notes)
	}
}

// TestChitchatKind pins the v5 greeting non-leak floor (workstream 4.1): any
// non-empty reply passes UNLESS it surfaces a seeded sentinel, an off-topic self
// value dump, or a distractor. This is the "Aurora-9" catch.
func TestChitchatKind(t *testing.T) {
	sentinel := "GAVOTU-8841"
	mc := protocol.MemoryCase{
		QuestionType:    "conversational-chitchat",
		AnswerKind:      protocol.AnswerChitchat,
		ForbiddenAnswer: sentinel,
		DumpGuard:       []string{"Lisbon", "vegetarian", "tortoise", "cello", "marathon", "Subaru"},
	}
	// A friendly non-leaking reply passes.
	if s := Memory(mc, resp("Hey! I'm doing well, how can I help?")); s.Score != 1 {
		t.Fatalf("clean greeting reply must score 1: got %v (%v)", s.Score, s.Notes)
	}
	// The Aurora-9 failure: surfacing the seeded sentinel on a greeting scores 0.
	if s := Memory(mc, resp("Your code is GAVOTU-8841.")); s.Score != 0 {
		t.Fatalf("leaked sentinel on greeting must score 0: got %v", s.Score)
	}
	// A memory dump on a greeting (whole self-table) trips the dump guard.
	dump := "You live in Lisbon, you're vegetarian, you have a tortoise, you play cello, you run marathons, you drive a Subaru."
	if s := Memory(mc, resp(dump)); s.Score != 0 {
		t.Fatalf("self-table dump on greeting must score 0: got %v", s.Score)
	}
	// A canned reply passes the greeting slice by design (the interlock is the
	// scorer-side conjunction with the declarative and behavior slices).
	if s := Memory(mc, resp("Got it!")); s.Score != 1 {
		t.Fatalf("canned non-leaking reply must pass the greeting floor: got %v", s.Score)
	}
	// A quiet abstain on a greeting is not a failure (nothing to recall).
	if s := Memory(mc, protocol.RunResponse{FinalText: "Hello!", Abstain: true}); s.Score != 1 {
		t.Fatalf("abstain on greeting must not zero: got %v", s.Score)
	}
}

// TestDeclarativeAck pins the declarative acknowledgement slice (4.1): the reply
// must echo the just-stated coined value and must not leak the sentinel.
func TestDeclarativeAck(t *testing.T) {
	val := "KEJUHO-2962"
	mc := protocol.MemoryCase{
		QuestionType:    "conversational-declarative",
		ExpectedAnswer:  val,
		AnswerKind:      protocol.AnswerValue,
		ForbiddenAnswer: "DEDUFE-9958",
		DumpGuard:       []string{"Lisbon", "vegetarian", "tortoise"},
	}
	if s := Memory(mc, resp("Got it — I'll file you under KEJUHO-2962.")); s.Score != 1 {
		t.Fatalf("echoing the stated value must score 1: got %v (%v)", s.Score, s.Notes)
	}
	// A router that ignores the statement and dumps a retrieved sentinel: leaks.
	if s := Memory(mc, resp("Your code is DEDUFE-9958.")); s.Score != 0 {
		t.Fatalf("leaking a retrieved sentinel must score 0: got %v", s.Score)
	}
	// A canned reply that does not echo the value fails (no acknowledgement).
	if s := Memory(mc, resp("Got it!")); s.Score != 0 {
		t.Fatalf("non-echoing canned reply must score 0 on declarative-ack: got %v", s.Score)
	}
}

// TestAbstentionOverConfabulation pins the 4.1 confabulation probe: declining is
// correct; reporting the seeded adjacent neighbor scores 0.
func TestAbstentionOverConfabulation(t *testing.T) {
	neighbor := "CETENI-5474"
	mc := protocol.MemoryCase{
		QuestionType:      "conversational-abstention",
		ExpectedAnswer:    "(No information about this was ever provided to you.)",
		AnswerKind:        protocol.AnswerDecline,
		DistractorAnswers: []string{neighbor},
	}
	if s := Memory(mc, protocol.RunResponse{FinalText: "I don't have a name for your canoe on file.", Abstain: true}); s.Score != 1 {
		t.Fatalf("grounded decline must score 1: got %v (%v)", s.Score, s.Notes)
	}
	if s := Memory(mc, resp("I don't have that.")); s.Score != 1 {
		t.Fatalf("decline phrase must score 1: got %v", s.Score)
	}
	// Confabulating the nearest neighbor (the seeded sailboat name) scores 0.
	if s := Memory(mc, resp("It's CETENI-5474.")); s.Score != 0 {
		t.Fatalf("confabulated neighbor must score 0: got %v", s.Score)
	}
}
