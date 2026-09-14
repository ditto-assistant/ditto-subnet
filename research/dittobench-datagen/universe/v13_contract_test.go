package universe

import (
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV13ContractCounterfactualChangesExactlyOneRecord is the record-level
// half of the metamorphic proof: for every business family and every seed,
// the causal-counterfactual reading of a group differs from the base reading
// in exactly one of the three state records (the targeted claim's record),
// while the decoy clause and the question are unchanged.
func TestV13ContractCounterfactualChangesExactlyOneRecord(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		schema := generateV13Schema(seed)
		d := newV13Draws(seed, "business")
		for group := 0; group < len(V13Families); group++ {
			g := d.drawGroup(group)
			base := renderV13Member(d, schema, group, 0, g, false)
			counter := renderV13Member(d, schema, group, 0, g, true)
			changed := 0
			for i := range base.Records {
				if base.Records[i] != counter.Records[i] {
					changed++
				}
			}
			if changed != 1 {
				t.Fatalf("seed %d %s: counterfactual changed %d records, want exactly 1\nbase=%q\ncounter=%q", seed, g.Family, changed, base.Records, counter.Records)
			}
			if base.DecoyClause != counter.DecoyClause || base.Question != counter.Question {
				t.Fatalf("seed %d %s: counterfactual moved the decoy clause or the question", seed, g.Family)
			}
			if base.Expected == counter.Expected || strings.Join(base.Items, "|") == strings.Join(counter.Items, "|") && len(base.Items) > 0 {
				t.Fatalf("seed %d %s: counterfactual did not change the graded answer (%q)", seed, g.Family, base.Expected)
			}
		}
	}
}

// TestV13ContractPersonalCounterfactualChangesExactlyOneRecord mirrors the
// business proof for every personal domain.
func TestV13ContractPersonalCounterfactualChangesExactlyOneRecord(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		d := newV13Draws(seed, "personal")
		for group, domain := range V13PersonalDomains {
			g := d.drawPersonalGroup(group, domain)
			base := renderV13PersonalMember(d, group, 0, g, false)
			counter := renderV13PersonalMember(d, group, 0, g, true)
			changed := 0
			for i := range base.Records {
				if base.Records[i] != counter.Records[i] {
					changed++
				}
			}
			if changed != 1 {
				t.Fatalf("seed %d %s: counterfactual changed %d records, want exactly 1\nbase=%q\ncounter=%q", seed, domain, changed, base.Records, counter.Records)
			}
			if base.DecoyClause != counter.DecoyClause || base.Question != counter.Question {
				t.Fatalf("seed %d %s: counterfactual moved the decoy clause or the question", seed, domain)
			}
			if base.Expected == counter.Expected {
				t.Fatalf("seed %d %s: counterfactual did not change the graded answer (%q)", seed, domain, base.Expected)
			}
		}
	}
}

// TestV13ContractTermClassesAreDisjoint guards the accept-cluster invariant a
// distractor scan depends on: no accepted form of one class is a bounded
// substring of any accepted form of a different class in the same bank, so a
// planted distractor can never fire on an honest synonym.
func TestV13ContractTermClassesAreDisjoint(t *testing.T) {
	banks := map[string][]V13TermClass{
		"status": V13StatusClasses, "event": V13EventClasses, "channel": V13ChannelClasses,
		"action": V13ActionClasses, "personal-status": V13PersonalStatusClasses, "personal-action": V13PersonalActionClasses,
	}
	for name, bank := range banks {
		for i, a := range bank {
			for j, b := range bank {
				if i == j {
					continue
				}
				for _, termA := range append([]string{a.Canonical}, a.Accept...) {
					for _, termB := range append([]string{b.Canonical}, b.Accept...) {
						if boundedContains(strings.ToLower(termB), strings.ToLower(termA)) {
							t.Fatalf("%s bank: %q (class %q) is contained in %q (class %q)", name, termA, a.Canonical, termB, b.Canonical)
						}
					}
				}
			}
		}
	}
	for i, a := range v13StatusAliasTerms {
		for _, cls := range V13StatusClasses {
			for _, term := range append([]string{cls.Canonical}, cls.Accept...) {
				if boundedContains(strings.ToLower(term), a) {
					t.Fatalf("status alias term %q collides with status term %q", a, term)
				}
			}
		}
		for j, b := range v13StatusAliasTerms {
			if i != j && a == b {
				t.Fatalf("duplicate status alias term %q", a)
			}
		}
	}
}

func boundedContains(text, phrase string) bool {
	for start := 0; ; {
		idx := strings.Index(text[start:], phrase)
		if idx < 0 {
			return false
		}
		idx += start
		end := idx + len(phrase)
		beforeOK := idx == 0 || !isWordByte(text[idx-1])
		afterOK := end == len(text) || !isWordByte(text[end])
		if beforeOK && afterOK {
			return true
		}
		start = idx + 1
	}
}

func isWordByte(b byte) bool {
	return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
}

// TestV13ContractRecordsShuffleAndTimestampsJitter proves the movable records
// land in more than one slot across groups and that no constant step separates
// consecutive record timestamps.
func TestV13ContractRecordsShuffleAndTimestampsJitter(t *testing.T) {
	generated, err := GenerateV13Programs(123456789, 28)
	if err != nil {
		t.Fatal(err)
	}
	bindingSlots := map[int]bool{}
	steps := map[string]bool{}
	for _, g := range generated {
		alias := g.Plan.Case.WritingProtected[0]
		for slot, pair := range g.Pairs {
			if strings.Contains(pair.Prompt, "="+alias) {
				bindingSlots[slot] = true
			}
			if slot > 0 {
				steps[pair.Timestamp[8:10]+pair.Timestamp[11:16]] = true
			}
		}
	}
	if len(bindingSlots) < 2 {
		t.Fatalf("binding record only ever landed in slot(s) %v", bindingSlots)
	}
	if len(steps) < 8 {
		t.Fatalf("record timestamps follow too few distinct (day,time) values: %d", len(steps))
	}
	for _, g := range generated {
		if g.Provenance.Revision != V13ProvenanceRevision || g.Plan.Case.BenchVersion != protocol.BenchVersionV13 {
			t.Fatalf("case %s carries revision %q / version %d", g.Plan.Case.ID, g.Provenance.Revision, g.Plan.Case.BenchVersion)
		}
	}
	again, err := GenerateV13Programs(123456789, 28)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(generated, again) {
		t.Fatal("same seed did not regenerate identical v13 programs")
	}
}

// TestV13ContractDateAndTimeAcceptVectors pins the accepted renderings of a
// day-granularity date and a time of day: ISO, spelled-out, and ordinal forms
// are accepted; ambiguous slashed numerics are not.
func TestV13ContractDateAndTimeAcceptVectors(t *testing.T) {
	got := V13DateAccept(2026, 3, 4)
	for _, want := range []string{"2026-03-04", "March 4", "Mar 4", "4 March", "4 March 2026", "March 4th", "4th of March", "March 4, 2026"} {
		if !containsString(got, want) {
			t.Fatalf("date accept set lacks %q: %v", want, got)
		}
	}
	for _, banned := range []string{"03/04/2026", "3/4/2026", "04/03/2026", "4/3"} {
		if containsString(got, banned) {
			t.Fatalf("date accept set carries the locale-ambiguous %q", banned)
		}
	}
	if got := V13DateAccept(2026, 11, 22); !containsString(got, "November 22nd") || !containsString(got, "22nd of November") {
		t.Fatalf("ordinal suffix wrong for 22: %v", got)
	}
	if got := V13DateAccept(2026, 1, 11); !containsString(got, "January 11th") {
		t.Fatalf("ordinal suffix wrong for 11: %v", got)
	}
	times := V13TimeAccept(7, 45)
	for _, want := range []string{"07:45", "7:45", "7:45 am", "7:45am", "7.45 am", "0745"} {
		if !containsString(times, want) {
			t.Fatalf("time accept set lacks %q: %v", want, times)
		}
	}
	times = V13TimeAccept(19, 20)
	for _, want := range []string{"19:20", "7:20 pm", "7:20pm", "1920"} {
		if !containsString(times, want) {
			t.Fatalf("pm time accept set lacks %q: %v", want, times)
		}
	}
}

func containsString(values []string, want string) bool {
	for _, v := range values {
		if v == want {
			return true
		}
	}
	return false
}

// TestV13ContractRecordTimestampsRespectAssertedDates: a record is never
// written before a day it reports as having happened, and a household note
// always precedes the plan date it schedules. The latest-event family asserts
// its three event dates (and the counterfactual's moved date) as past, so
// every one of its record timestamps lands after the latest of them; every
// other business group and every personal group mentions only plan dates,
// which all fall after the default record month.
func TestV13ContractRecordTimestampsRespectAssertedDates(t *testing.T) {
	parse := func(ts string) time.Time {
		at, err := time.Parse(time.RFC3339, ts)
		if err != nil {
			t.Fatalf("timestamp %q: %v", ts, err)
		}
		return at
	}
	dayStart := func(d v13Date) time.Time {
		return time.Date(v13RecordYear, time.Month(d.Month), d.Day, 0, 0, 0, 0, time.UTC)
	}
	for seed := int64(1); seed <= 40; seed++ {
		generated, err := GenerateV13Programs(seed, 28)
		if err != nil {
			t.Fatal(err)
		}
		d := newV13Draws(seed, "business")
		for group := 0; group < len(V13Families); group++ {
			g := d.drawGroup(group)
			members := generated[group*4 : group*4+4]
			if members[0].Plan.Case.WritingProtected[0] != g.Alias {
				t.Fatalf("seed %d group %d: draw out of sync with generation (%q vs %q)", seed, group, members[0].Plan.Case.WritingProtected[0], g.Alias)
			}
			latest, asserted := v13LatestAssertedDate(g)
			for _, m := range members {
				for _, pair := range m.Pairs {
					at := parse(pair.Timestamp)
					if asserted {
						for _, past := range append([]v13Date{g.CounterDate}, g.Events[0].Date, g.Events[1].Date, g.Events[2].Date) {
							if !at.After(dayStart(past).AddDate(0, 0, 1)) {
								t.Fatalf("seed %d %s: record at %s precedes asserted event day %v (latest %v)", seed, g.Family, pair.Timestamp, past, latest)
							}
						}
						continue
					}
					if !at.Before(dayStart(g.Milestone)) {
						t.Fatalf("seed %d %s: record at %s is not before the planned milestone %v", seed, g.Family, pair.Timestamp, g.Milestone)
					}
				}
			}
		}
		personal, err := GenerateV13PersonalPrograms(seed, 24)
		if err != nil {
			t.Fatal(err)
		}
		pd := newV13Draws(seed, "personal")
		domainPerm := v13Perm(seed, "personal-domains", len(V13PersonalDomains))
		for group := 0; group < 6; group++ {
			g := pd.drawPersonalGroup(group, V13PersonalDomains[domainPerm[group]])
			for _, m := range personal[group*4 : group*4+4] {
				for _, pair := range m.Pairs {
					at := parse(pair.Timestamp)
					for _, plan := range g.Dates {
						if !at.Before(dayStart(plan)) {
							t.Fatalf("seed %d %s: household note at %s is not before the plan date %v", seed, g.Domain, pair.Timestamp, plan)
						}
					}
				}
			}
		}
	}
}
