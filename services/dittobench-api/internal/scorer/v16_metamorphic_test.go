package scorer

import (
	"fmt"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// twinRun builds a run of `groups` two-member invariance twin groups, the
// first `split` of them split (one sibling right, one wrong).
func twinRun(groups, split int) []protocol.CaseScore {
	var out []protocol.CaseScore
	for g := 0; g < groups; g++ {
		id := fmt.Sprintf("twin-%d", g)
		out = append(out,
			protocol.CaseScore{Kind: protocol.KindMemory, TwinGroup: id, Correct: true, Score: 1},
			protocol.CaseScore{Kind: protocol.KindMemory, TwinGroup: id, Correct: g >= split, Score: 1},
		)
	}
	return out
}

// Bench 16's datagen half measures 5 twin groups where v15 measured 13. One
// split group must cost what it cost on v15 (0.40/13), not 0.40/5.
func TestV16MetamorphicChargesPerSplitAgainstTheV15Reference(t *testing.T) {
	v15 := metamorphicConsistencyFactorForVersion(twinRun(13, 1), v7MetamorphicMaxPenalty, protocol.BenchVersionV15)
	v16 := metamorphicConsistencyFactorForVersion(twinRun(5, 1), v7MetamorphicMaxPenalty, protocol.DuelingMinBenchVersion)
	if v16 != v15 || v16 != round6(1-0.40/13) {
		t.Fatalf("one split group: v16 factor %v, v15 factor %v, want %v", v16, v15, round6(1-0.40/13))
	}
	// The old v16 reading (rate over its own 5 groups) was 0.92.
	if old := metamorphicConsistencyFactorWith(twinRun(5, 1), v7MetamorphicMaxPenalty); old != 0.92 {
		t.Fatalf("v15 rule on 5 groups = %v, want 0.92", old)
	}
	// Every group split still costs, scaled to the reference.
	if got := metamorphicConsistencyFactorForVersion(twinRun(5, 5), v7MetamorphicMaxPenalty, 16); got != round6(1-0.40*5/13) {
		t.Fatalf("all split = %v", got)
	}
	// Nothing measured stays neutral.
	if got := metamorphicConsistencyFactorForVersion(nil, v7MetamorphicMaxPenalty, 16); got != 1 {
		t.Fatalf("no twins = %v", got)
	}
}

// At or above the reference, and below bench 16 at any size, the factor is
// byte-identical to the v15 rule.
func TestMetamorphicReferenceIsByteIdenticalWhereItDoesNotApply(t *testing.T) {
	for _, groups := range []int{1, 3, 5, 13, 20} {
		for split := 0; split <= groups; split++ {
			run := twinRun(groups, split)
			want := metamorphicConsistencyFactorWith(run, v7MetamorphicMaxPenalty)
			for _, v := range []int{protocol.BenchVersionV7, protocol.BenchVersionV14, protocol.BenchVersionV15} {
				if got := metamorphicConsistencyFactorForVersion(run, v7MetamorphicMaxPenalty, v); got != want {
					t.Fatalf("v%d groups=%d split=%d: %v != %v", v, groups, split, got, want)
				}
			}
			if groups >= MetamorphicReferenceGroups {
				if got := metamorphicConsistencyFactorForVersion(run, v7MetamorphicMaxPenalty, 16); got != want {
					t.Fatalf("v16 at/above reference groups=%d split=%d: %v != %v", groups, split, got, want)
				}
			}
		}
	}
	// The published advisory rate is unchanged.
	if r := MetamorphicConsistency(twinRun(5, 1)); r == nil || *r != 0.8 {
		t.Fatalf("rate = %v", r)
	}
}

// The composite gate for a v16 run with one split of five groups.
func TestV16CompositeGateUsesTheReference(t *testing.T) {
	run := twinRun(5, 1)
	if got, want := compositeGateV7(run, 16), round6(1-0.40/13); got != want {
		t.Fatalf("v16 composite gate = %v, want %v", got, want)
	}
	if got := compositeGateV7(run, protocol.BenchVersionV15); got != 0.92 {
		t.Fatalf("v15 composite gate = %v, want 0.92", got)
	}
}
