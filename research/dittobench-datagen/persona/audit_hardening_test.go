package persona

import (
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// fullOpts mirrors the full-run profile (gen.personaOptsFor for n > 25), the
// shape the audit's 40-seed census was taken over.
func fullOpts() Opts {
	return Opts{Sessions: 9, Projects: 10, Trips: 8, Pets: 5, UpdateChains: 4, Reversals: 3, DecoyPeople: 10, DomainItems: 4, LongChain: 4}
}

// TestNoDistractorOverlapsAcceptedAnswer is the cross-pool overlap regression
// (P0): no emitted question may carry a distractor that bound-matches inside
// its expected answer or any accepted item, because a fully correct response
// would surface that distractor by construction and be zeroed.
func TestNoDistractorOverlapsAcceptedAnswer(t *testing.T) {
	for seed := int64(1); seed <= 200; seed++ {
		p := BuildPlan(seed, fullOpts())
		for _, q := range DeriveQuestions(p) {
			for _, d := range q.Distractors {
				if q.Answer != "" && grade.Hit(d, q.Answer) {
					t.Fatalf("seed %d %s: distractor %q bound-matches inside answer %q", seed, q.ID, d, q.Answer)
				}
				for _, it := range q.Items {
					if grade.Hit(d, it) {
						t.Fatalf("seed %d %s: distractor %q bound-matches inside item %q", seed, q.ID, d, it)
					}
				}
			}
		}
	}
}

// TestListCountAnswersVaryAcrossSeeds pins the P0 hardcodability fix: the
// list-count answers (projects/trips/pets) must take several distinct values
// across seeds instead of one constant per run profile.
func TestListCountAnswersVaryAcrossSeeds(t *testing.T) {
	answers := map[string]map[string]bool{}
	for seed := int64(1); seed <= 60; seed++ {
		p := BuildPlan(seed, fullOpts())
		for _, q := range DeriveQuestions(p) {
			switch q.ID {
			case "q-count-project", "q-count-trip", "q-count-pet":
				if answers[q.ID] == nil {
					answers[q.ID] = map[string]bool{}
				}
				answers[q.ID][q.Answer] = true
			}
		}
	}
	for id, vals := range answers {
		if len(vals) < 3 {
			t.Errorf("%s: only %d distinct count answers across 60 seeds (%v) — hardcodable", id, len(vals), vals)
		}
	}
	if len(answers) != 3 {
		t.Fatalf("expected all three universal list-count questions, got %v", answers)
	}
}

// TestComputedFamilyAlwaysEmits pins the P2 coverage fix: every seed emits at
// least one computed-answer question (BuildPlan guarantees an employer-or-city
// update anchor), and the answers are spread rather than skewed to 0.
func TestComputedFamilyAlwaysEmits(t *testing.T) {
	vals := map[string]int{}
	zero := 0
	total := 0
	for seed := int64(1); seed <= 60; seed++ {
		p := BuildPlan(seed, fullOpts())
		n := 0
		for _, q := range DeriveQuestions(p) {
			if q.Type != QTComputed {
				continue
			}
			n++
			total++
			vals[q.Answer]++
			if q.Answer == "0" {
				zero++
			}
		}
		if n == 0 {
			t.Fatalf("seed %d: no computed-answer question emitted", seed)
		}
	}
	if len(vals) < 3 {
		t.Fatalf("computed answers take only %d distinct values across seeds: %v", len(vals), vals)
	}
	if zero*2 >= total {
		t.Fatalf("computed answers still skewed to 0: %d/%d", zero, total)
	}
}

// TestContradictionNotTypeDetermined pins the P2 fix: contradiction questions
// cover BOTH reversed (cessation) and standing (persistence) opinions with the
// same question surface, so "you no longer do it" is not a free win.
func TestContradictionNotTypeDetermined(t *testing.T) {
	kinds := map[string]int{}
	for seed := int64(1); seed <= 20; seed++ {
		p := BuildPlan(seed, fullOpts())
		for _, q := range DeriveQuestions(p) {
			if q.Type != QTContradiction {
				continue
			}
			kinds[q.Kind]++
			if !strings.HasPrefix(q.Text, "How do I feel about ") {
				t.Fatalf("seed %d %s: contradiction surfaces diverged by kind: %q", seed, q.ID, q.Text)
			}
		}
	}
	// SYMMETRIC mix: equal reversed and standing pools cap a retrieval-free
	// stance guess (including a hedged both-ways template) at ~50%.
	if kinds[protocol.AnswerReversal] == 0 || kinds[protocol.AnswerReversal] != kinds[protocol.AnswerPersistence] {
		t.Fatalf("expected an equal reversal/persistence contradiction mix, got %v", kinds)
	}
}

// TestRecurringCountSpread pins the P1 entropy fix: the recurring-mention count
// answer spans a wide range (2..8) across seeds instead of clustering in 3..5.
func TestRecurringCountSpread(t *testing.T) {
	vals := map[string]bool{}
	for seed := int64(1); seed <= 80; seed++ {
		p := BuildPlan(seed, fullOpts())
		for _, q := range DeriveQuestions(p) {
			if q.Type == QTAggregation {
				vals[q.Answer] = true
			}
		}
	}
	if len(vals) < 5 {
		t.Fatalf("recurring-mention counts take only %d distinct values across 80 seeds: %v", len(vals), vals)
	}
}

// TestDurationAnswersReachMonthScale pins the P1 day-offset spread: elapsed-
// duration answers must include month-scale gaps, not only day/week buckets.
func TestDurationAnswersReachMonthScale(t *testing.T) {
	months := false
	for seed := int64(1); seed <= 60 && !months; seed++ {
		p := BuildPlan(seed, fullOpts())
		for _, q := range DeriveQuestions(p) {
			if q.Kind == protocol.AnswerDuration && strings.Contains(q.Answer, "month") {
				months = true
				break
			}
		}
	}
	if !months {
		t.Fatal("no month-scale duration answer in 60 seeds — day offsets still clustered")
	}
}

// TestInvarianceTwinsSeedKeyed pins the P1 fix and the N2 family extension: a run
// derives MULTIPLE metamorphic families (so the consistency factor averages
// rather than coin-flips), each family is exactly twinSiblings distinct surfaces
// of one attribute, and which families a seed produces varies across seeds.
func TestInvarianceTwinsSeedKeyed(t *testing.T) {
	familySets := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		p := BuildPlan(seed, fullOpts())
		byGroup := map[string][]string{}
		for _, q := range DeriveQuestions(p) {
			if q.TwinGroup == "" {
				continue
			}
			byGroup[q.TwinGroup] = append(byGroup[q.TwinGroup], q.Text)
		}
		if len(byGroup) < 2 {
			t.Fatalf("seed %d: expected multiple metamorphic families, got %d", seed, len(byGroup))
		}
		keys := make([]string, 0, len(byGroup))
		for grp, texts := range byGroup {
			seen := map[string]bool{}
			for _, tx := range texts {
				if seen[tx] {
					t.Fatalf("seed %d family %s: duplicate surface %q", seed, grp, tx)
				}
				seen[tx] = true
			}
			if len(texts) != twinSiblings {
				t.Fatalf("seed %d family %s: expected %d siblings, got %d", seed, grp, twinSiblings, len(texts))
			}
			sort.Strings(texts)
			keys = append(keys, grp+":"+strings.Join(texts, "|"))
		}
		sort.Strings(keys)
		familySets[strings.Join(keys, " || ")] = true
	}
	if len(familySets) < 8 {
		t.Fatalf("twin family sets take only %d distinct forms across 40 seeds — not seed-keyed", len(familySets))
	}
}

// TestAggregationAskSeedKeyed pins the P1 fix: the recurring-aggregation ask is
// phrased from a variant pool, keyed by seed.
func TestAggregationAskSeedKeyed(t *testing.T) {
	byAttr := map[string]map[string]bool{}
	for seed := int64(1); seed <= 80; seed++ {
		p := BuildPlan(seed, fullOpts())
		for _, q := range DeriveQuestions(p) {
			if q.Type != QTAggregation {
				continue
			}
			attr := strings.TrimPrefix(q.ID, "q-agg-")
			if byAttr[attr] == nil {
				byAttr[attr] = map[string]bool{}
			}
			byAttr[attr][q.Text] = true
		}
	}
	multi := 0
	for _, texts := range byAttr {
		if len(texts) >= 2 {
			multi++
		}
	}
	if multi == 0 {
		t.Fatalf("aggregation asks are mono-phrased per topic across 80 seeds: %v", byAttr)
	}
}

// TestTemporalGroundTruthIsChronological pins the ordering fix found during
// the audit follow-up: the rendered haystack orders pairs by session day
// offset, so order3/duration ground truth must be in strictly increasing
// SESSION order — a Seq-ordered triple contradicts the timestamps a correct
// harness reads.
func TestTemporalGroundTruthIsChronological(t *testing.T) {
	for seed := int64(1); seed <= 80; seed++ {
		p := BuildPlan(seed, fullOpts())
		byID := map[string]Fact{}
		for _, f := range p.Facts {
			byID[f.ID] = f
		}
		for _, q := range DeriveQuestions(p) {
			if !strings.HasPrefix(q.ID, "q-order3-") && !strings.HasPrefix(q.ID, "q-dur-") {
				continue
			}
			last := -1
			for _, id := range q.Evidence {
				f, ok := byID[id]
				if !ok {
					t.Fatalf("seed %d %s: unknown evidence fact %s", seed, q.ID, id)
				}
				if f.Session <= last {
					t.Fatalf("seed %d %s: evidence sessions not strictly increasing (%d after %d)", seed, q.ID, f.Session, last)
				}
				last = f.Session
			}
		}
	}
}

// TestFilteredAggStraddle pins the both-sides guarantee for every
// filtered-aggregation join (filtAggSpecs): whenever the anchor chain updated,
// its joined list family has at least one event strictly before AND one
// strictly after the change, and NONE on the change session itself (an event
// there renders after the change inside the conversation but a strict Session
// comparison would not count it, so the ground truth would contradict the
// visible timeline).
func TestFilteredAggStraddle(t *testing.T) {
	for seed := int64(1); seed <= 200; seed++ {
		p := BuildPlan(seed, fullOpts())
		for _, spec := range filtAggSpecs {
			change := -1
			for _, f := range p.Facts {
				if f.Kind == KindScalar && f.Entity == "self" && f.Attribute == spec.chainAttr && f.Current && f.Supersedes != "" {
					change = f.Session
				}
			}
			if change < 0 {
				continue
			}
			before, after, at := 0, 0, 0
			for _, f := range p.Facts {
				if f.Kind != KindList || f.Attribute != spec.listAttr {
					continue
				}
				switch {
				case f.Session < change:
					before++
				case f.Session > change:
					after++
				default:
					at++
				}
			}
			if before == 0 || after == 0 || at != 0 {
				t.Fatalf("seed %d: %s change (session %d) vs %s events: before=%d at=%d after=%d",
					seed, spec.chainAttr, change, spec.listAttr, before, at, after)
			}
		}
	}
}

// TestPoolValuesGradeable pins the answer-key sufficiency invariant that
// surfaced the unwinnable "May" birthday month: every pool value a plan can
// pick as an expected answer must be creditable by the grader when present in
// a response (the common-word guard must never block a canonical value).
func TestPoolValuesGradeable(t *testing.T) {
	pools := map[string][]string{}
	for _, s := range allScalarSpecs() {
		pools[s.attr] = s.pool
	}
	for _, s := range prefSpecs {
		pools[s.attr] = s.pool
	}
	for _, ls := range []listSpec{projectSpec, tripSpec, petSpec} {
		pools[ls.attr] = ls.pool
	}
	for _, d := range domains {
		for _, lc := range d.lists {
			pools[lc.spec.attr] = lc.spec.pool
		}
	}
	for _, s := range asstRecSpecs {
		pools[s.attr] = s.pool
	}
	for attr, pool := range pools {
		for _, v := range pool {
			if !grade.Hit(v, "for the record, the value is "+v+" as stated.") {
				t.Errorf("pool %s: value %q cannot be credited by the grader — any case expecting it is unwinnable", attr, v)
			}
		}
	}
}

// TestAnswerableDistractorsNeverEmpty pins the anti-shotgun floor: every
// value-kind question with a pooled attribute carries at least two distractors
// (pool backfill), so dumping an attribute's whole pool always trips the scan.
func TestAnswerableDistractorsNeverEmpty(t *testing.T) {
	for seed := int64(1); seed <= 120; seed++ {
		p := BuildPlan(seed, fullOpts())
		for _, q := range DeriveQuestions(p) {
			if q.Abstain || q.Kind == protocol.AnswerNumber || q.Kind == protocol.AnswerDuration ||
				q.Kind == protocol.AnswerReversal || q.Kind == protocol.AnswerPersistence ||
				q.Kind == protocol.AnswerOrderedList || q.Type == QTCanary {
				continue
			}
			if len(q.Distractors) < 2 {
				t.Fatalf("seed %d %s: only %d distractors — a pool dump would go unpunished (%v)",
					seed, q.ID, len(q.Distractors), q.Distractors)
			}
		}
	}
}
