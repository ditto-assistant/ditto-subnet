package universe

import (
	"fmt"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// v14TemporalGroups replays the v14 business draw for seed and returns each
// group with its rendered base and counterfactual members, in generation
// order. renderV13Member draws no names, so a fresh draw state reproduces the
// generator's groups exactly.
type v14RenderedGroup struct {
	group         int
	g             v13Group
	base, counter v13Member
}

func v14TemporalGroups(t *testing.T, seed int64, groups int) []v14RenderedGroup {
	t.Helper()
	schema := generateV13Schema(seed)
	d := newV13Draws(seed, "business", protocol.BenchVersionV14)
	out := make([]v14RenderedGroup, 0, groups)
	for group := 0; group < groups; group++ {
		g := d.drawGroup(group)
		out = append(out, v14RenderedGroup{
			group:   group,
			g:       g,
			base:    renderV13Member(d, schema, group, 0, g, false),
			counter: renderV13Member(d, schema, group, 0, g, true),
		})
	}
	return out
}

// v14GradeMember grades reply text against a member as a v14 case.
func v14GradeMember(m v13Member, reply string) float64 {
	mc := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV14,
		QuestionType:      V13ProgramQuestionType,
		Question:          m.Question,
		ExpectedAnswer:    m.Expected,
		AnswerKind:        m.Kind,
		AcceptAny:         m.AcceptAny,
		AnswerItems:       m.Items,
		AnswerItemKinds:   m.ItemKinds,
		DistractorAnswers: m.Distractors,
		Claims:            m.Claims,
	}
	if len(m.ItemAccept) > 0 {
		mc.AnswerItemAcceptAny = m.ItemAccept
	}
	return v14GradeMemberCase(mc, reply)
}

// v14GradeMemberCase grades reply text against a memory case.
func v14GradeMemberCase(mc protocol.MemoryCase, reply string) float64 {
	return grade.Memory(mc, protocol.RunResponse{FinalText: reply}).Score
}

func v14AllRecords(m v13Member) []string {
	return append(append([]string(nil), m.Records[:]...), m.Extra...)
}

// TestV14TemporalSlotRotatesPerSeed: from v14 the temporal slot (cycle
// position 2) draws latest-event, next-action-responsible or current-channel
// per seed, the other two keep positions 4 and 5, and every other position
// keeps its v13 family. v13 draws are untouched.
func TestV14TemporalSlotRotatesPerSeed(t *testing.T) {
	seen := map[V13Family]int{}
	for seed := int64(1); seed <= 90; seed++ {
		d13 := newV13Draws(seed, "business", protocol.BenchVersionV13)
		d14 := newV13Draws(seed, "business", protocol.BenchVersionV14)
		var temporal []V13Family
		for group := 0; group < len(V13Families); group++ {
			f13, f14 := d13.drawGroup(group).Family, d14.drawGroup(group).Family
			if f13 != V13Families[group] {
				t.Fatalf("seed %d group %d: v13 family %s moved", seed, group, f13)
			}
			if f14 != V14FamilyOf(seed, group*4) {
				t.Fatalf("seed %d group %d: V14FamilyOf disagrees with the draw", seed, group)
			}
			switch group {
			case 2, 4, 5:
				temporal = append(temporal, f14)
			default:
				if f14 != f13 {
					t.Fatalf("seed %d group %d: non-temporal family moved to %s", seed, group, f14)
				}
			}
		}
		sorted := append([]V13Family(nil), temporal...)
		sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
		want := append([]V13Family(nil), v14TemporalFamilies...)
		sort.Slice(want, func(i, j int) bool { return want[i] < want[j] })
		if !reflect.DeepEqual(sorted, want) {
			t.Fatalf("seed %d: temporal positions hold %v", seed, temporal)
		}
		seen[temporal[0]]++
	}
	for _, f := range v14TemporalFamilies {
		if seen[f] < 15 {
			t.Fatalf("family %s fills the scored temporal slot on only %d of 90 seeds: %v", f, seen[f], seen)
		}
	}
}

// TestV14TemporalFamiliesDeterministic: two generations of one seed agree
// byte for byte, and different seeds differ.
func TestV14TemporalFamiliesDeterministic(t *testing.T) {
	a, err := GenerateV13ProgramsForVersion(77, 28, protocol.BenchVersionV14)
	if err != nil {
		t.Fatal(err)
	}
	b, err := GenerateV13ProgramsForVersion(77, 28, protocol.BenchVersionV14)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatal("v14 business generation is not deterministic by seed")
	}
	c, err := GenerateV13ProgramsForVersion(78, 28, protocol.BenchVersionV14)
	if err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(a, c) {
		t.Fatal("two seeds generated identical business programs")
	}
}

// TestV13ProgramsCarryNoV14TemporalDraw: v13 groups never carry the v14
// draw, so every v13 member renders exactly three state records.
func TestV13ProgramsCarryNoV14TemporalDraw(t *testing.T) {
	for seed := int64(1); seed <= 20; seed++ {
		d := newV13Draws(seed, "business", protocol.BenchVersionV13)
		schema := generateV13Schema(seed)
		for group := 0; group < len(V13Families); group++ {
			g := d.drawGroup(group)
			if g.T14 != nil {
				t.Fatalf("seed %d group %d: v13 group carries a v14 temporal draw", seed, group)
			}
			if m := renderV13Member(d, schema, group, 0, g, false); len(m.Extra) != 0 {
				t.Fatalf("seed %d group %d: v13 member has %d extra records", seed, group, len(m.Extra))
			}
		}
	}
}

var v14RelativeDate = regexp.MustCompile(`(\d+) days (after|before) the ([a-z -]+?)[.]`)

// TestV14EventSequenceOracle checks the SN-358 family's ground truth against
// an independent oracle built from the rendered records alone: every
// absolute date is parsed from its record, every relative date is resolved
// against its named anchor, and the events are sorted. The asked position
// must name the expected event in the base and the counterfactual.
func TestV14EventSequenceOracle(t *testing.T) {
	lengths := map[int]bool{}
	queries := map[string]bool{}
	checked := 0
	for seed := int64(1); seed <= 120; seed++ {
		for _, rg := range v14TemporalGroups(t, seed, len(V13Families)) {
			if rg.g.Family != V13FamilyLatestEvent {
				continue
			}
			s := rg.g.T14.Seq
			if s == nil {
				t.Fatalf("seed %d: v14 latest-event group has no sequence draw", seed)
			}
			n := len(s.Classes)
			if n < 5 || n > 8 {
				t.Fatalf("seed %d: %d events, want 5-8", seed, n)
			}
			lengths[n] = true
			queries[s.Query] = true
			if len(s.Relative) == 0 {
				t.Fatalf("seed %d: no relatively dated event", seed)
			}
			for _, member := range []struct {
				m       v13Member
				counter bool
			}{{rg.base, false}, {rg.counter, true}} {
				order := v14OracleEventOrder(t, seed, member.m, s)
				var want string
				switch s.Query {
				case v14SeqNth:
					want = order[s.Answer]
				case v14SeqAfter, v14SeqBefore:
					named := V14SequenceEventClasses[s.Classes[s.Named]].Canonical
					at := indexOf(order, named)
					if s.Query == v14SeqAfter {
						want = order[at+1]
					} else {
						want = order[at-1]
					}
				}
				if want != member.m.Expected {
					t.Fatalf("seed %d counter=%v %s: oracle says %q, generator expects %q\norder=%v\nrecords=%q", seed, member.counter, s.Query, want, member.m.Expected, order, v14AllRecords(member.m))
				}
				if member.m.Expected == order[0] || member.m.Expected == order[n-1] {
					t.Fatalf("seed %d counter=%v: answer %q is the first or last event by date", seed, member.counter, member.m.Expected)
				}
			}
			if rg.base.Expected == rg.counter.Expected {
				t.Fatalf("seed %d: counterfactual left the answer at %q", seed, rg.base.Expected)
			}
			changed := 0
			for i, rec := range v14AllRecords(rg.base) {
				if rec != v14AllRecords(rg.counter)[i] {
					changed++
				}
			}
			if changed != 1 || rg.base.Question != rg.counter.Question || rg.base.DecoyClause != rg.counter.DecoyClause {
				t.Fatalf("seed %d: counterfactual changed %d records (want 1) or moved the question/decoy", seed, changed)
			}
			checked++
		}
	}
	if checked < 25 {
		t.Fatalf("only %d event-sequence groups checked", checked)
	}
	for n := 5; n <= 8; n++ {
		if !lengths[n] {
			t.Fatalf("no %d-event sequence generated across seeds: %v", n, lengths)
		}
	}
	for _, q := range []string{v14SeqNth, v14SeqBefore, v14SeqAfter} {
		if !queries[q] {
			t.Fatalf("query kind %s never generated: %v", q, queries)
		}
	}
}

func indexOf(list []string, v string) int {
	for i, x := range list {
		if x == v {
			return i
		}
	}
	return -1
}

// v14OracleEventOrder reads every event record of a member, resolves the
// relative dates, and returns the event names in date order.
func v14OracleEventOrder(t *testing.T, seed int64, m v13Member, s *v14Sequence) []string {
	t.Helper()
	type ev struct {
		name   string
		at     time.Time
		rel    string
		offset int
	}
	var events []ev
	for _, rec := range v14AllRecords(m) {
		name := v14RecordEvent(rec, s)
		if name == "" {
			continue
		}
		if mt := v14RelativeDate.FindStringSubmatch(rec); mt != nil {
			days, _ := strconv.Atoi(mt[1])
			if days < 1 || days > 27 {
				t.Fatalf("seed %d: relative gap %d days is outside one month: %q", seed, days, rec)
			}
			if mt[2] == "before" {
				days = -days
			}
			events = append(events, ev{name: name, rel: mt[3], offset: days})
			continue
		}
		at, ok := v14ParseRecordDate(rec)
		if !ok {
			t.Fatalf("seed %d: no date in %q", seed, rec)
		}
		events = append(events, ev{name: name, at: at})
	}
	if len(events) != len(s.Classes) {
		t.Fatalf("seed %d: parsed %d events, want %d: %q", seed, len(events), len(s.Classes), v14AllRecords(m))
	}
	for i := range events {
		if events[i].rel == "" {
			continue
		}
		found := false
		for _, a := range events {
			if a.name == events[i].rel && a.rel == "" {
				events[i].at = a.at.AddDate(0, 0, events[i].offset)
				found = true
			}
		}
		if !found {
			t.Fatalf("seed %d: relative anchor %q is not an absolutely dated event", seed, events[i].rel)
		}
	}
	sort.Slice(events, func(i, j int) bool { return events[i].at.Before(events[j].at) })
	out := make([]string, len(events))
	for i, e := range events {
		if i > 0 && !events[i-1].at.Before(e.at) {
			t.Fatalf("seed %d: two events share %s", seed, e.at.Format("2006-01-02"))
		}
		out[i] = e.name
	}
	return out
}

// v14RecordEvent returns the event a sequence record logs (not the anchor a
// relative record names), or "" for a record that logs none.
func v14RecordEvent(rec string, s *v14Sequence) string {
	for i := range s.Classes {
		c := V14SequenceEventClasses[s.Classes[i]].Canonical
		for _, form := range []string{"— " + c + " —", ": " + c + ".", "reads " + c + ".", ": " + c + ",", "reads " + c + ","} {
			if strings.Contains(rec, form) {
				return c
			}
		}
	}
	return ""
}

// v14ParseRecordDate finds the one calendar day a record states, in any of
// the year-bearing forms v14DayProse renders.
func v14ParseRecordDate(rec string) (time.Time, bool) {
	for _, re := range []struct {
		re     *regexp.Regexp
		layout func([]string) string
		form   string
	}{
		{regexp.MustCompile(`\d{4}-\d{2}-\d{2}`), func(m []string) string { return m[0] }, "2006-01-02"},
		{regexp.MustCompile(`([A-Z][a-z]+) (\d{1,2}), (\d{4})`), func(m []string) string { return m[1][:3] + " " + m[2] + " " + m[3] }, "Jan 2 2006"},
		{regexp.MustCompile(`(\d{1,2}) ([A-Z][a-z]+) (\d{4})`), func(m []string) string { return m[2][:3] + " " + m[1] + " " + m[3] }, "Jan 2 2006"},
	} {
		if m := re.re.FindStringSubmatch(rec); m != nil {
			at, err := time.Parse(re.form, re.layout(m))
			if err == nil {
				return at, true
			}
		}
	}
	return time.Time{}, false
}

// TestV14EventSequenceShortcutsScoreZero is the SN-358 negative test: the
// shortcuts the fixed latest-of-three family rewarded no longer score. A
// reply naming the latest-dated event, the earliest-dated event, or the event
// in the first filed state record scores 0; the honest answer scores 1.
func TestV14EventSequenceShortcutsScoreZero(t *testing.T) {
	checked := 0
	for seed := int64(1); seed <= 80; seed++ {
		for _, rg := range v14TemporalGroups(t, seed, len(V13Families)) {
			if rg.g.Family != V13FamilyLatestEvent {
				continue
			}
			s := rg.g.T14.Seq
			for _, member := range []struct {
				m       v13Member
				counter bool
			}{{rg.base, false}, {rg.counter, true}} {
				m := member.m
				if got := v14GradeMember(m, "It was the "+m.Expected+"."); got != 1 {
					t.Fatalf("seed %d counter=%v: honest answer %q scored %.2f", seed, member.counter, m.Expected, got)
				}
				order := v14OracleEventOrder(t, seed, m, s)
				// State records are held in story (filing) order.
				firstFiled := ""
				for _, rec := range v14AllRecords(m) {
					if name := v14RecordEvent(rec, s); name != "" && firstFiled == "" {
						firstFiled = name
					}
				}
				for label, shortcut := range map[string]string{
					"latest event": order[len(order)-1],
					"first event":  order[0],
					"first filed":  firstFiled,
				} {
					if shortcut == m.Expected {
						t.Fatalf("seed %d counter=%v: %s shortcut %q is the answer", seed, member.counter, label, shortcut)
					}
					if got := v14GradeMember(m, "It was the "+shortcut+"."); got != 0 {
						t.Fatalf("seed %d counter=%v: %s shortcut %q scored %.2f", seed, member.counter, label, shortcut, got)
					}
				}
			}
			checked++
		}
	}
	if checked < 15 {
		t.Fatalf("only %d event-sequence groups checked", checked)
	}
}

// TestV14EventSequenceVariesCaseLength: the v13 business layout always had six
// pairs; v14 event-sequence cases carry one record per event, so the pair
// count varies with the seed.
func TestV14EventSequenceVariesCaseLength(t *testing.T) {
	sizes := map[int]bool{}
	for seed := int64(1); seed <= 60; seed++ {
		cases, err := GenerateV13ProgramsForVersion(seed, 28, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		for i, c := range cases {
			if V14FamilyOf(seed, i) == V13FamilyLatestEvent {
				sizes[len(c.Pairs)] = true
				if len(c.Pairs) != len(c.Plan.RequiredPairIDs) {
					t.Fatalf("seed %d case %d: %d pairs but %d required ids", seed, i, len(c.Pairs), len(c.Plan.RequiredPairIDs))
				}
			}
		}
	}
	if len(sizes) < 3 {
		t.Fatalf("event-sequence pair counts %v do not vary", sizes)
	}
}

// TestV14ChannelAsOfOracle checks the SN-355 family: the question date falls
// strictly inside one interval of the dated channel chain, never on a change
// date, and the expected channel is the one in force that day (independently
// recomputed from the rendered records). The latest and the first values are
// never the answer.
func TestV14ChannelAsOfOracle(t *testing.T) {
	checked := 0
	for seed := int64(1); seed <= 120; seed++ {
		for _, rg := range v14TemporalGroups(t, seed, len(V13Families)) {
			if rg.g.Family != V13FamilyCurrentChannel {
				continue
			}
			a := rg.g.T14.AsOf
			if a == nil {
				t.Fatalf("seed %d: v14 current-channel group has no as-of draw", seed)
			}
			query, ok := v14ParseRecordDate(rg.base.Question)
			if !ok || !query.Equal(a.Query) {
				t.Fatalf("seed %d: question %q does not state the query date %s", seed, rg.base.Question, a.Query.Format("2006-01-02"))
			}
			for _, member := range []struct {
				m       v13Member
				counter bool
			}{{rg.base, false}, {rg.counter, true}} {
				type change struct {
					at      time.Time
					channel string
				}
				var chain []change
				for _, rec := range v14AllRecords(member.m) {
					at, ok := v14ParseRecordDate(rec)
					if !ok {
						continue
					}
					name := ""
					for _, v := range a.Values {
						c := V13ChannelClasses[v].Canonical
						if strings.Contains(rec, " "+c+" ") || strings.Contains(rec, " "+c+".") {
							name = c
						}
					}
					if name == "" {
						continue // the neutral milestone record
					}
					chain = append(chain, change{at, name})
				}
				sort.Slice(chain, func(i, j int) bool { return chain[i].at.Before(chain[j].at) })
				if len(chain) != len(a.Values) {
					t.Fatalf("seed %d: %d dated changes, want %d", seed, len(chain), len(a.Values))
				}
				inForce := -1
				for i, c := range chain {
					if c.at.Equal(query) {
						t.Fatalf("seed %d counter=%v: a change falls on the question date", seed, member.counter)
					}
					if c.at.Before(query) {
						inForce = i
					}
				}
				if inForce <= 0 || inForce == len(chain)-1 {
					t.Fatalf("seed %d counter=%v: the question date falls in interval %d of %d", seed, member.counter, inForce, len(chain))
				}
				if chain[inForce].channel != member.m.Expected {
					t.Fatalf("seed %d counter=%v: oracle says %q, generator expects %q", seed, member.counter, chain[inForce].channel, member.m.Expected)
				}
			}
			if rg.base.Expected == rg.counter.Expected {
				t.Fatalf("seed %d: counterfactual left the answer at %q", seed, rg.base.Expected)
			}
			changed := 0
			for i, rec := range v14AllRecords(rg.base) {
				if rec != v14AllRecords(rg.counter)[i] {
					changed++
				}
			}
			if changed != 1 || rg.base.Question != rg.counter.Question {
				t.Fatalf("seed %d: counterfactual changed %d records (want 1) or moved the question", seed, changed)
			}
			checked++
		}
	}
	if checked < 25 {
		t.Fatalf("only %d as-of groups checked", checked)
	}
}

// TestV14ChannelAsOfShortcutsScoreZero is the SN-355 negative test: the
// latest value ("pick the newest record") and the first value ("read record
// 0") both score 0; the value in force on the question date scores 1.
func TestV14ChannelAsOfShortcutsScoreZero(t *testing.T) {
	checked := 0
	for seed := int64(1); seed <= 80; seed++ {
		for _, rg := range v14TemporalGroups(t, seed, len(V13Families)) {
			if rg.g.Family != V13FamilyCurrentChannel {
				continue
			}
			a := rg.g.T14.AsOf
			latest := V13ChannelClasses[a.Values[len(a.Values)-1]].Canonical
			first := V13ChannelClasses[a.Values[0]].Canonical
			for _, m := range []v13Member{rg.base, rg.counter} {
				if got := v14GradeMember(m, fmt.Sprintf("On that date it was %s.", m.Expected)); got != 1 {
					t.Fatalf("seed %d: honest answer %q scored %.2f", seed, m.Expected, got)
				}
				for label, shortcut := range map[string]string{"latest": latest, "first": first} {
					if shortcut == m.Expected {
						t.Fatalf("seed %d: the %s value is the answer", seed, label)
					}
					if got := v14GradeMember(m, fmt.Sprintf("It is %s.", shortcut)); got != 0 {
						t.Fatalf("seed %d: %s shortcut %q scored %.2f", seed, label, shortcut, got)
					}
				}
			}
			checked++
		}
	}
	if checked < 15 {
		t.Fatalf("only %d as-of groups checked", checked)
	}
}
