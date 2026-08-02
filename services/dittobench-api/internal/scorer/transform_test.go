package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func auditPair(group string, baseCorrect, xfCorrect bool) []protocol.CaseScore {
	return []protocol.CaseScore{
		{CaseID: group + "-base", Kind: protocol.KindMemory, TwinGroup: persona.AuditTwinPrefix + group, Correct: baseCorrect},
		{CaseID: group + "-xf", Kind: protocol.KindMemory, TwinGroup: persona.AuditTwinPrefix + group, Correct: xfCorrect},
	}
}

// TestTransformRobustnessMeasuresSplits pins the core of the audit metric: it
// scores CONSISTENCY, not correctness. A pair the harness got wrong twice is
// already penalized by accuracy and must not be charged again here; only the
// split -- right on the phrasing it was built for, wrong on the one it was not --
// is the brittleness signature.
func TestTransformRobustnessMeasuresSplits(t *testing.T) {
	cases := []struct {
		name  string
		in    []protocol.CaseScore
		want  float64
		pairs int
	}{
		{"both correct", auditPair("a", true, true), 1.0, 1},
		{"both wrong is consistent, not brittle", auditPair("a", false, false), 1.0, 1},
		{"split is brittle", auditPair("a", true, false), 0.0, 1},
		{"split the other way", auditPair("a", false, true), 0.0, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, pairs := TransformRobustness(tc.in)
			if got == nil {
				t.Fatal("want a robustness value, got nil")
			}
			if *got != tc.want || pairs != tc.pairs {
				t.Fatalf("got %.3f over %d pairs, want %.3f over %d", *got, pairs, tc.want, tc.pairs)
			}
		})
	}
}

// TestTransformRobustnessRate checks the aggregate over several pairs.
func TestTransformRobustnessRate(t *testing.T) {
	var in []protocol.CaseScore
	in = append(in, auditPair("a", true, true)...)   // consistent
	in = append(in, auditPair("b", true, true)...)   // consistent
	in = append(in, auditPair("c", true, false)...)  // split
	in = append(in, auditPair("d", false, false)...) // consistent
	got, pairs := TransformRobustness(in)
	if got == nil || *got != 0.75 || pairs != 4 {
		t.Fatalf("got %v over %d pairs, want 0.75 over 4", got, pairs)
	}
}

// TestTransformRobustnessIgnoresHalfPairs pins the transport-failure guard: a
// pair whose second half never came back (a dropped or timed-out case) is
// DROPPED, not scored. Scoring it would read a delivery failure as brittleness
// and cost an honest miner.
func TestTransformRobustnessIgnoresHalfPairs(t *testing.T) {
	in := []protocol.CaseScore{
		{CaseID: "lonely", Kind: protocol.KindMemory, TwinGroup: persona.AuditTwinPrefix + "a", Correct: true},
	}
	if got, pairs := TransformRobustness(in); got != nil || pairs != 0 {
		t.Fatalf("a half-delivered pair must not be scored; got %v over %d pairs", got, pairs)
	}
}

// TestTransformRobustnessNilWithoutAudits keeps the metric absent rather than
// defaulting to a number when no audit ran; a hard-coded 1.0 would look like a
// passing audit that never happened.
func TestTransformRobustnessNilWithoutAudits(t *testing.T) {
	in := []protocol.CaseScore{
		{CaseID: "x", Kind: protocol.KindMemory, TwinGroup: "twin-city", Correct: true},
		{CaseID: "y", Kind: protocol.KindMemory, TwinGroup: "twin-city", Correct: false},
	}
	if got, pairs := TransformRobustness(in); got != nil || pairs != 0 {
		t.Fatalf("want nil robustness with no audit groups, got %v over %d", got, pairs)
	}
}

// TestMetamorphicExcludesAuditGroups is the double-charge guard. Audit pairs
// share a TwinGroup, so without an explicit exclusion they would also feed the
// metamorphic-consistency factor and a single split would be penalized twice --
// a silent extra cost to a miner rather than a stronger defense.
func TestMetamorphicExcludesAuditGroups(t *testing.T) {
	in := []protocol.CaseScore{
		// One ordinary twin family, answered consistently.
		{CaseID: "t1", Kind: protocol.KindMemory, TwinGroup: "twin-city", Correct: true},
		{CaseID: "t2", Kind: protocol.KindMemory, TwinGroup: "twin-city", Correct: true},
	}
	in = append(in, auditPair("a", true, false)...) // a SPLIT audit pair
	rate := MetamorphicConsistency(in)
	if rate == nil || *rate != 1.0 {
		t.Fatalf("metamorphic consistency should ignore the split audit pair, got %v", rate)
	}
	if f := MetamorphicConsistencyFactor(in); f != 1.0 {
		t.Fatalf("metamorphic factor should be unaffected by audit pairs, got %.3f", f)
	}
	// The audit signal is still measured -- just in its own metric.
	if got, _ := TransformRobustness(in); got == nil || *got != 0.0 {
		t.Fatalf("the split audit pair should score 0 transform robustness, got %v", got)
	}
}

// TestMemoryOverCallFactor pins B4: taking a NON-memory action on a pure-memory
// question is penalized, a legitimate memory retrieval call is not, and an
// unobserved trajectory is never penalized (the validator cannot see it, and a
// self-report must not be able to create or hide the signal).
func TestMemoryOverCallFactor(t *testing.T) {
	mem := func(category string, observed bool, called ...string) protocol.CaseScore {
		return protocol.CaseScore{Kind: protocol.KindMemory, Category: category, Observed: observed, Called: called}
	}
	cases := []struct {
		name string
		in   []protocol.CaseScore
		want float64
	}{
		{"no observed memory cases", []protocol.CaseScore{mem("recall", false, "gmail_send")}, 1.0},
		{"memory retrieval is not an over-call", []protocol.CaseScore{mem("recall", true, "search_memories")}, 1.0},
		{"no calls at all", []protocol.CaseScore{mem("recall", true)}, 1.0},
		{"every case over-calls", []protocol.CaseScore{mem("recall", true, "gmail_send")}, 1.0 - memoryOverCallMaxPenalty},
		{"mixed retrieval and action still over-calls", []protocol.CaseScore{mem("recall", true, "search_memories", "gmail_send")}, 1.0 - memoryOverCallMaxPenalty},
		{"half the cases over-call", []protocol.CaseScore{mem("recall", true, "gmail_send"), mem("recall", true, "search_memories")}, 1.0 - memoryOverCallMaxPenalty/2},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := MemoryOverCallFactor(tc.in); got != round6(tc.want) {
				t.Fatalf("got %.6f, want %.6f", got, round6(tc.want))
			}
		})
	}
}

// Lifecycle writes are action prompts, not recall prompts. B4 must leave their
// mutation calls to the lifecycle read case's persistence grading, while the
// same calls on ordinary recall cases remain over-calls.
func TestMemoryOverCallExcludesLifecycleWrites(t *testing.T) {
	for _, tool := range []string{"save_memory", "update_memory", "delete_memory"} {
		t.Run(tool, func(t *testing.T) {
			in := []protocol.CaseScore{
				{Kind: protocol.KindMemory, Category: gen.QTLifecycleWrite, Observed: true, Called: []string{tool}},
				{Kind: protocol.KindMemory, Category: "single-session-recall", Observed: true, Called: []string{tool}},
			}
			if got := MemoryOverCallFactor(in); got != 1.0-memoryOverCallMaxPenalty {
				t.Fatalf("lifecycle write must be excluded and recall write penalized: got %.3f", got)
			}
		})
	}
}

func TestMemoryOverCallLifecycleExemptionIsCategorySpecific(t *testing.T) {
	in := []protocol.CaseScore{
		{Kind: protocol.KindMemory, Category: gen.QTLifecycleWrite, Observed: true, Called: []string{"gmail_send"}},
		{Kind: protocol.KindMemory, Category: gen.QTLifecycleRead, Observed: true, Called: []string{"gmail_send"}},
	}
	if got := MemoryOverCallFactor(in); got != 1.0-memoryOverCallMaxPenalty {
		t.Fatalf("only the lifecycle write half should be exempt, got %.3f", got)
	}
}

// TestMemoryOverCallIgnoresToolCases guards the scope: a tool case is SUPPOSED
// to call tools, and tool-call discipline is already scored by
// ToolEfficiencyFactor. Counting tool cases here would penalize a harness twice
// for doing its job.
func TestMemoryOverCallIgnoresToolCases(t *testing.T) {
	in := []protocol.CaseScore{
		{Kind: protocol.KindTool, Observed: true, Called: []string{"gmail_send"}},
	}
	if got := MemoryOverCallFactor(in); got != 1.0 {
		t.Fatalf("tool cases must not feed the memory over-call factor, got %.3f", got)
	}
}

func auditPair2(group string, baseCorrect, xfCorrect bool) []protocol.CaseScore {
	return []protocol.CaseScore{
		{CaseID: group + "-base", Kind: protocol.KindMemory, TwinGroup: persona.AuditTwinPrefix + group,
			AuditHalf: protocol.AuditHalfBase, Correct: baseCorrect},
		{CaseID: group + "-xf", Kind: protocol.KindMemory, TwinGroup: persona.AuditTwinPrefix + group,
			AuditHalf: protocol.AuditHalfTransform, Correct: xfCorrect},
	}
}

// TestAuditPairsKeepsDirection is the fix for what the 2026-07-18 calibration
// exposed. The old ratio scored a base-correct/transform-wrong pair and its
// mirror image identically, which is precisely the distinction between a
// brittle harness and a noisy honest one, so it could not separate them.
func TestAuditPairsKeepsDirection(t *testing.T) {
	var in []protocol.CaseScore
	in = append(in, auditPair2("a", true, false)...)  // brittleness event
	in = append(in, auditPair2("b", false, true)...)  // the mirror: not brittleness
	in = append(in, auditPair2("c", true, true)...)   // both correct
	in = append(in, auditPair2("d", false, false)...) // both wrong

	got := AuditPairs(in)
	want := protocol.AuditPairCounts{BothCorrect: 1, BaseOnly: 1, TransformOnly: 1, BothWrong: 1}
	if got != want {
		t.Fatalf("got %+v, want %+v", got, want)
	}
	if got.Discordant() != 2 || got.Total() != 4 {
		t.Fatalf("discordant %d total %d, want 2 and 4", got.Discordant(), got.Total())
	}
	// The old ratio cannot tell the brittle pair from its mirror; the counts can.
	if got.BaseOnly == got.TransformOnly {
		t.Log("symmetric here by construction; the separation is in the counts, not a rate")
	}
}

// TestAuditPairsDropsHalfDeliveredPairs: a dropped or timed-out case must not
// read as brittleness.
func TestAuditPairsDropsHalfDeliveredPairs(t *testing.T) {
	in := []protocol.CaseScore{
		{CaseID: "lonely", Kind: protocol.KindMemory, TwinGroup: persona.AuditTwinPrefix + "a",
			AuditHalf: protocol.AuditHalfBase, Correct: true},
	}
	if got := AuditPairs(in); got.Total() != 0 {
		t.Fatalf("half-delivered pair counted: %+v", got)
	}
}

// TestAuditPairsIgnoresOrdinaryTwins keeps the ordinary metamorphic families out
// of the audit table; they are a different signal with a different penalty.
func TestAuditPairsIgnoresOrdinaryTwins(t *testing.T) {
	in := []protocol.CaseScore{
		{CaseID: "t1", Kind: protocol.KindMemory, TwinGroup: "twin-city", Correct: true},
		{CaseID: "t2", Kind: protocol.KindMemory, TwinGroup: "twin-city", Correct: false},
	}
	if got := AuditPairs(in); got.Total() != 0 {
		t.Fatalf("ordinary twins counted as audit pairs: %+v", got)
	}
}

// TestAuditPairsPool checks the property the counts exist for: they sum, so a
// verdict can be taken over an agent's whole history instead of per run, where
// ~4 pairs can never reach significance.
func TestAuditPairsPool(t *testing.T) {
	run1 := AuditPairs(auditPair2("a", true, false))
	run2 := AuditPairs(auditPair2("b", true, false))
	run3 := AuditPairs(auditPair2("c", false, false))
	pooled := run1.Add(run2).Add(run3)
	if pooled.BaseOnly != 2 || pooled.BothWrong != 1 || pooled.Total() != 3 {
		t.Fatalf("pooled %+v", pooled)
	}
}
