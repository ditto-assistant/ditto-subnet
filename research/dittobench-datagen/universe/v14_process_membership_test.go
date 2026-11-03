package universe

import (
	"fmt"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// ── SN-356: multi-step process ──────────────────────────────────────────────

var v14PlanStep = regexp.MustCompile(`\((\d)\) (.+?), \S+ ([A-Z][^;.]+)`)

// v14ProcessOracle folds the rendered records of a process member: it reads
// the ordered plan, applies every closure, early finish, reopen and
// reassignment in story order, and returns the first open step and its owner.
func v14ProcessOracle(t *testing.T, m v13Member, p *v14Process) (string, string) {
	t.Helper()
	records := v14AllRecords(m)
	type step struct{ name, owner, done string }
	var steps []step
	for _, mt := range v14PlanStep.FindAllStringSubmatch(records[0], -1) {
		steps = append(steps, step{name: mt[2], owner: strings.TrimSpace(mt[3])})
	}
	if len(steps) != len(p.Steps) {
		t.Fatalf("plan record lists %d steps, want %d: %q", len(steps), len(p.Steps), records[0])
	}
	for i := range steps {
		for _, c := range V14ProcessStepClasses {
			if c.Canonical == steps[i].name {
				steps[i].done = c.Done
			}
		}
	}
	closed := make([]bool, len(steps))
	for _, rec := range records[1:] {
		for i, s := range steps {
			switch {
			case strings.Contains(rec, "is under way") || strings.Contains(rec, "has started to"):
				// Started but still open: no state change.
			case strings.Contains(rec, s.done):
				closed[i] = true
			case strings.Contains(rec, "step to "+s.name) && (strings.Contains(rec, "reopened") || strings.Contains(rec, "open again")):
				closed[i] = false
			case strings.Contains(rec, "step to "+s.name) && (strings.Contains(rec, "dropped") || strings.Contains(rec, "skipping")):
				closed[i] = true
			case strings.Contains(rec, "step to "+s.name) && (strings.Contains(rec, "takes over") || strings.Contains(rec, "is now")):
				for _, name := range []string{p.ReassignTo} {
					if strings.Contains(rec, name) {
						steps[i].owner = name
					}
				}
			}
		}
	}
	for i, s := range steps {
		if !closed[i] {
			return s.name, s.owner
		}
	}
	t.Fatalf("no open step: %q", records)
	return "", ""
}

func v14ProcessGroups(t *testing.T, seeds int) []v14RenderedGroup {
	t.Helper()
	var out []v14RenderedGroup
	for seed := int64(1); seed <= int64(seeds); seed++ {
		for _, rg := range v14TemporalGroups(t, seed, len(V13Families)) {
			if rg.g.Family == V13FamilyNextActionResponsible {
				out = append(out, rg)
			}
		}
	}
	return out
}

// TestV14ProcessOracle: an independent fold of the rendered records gives the
// generator's step and owner in the base and the counterfactual; the answer
// step is never the plan's first step; the counterfactual changes exactly one
// record and moves the answer to another step. Both modes and process lengths
// three to five occur.
func TestV14ProcessOracle(t *testing.T) {
	modes, lengths := map[string]bool{}, map[int]bool{}
	groups := v14ProcessGroups(t, 120)
	if len(groups) < 100 {
		t.Fatalf("only %d process groups", len(groups))
	}
	for _, rg := range groups {
		p := rg.g.T14.Proc
		modes[p.Mode] = true
		lengths[len(p.Steps)] = true
		for _, member := range []struct {
			m       v13Member
			counter bool
		}{{rg.base, false}, {rg.counter, true}} {
			name, owner := v14ProcessOracle(t, member.m, p)
			if want := name + "; " + owner; want != member.m.Expected {
				t.Fatalf("group %d counter=%v %s: oracle %q, generator %q\nrecords=%q", rg.group, member.counter, p.Mode, want, member.m.Expected, v14AllRecords(member.m))
			}
			if name == V14ProcessStepClasses[p.Steps[0]].Canonical {
				t.Fatalf("counter=%v: the plan's first step is the answer", member.counter)
			}
		}
		if rg.base.Items[0] == rg.counter.Items[0] {
			t.Fatalf("%s: counterfactual left the step at %q", p.Mode, rg.base.Items[0])
		}
		changed := 0
		for i, rec := range v14AllRecords(rg.base) {
			if rec != v14AllRecords(rg.counter)[i] {
				changed++
			}
		}
		if changed != 1 || rg.base.Question != rg.counter.Question {
			t.Fatalf("%s: counterfactual changed %d records or the question", p.Mode, changed)
		}
	}
	if !modes[v14ProcessUntouched] || !modes[v14ProcessReopened] {
		t.Fatalf("modes %v", modes)
	}
	for n := 3; n <= 5; n++ {
		if !lengths[n] {
			t.Fatalf("no %d-step process: %v", n, lengths)
		}
	}
}

// TestV14ProcessRecordZeroNoLongerHalfCredit is the SN-356 negative test. At
// v13 copying the action out of record 0 earned the action claim's half
// credit on every case. At v14 record 0 is the whole plan: its first step, or
// its first step with its planned owner, scores 0. Another step from the plan
// with the right owner earns only the owner's half; the honest reply scores 1.
func TestV14ProcessRecordZeroNoLongerHalfCredit(t *testing.T) {
	for _, rg := range v14ProcessGroups(t, 60) {
		p := rg.g.T14.Proc
		for _, m := range []v13Member{rg.base, rg.counter} {
			action, owner := m.Items[0], m.Items[1]
			first := V14ProcessStepClasses[p.Steps[0]].Canonical
			if got := v14GradeMember(m, fmt.Sprintf("The first open step is to %s, and %s owns it.", action, owner)); got != 1 {
				t.Fatalf("%s: honest reply scored %.2f", p.Mode, got)
			}
			if got := v14GradeMember(m, fmt.Sprintf("The next step is to %s.", first)); got != 0 {
				t.Fatalf("%s: record-0 action %q scored %.2f", p.Mode, first, got)
			}
			if got := v14GradeMember(m, fmt.Sprintf("The next step is to %s, owned by %s.", first, p.Owners[0])); got != 0 {
				t.Fatalf("%s: record-0 action and owner scored %.2f", p.Mode, got)
			}
		}
	}
}

// TestV13NextActionStillReadableFromRecordZero pins the v13 behaviour the
// issue describes, so the v14 change is visibly scoped: the v13 action is the
// one record 0 states, and naming it alone still earns half credit there.
func TestV13NextActionStillReadableFromRecordZero(t *testing.T) {
	for seed := int64(1); seed <= 10; seed++ {
		d := newV13Draws(seed, "business", protocol.BenchVersionV13)
		schema := generateV13Schema(seed)
		for group := 0; group < len(V13Families); group++ {
			g := d.drawGroup(group)
			if g.Family != V13FamilyNextActionResponsible {
				continue
			}
			m := renderV13Member(d, schema, group, 0, g, false)
			if !strings.Contains(m.Records[0], m.Items[0]) {
				t.Fatalf("seed %d: v13 record 0 does not state the action", seed)
			}
			mc := protocol.MemoryCase{BenchVersion: protocol.BenchVersionV13, Question: m.Question, ExpectedAnswer: m.Expected,
				AnswerKind: m.Kind, AnswerItems: m.Items, AnswerItemKinds: m.ItemKinds, AnswerItemAcceptAny: m.ItemAccept,
				DistractorAnswers: m.Distractors, Claims: m.Claims, QuestionType: V13ProgramQuestionType}
			if got := v14GradeMemberCase(mc, "The next step is to "+m.Items[0]+"."); got != 0.5 {
				t.Fatalf("seed %d: v13 record-0 action scored %.2f, want 0.5", seed, got)
			}
		}
	}
}

// ── SN-357: membership fold ─────────────────────────────────────────────────

func v14SchoolGroups(t *testing.T, seeds int) []struct {
	g             v13PersonalGroup
	base, counter v13Member
} {
	t.Helper()
	var out []struct {
		g             v13PersonalGroup
		base, counter v13Member
	}
	for seed := int64(1); seed <= int64(seeds); seed++ {
		d := newV13Draws(seed, "personal", protocol.BenchVersionV14)
		for group, domain := range V13PersonalDomains {
			g := d.drawPersonalGroup(group, domain)
			if domain != V13PersonalSchool {
				continue
			}
			out = append(out, struct {
				g             v13PersonalGroup
				base, counter v13Member
			}{g, renderV13PersonalMember(d, group, 0, g, false), renderV13PersonalMember(d, group, 0, g, true)})
		}
	}
	return out
}

// v14MembershipOracle folds the rendered records: the first list, then every
// removal, re-add, replacement and addition in story order. A no-op removal of
// an item never on the list changes nothing.
func v14MembershipOracle(t *testing.T, m v13Member, g v13PersonalGroup) []string {
	t.Helper()
	set := map[string]bool{}
	var order []string
	add := func(item string) {
		if _, seen := set[item]; !seen {
			order = append(order, item)
		}
		set[item] = true
	}
	records := v14AllRecords(m)
	for _, item := range g.Items[:4] {
		if !strings.Contains(records[0], item) {
			t.Fatalf("first list misses %q: %q", item, records[0])
		}
		add(item)
	}
	s := g.T14Set
	for _, rec := range records[1:] {
		switch {
		case strings.Contains(rec, "back on the list") || strings.Contains(rec, "after all"):
			add(g.Items[s.Removed])
		case strings.Contains(rec, "swap the") || strings.Contains(rec, "instead of"):
			for _, item := range g.Items[:4] {
				if strings.Contains(rec, "the "+item) {
					set[item] = false
				}
			}
			add(s.Swap)
		case strings.Contains(rec, "never"):
			// no-op
		case strings.Contains(rec, "took the") || strings.Contains(rec, "needed any more"):
			set[g.Items[s.Removed]] = false
		case strings.Contains(rec, "too") || strings.Contains(rec, "added a"):
			add(s.Added)
		}
	}
	var out []string
	for _, item := range order {
		if set[item] {
			out = append(out, item)
		}
	}
	return out
}

// TestV14MembershipFoldOracle: three to five membership events (removal and
// re-add, replacement, optional addition, optional no-op), an independent
// fold of the rendered records gives the generator's set, and the
// counterfactual changes exactly one record and two members.
func TestV14MembershipFoldOracle(t *testing.T) {
	events := map[int]bool{}
	groups := v14SchoolGroups(t, 120)
	if len(groups) < 60 {
		t.Fatalf("only %d school groups", len(groups))
	}
	for _, sg := range groups {
		s := sg.g.T14Set
		if s == nil {
			t.Fatal("v14 school group without a membership draw")
		}
		n := 0
		for _, ev := range s.Log {
			if ev != v14SetNeutral {
				n++
			}
		}
		if n < 3 || n > 5 {
			t.Fatalf("%d membership events, want 3-5", n)
		}
		events[n] = true
		for _, m := range []v13Member{sg.base, sg.counter} {
			got := v14MembershipOracle(t, m, sg.g)
			want := append([]string(nil), m.Items...)
			sort.Strings(got)
			sort.Strings(want)
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("oracle %q, generator %q\nrecords=%q", got, m.Expected, v14AllRecords(m))
			}
			if len(m.Claims) != len(m.Items) {
				t.Fatalf("%d claims for %d members", len(m.Claims), len(m.Items))
			}
		}
		changed := 0
		for i, rec := range v14AllRecords(sg.base) {
			if rec != v14AllRecords(sg.counter)[i] {
				changed++
			}
		}
		if changed != 1 {
			t.Fatalf("counterfactual changed %d records, want 1", changed)
		}
	}
	for n := 3; n <= 5; n++ {
		if !events[n] {
			t.Fatalf("no group with %d events: %v", n, events)
		}
	}
}

// TestV13SchoolListKeepsOneDropAndOneAdd: v13 groups carry no fold.
func TestV13SchoolListKeepsOneDropAndOneAdd(t *testing.T) {
	for seed := int64(1); seed <= 20; seed++ {
		d := newV13Draws(seed, "personal", protocol.BenchVersionV13)
		for group, domain := range V13PersonalDomains {
			g := d.drawPersonalGroup(group, domain)
			if g.T14Set != nil {
				t.Fatalf("seed %d: v13 group carries a v14 membership draw", seed)
			}
			if m := renderV13PersonalMember(d, group, 0, g, false); len(m.Extra) != 0 {
				t.Fatalf("seed %d: v13 personal member has extra records", seed)
			}
		}
	}
}

func v14PersonalCase(m v13Member) protocol.MemoryCase {
	return protocol.MemoryCase{BenchVersion: protocol.BenchVersionV14, QuestionType: V13PersonalQuestionType, Question: m.Question,
		ExpectedAnswer: m.Expected, AnswerKind: m.Kind, AnswerItems: m.Items, AnswerItemKinds: m.ItemKinds,
		DistractorAnswers: m.Distractors, Claims: m.Claims}
}

// TestV14MembershipPerMemberPartialCredit is the SN-357 grader contract:
// the full set scores 1; each missing member costs exactly its 1/n share; a
// stale extra member (the replaced item), the no-op item, or the decoy list's
// item zeroes the list; an unrelated extra word does not. The first list
// (record 0) and the v13 one-swap reading both score 0.
func TestV14MembershipPerMemberPartialCredit(t *testing.T) {
	near := func(a, b float64) bool { return a-b < 1e-9 && b-a < 1e-9 }
	sawNoOp := false
	for _, sg := range v14SchoolGroups(t, 60) {
		s := sg.g.T14Set
		for _, m := range []v13Member{sg.base, sg.counter} {
			mc := v14PersonalCase(m)
			n := float64(len(m.Items))
			grade := func(items []string) float64 {
				return v14GradeMemberCase(mc, "Currently on the list: "+strings.Join(items, ", ")+".")
			}
			if got := grade(m.Items); got != 1 {
				t.Fatalf("full set scored %.3f", got)
			}
			for missing := 1; missing < len(m.Items); missing++ {
				if got, want := grade(m.Items[missing:]), (n-float64(missing))/n; !near(got, want) {
					t.Fatalf("missing %d of %d members scored %.3f, want %.3f", missing, len(m.Items), got, want)
				}
			}
			replaced := m.Distractors[0]
			if got := grade(append(append([]string(nil), m.Items...), replaced)); got != 0 {
				t.Fatalf("extra replaced member %q scored %.3f", replaced, got)
			}
			if got := grade(append(append([]string(nil), m.Items...), sg.g.DecoyItm)); got != 0 {
				t.Fatalf("decoy member %q scored %.3f", sg.g.DecoyItm, got)
			}
			if s.NoOp != "" {
				sawNoOp = true
				if got := grade(append(append([]string(nil), m.Items...), s.NoOp)); got != 0 {
					t.Fatalf("no-op member %q scored %.3f", s.NoOp, got)
				}
			}
			if got := grade(sg.g.Items[:4]); got != 0 {
				t.Fatalf("record-0 list scored %.3f", got)
			}
			if got := v14GradeMemberCase(mc, "Currently on the list: "+strings.Join(m.Items, ", ")+", and some snacks."); got != 1 {
				t.Fatalf("unrelated extra item scored %.3f", got)
			}
		}
	}
	if !sawNoOp {
		t.Fatal("no group with a no-op removal")
	}
}
