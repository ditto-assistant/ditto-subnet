package persona

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestBuildPlanDeterministic is the golden test: same (seed, opts) ⇒
// byte-identical plan. This is the reproducibility contract: if
// any wall-clock, crypto-rand, or map-iteration nondeterminism creeps into the
// plan layer, this fails.
func TestBuildPlanDeterministic(t *testing.T) {
	opts := DefaultOpts()
	for _, seed := range []int64{1, 42, 1 << 40, 9223372036854775807} {
		a := marshal(t, BuildPlan(seed, opts))
		b := marshal(t, BuildPlan(seed, opts))
		if a != b {
			t.Fatalf("seed %d: plan not reproducible across two builds", seed)
		}
	}
}

// TestBuildPlanDistinctSeeds guards against a degenerate generator that ignores
// the seed (every universe identical would defeat the anti-memorization point).
func TestBuildPlanDistinctSeeds(t *testing.T) {
	opts := DefaultOpts()
	seen := map[string]bool{}
	for _, seed := range []int64{1, 2, 3, 100, 12345, 999999} {
		m := marshal(t, BuildPlan(seed, opts))
		if seen[m] {
			t.Fatalf("seed %d produced a duplicate plan — seed is not driving generation", seed)
		}
		seen[m] = true
	}
}

// TestPlanStructure checks the plan contains the material every question type
// needs: an update chain, a reversal, list facts, preferences, and
// near-miss distractors, and that no fact carries an empty canonical value.
func TestPlanStructure(t *testing.T) {
	p := BuildPlan(42, DefaultOpts())
	if p.Name == "" || !strings.Contains(p.Name, " ") {
		t.Fatalf("persona name malformed: %q", p.Name)
	}
	var updates, reversals, lists, prefs, distractors, current int
	for _, f := range p.Facts {
		if strings.TrimSpace(f.Value) == "" {
			t.Fatalf("fact %s has empty canonical value", f.ID)
		}
		if f.Supersedes != "" && !f.Reversal {
			updates++
		}
		if f.Reversal {
			reversals++
		}
		switch f.Kind {
		case KindList:
			lists++
		case KindPreference:
			prefs++
		case KindDistractor:
			distractors++
		}
		if f.Current {
			current++
		}
	}
	if updates == 0 {
		t.Error("no update-chain fact (knowledge-update material missing)")
	}
	if reversals == 0 {
		t.Error("no reversal fact (contradiction material missing)")
	}
	if lists == 0 {
		t.Error("no list facts (multi-session synthesis material missing)")
	}
	if prefs == 0 {
		t.Error("no preference facts")
	}
	if distractors == 0 {
		t.Error("no near-miss distractors")
	}
	if current == 0 {
		t.Error("no current-state facts")
	}

	// Every superseded fact must reference a real predecessor of the same
	// attribute. In an N-state chain an intermediate fact both supersedes its
	// predecessor AND is itself superseded, so the invariant is: a fact that has
	// been superseded is never current (the current one is the non-superseded tail).
	byID := map[string]Fact{}
	superseded := map[string]bool{}
	for _, f := range p.Facts {
		byID[f.ID] = f
		if f.Supersedes != "" {
			superseded[f.Supersedes] = true
		}
	}
	for _, f := range p.Facts {
		if f.Supersedes == "" {
			continue
		}
		prev, ok := byID[f.Supersedes]
		if !ok {
			t.Fatalf("fact %s supersedes unknown %s", f.ID, f.Supersedes)
		}
		if prev.Attribute != f.Attribute {
			t.Fatalf("fact %s supersedes a different attribute (%s vs %s)", f.ID, prev.Attribute, f.Attribute)
		}
	}
	for _, f := range p.Facts {
		if superseded[f.ID] && f.Current {
			t.Fatalf("superseded fact %s still marked current", f.ID)
		}
	}
}

// TestSessionsCoverFacts checks every fact is scripted into exactly one session
// beat and sessions have strictly increasing day offsets (temporal-reasoning
// ground truth).
func TestSessionsCoverFacts(t *testing.T) {
	p := BuildPlan(7, DefaultOpts())
	beatFor := map[string]int{}
	prevDay := -1
	for _, s := range p.Sessions {
		if s.DayOffset <= prevDay {
			t.Fatalf("session %d day offset %d not strictly increasing (prev %d)", s.Index, s.DayOffset, prevDay)
		}
		prevDay = s.DayOffset
		if len(s.Beats) == 0 {
			t.Fatalf("session %d has no beats", s.Index)
		}
		for _, b := range s.Beats {
			if b.Kind == BeatFact {
				beatFor[b.FactID]++
			}
			if strings.TrimSpace(b.UserText) == "" || strings.TrimSpace(b.AsstText) == "" {
				t.Fatalf("session %d beat has empty text", s.Index)
			}
		}
	}
	for _, f := range p.Facts {
		if beatFor[f.ID] != 1 {
			t.Fatalf("fact %s scripted into %d beats (want 1)", f.ID, beatFor[f.ID])
		}
	}
}

// TestFactValuePreservedInBeat ensures the template surface already carries the
// canonical value verbatim: the invariant the LLM surface verification enforces.
func TestFactValuePreservedInBeat(t *testing.T) {
	p := BuildPlan(123, DefaultOpts())
	beat := map[string]Beat{}
	for _, s := range p.Sessions {
		for _, b := range s.Beats {
			if b.Kind == BeatFact {
				beat[b.FactID] = b
			}
		}
	}
	for _, f := range p.Facts {
		b := beat[f.ID]
		// Assistant-side recommendations put the value ONLY in the assistant turn
		// (the user request names no value); that is what makes them assistant-side
		// recall. Every other fact is user-stated.
		// Coreference (anti-gaming V4): recurring-topic follow-up mentions refer to
		// the topic obliquely and deliberately do NOT carry the full label, so the
		// per-beat containment invariant does not apply to them. The topic is still
		// grounded by its label in the anchor mention (verified in
		// evidenceCarriesValue). Skip recurring facts here.
		if f.Kind == KindRecurring {
			continue
		}
		side, sideName := b.UserText, "user"
		if f.Kind == KindAsstRec {
			side, sideName = b.AsstText, "assistant"
			if strings.Contains(b.UserText, f.Value) {
				t.Fatalf("asst-rec fact %s value %q leaked into user text %q", f.ID, f.Value, b.UserText)
			}
		}
		if !strings.Contains(side, f.Value) {
			t.Fatalf("fact %s value %q not present in its beat %s text %q", f.ID, f.Value, sideName, side)
		}
	}
}

func marshal(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(b)
}

// TestNegatedDistractorBecomesConfusable verifies the NoOp/knowledge-conflict
// distractor: each run seeds a "considered but rejected" mention whose value
// becomes a distractor for that attribute's recall question, so a retriever that
// surfaces it is scored 0 while a reader honoring the negation is unaffected.
// Every plan is guaranteed one: BuildPlan always emits current self scalars for
// every universal scalarSpec, all of whose pools hold more than one value, so
// addNegatedDistractor never hits its empty-candidate early return (its only
// other bail-out, eight straight rng draws of the user's own value, is a
// deterministic per-seed event that never occurs; probed across 500 seeds).
// A missing negated fact on ANY seed is therefore a generator regression.
func TestNegatedDistractorBecomesConfusable(t *testing.T) {
	for seed := int64(1); seed <= 30; seed++ {
		// The adversarial NoOp distractor is v3-only: v2 is a frozen contract, so
		// adding a fact to it would change bytes already scored against.
		p, err := BuildPlanForVersion(seed, fullOpts(), protocol.BenchVersionV3)
		if err != nil {
			t.Fatalf("plan: %v", err)
		}
		var neg *Fact
		for i := range p.Facts {
			if p.Facts[i].Kind == KindNegated {
				neg = &p.Facts[i]
				break
			}
		}
		if neg == nil {
			t.Fatalf("seed %d: no KindNegated fact in the plan (the negated distractor family regressed)", seed)
		}
		// The rejected value must be first-person in the haystack.
		if !strings.Contains(neg.UserText, neg.Value) {
			t.Fatalf("seed %d: negated value %q not in its mention %q", seed, neg.Value, neg.UserText)
		}
		// It must NOT be a current self value.
		for _, f := range p.Facts {
			if f.Kind == KindScalar && f.Entity == "self" && f.Current && f.Attribute == neg.Attribute && f.Value == neg.Value {
				t.Fatalf("seed %d: negated value %q equals the real self value", seed, neg.Value)
			}
		}
		// It must appear as a distractor for that attribute's recall question.
		dis := distractorsFor(p, neg.Attribute)
		found := false
		for _, d := range dis {
			if d == neg.Value {
				found = true
			}
		}
		if !found {
			t.Fatalf("seed %d: negated value %q not in distractorsFor(%q)=%v", seed, neg.Value, neg.Attribute, dis)
		}
	}
}
