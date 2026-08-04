package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestRedTeamHalfDumpScoresZero is the B1 gate. The dump guard catches the
// attribute-blind WHOLE-table dump, but a parser that narrows to a few
// candidates including the answer and emits them all stays under DumpFloor.
// Lowering the floor was rejected (it false-positives on a verbose correct
// model), so detection is distractor-based instead: value-recall cases are
// salted with plausible same-attribute values no persona holds, and a single
// hit zeroes. This pins the half-hedge described in the v3 plan: answer plus
// DumpFloor-1 guard values plus one distractor must score 0.
func TestRedTeamHalfDumpScoresZero(t *testing.T) {
	prof, _ := ProfileFor("full")
	checked := 0
	for seed := int64(1); seed <= 25; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		for _, c := range cases {
			floor := grade.DumpFloor(len(c.DumpGuard))
			if len(c.DumpGuard) < floor || len(c.DistractorAnswers) == 0 {
				continue
			}
			// Deliberately stay UNDER the dump floor, then add a single distractor:
			// the half-hedge the count-based guard cannot see.
			parts := []string{c.ExpectedAnswer}
			parts = append(parts, c.DumpGuard[:floor-1]...)
			parts = append(parts, c.DistractorAnswers[0])
			resp := strings.Join(parts, ", ") + "."
			checked++
			if v := grade.Memory(c, protocol.RunResponse{FinalText: resp, Answer: resp}); v.Score != 0 {
				t.Fatalf("case %s: half-dump (answer + %d guard values + 1 distractor) scored %.2f, want 0", c.ID, floor-1, v.Score)
			}
		}
	}
	if checked == 0 {
		t.Skip("no case carried both a dump guard and a distractor")
	}
	t.Logf("half-dump gate: %d cases zeroed", checked)
}

// TestRedTeamCandidateSweepScoresZero is the gate that makes B1 bite in
// practice. The half-dump gate above is only interesting if a REAL sweep
// actually hits a distractor. Here a harness that cannot route to the asked
// attribute emits the answer plus a handful of same-attribute candidates -- the
// narrowed shortcut the whole-table dump guard was never able to catch -- and
// must be zeroed by the planted salt.
func TestRedTeamCandidateSweepScoresZero(t *testing.T) {
	prof, _ := ProfileFor("full")
	swept, zeroed := 0, 0
	for seed := int64(1); seed <= 25; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		for _, c := range cases {
			if len(c.DistractorAnswers) < 2 {
				continue
			}
			// "It is one of these": the answer hedged among its neighbours.
			resp := "It's probably " + c.ExpectedAnswer + ", though it could be " +
				strings.Join(c.DistractorAnswers[:2], " or ") + "."
			swept++
			if grade.Memory(c, protocol.RunResponse{FinalText: resp, Answer: resp}).Score == 0 {
				zeroed++
			}
		}
	}
	if swept == 0 {
		t.Skip("no multi-distractor cases sampled")
	}
	if zeroed != swept {
		t.Fatalf("candidate sweep zeroed on only %d/%d cases — the salt is not covering value-recall", zeroed, swept)
	}
	t.Logf("candidate-sweep gate: %d/%d cases zeroed", zeroed, swept)
}

// TestCandidateSaltIsAbsentFromHaystack is the false-positive guard for B1, and
// the reason the salt can be applied where lowering the dump floor could not.
// A planted distractor must never appear in the seeded conversation: if it did,
// a grounded reader could legitimately surface it and be zeroed for reading
// correctly, which would cost honest miners exactly the way a too-low floor
// would.
func TestCandidateSaltIsAbsentFromHaystack(t *testing.T) {
	prof, _ := ProfileFor("full")
	checked := 0
	for seed := int64(1); seed <= 15; seed++ {
		// Reproduce the pipeline's own inputs, then diff the distractor lists across
		// ApplyCandidateSalt so the assertion covers exactly the values the SALT
		// added. Scoping matters: the pre-existing distractor pool-backfill in
		// distractorsFor draws from these same pools without a haystack check, so a
		// blanket assertion over every distractor would fail on behaviour this
		// change did not introduce.
		plan := persona.BuildPlan(seed, personaOptsFor(prof.Mem))
		pairs, _ := RenderHaystack(plan)
		var hay strings.Builder
		for _, pr := range pairs {
			hay.WriteString(pr.Prompt)
			hay.WriteString(" ")
			hay.WriteString(pr.Response)
			hay.WriteString(" ")
		}
		text := hay.String()

		qs := persona.DeriveQuestions(plan)
		before := make([]int, len(qs))
		for i, q := range qs {
			before[i] = len(q.Distractors)
		}
		persona.ApplyCandidateSalt(plan, qs, func(v string) bool { return grade.Hit(v, text) })
		for i, q := range qs {
			for _, d := range q.Distractors[before[i]:] {
				checked++
				if grade.Hit(d, text) {
					t.Fatalf("seed %d, case %s: salt value %q appears in the haystack — a grounded reader could surface it and be zeroed for reading correctly", seed, q.ID, d)
				}
			}
		}
	}
	if checked == 0 {
		t.Fatal("no salt distractors were planted — B1 is not in effect")
	}
	t.Logf("salt gate: %d planted values, none present in the haystack", checked)
}
