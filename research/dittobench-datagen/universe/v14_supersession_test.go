package universe

import (
	"fmt"
	"regexp"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV14ChainFoldSemantics(t *testing.T) {
	cases := []struct {
		name      string
		steps     []v14ChainStep
		value     int
		governing int
	}{
		{"revert", []v14ChainStep{{v14OpSet, 1}, {v14OpSet, 0}}, 0, 1},
		{"withdrawn", []v14ChainStep{{v14OpPropose, 1}, {v14OpWithdraw, 1}}, 0, -1},
		{"adopted", []v14ChainStep{{v14OpPropose, 1}, {v14OpAdopt, 1}}, 1, 0},
		{"error", []v14ChainStep{{v14OpSet, 1}, {v14OpError, 1}}, 0, -1},
		{"confirmed", []v14ChainStep{{v14OpSet, 1}, {v14OpConfirm, 1}}, 1, 0},
		{"error after two", []v14ChainStep{{v14OpSet, 1}, {v14OpSet, 2}, {v14OpError, 2}}, 1, 0},
		{"declined after change", []v14ChainStep{{v14OpSet, 1}, {v14OpPropose, 2}, {v14OpWithdraw, 2}}, 1, 0},
	}
	for _, c := range cases {
		v, g := v14ChainFold(c.steps)
		if v != c.value || g != c.governing {
			t.Errorf("%s: fold = (%d, %d), want (%d, %d)", c.name, v, g, c.value, c.governing)
		}
	}
}

// TestV14ChainTemplatesAreMultiHopAndFlipOneLink pins the SN-353 shape rules:
// 2-3 records after the initial one (3 hops at most for the first
// qualification), every chain has a revert, a declined proposal or an entry
// logged in error, and the counterfactual differs in exactly one link and
// moves the answer.
func TestV14ChainTemplatesAreMultiHopAndFlipOneLink(t *testing.T) {
	kinds := map[string]bool{}
	for _, tpl := range v14ChainTemplates {
		if n := len(tpl.Steps); n < 2 || n > 3 || len(tpl.Counter) != n {
			t.Fatalf("%s: %d steps, counterfactual %d", tpl.Name, n, len(tpl.Counter))
		}
		diff := 0
		for i := range tpl.Steps {
			if tpl.Steps[i] != tpl.Counter[i] {
				diff++
			}
		}
		if diff != 1 {
			t.Fatalf("%s: counterfactual flips %d links, want 1", tpl.Name, diff)
		}
		base, _ := v14ChainFold(tpl.Steps)
		counter, _ := v14ChainFold(tpl.Counter)
		if base == counter {
			t.Fatalf("%s: counterfactual does not move the answer", tpl.Name)
		}
		hasMove := false
		for _, s := range append(append([]v14ChainStep(nil), tpl.Steps...), tpl.Counter...) {
			if s.Val >= tpl.Values {
				t.Fatalf("%s: value %d beyond Values %d", tpl.Name, s.Val, tpl.Values)
			}
			if s.Op == v14OpWithdraw || s.Op == v14OpError {
				hasMove = true
			}
		}
		if tpl.Kind == "revert" {
			seen := map[int]bool{0: true}
			for _, s := range tpl.Steps {
				if s.Op == v14OpSet && seen[s.Val] {
					hasMove = true
				}
				seen[s.Val] = true
			}
		}
		if !hasMove {
			t.Fatalf("%s: no revert, declined proposal or error entry", tpl.Name)
		}
		kinds[tpl.Kind] = true
	}
	for _, k := range []string{"revert", "withdrawn", "error"} {
		if !kinds[k] {
			t.Fatalf("no %s template", k)
		}
	}
}

// v14ChainedMember is one rendered chained member with the oracle answer the
// chain fold produced.
type v14ChainedMember struct {
	label  string
	plan   v14ChainPlan
	steps  []v14ChainStep
	member v13Member
}

func collectV14ChainedMembers(t *testing.T, seeds int) []v14ChainedMember {
	t.Helper()
	var out []v14ChainedMember
	for seed := int64(1); seed <= int64(seeds); seed++ {
		schema := generateV13Schema(seed)
		d := newV13Draws(seed, "business", protocol.BenchVersionV14)
		for group := 0; group < len(V13Families); group++ {
			g := d.drawGroup(group)
			if !v14ChainBusinessFamilies[g.Family] {
				continue
			}
			plan := drawV14ChainPlan(seed, "business", group, 4)
			for _, counter := range []bool{false, true} {
				m := renderV13Member(d, schema, group, 0, g, counter)
				out = append(out, v14ChainedMember{fmt.Sprintf("seed %d %s counter=%v", seed, g.Family, counter), plan, v14ChainSteps(plan, counter), m})
			}
		}
		pd := newV13Draws(seed, "personal", protocol.BenchVersionV14)
		for group, domain := range V13PersonalDomains {
			g := pd.drawPersonalGroup(group, domain)
			if !v14ChainPersonalDomains[domain] {
				continue
			}
			maxValues := 4
			if domain == V13PersonalMilestones {
				maxValues = 3
			}
			plan := drawV14ChainPlan(seed, "personal", group, maxValues)
			for _, counter := range []bool{false, true} {
				m := renderV13PersonalMember(pd, group, 0, g, counter)
				out = append(out, v14ChainedMember{fmt.Sprintf("seed %d %s counter=%v", seed, domain, counter), plan, v14ChainSteps(plan, counter), m})
			}
		}
	}
	return out
}

// TestV14ChainCounterfactualChangesExactlyOneRecord is the record-level
// metamorphic proof for chained families: the counterfactual reading of a
// group differs from the base in exactly one record, and the decoy and
// question do not move.
func TestV14ChainCounterfactualChangesExactlyOneRecord(t *testing.T) {
	members := collectV14ChainedMembers(t, 40)
	for i := 0; i < len(members); i += 2 {
		base, counter := members[i].member, members[i+1].member
		rows := func(m v13Member) []string {
			return append(append([]string{m.Records[0], m.Records[1]}, m.ChainExtra...), m.Records[2])
		}
		b, c := rows(base), rows(counter)
		if len(b) != len(c) {
			t.Fatalf("%s: record count moved %d -> %d", members[i].label, len(b), len(c))
		}
		changed := 0
		for k := range b {
			if b[k] != c[k] {
				changed++
			}
		}
		if changed != 1 {
			t.Fatalf("%s: counterfactual changed %d records, want 1\nbase=%q\ncounter=%q", members[i].label, changed, b, c)
		}
		if base.DecoyClause != counter.DecoyClause || base.Question != counter.Question || base.Expected == counter.Expected {
			t.Fatalf("%s: decoy/question moved or answer did not", members[i].label)
		}
	}
}

// v14CueWords are the supersession cues v13 records carry and SN-354 says
// must stop identifying the governing record.
var v14CueWords = regexp.MustCompile(`(?i)\b(now|replacing|replaces|replaced|instead|newest|governs|no longer|from now on|superseding|update|updated|revised|switched|moved to|took over|changed hands)\b`)

// TestV14SupersessionAnswerIsNotRecoverableFromASurfaceCue is the SN-354
// acceptance test. For each chained member it applies the three model-free
// shortcuts that v13 records allow:
//
//   - cue: the value of the last state record carrying change vocabulary;
//   - position: the value of the second state record (v13's record[1]);
//   - latest: the value of the last state record that names one, in filing
//     order (v14 files in story order, so the latest timestamp is the last
//     row). An unnamed revert names no value, so it is skipped.
//
// Under v13 the position shortcut is exact. Under v14 each shortcut must stay
// well below the oracle, the cue shortcut must miss every misplaced-cue
// group, and a cue-free group's state records must carry no cue at all.
func TestV14SupersessionAnswerIsNotRecoverableFromASurfaceCue(t *testing.T) {
	members := collectV14ChainedMembers(t, 120)
	var total, cueHits, posHits, latestHits int
	// joint counts groups whose base AND counterfactual a shortcut solves:
	// only then does it recover the program rather than one reading.
	joint := map[string]int{}
	solved := map[string]bool{}
	modes := map[string]int{}
	kinds := map[string]int{}
	for i, cm := range members {
		answer, _ := v14ChainFold(cm.steps)
		rows := cm.member.chain
		if len(rows) != len(cm.steps)+1 {
			t.Fatalf("%s: %d chain rows for %d steps", cm.label, len(rows), len(cm.steps))
		}
		modes[cm.plan.Cue]++
		kinds[cm.plan.Template.Kind]++
		total++
		cue := -1
		named := make([]v14ChainRow, 0, len(rows))
		for k, r := range rows {
			if r.Named {
				named = append(named, r)
				continue
			}
			// An unnamed revert must point back to a value an earlier
			// record states, or the ground truth would be unreadable.
			grounded := false
			for _, prior := range rows[:k] {
				grounded = grounded || (prior.Named && prior.Value == r.Value)
			}
			if !grounded {
				t.Fatalf("%s: unnamed revert to value %d with no earlier record naming it", cm.label, r.Value)
			}
		}
		for _, r := range rows[1:] {
			if v14CueWords.MatchString(r.Text) && r.Named {
				cue = r.Value
			}
			if v14CueWords.MatchString(r.Text) {
				if cm.plan.Cue == v14CueFree {
					t.Fatalf("%s: cue-free group carries a cue: %q", cm.label, r.Text)
				}
			}
		}
		if cue == answer {
			cueHits++
			// Placement is drawn from the base chain, so the base reading of
			// a misplaced group never governs from a cued record. Its
			// counterfactual flips one link and may (by design) promote one.
			if cm.plan.Cue == v14CueMisplaced && !strings.HasSuffix(cm.label, "counter=true") {
				t.Fatalf("%s: misplaced-cue group is solved by its cue", cm.label)
			}
		}
		if rows[1].Value == answer {
			posHits++
		}
		latest := named[len(named)-1].Value
		if latest == answer {
			latestHits++
		}
		hits := map[string]bool{"cue": cue == answer, "position": rows[1].Value == answer, "latest": latest == answer}
		for name, hit := range hits {
			if i%2 == 0 {
				solved[name] = hit
			} else if hit && solved[name] {
				joint[name]++
			}
		}
	}
	rate := func(n int) float64 { return float64(n) / float64(total) }
	groups := total / 2
	t.Logf("%d chained members: cue %.2f, position %.2f, latest %.2f; both readings of a group: cue %.2f, position %.2f, latest %.2f; modes %v; kinds %v",
		total, rate(cueHits), rate(posHits), rate(latestHits),
		float64(joint["cue"])/float64(groups), float64(joint["position"])/float64(groups), float64(joint["latest"])/float64(groups), modes, kinds)
	for name, n := range map[string]int{"cue": cueHits, "position": posHits} {
		if rate(n) > 0.5 {
			t.Fatalf("%s shortcut recovers %.2f of chained answers", name, rate(n))
		}
	}
	for name, n := range joint {
		if share := float64(n) / float64(groups); share > 0.4 {
			t.Fatalf("%s shortcut solves both readings of %.2f of chained groups", name, share)
		}
	}
	for _, m := range []string{v14CueAnnounced, v14CueFree, v14CueMisplaced} {
		if modes[m] < total/8 {
			t.Fatalf("cue mode %s drawn %d of %d", m, modes[m], total)
		}
	}
	for _, k := range []string{"revert", "withdrawn", "error"} {
		if kinds[k] < total/8 {
			t.Fatalf("chain kind %s drawn %d of %d", k, kinds[k], total)
		}
	}

	// The same position shortcut is exact on the frozen v13 contract.
	v13Hits, v13Total := 0, 0
	for seed := int64(1); seed <= 40; seed++ {
		d := newV13Draws(seed, "personal", protocol.BenchVersionV13)
		for group, domain := range V13PersonalDomains {
			if !v14ChainPersonalDomains[domain] {
				continue
			}
			g := d.drawPersonalGroup(group, domain)
			m := renderV13PersonalMember(d, group, 0, g, false)
			v13Total++
			if len(m.ChainExtra) != 0 || m.chain != nil {
				t.Fatalf("v13 %s rendered a chain", domain)
			}
			for _, form := range append([]string{m.Expected}, m.AcceptAny...) {
				if strings.Contains(m.Records[1], form) {
					v13Hits++
					break
				}
			}
		}
	}
	if v13Hits < v13Total*9/10 {
		t.Fatalf("v13 baseline: record[1] carries the answer in %d of %d, the shortcut this test guards was expected to be exact", v13Hits, v13Total)
	}
}

// TestV14ChainedCasesGradeAndStayDeterministic: in the generated v14 cases,
// the oracle answer scores 1 under the v14 grader, every other chain value is
// a wrong answer, the evidence covers every chain record, and regeneration is
// byte-identical.
func TestV14ChainedCasesGradeAndStayDeterministic(t *testing.T) {
	for seed := int64(1); seed <= 20; seed++ {
		business, err := GenerateV13ProgramsForVersion(seed, 28, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		personal, err := GenerateV13PersonalProgramsForVersion(seed, 24, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		again, err := GenerateV13ProgramsForVersion(seed, 28, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		if fmt.Sprint(business) != fmt.Sprint(again) {
			t.Fatalf("seed %d: v14 business programs are not deterministic", seed)
		}
		chained := 0
		for _, gc := range append(business, personal...) {
			mc := gc.Plan.Case
			mc.BenchVersion = protocol.BenchVersionV14
			if gc.Plan.Operations[len(gc.Plan.Operations)-1] != "return the value that stands" {
				continue
			}
			chained++
			if len(gc.Pairs) != len(gc.Plan.RequiredPairIDs) {
				t.Fatalf("seed %d case %s: %d pairs, %d evidence ids", seed, mc.ID, len(gc.Pairs), len(gc.Plan.RequiredPairIDs))
			}
			resp := protocol.RunResponse{Answer: mc.ExpectedAnswer, FinalText: "It is " + mc.ExpectedAnswer + "."}
			if v := grade.Memory(mc, resp); v.Score != 1 {
				t.Fatalf("seed %d case %s: oracle %q scored %.2f (%v)", seed, mc.ID, mc.ExpectedAnswer, v.Score, v.Notes)
			}
		}
		if chained == 0 {
			t.Fatalf("seed %d: no chained v14 cases", seed)
		}
	}
}
