package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestMultiHopAndTemporalDepthAreV5Only pins that the two new hard classes appear
// only under v5 and never disturb pre-v5 contracts.
func TestMultiHopAndTemporalDepthAreV5Only(t *testing.T) {
	hard := map[string]bool{QTMultiHop: true, QTTemporalDepth: true}
	for _, v := range []int{protocol.BenchVersionV2, protocol.BenchVersionV3, protocol.BenchVersionV4} {
		r, _ := NewRNGForVersion(4242, v)
		s, err := GenerateMemorySuiteForVersion(r, 4242, 55, 4, 0.15, v)
		if err != nil {
			t.Fatalf("v%d: %v", v, err)
		}
		for _, sc := range s.Cases {
			if hard[sc.Case.QuestionType] {
				t.Errorf("v%d leaked a hard class %q", v, sc.Case.QuestionType)
			}
		}
	}
	r, _ := NewRNGForVersion(4242, protocol.BenchVersionV5)
	s, _ := GenerateMemorySuiteForVersion(r, 4242, 55, 4, 0.15, protocol.BenchVersionV5)
	seen := map[string]int{}
	for _, sc := range s.Cases {
		seen[sc.Case.QuestionType]++
	}
	if seen[QTMultiHop] == 0 {
		t.Error("v5 emitted no multi-hop cases")
	}
	if seen[QTTemporalDepth] == 0 {
		t.Error("v5 emitted no temporal-depth cases")
	}
	if s.MultiHopCases == 0 || s.TemporalDepthCases == 0 {
		t.Fatalf("telemetry zero: mh=%d td=%d", s.MultiHopCases, s.TemporalDepthCases)
	}
}

// TestMultiHopJoinStructure pins the KG-join property: the answer leaf token IS in
// the haystack (reachable by traversing the relation link) while the wrong-relative
// decoy is a distractor, so a one-hop match on the leaf entity zeros.
func TestMultiHopJoinStructure(t *testing.T) {
	r, _ := NewRNGForVersion(31337, protocol.BenchVersionV5)
	s, _ := GenerateMemorySuiteForVersion(r, 31337, 70, 4, 0.2, protocol.BenchVersionV5)
	var hay strings.Builder
	for _, w := range s.Waves {
		for _, p := range w.Pairs {
			hay.WriteString(p.Prompt + " " + p.Response + " ")
		}
	}
	haystack := hay.String()
	n := 0
	for _, sc := range s.Cases {
		if sc.Case.QuestionType != QTMultiHop {
			continue
		}
		n++
		mc := sc.Case
		// The leaf answer is present (via the leaf pair) but only reachable by join.
		if !grade.Hit(mc.ExpectedAnswer, haystack) {
			t.Errorf("multi-hop leaf %q not seeded — unwinnable", mc.ExpectedAnswer)
		}
		// The wrong-relative decoy is present too (so a shallow one-hop match hits it).
		for _, d := range mc.DistractorAnswers {
			if !grade.Hit(d, haystack) {
				t.Errorf("multi-hop decoy %q not seeded — no shallow-match trap", d)
			}
		}
	}
	if n == 0 {
		t.Fatal("no multi-hop cases at n=70")
	}
}

// TestTemporalDepthAnswerIsSecondMostRecent pins that the graded answer is the
// SECOND-most-recent value, with the latest and the oldest as distractors, so
// naive recency (latest) and grep-the-earliest both zero.
func TestTemporalDepthAnswerIsSecondMostRecent(t *testing.T) {
	r, _ := NewRNGForVersion(99, protocol.BenchVersionV5)
	s, _ := GenerateMemorySuiteForVersion(r, 99, 80, 4, 0.2, protocol.BenchVersionV5)
	n := 0
	for _, sc := range s.Cases {
		if sc.Case.QuestionType != QTTemporalDepth {
			continue
		}
		n++
		mc := sc.Case
		if len(mc.DistractorAnswers) != 2 {
			t.Errorf("temporal-depth must have 2 distractors (latest+oldest), got %d", len(mc.DistractorAnswers))
		}
		// The latest value is named in the question (the one the user "changed to");
		// the answer must NOT be that value.
		for _, d := range mc.DistractorAnswers {
			if grade.Hit(d, mc.ExpectedAnswer) {
				t.Errorf("temporal-depth answer %q overlaps a distractor %q", mc.ExpectedAnswer, d)
			}
		}
	}
	if n == 0 {
		t.Fatal("no temporal-depth cases at n=80")
	}
}
