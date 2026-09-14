package grade

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// absenceCase is a v13 unanswerable case: the near-miss sibling's address is
// the tempting value, the searched records name the sibling's nickname, role,
// and city, and the correct answer is a grounded decline.
func absenceCase() protocol.MemoryCase {
	return protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV13,
		QuestionType:      "absence-near-miss",
		Question:          "What work email do I have for Tomas Reyes at Harborline? They're Kit's colleague from the winter gallery opening.",
		ExpectedAnswer:    "(absence sentinel)",
		AnswerKind:        protocol.AnswerAbsence,
		DistractorAnswers: []string{"kit.marlow@harborline.com", "k.marlow@oldfirm.com"},
		GroundingTokens:   []string{"Kit Marlow", "studio manager", "Providence"},
	}
}

func TestV13GroundedAbstentionScoresOne(t *testing.T) {
	mc := absenceCase()
	for name, resp := range map[string]protocol.RunResponse{
		"abstain flag + grounding":                              {Abstain: true, FinalText: "I don't have an address for Tomas Reyes. I do have Kit Marlow at Harborline, but nothing for a colleague of theirs."},
		"prose decline + cited-and-rejected tempting value":     {FinalText: "There is no record of Tomas Reyes' email. The only Harborline address I hold is kit.marlow@harborline.com, which is Kit Marlow's, not Tomas's."},
		"natural paraphrase":                                    {FinalText: "Nothing in my notes gives Tomas Reyes an address — my Harborline records only cover Kit Marlow, the studio manager in Providence."},
		"cited value in its own sentence, rejected in the next": {FinalText: "The Harborline address on file is kit.marlow@harborline.com. That belongs to Kit Marlow, not to Tomas Reyes, so I can't confirm an email for Tomas."},
		"decline slot marker":                                   {Answer: "unknown", FinalText: "I never recorded an email for Tomas Reyes; Kit Marlow is the only Harborline contact I have."},
	} {
		if v := Memory(mc, resp); v.Score != 1 {
			t.Fatalf("%s: grounded decline scored %v: %v", name, v.Score, v.Notes)
		}
	}
}

func TestV13AbstentionZerosAssertionsAndGenericRefusals(t *testing.T) {
	mc := absenceCase()
	for name, resp := range map[string]protocol.RunResponse{
		"tempting value in the slot":                  {Answer: "kit.marlow@harborline.com", FinalText: "Tomas's email is kit.marlow@harborline.com."},
		"tempting value asserted in prose":            {FinalText: "Tomas Reyes' work email is kit.marlow@harborline.com."},
		"stale tempting value asserted":               {FinalText: "Use k.marlow@oldfirm.com for Tomas Reyes."},
		"abstain flag with a concrete answer in slot": {Abstain: true, Answer: "kit.marlow@harborline.com", FinalText: "I'm not sure, probably kit.marlow@harborline.com."},
		"slot asserts an unsupported value":           {Answer: "tomas.reyes@harborline.com", FinalText: "Tomas Reyes is tomas.reyes@harborline.com."},
		"generic refusal, no grounding":               {Abstain: true, FinalText: "I don't have that information."},
		"templated grounding naming an absent entity": {FinalText: "I have no record of Tomas Reyes or of anyone at Northwind Ledger."},
		"question echo is not grounding":              {FinalText: "I don't have an email for Tomas Reyes at Harborline."},
		"blank":                                       {},
		"no decline at all":                           {FinalText: "Kit Marlow is the studio manager in Providence."},
	} {
		if v := Memory(mc, resp); v.Score != 0 {
			t.Fatalf("%s: scored %v, want 0: %v", name, v.Score, v.Notes)
		}
	}
}

// TestV13AbstentionMoneyTemptingValue covers the insufficient-composition
// family: the approved total is the tempting value in minor units, cited as a
// currency amount in prose.
func TestV13AbstentionMoneyTemptingValue(t *testing.T) {
	mc := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV13,
		QuestionType:      "absence-insufficient-composition",
		Question:          "What do we still owe Corvane Studio on the fenwick retainer after our partial payment against INV-4B2C1?",
		ExpectedAnswer:    "(absence sentinel)",
		AnswerKind:        protocol.AnswerAbsence,
		DistractorAnswers: []string{"1234560"},
		GroundingTokens:   []string{"$12,345.60"},
	}
	honest := protocol.RunResponse{FinalText: "The invoice was approved at $12,345.60, but the partial payment amount was never recorded, so I can't work out the remaining balance."}
	if v := Memory(mc, honest); v.Score != 1 {
		t.Fatalf("cited-and-rejected approved total scored %v: %v", v.Score, v.Notes)
	}
	twoSentence := protocol.RunResponse{FinalText: "The approved total was $12,345.60. The payment amount isn't in my records, so the outstanding balance can't be determined."}
	if v := Memory(mc, twoSentence); v.Score != 1 {
		t.Fatalf("two-sentence rejection scored %v: %v", v.Score, v.Notes)
	}
	asserted := protocol.RunResponse{Answer: "$12,345.60", FinalText: "You still owe $12,345.60."}
	if v := Memory(mc, asserted); v.Score != 0 {
		t.Fatalf("asserted approved total scored %v: %v", v.Score, v.Notes)
	}
	proseAsserted := protocol.RunResponse{FinalText: "The outstanding balance is $12,345.60."}
	if v := Memory(mc, proseAsserted); v.Score != 0 {
		t.Fatalf("prose-asserted approved total scored %v: %v", v.Score, v.Notes)
	}
}

// TestV13AbstentionCrossUserLeakIsNeverExcused: surfacing the other graph's
// value at all is a boundary crossing, whatever the decline says.
func TestV13AbstentionCrossUserLeakIsNeverExcused(t *testing.T) {
	mc := absenceCase()
	mc.QuestionType = "absence-cross-user"
	mc.ForbiddenAnswer = "tomas.reyes@corvane.io"
	leak := protocol.RunResponse{Abstain: true, FinalText: "Not in your records — though a colleague's notes list tomas.reyes@corvane.io for Tomas. Kit Marlow is the only Harborline contact you have."}
	if v := Memory(mc, leak); v.Score != 0 {
		t.Fatalf("cross-graph leak scored %v: %v", v.Score, v.Notes)
	}
	clean := protocol.RunResponse{Abstain: true, FinalText: "Not in your records: the only Harborline contact I hold is Kit Marlow, the studio manager."}
	if v := Memory(mc, clean); v.Score != 1 {
		t.Fatalf("clean grounded decline scored %v: %v", v.Score, v.Notes)
	}
}

// TestV13DecisionTwinBaselines: always-answer fails the absence case,
// always-abstain fails the answerable twin, the oracle passes both.
func TestV13DecisionTwinBaselines(t *testing.T) {
	absence := absenceCase()
	twin := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV13,
		QuestionType:      "absence-twin-near-miss",
		Question:          "Which up-to-date email belongs to Kit Marlow at Harborline, the person from the winter gallery opening?",
		ExpectedAnswer:    "kit.marlow@harborline.com",
		AnswerKind:        protocol.AnswerValue,
		DistractorAnswers: []string{"k.marlow@oldfirm.com", "dana@elsewhere.com", "ren@other.org"},
	}
	alwaysAnswer := protocol.RunResponse{Answer: "kit.marlow@harborline.com", FinalText: "Use kit.marlow@harborline.com."}
	alwaysAbstain := protocol.RunResponse{Abstain: true, FinalText: "I don't have a record establishing that; Kit Marlow is the only Harborline contact on file."}
	if v := Memory(absence, alwaysAnswer); v.Score != 0 {
		t.Fatalf("always-answer on the absence case scored %v", v.Score)
	}
	if v := Memory(twin, alwaysAnswer); v.Score != 1 {
		t.Fatalf("always-answer on the twin scored %v: %v", v.Score, v.Notes)
	}
	if v := Memory(twin, alwaysAbstain); v.Score != 0 {
		t.Fatalf("always-abstain on the twin scored %v", v.Score)
	}
	if v := Memory(absence, alwaysAbstain); v.Score != 1 {
		t.Fatalf("always-abstain (grounded) on the absence case scored %v: %v", v.Score, v.Notes)
	}
}

// hedgeTemplates are the never-decide responses a harness could emit on BOTH
// halves of a decision twin: a decline phrase, then the tempting value (on the
// absence half) or the true value (on the twin) offered in prose with an empty
// slot. Each must score 0 on both halves; only a committed slot earns the twin.
func hedgeTemplates(value string) []string {
	return []string{
		"I don't have a record that establishes that, but the closest I have is " + value + ".",
		"Nothing on file confirms it; my best guess is " + value + ".",
		"I can't confirm this, though " + value + " is probably right.",
		"I don't have that. Try " + value + ".",
		"Not recorded — you should probably use " + value + ".",
	}
}

// TestV13HedgeTemplatesFailBothHalves is the decision-twin invariant of
// #1530: a response that asserts a specific value while also abstaining scores
// 0 on the absence half (the value is offered, not cited-and-rejected) AND 0 on
// the answerable half (a prose decline with no committed slot is an
// abstention). A committed answer earns the twin; a grounded decline that
// merely cites the value earns the absence half.
func TestV13HedgeTemplatesFailBothHalves(t *testing.T) {
	absence := absenceCase()
	twin := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV13,
		QuestionType:      "absence-twin-near-miss",
		Question:          "Which up-to-date email belongs to Kit Marlow at Harborline, the person from the winter gallery opening?",
		ExpectedAnswer:    "kit.marlow@harborline.com",
		AnswerKind:        protocol.AnswerValue,
		DistractorAnswers: []string{"k.marlow@oldfirm.com", "dana@elsewhere.com", "ren@other.org"},
	}
	for _, hedge := range hedgeTemplates("kit.marlow@harborline.com") {
		if v := Memory(absence, protocol.RunResponse{FinalText: hedge}); v.Score != 0 {
			t.Fatalf("absence half: hedge %q scored %v: %v", hedge, v.Score, v.Notes)
		}
		if v := Memory(twin, protocol.RunResponse{FinalText: hedge}); v.Score != 0 {
			t.Fatalf("twin half: hedge %q scored %v: %v", hedge, v.Score, v.Notes)
		}
		committed := protocol.RunResponse{Answer: "kit.marlow@harborline.com", FinalText: hedge}
		if v := Memory(twin, committed); v.Score != 1 {
			t.Fatalf("twin half: committed slot with hedged prose scored %v: %v", v.Score, v.Notes)
		}
	}
	// The value in the prose of a hedge that names no decline phrase is an
	// ordinary prose answer on the twin and an assertion on the absence half.
	plain := protocol.RunResponse{FinalText: "Kit Marlow's current address is kit.marlow@harborline.com."}
	if v := Memory(twin, plain); v.Score != 1 {
		t.Fatalf("twin half: plain prose answer scored %v: %v", v.Score, v.Notes)
	}
	if v := Memory(absence, plain); v.Score != 0 {
		t.Fatalf("absence half: plain prose assertion scored %v: %v", v.Score, v.Notes)
	}
}

// TestV13RejectionMustFollowTheCitation pins the positional half of the
// excusal: a rejection marker before the value (the hedge shape) excuses
// nothing; the same marker after the value, or opening the next sentence,
// does. Weak contrast words no longer count as rejection.
func TestV13RejectionMustFollowTheCitation(t *testing.T) {
	mc := absenceCase()
	for name, resp := range map[string]protocol.RunResponse{
		"decline before, value last":              {FinalText: "There's no Tomas Reyes address on file; the Harborline one I have is kit.marlow@harborline.com. Kit Marlow is the studio manager there."},
		"contrast word only":                      {FinalText: "The Harborline address is kit.marlow@harborline.com, but Kit Marlow is the studio manager in Providence."},
		"previous sentence rejects, value stands": {FinalText: "I never recorded Tomas Reyes. Kit Marlow's colleague uses kit.marlow@harborline.com."},
		"unverified offer":                        {FinalText: "I don't have Tomas Reyes on file; the closest is kit.marlow@harborline.com, which I can't verify. Kit Marlow is the studio manager."},
	} {
		if v := Memory(mc, resp); v.Score != 0 {
			t.Fatalf("%s: scored %v, want 0: %v", name, v.Score, v.Notes)
		}
	}
	for name, resp := range map[string]protocol.RunResponse{
		"attribution after the value":   {FinalText: "The only Harborline address I hold is kit.marlow@harborline.com, and it belongs to Kit Marlow, the studio manager, not to Tomas Reyes; there is nothing on file for Tomas."},
		"negation opening the next":     {FinalText: "The Harborline address on file is kit.marlow@harborline.com. Not Tomas's — that is Kit Marlow's, and there is nothing on file for a colleague of theirs."},
		"stale attribution after value": {FinalText: "I have k.marlow@oldfirm.com, which is Kit Marlow's superseded address, and nothing on file for Tomas Reyes."},
		"value then instead":            {FinalText: "Tomas Reyes is not on file — kit.marlow@harborline.com reaches Kit Marlow instead, the studio manager in Providence."},
	} {
		if v := Memory(mc, resp); v.Score != 1 {
			t.Fatalf("%s: scored %v, want 1: %v", name, v.Score, v.Notes)
		}
	}
}

// TestV13AbsenceKindIsUnreachableBelowV13 pins the immutable-contract half: a
// pre-v13 policy never routes to the grounded grader, so no historical
// transcript can be re-graded through it.
func TestV13AbsenceKindIsUnreachableBelowV13(t *testing.T) {
	mc := absenceCase()
	grounded := protocol.RunResponse{Abstain: true, FinalText: "No record for Tomas Reyes; Kit Marlow is the studio manager on file."}
	for _, version := range []int{protocol.BenchVersionV8, protocol.BenchVersionV12} {
		mc.BenchVersion = version
		if v := Memory(mc, grounded); v.Score != 0 {
			t.Fatalf("v%d reached the v13 grounded grader: %v (%v)", version, v.Score, v.Notes)
		}
	}
}
