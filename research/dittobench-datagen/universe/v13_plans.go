package universe

import (
	"errors"
	"fmt"
	"hash/fnv"
	"math/rand"
	"sort"
	"strings"
)

// WorldPlanSelection is the Bench v13 world-question budget for one run. It
// replaces the v8 "all story plans, then fill" quota with three explicit slots
// so the published memory envelope (gen.V13Envelope) is a contract rather than
// a residual:
//
//   - StoryPerArc oracles are kept per story arc. When an arc offers more
//     oracles than the slot allows, the dropped oracle rotates through the
//     pure-money story oracles by arc index (storyDropRotation), so the money
//     share released by the rebalance comes out of the composed-ledger families
//     first and every story oracle still appears in every full run.
//   - Ordinary is the number of ordinary (person / project / trip) plans, taken
//     from the seed-shuffled candidate order subject to OrdinaryCaps, a per-oracle
//     ceiling keyed by the unprefixed oracle kind (for example
//     "project-outstanding": 4).
//   - Interim is the number of additional NON-MONETARY ordinary plans that stand
//     in for v13 slots whose dedicated generator has not landed yet
//     (gen.v13InterimSlots). It draws from the same shuffled order after the
//     ordinary slot and never takes a monetary oracle, so the interim fill cannot
//     widen the monetary exposure the v13 cap bounds.
//
// The selection is a pure function of (World, WorldPlanSelection): the candidate
// order comes from an independent per-version shuffle stream so changing the v13
// budget can never perturb the v8-v12 selection (QuestionPlans), which stays
// byte-identical.
type WorldPlanSelection struct {
	StoryPerArc  int
	Ordinary     int
	OrdinaryCaps map[string]int
	Interim      int
	// Salt names the shuffle stream. Distinct contracts pass distinct salts so a
	// later budget change never re-orders an earlier version's candidates.
	Salt string
}

// WorldPlanCounts reports how many plans each slot of a WorldPlanSelection
// actually received. Every count is exact: a shortfall is a generation error,
// never a silently smaller run.
type WorldPlanCounts struct {
	Story    int
	Ordinary int
	Interim  int
}

// Total is the number of selected plans.
func (c WorldPlanCounts) Total() int { return c.Story + c.Ordinary + c.Interim }

// storyDropRotation lists the story oracles eligible to be dropped when an arc
// offers more oracles than StoryPerArc keeps. They are the pure-money story
// oracles: the composed balance, the correction magnitude, and the mid-point
// balance. The list-valued net-change and outcome-summary oracles carry a money
// item at fractional weight and stay, as do the two non-monetary oracles.
var storyDropRotation = []string{oracleStoryBalanceCurrent, oracleStoryBudgetDelta, oracleStoryPostApproval}

// monetaryOracles are the ordinary oracle kinds whose graded answer is a
// currency amount. The interim fill never selects them.
var monetaryOracles = map[string]bool{oracleProjectOutstanding: true}

// SelectQuestionPlans executes the v13 world-question budget. Like
// QuestionPlans it validates every candidate with the counterfactual-removal
// oracle (validatePlan) and deterministically excludes accidental lexical
// shortcuts; every structural failure is a generation error. The returned plans
// are shuffled together so the wire order carries no slot boundary.
func (w World) SelectQuestionPlans(sel WorldPlanSelection) ([]QuestionPlan, WorldPlanCounts, error) {
	if sel.StoryPerArc < 0 || sel.Ordinary < 0 || sel.Interim < 0 {
		return nil, WorldPlanCounts{}, fmt.Errorf("negative world plan selection %+v", sel)
	}
	candidates := w.questionCandidates()
	r := rand.New(rand.NewSource(selectionSeed(w.Seed, sel.Salt)))
	r.Shuffle(len(candidates), func(i, j int) { candidates[i], candidates[j] = candidates[j], candidates[i] })

	storyByArc := make(map[int][]QuestionPlan, len(w.StoryArcs))
	ordinary := make([]QuestionPlan, 0, len(candidates))
	seenQuestions := make(map[string]bool, len(candidates))
	for i := range candidates {
		if err := w.validatePlan(candidates[i]); err != nil {
			if errors.Is(err, errLexicalShortcut) {
				continue
			}
			return nil, WorldPlanCounts{}, fmt.Errorf("candidate %s: %w", candidates[i].Case.QuestionType, err)
		}
		q := strings.ToLower(strings.TrimSpace(candidates[i].Case.Question))
		if seenQuestions[q] {
			return nil, WorldPlanCounts{}, fmt.Errorf("duplicate rendered question %q", candidates[i].Case.Question)
		}
		seenQuestions[q] = true
		if isStoryOracle(candidates[i].oracleKind) {
			storyByArc[candidates[i].oracleIndex] = append(storyByArc[candidates[i].oracleIndex], candidates[i])
		} else {
			ordinary = append(ordinary, candidates[i])
		}
	}

	selected := make([]QuestionPlan, 0, sel.StoryPerArc*len(w.StoryArcs)+sel.Ordinary+sel.Interim)
	var counts WorldPlanCounts
	arcs := make([]int, 0, len(storyByArc))
	for arc := range storyByArc {
		arcs = append(arcs, arc)
	}
	sort.Ints(arcs)
	for _, arc := range arcs {
		plans := storyByArc[arc]
		if sel.StoryPerArc > len(plans) {
			return nil, WorldPlanCounts{}, fmt.Errorf("story arc %d offers %d validated oracles, need %d", arc, len(plans), sel.StoryPerArc)
		}
		// Drop pure-money oracles in arc rotation until the arc fits its slot.
		// The rotation starts at the arc index so the dropped oracle differs
		// between neighbouring arcs and every oracle survives somewhere in the
		// run.
		for drop := 0; len(plans) > sel.StoryPerArc; drop++ {
			if drop >= len(storyDropRotation) {
				return nil, WorldPlanCounts{}, fmt.Errorf("story arc %d keeps %d oracles after dropping every monetary oracle, want %d", arc, len(plans), sel.StoryPerArc)
			}
			kind := storyDropRotation[(arc+drop)%len(storyDropRotation)]
			plans = withoutOracle(plans, kind)
		}
		selected = append(selected, plans...)
		counts.Story += len(plans)
	}

	taken := make([]bool, len(ordinary))
	perKind := map[string]int{}
	for i := range ordinary {
		if counts.Ordinary == sel.Ordinary {
			break
		}
		kind := ordinary[i].oracleKind
		if cap, ok := sel.OrdinaryCaps[kind]; ok && perKind[kind] >= cap {
			continue
		}
		perKind[kind]++
		taken[i] = true
		selected = append(selected, ordinary[i])
		counts.Ordinary++
	}
	if counts.Ordinary != sel.Ordinary {
		return nil, WorldPlanCounts{}, fmt.Errorf("world has %d shortcut-free ordinary candidates under caps %v, need %d", counts.Ordinary, sel.OrdinaryCaps, sel.Ordinary)
	}
	for i := range ordinary {
		if counts.Interim == sel.Interim {
			break
		}
		if taken[i] || monetaryOracles[ordinary[i].oracleKind] {
			continue
		}
		taken[i] = true
		selected = append(selected, ordinary[i])
		counts.Interim++
	}
	if counts.Interim != sel.Interim {
		return nil, WorldPlanCounts{}, fmt.Errorf("world has %d spare non-monetary ordinary candidates, need %d for the interim fill", counts.Interim, sel.Interim)
	}
	r.Shuffle(len(selected), func(i, j int) { selected[i], selected[j] = selected[j], selected[i] })
	return selected, counts, nil
}

func withoutOracle(plans []QuestionPlan, kind string) []QuestionPlan {
	out := make([]QuestionPlan, 0, len(plans))
	for _, plan := range plans {
		if plan.oracleKind != kind {
			out = append(out, plan)
		}
	}
	return out
}

func selectionSeed(seed int64, salt string) int64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-world-question-selection:%s:%d", salt, seed)
	return int64(h.Sum64() & ((1 << 63) - 1))
}

// OracleKind exposes the generator-internal oracle kind ("story-lesson",
// "project-outstanding") for audits and tests. It is the QuestionType without
// the "world-" prefix and never crosses the harness wire.
func (p QuestionPlan) OracleKind() string { return p.oracleKind }

// StoryArcIndex exposes which story arc a story plan joins, or -1 for an
// ordinary plan.
func (p QuestionPlan) StoryArcIndex() int {
	if !isStoryOracle(p.oracleKind) {
		return -1
	}
	return p.oracleIndex
}
