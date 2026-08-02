package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// mem builds a memory CaseScore for a category with a 0/1 correctness.
func mem(category string, correct bool) protocol.CaseScore {
	score := 0.0
	if correct {
		score = 1.0
	}
	return protocol.CaseScore{
		Kind:     protocol.KindMemory,
		Category: category,
		Score:    score,
		Correct:  correct,
	}
}

// TestConversationalSanityMetric pins the graded (geometric-mean) conjunction: a
// FULLY-failed slice still zeroes the metric (a canned reply cannot bank the
// greeting slice and hide a leaked/uncaptured declarative), but a partially-weak
// slice is graded rather than allowed to wholly dictate the score the way the old
// weakest-link minimum did — the change that halves the metric's per-seed variance.
func TestConversationalSanityMetric(t *testing.T) {
	if ConversationalSanity(nil) != nil {
		t.Fatal("no conversational cases must yield nil metric")
	}
	// Canned bot: passes greeting non-leak, fails declarative echo and behavior.
	// A fully-failed slice zeroes the geometric mean (anti-gaming interlock).
	canned := []protocol.CaseScore{
		mem(gen.QTChitchat, true), mem(gen.QTChitchat, true),
		mem(gen.QTDeclarativeAck, false), mem(gen.QTDeclarativeAck, false),
		mem(gen.QTDeclarativeBehavior, false),
	}
	if m := ConversationalSanity(canned); m == nil || *m != 0 {
		t.Fatalf("canned bot conjunction must be 0, got %v", m)
	}
	// Honest harness: all three slices pass -> geometric mean 1.
	honest := []protocol.CaseScore{
		mem(gen.QTChitchat, true),
		mem(gen.QTDeclarativeAck, true),
		mem(gen.QTDeclarativeBehavior, true),
	}
	if m := ConversationalSanity(honest); m == nil || *m != 1 {
		t.Fatalf("honest conjunction must be 1, got %v", m)
	}
	// Partial: greeting 1.0, declarative 0.5, behavior 1.0 -> geometric mean
	// (1.0 * 0.5 * 1.0)^(1/3) ~= 0.7937, graded rather than the old min of 0.5.
	partial := []protocol.CaseScore{
		mem(gen.QTChitchat, true),
		mem(gen.QTDeclarativeAck, true), mem(gen.QTDeclarativeAck, false),
		mem(gen.QTDeclarativeBehavior, true),
	}
	if m := ConversationalSanity(partial); m == nil || *m < 0.793 || *m > 0.794 {
		t.Fatalf("partial geometric-mean conjunction must be ~0.7937, got %v", m)
	}
}

// TestConversationalSanityFactor pins the bounded, saturating factor: 1.0 for an
// honest harness, convSanityFloor for a total failure, 1.0 when nothing ran.
func TestConversationalSanityFactor(t *testing.T) {
	if f := ConversationalSanityFactor(nil); f != 1.0 {
		t.Fatalf("no conversational cases must give factor 1.0, got %v", f)
	}
	honest := []protocol.CaseScore{mem(gen.QTChitchat, true), mem(gen.QTDeclarativeAck, true), mem(gen.QTDeclarativeBehavior, true)}
	if f := ConversationalSanityFactor(honest); f != 1.0 {
		t.Fatalf("honest harness must take no penalty, got %v", f)
	}
	fail := []protocol.CaseScore{mem(gen.QTChitchat, false), mem(gen.QTDeclarativeAck, false), mem(gen.QTDeclarativeBehavior, false)}
	if f := ConversationalSanityFactor(fail); f != convSanityFloor {
		t.Fatalf("total failure must hit the floor %v, got %v", convSanityFloor, f)
	}
}

// TestTransformAuditFactorGate pins the enforcement gate: OFF by default (factor
// 1.0 regardless of brittleness), and when enforced it penalizes only DIRECTIONAL
// brittleness over enough pairs (a symmetric honest split is not penalized).
func TestTransformAuditFactorGate(t *testing.T) {
	// Directional brittleness: 4 base-only, 0 transform-only.
	brittle := auditPerCase(4, 0, 2)
	if f := TransformAuditFactor(brittle, false); f != 1.0 {
		t.Fatalf("default (unenforced) must be 1.0, got %v", f)
	}
	if f := TransformAuditFactor(brittle, true); f >= 1.0 {
		t.Fatalf("enforced directional brittleness must penalize, got %v", f)
	}
	// Symmetric honest split (3 base-only, 3 transform-only): not penalized.
	symmetric := auditPerCase(3, 3, 2)
	if f := TransformAuditFactor(symmetric, true); f != 1.0 {
		t.Fatalf("symmetric split must not be penalized, got %v", f)
	}
	// Too few pairs: not acted on.
	tiny := auditPerCase(2, 0, 0)
	if f := TransformAuditFactor(tiny, true); f != 1.0 {
		t.Fatalf("below min-pairs must be 1.0, got %v", f)
	}
}

// auditPerCase builds a perCase slice with the given transform-audit outcome
// counts (baseOnly correct-on-base-only, transformOnly correct-on-transform-only,
// bothCorrect both), as scorer.AuditPairs consumes them.
func auditPerCase(baseOnly, transformOnly, bothCorrect int) []protocol.CaseScore {
	var out []protocol.CaseScore
	add := func(baseCorrect, xformCorrect bool, i int) {
		grp := persona.AuditTwinPrefix + "g" + string(rune('a'+i))
		out = append(out,
			protocol.CaseScore{Kind: protocol.KindMemory, TwinGroup: grp, AuditHalf: protocol.AuditHalfBase, Correct: baseCorrect},
			protocol.CaseScore{Kind: protocol.KindMemory, TwinGroup: grp, AuditHalf: protocol.AuditHalfTransform, Correct: xformCorrect},
		)
	}
	i := 0
	for ; baseOnly > 0; baseOnly-- {
		add(true, false, i)
		i++
	}
	for ; transformOnly > 0; transformOnly-- {
		add(false, true, i)
		i++
	}
	for ; bothCorrect > 0; bothCorrect-- {
		add(true, true, i)
		i++
	}
	return out
}

// TestV5ShipGateRegression is the offline portion of the 4.11 ship gate: a
// Unicorn-style router with strong memory retrieval but a leak-on-greeting,
// no-declarative-capture strategy scores near the top on the memory half a v4-style
// contract sees, yet drops below the 0.85 champion line under v5 because it fails
// conversational sanity -- while an honest harness with the same retrieval stays
// in its fair band. This is the direct proof that 4.1/4.2 killed the exploit
// without a blanket difficulty hike.
func TestV5ShipGateRegression(t *testing.T) {
	// A shared strong-retrieval memory half: 30 ordinary recall cases at ~0.9.
	strongHalf := func() []protocol.CaseScore {
		var cs []protocol.CaseScore
		for i := 0; i < 30; i++ {
			cs = append(cs, mem(persona.QTMultiSession, i%10 != 0)) // 27/30 correct
		}
		return cs
	}

	// Conversational cases (2 greeting, 2 declarative, 2 behavior + read/write).
	convHonest := []protocol.CaseScore{
		mem(gen.QTChitchat, true), mem(gen.QTChitchat, true),
		mem(gen.QTDeclarativeAck, true), mem(gen.QTDeclarativeAck, true),
		mem(gen.QTDeclarativeWrite, true), mem(gen.QTDeclarativeRead, true),
		mem(gen.QTDeclarativeBehavior, true), mem(gen.QTDeclarativeBehavior, true),
	}
	// The router leaks on greetings, never captured the declarative writes, and
	// misapplies preferences: it fails every conversational case.
	convRouter := []protocol.CaseScore{
		mem(gen.QTChitchat, false), mem(gen.QTChitchat, false),
		mem(gen.QTDeclarativeAck, false), mem(gen.QTDeclarativeAck, false),
		mem(gen.QTDeclarativeWrite, true), mem(gen.QTDeclarativeRead, false),
		mem(gen.QTDeclarativeBehavior, false), mem(gen.QTDeclarativeBehavior, false),
	}

	honest := append(strongHalf(), convHonest...)
	router := append(strongHalf(), convRouter...)

	// What a v4-style contract sees: only the v4 case classes exist (no
	// conversational cases at all), so the router looks like a near-champion.
	v4Router := scorer_v4View(strongHalf())
	if v4Router < 0.85 {
		t.Fatalf("router should look like a champion on the memory half alone, got %.3f", v4Router)
	}

	honestV5 := AggregateForVersion("h", honest, protocol.BenchVersionV5).Composite
	routerV5 := AggregateForVersion("r", router, protocol.BenchVersionV5).Composite

	if routerV5 >= 0.85 {
		t.Fatalf("v5 must drop the routing exploiter below 0.85, got %.3f", routerV5)
	}
	if honestV5 < 0.85 {
		t.Fatalf("honest harness must stay in its fair band (>=0.85), got %.3f", honestV5)
	}
	if honestV5 <= routerV5 {
		t.Fatalf("v5 must separate honest (%.3f) from router (%.3f)", honestV5, routerV5)
	}
	t.Logf("ship gate: honest v5=%.3f, router v5=%.3f (router v4-view=%.3f)", honestV5, routerV5, v4Router)
}

// scorer_v4View is the composite a pre-v5 contract would assign: no conversational
// tier. It reuses the v4 aggregation path over the same cases.
func scorer_v4View(perCase []protocol.CaseScore) float64 {
	return AggregateForVersion("v4", perCase, protocol.BenchVersionV4).Composite
}
