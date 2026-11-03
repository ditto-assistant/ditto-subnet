package universe

import (
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// v14RoleMember is one rendered role-family member with its group draw.
type v14RoleMember struct {
	seed    int64
	group   int
	g       v13Group
	base    v13Member
	counter v13Member
}

// v14RoleMembers draws every group of a 36-case v14 request (the full cycle)
// and returns the members of the two v14 role families.
func v14RoleMembers(seed int64) []v14RoleMember {
	schema := generateV13Schema(seed)
	d := newV13Draws(seed, "business", protocol.BenchVersionV14)
	var out []v14RoleMember
	for group := 0; group < len(V14Families); group++ {
		g := d.drawGroup(group)
		if g.Family != V14FamilyHolderVsActor && g.Family != V14FamilyIdentityJoin {
			continue
		}
		out = append(out, v14RoleMember{
			seed: seed, group: group, g: g,
			base:    renderV13Member(d, schema, group, 0, g, false),
			counter: renderV13Member(d, schema, group, 0, g, true),
		})
	}
	return out
}

// TestV14RoleSlotKeepsV13Cycle: v13 keeps its seven-family cycle at every
// group, and v14 changes only the role-slot positions.
func TestV14RoleSlotKeepsV13Cycle(t *testing.T) {
	roles := map[int]bool{}
	for _, slot := range v14RoleSlots {
		roles[slot] = true
	}
	counts := map[V13Family]int{}
	for seed := int64(1); seed <= 60; seed++ {
		v13 := newV13Draws(seed, "business", protocol.BenchVersionV13)
		v14 := newV13Draws(seed, "business", protocol.BenchVersionV14)
		seen := map[V13Family]bool{}
		for group := 0; group < 2*len(V14Families); group++ {
			if got := v13.family(group); got != V13Families[group%len(V13Families)] {
				t.Fatalf("seed %d group %d: v13 family moved to %s", seed, group, got)
			}
			f := v14.family(group)
			if got := V13FamilyOfVersion(seed, 4*group, protocol.BenchVersionV14); got != f {
				t.Fatalf("seed %d group %d: provenance family %s, draw family %s", seed, group, got, f)
			}
			slot := group % len(V14Families)
			// Non-role slots keep their family up to the temporal-slot
			// permutation (V14TemporalFamily, SN-355/SN-358).
			if !roles[slot] && f != V14TemporalFamily(seed, V14Families[slot]) {
				t.Fatalf("seed %d group %d: non-role slot moved to %s", seed, group, f)
			}
			if group < len(V14Families) {
				seen[f] = true
			}
		}
		if len(seen) != len(V14Families) {
			t.Fatalf("seed %d: a 36-case v14 request covers %d families, want %d", seed, len(seen), len(V14Families))
		}
		// The full profile spends four business groups: the fourth is the
		// role slot.
		counts[v14.family(3)]++
	}
	for _, f := range v14RoleFamilies {
		if counts[f] < 10 {
			t.Fatalf("role family %s fills the full-profile role slot on only %d/60 seeds", f, counts[f])
		}
	}
}

func TestV14RoleFamiliesCounterfactualChangesExactlyOneRecord(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		for _, rm := range v14RoleMembers(seed) {
			changed := 0
			for i := range rm.base.Records {
				if rm.base.Records[i] != rm.counter.Records[i] {
					changed++
				}
			}
			if changed != 1 {
				t.Fatalf("seed %d %s: counterfactual changed %d records\nbase=%q\ncounter=%q", seed, rm.g.Family, changed, rm.base.Records, rm.counter.Records)
			}
			if rm.base.DecoyClause != rm.counter.DecoyClause || rm.base.Question != rm.counter.Question {
				t.Fatalf("seed %d %s: counterfactual moved the decoy clause or the question", seed, rm.g.Family)
			}
			if rm.base.Expected == rm.counter.Expected {
				t.Fatalf("seed %d %s: counterfactual kept the answer %q", seed, rm.g.Family, rm.base.Expected)
			}
		}
	}
}

// foldV14Holder is the independent holder oracle: link handovers giver to
// recipient and return the one person who receives the item and never gives
// it on.
func foldV14Holder(t *testing.T, events []v14Handover) string {
	t.Helper()
	gives, receives := map[string]int{}, map[string]int{}
	for _, ev := range events {
		if ev.Giver == "" {
			continue
		}
		gives[ev.Giver]++
		receives[ev.Recipient]++
	}
	var ends []string
	for p := range receives {
		if gives[p] == 0 {
			ends = append(ends, p)
		}
	}
	if len(ends) != 1 {
		t.Fatalf("custody chain has %d holders %v, want exactly one", len(ends), ends)
	}
	return ends[0]
}

func TestV14HolderVsActorOracle(t *testing.T) {
	hops, actor, holder := map[int]int{}, 0, 0
	for seed := int64(1); seed <= 80; seed++ {
		for _, rm := range v14RoleMembers(seed) {
			if rm.g.Family != V14FamilyHolderVsActor {
				continue
			}
			x := rm.g.V14
			hops[x.Hops]++
			for _, m := range []v13Member{rm.base, rm.counter} {
				people := map[string]bool{}
				for _, ev := range m.custody {
					rec := m.Records[ev.Record]
					for _, name := range []string{ev.Actor, ev.Giver, ev.Recipient} {
						if name == "" {
							continue
						}
						people[name] = true
						if !strings.Contains(rec, name) {
							t.Fatalf("seed %d: record %d %q does not name %s", seed, ev.Record, rec, name)
						}
					}
					item := x.Item[0]
					if ev.Record > 0 {
						item = x.Item[1]
					}
					if !strings.Contains(rec, ev.Step) || !strings.Contains(rec, item) {
						t.Fatalf("seed %d: record %q lacks its step or item", seed, rec)
					}
					// No pronoun stands in for a person across a handoff.
					for _, pronoun := range []string{" he ", " she ", " him ", " her "} {
						if strings.Contains(" "+strings.ToLower(rec)+" ", pronoun) {
							t.Fatalf("seed %d: record %q uses a pronoun", seed, rec)
						}
					}
				}
				if len(people) != 2+x.Hops {
					t.Fatalf("seed %d: custody names %d people, want %d distinct", seed, len(people), 2+x.Hops)
				}
				if x.AskActor {
					actor++
					step := x.Steps[x.ActorHop]
					var doers []string
					for _, ev := range m.custody {
						if ev.Step == step {
							doers = append(doers, ev.Actor)
						}
					}
					if len(doers) != 1 || doers[0] != m.Expected {
						t.Fatalf("seed %d: step %q doers %v, expected %q", seed, step, doers, m.Expected)
					}
				} else {
					holder++
					if got := foldV14Holder(t, m.custody); got != m.Expected {
						t.Fatalf("seed %d: holder fold %q, expected %q", seed, got, m.Expected)
					}
				}
				// The answer shares a record with a same-type distractor.
				shared := false
				for _, rec := range m.Records {
					if !strings.Contains(rec, m.Expected) {
						continue
					}
					for _, d := range m.Distractors {
						if strings.Contains(rec, d) {
							shared = true
						}
					}
				}
				if !shared {
					t.Fatalf("seed %d: answer %q never shares a record with a distractor %v", seed, m.Expected, m.Distractors)
				}
				for _, d := range m.Distractors {
					if d == m.Expected {
						t.Fatalf("seed %d: answer %q is also a distractor", seed, d)
					}
				}
			}
		}
	}
	t.Logf("holder-vs-actor groups by hops %v; actor members %d, holder members %d", hops, actor, holder)
	if hops[1] == 0 || hops[2] == 0 || hops[3] != 0 || actor == 0 || holder == 0 {
		t.Fatalf("holder-vs-actor coverage: hops %v, actor %d, holder %d", hops, actor, holder)
	}
}

// parseV14Directory reads a rendered directory record into handle -> name.
func parseV14Directory(t *testing.T, rec string) map[string]string {
	t.Helper()
	body := rec[strings.Index(rec, ": ")+2:]
	body = strings.TrimSuffix(body, ".")
	out := map[string]string{}
	for _, part := range strings.Split(body, "; ") {
		var handle, name string
		switch {
		case strings.Contains(part, " belongs to "):
			kv := strings.SplitN(part, " belongs to ", 2)
			handle, name = kv[0], kv[1]
		case strings.Contains(part, " uses "):
			kv := strings.SplitN(part, " uses ", 2)
			name, handle = kv[0], kv[1]
		case strings.Contains(part, " is "):
			kv := strings.SplitN(part, " is ", 2)
			handle, name = kv[0], kv[1]
		default:
			t.Fatalf("unparsed directory entry %q", part)
		}
		if _, dup := out[handle]; dup {
			t.Fatalf("directory binds %s twice: %q", handle, rec)
		}
		out[handle] = name
	}
	return out
}

// names2 returns the group's people in draw order.
func names2(rm v14RoleMember) [4]string { return rm.g.People }

// v14Bounded reports whether value occurs in text not followed by a letter.
func v14Bounded(text, value string) bool {
	for i := 0; ; {
		j := strings.Index(text[i:], value)
		if j < 0 {
			return false
		}
		end := i + j + len(value)
		if end == len(text) || !(text[end] >= 'a' && text[end] <= 'z' || text[end] >= 'A' && text[end] <= 'Z') {
			return true
		}
		i += j + 1
	}
}

func v14EditDistance(a, b string) int {
	prev := make([]int, len(b)+1)
	for j := range prev {
		prev[j] = j
	}
	for i := 1; i <= len(a); i++ {
		cur := make([]int, len(b)+1)
		cur[0] = i
		for j := 1; j <= len(b); j++ {
			cost := 1
			if a[i-1] == b[j-1] {
				cost = 0
			}
			cur[j] = min(prev[j]+1, cur[j-1]+1, prev[j-1]+cost)
		}
		prev = cur
	}
	return prev[len(b)]
}

func TestV14IdentityJoinBindings(t *testing.T) {
	contact, name, initials := 0, 0, 0
	for seed := int64(1); seed <= 80; seed++ {
		seedHandles := map[string]bool{}
		for _, rm := range v14RoleMembers(seed) {
			if rm.g.Family != V14FamilyIdentityJoin {
				continue
			}
			x := rm.g.V14
			for _, h := range x.Handles {
				if seedHandles[h] {
					t.Fatalf("seed %d: handle %s reused", seed, h)
				}
				seedHandles[h] = true
				if !strings.HasSuffix(h, ".example.invalid") && !strings.HasPrefix(h, "@") {
					t.Fatalf("seed %d: handle %s is not clearly synthetic", seed, h)
				}
			}
			if d := v14EditDistance(x.Handles[1], x.Handles[4]); d < 2 {
				t.Fatalf("seed %d: near miss %s is edit distance %d from %s", seed, x.Handles[4], d, x.Handles[1])
			}
			for _, m := range []v13Member{rm.base, rm.counter} {
				dir := parseV14Directory(t, m.Records[2])
				names := map[string]bool{}
				for _, n := range dir {
					if names[n] {
						t.Fatalf("seed %d: %s holds two handles", seed, n)
					}
					names[n] = true
				}
				governing := m.Records[1]
				if strings.Contains(governing, m.Expected) || strings.Contains(m.Records[0], m.Expected) {
					t.Fatalf("seed %d: answer %q appears in an owner record", seed, m.Expected)
				}
				// Join the governing record's referent through the directory.
				var joined []string
				for h, n := range dir {
					if x.AskContact && (v14Bounded(governing, n) || x.Initials && v14Bounded(governing, v14Initials(n))) {
						joined = append(joined, h)
					}
					if !x.AskContact && strings.Contains(governing, h) {
						joined = append(joined, n)
					}
				}
				if len(joined) != 1 || joined[0] != m.Expected {
					t.Fatalf("seed %d: directory join %v, expected %q\n%q\n%q", seed, joined, m.Expected, governing, m.Records[2])
				}
				if x.Initials && strings.Contains(governing, names2(rm)[1]) {
					t.Fatalf("seed %d: initials record %q carries the full name", seed, governing)
				}
				if x.Initials {
					initials++
				}
				if x.AskContact {
					contact++
				} else {
					name++
				}
			}
		}
	}
	t.Logf("identity-join members: contact %d (initials %d), name %d", contact, initials, name)
	if contact == 0 || name == 0 || initials == 0 {
		t.Fatalf("identity-join coverage: contact %d, name %d", contact, name)
	}
}

// TestV14RoleDrawsAreIndependent: every reachable combination of a group's
// binary draws occurs, so no draw predicts another (FNV parity would
// otherwise tie them).
func TestV14RoleDrawsAreIndependent(t *testing.T) {
	holder, join := map[[3]bool]int{}, map[[3]bool]int{}
	for seed := int64(1); seed <= 400; seed++ {
		for _, rm := range v14RoleMembers(seed) {
			x := rm.g.V14
			if rm.g.Family == V14FamilyHolderVsActor {
				holder[[3]bool{x.Hops == 2, x.AskActor, x.ActorHop == 1}]++
			} else {
				join[[3]bool{x.AskContact, x.Initials, strings.HasPrefix(x.Handles[0], "@")}]++
			}
		}
	}
	// Six reachable holder combinations (ActorHop is 0 on one-hop chains) and
	// six identity-join combinations (Initials only on contact questions).
	if len(holder) != 6 || len(join) != 6 {
		t.Fatalf("draw combinations: holder %v, identity-join %v", holder, join)
	}
	for _, m := range []map[[3]bool]int{holder, join} {
		for k, n := range m {
			if n < 10 {
				t.Fatalf("combination %v drawn only %d times", k, n)
			}
		}
	}
}

func TestV14RoleFamiliesDeterministic(t *testing.T) {
	a, err := GenerateV13ProgramsForVersion(77, 36, protocol.BenchVersionV14)
	if err != nil {
		t.Fatal(err)
	}
	b, err := GenerateV13ProgramsForVersion(77, 36, protocol.BenchVersionV14)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatal("same seed did not regenerate identical v14 programs")
	}
	c, err := GenerateV13ProgramsForVersion(78, 36, protocol.BenchVersionV14)
	if err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(a, c) {
		t.Fatal("different seeds generated identical v14 programs")
	}
}

// TestV14RoleFamiliesGrade is the grader proof under the v14 policy: the
// honest answer scores; the actor, the stale holder, the previous owner, the
// near-miss handle, the governing record's literal referent, and a stuffed
// pair all score zero.
func TestV14RoleFamiliesGrade(t *testing.T) {
	score := func(mc protocol.MemoryCase, text string) grade.Verdict {
		return grade.Memory(mc, protocol.RunResponse{FinalText: text})
	}
	shortcutWrong, shortcutTotal := 0, 0
	for seed := int64(1); seed <= 40; seed++ {
		cases, err := GenerateV13ProgramsForVersion(seed, 36, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		members := map[int]v14RoleMember{}
		for _, rm := range v14RoleMembers(seed) {
			members[rm.group] = rm
		}
		for i, gc := range cases {
			family := V13FamilyOfVersion(seed, i, protocol.BenchVersionV14)
			if family != V14FamilyHolderVsActor && family != V14FamilyIdentityJoin {
				continue
			}
			mc := gc.Plan.Case
			mc.BenchVersion = protocol.BenchVersionV14
			for _, text := range []string{mc.ExpectedAnswer, mc.ExpectedAnswer + ".", fmt.Sprintf("It's %s.", mc.ExpectedAnswer)} {
				if v := score(mc, text); v.Score != 1 {
					t.Fatalf("seed %d case %d %s: honest %q scored %v (%v)", seed, i, family, text, v.Score, v.Notes)
				}
			}
			if len(mc.DistractorAnswers) < 2 {
				t.Fatalf("seed %d case %d %s: %d distractors", seed, i, family, len(mc.DistractorAnswers))
			}
			for _, d := range mc.DistractorAnswers {
				for _, text := range []string{d, fmt.Sprintf("%s and %s", mc.ExpectedAnswer, d)} {
					if v := score(mc, text); v.Score != 0 {
						t.Fatalf("seed %d case %d %s: %q scored %v (%v)", seed, i, family, text, v.Score, v.Notes)
					}
				}
			}
			rm := members[i/4]
			m := rm.base
			if gc.Provenance.Relation == protocol.RelationCausalCounterfactual {
				m = rm.counter
			}
			// The direct-reading shortcut: answer with the last person the
			// governing record names (holder-vs-actor), or copy the value the
			// governing record carries (identity-join). Neither may score
			// when it misses the answer.
			var shortcut string
			if family == V14FamilyIdentityJoin {
				x := rm.g.V14
				candidates := append(append([]string(nil), x.Handles[:]...), rm.g.People[0], rm.g.People[1], rm.g.People[2], x.NearMiss)
				for _, p := range []string{rm.g.People[0], rm.g.People[1], rm.g.People[2], x.NearMiss} {
					candidates = append(candidates, v14Initials(p))
				}
				for _, v := range candidates {
					if strings.Contains(m.Records[1], v) {
						shortcut = v
					}
				}
				if shortcut == "" || shortcut == mc.ExpectedAnswer {
					t.Fatalf("seed %d case %d: governing record %q yields shortcut %q", seed, i, m.Records[1], shortcut)
				}
			} else {
				last := len(m.custody) - 1
				rec := m.Records[m.custody[last].Record]
				at := -1
				for _, ev := range m.custody {
					for _, p := range []string{ev.Actor, ev.Giver, ev.Recipient} {
						if p != "" && strings.LastIndex(rec, p) > at {
							at, shortcut = strings.LastIndex(rec, p), p
						}
					}
				}
			}
			shortcutTotal++
			if shortcut != mc.ExpectedAnswer {
				shortcutWrong++
				if v := score(mc, shortcut); v.Score != 0 {
					t.Fatalf("seed %d case %d %s: shortcut %q scored %v (%v)", seed, i, family, shortcut, v.Score, v.Notes)
				}
			}
		}
	}
	t.Logf("direct-reading shortcut wrong on %d/%d role-family cases", shortcutWrong, shortcutTotal)
	if shortcutTotal == 0 || 2*shortcutWrong < shortcutTotal {
		t.Fatalf("the direct-reading shortcut is right on %d/%d role-family cases", shortcutTotal-shortcutWrong, shortcutTotal)
	}
}
