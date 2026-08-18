package grade

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV12DistractorScanScopedToAnswerSlot pins the C-obs1 grader fix: at v12 the
// same-attribute distractor disqualifier is scoped to the authoritative answer
// slot, so an honest agent that shows its reasoning ("it used to be Oslo, but
// it's Lisbon now") is no longer zeroed for naming the superseded value, while a
// response whose ANSWER is a distractor still fails. v11 and earlier keep the
// full-response scan (immutable-contract-frozen behavior).
func TestV12DistractorScanScopedToAnswerSlot(t *testing.T) {
	base := protocol.MemoryCase{
		ExpectedAnswer:    "Lisbon",
		DistractorAnswers: []string{"Oslo"},
	}
	// Honest reasoning: correct answer in the slot, the superseded distractor
	// mentioned only in the explanatory prose.
	reasoned := protocol.RunResponse{
		Answer:    "Lisbon",
		FinalText: "You used to live in Oslo, but you moved to Lisbon last year.",
	}

	// (a) v12: correct slotted answer + reasoning that mentions a distractor PASSES.
	v12 := base
	v12.BenchVersion = protocol.BenchVersionV12
	if s := Memory(v12, reasoned); s.Score != 1 {
		t.Fatalf("v12 honest-reasoning answer must score 1: got %v (%v)", s.Score, s.Notes)
	}

	// (b) v12: an answer that IS a distractor still fails (slot holds the wrong value).
	wrongSlot := protocol.RunResponse{Answer: "Oslo", FinalText: "You live in Oslo."}
	if s := Memory(v12, wrongSlot); s.Score != 0 {
		t.Fatalf("v12 distractor-as-answer must still score 0: got %v (%v)", s.Score, s.Notes)
	}
	// (b') v12: an enumerated/shotgun slot ("Lisbon or Oslo") still trips the scan.
	shotgunSlot := protocol.RunResponse{Answer: "Lisbon or Oslo", FinalText: "not sure which"}
	if s := Memory(v12, shotgunSlot); s.Score != 0 {
		t.Fatalf("v12 slot enumerating a distractor must score 0: got %v (%v)", s.Score, s.Notes)
	}
	// (b'') v12: prose-only response (no slot) still falls back to the full scan,
	// so a prose shotgun is still caught.
	proseShotgun := protocol.RunResponse{FinalText: "It's either Lisbon or Oslo."}
	if s := Memory(v12, proseShotgun); s.Score != 0 {
		t.Fatalf("v12 prose-only distractor shotgun must score 0: got %v (%v)", s.Score, s.Notes)
	}

	// (c) v11 (and earlier): behavior unchanged — the same honest-reasoning answer
	// is still zeroed by the full-response scan. This is the frozen contract.
	v11 := base
	v11.BenchVersion = protocol.BenchVersionV11
	if s := Memory(v11, reasoned); s.Score != 0 {
		t.Fatalf("v11 must keep the frozen full-response distractor scan (score 0): got %v (%v)", s.Score, s.Notes)
	}
	// And every earlier gated version keeps the frozen behavior too.
	for _, version := range []int{
		protocol.BenchVersionV8, protocol.BenchVersionV9, protocol.BenchVersionV10,
	} {
		mc := base
		mc.BenchVersion = version
		if s := Memory(mc, reasoned); s.Score != 0 {
			t.Fatalf("v%d must keep the frozen full-response distractor scan (score 0): got %v (%v)", version, s.Score, s.Notes)
		}
	}
}
