package gen

import (
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// decimalOf renders a minor-unit expected answer the way a correct reader would
// write it once asked for a decimal amount.
func decimalOf(t *testing.T, expected string) string {
	t.Helper()
	cents, err := strconv.Atoi(expected)
	if err != nil {
		t.Fatalf("expected answer %q is not an integer: %v", expected, err)
	}
	return fmt.Sprintf("%d.%02d", cents/100, cents%100)
}

// TestV12OpenProgramInstructsAnAnswerItsOwnGraderRejects pins the defect this
// change repairs, so the repair cannot be reverted silently: the v12 question
// asks for minor units, and answering in minor units — exactly as instructed —
// is graded 0 by the shipped grader.
func TestV12OpenProgramInstructsAnAnswerItsOwnGraderRejects(t *testing.T) {
	generated, err := universe.GenerateV12Programs(41, 40)
	if err != nil {
		t.Fatal(err)
	}
	instructed := 0
	for _, g := range generated {
		q := strings.ToLower(g.Plan.Case.Question)
		if !strings.Contains(q, "minor unit") && !strings.Contains(q, "minor-unit") {
			continue
		}
		instructed++
		mc := g.Plan.Case
		v := grade.Memory(mc, protocol.RunResponse{Answer: mc.ExpectedAnswer})
		if v.Score != 0 {
			t.Fatalf("v12 case %s: the minor-unit answer it asks for now scores %v; "+
				"if the grader changed, this test and the v13 repair both need revisiting", mc.ID, v.Score)
		}
	}
	if instructed != len(generated) {
		t.Fatalf("v12 asked for minor units on %d of %d open programs, want all", instructed, len(generated))
	}
}

// TestV13OpenProgramAsksForTheAnswerTheGraderAccepts is the (a) repair: every
// v13 open program asks for a decimal amount in the currency's major unit, and
// that answer grades 1.0 end to end through the shipped grader.
func TestV13OpenProgramAsksForTheAnswerTheGraderAccepts(t *testing.T) {
	for seed := int64(1); seed <= 12; seed++ {
		generated, err := universe.GenerateV13Programs(seed, 40)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		for _, g := range generated {
			mc := g.Plan.Case
			q := strings.ToLower(mc.Question)
			if strings.Contains(q, "minor unit") || strings.Contains(q, "minor-unit") {
				t.Fatalf("seed %d case %s still asks for minor units: %s", seed, mc.ID, mc.Question)
			}
			if !strings.Contains(q, "decimal") || !strings.Contains(q, "two decimal places") {
				t.Fatalf("seed %d case %s does not ask for a two-place decimal: %s", seed, mc.ID, mc.Question)
			}
			code, _, _ := strings.Cut(mc.WritingProtected[6], " ") // the unit noun, e.g. "USD cents"
			if code == "" || !strings.Contains(mc.Question, code) {
				t.Fatalf("seed %d case %s does not name the currency %q: %s", seed, mc.ID, code, mc.Question)
			}

			if v := grade.Memory(mc, protocol.RunResponse{Answer: decimalOf(t, mc.ExpectedAnswer)}); v.Score != 1 {
				t.Fatalf("seed %d case %s: the decimal answer it asks for scored %v (%v)", seed, mc.ID, v.Score, v.Notes)
			}
			// The stored expected answer is still the minor-unit integer, so
			// nothing downstream of the grader had to change.
			if _, err := strconv.Atoi(mc.ExpectedAnswer); err != nil {
				t.Fatalf("seed %d case %s expected answer stopped being a minor-unit integer: %q", seed, mc.ID, mc.ExpectedAnswer)
			}
		}
	}
}

// TestV13CounterfactualSelectorIdentifiesOneRecordSet is the (c) repair. Each
// metamorphic group renders two record sets with two different graded answers;
// the question must select exactly one of them.
func TestV13CounterfactualSelectorIdentifiesOneRecordSet(t *testing.T) {
	const (
		originalSel = "filed on the original docket"
		reissuedSel = "filed on the reissued docket"
	)
	for seed := int64(1); seed <= 12; seed++ {
		generated, err := universe.GenerateV13Programs(seed, 40)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		byGroup := map[string][]universe.V10GeneratedCase{}
		counterfactuals := 0
		for _, g := range generated {
			byGroup[g.Provenance.MetamorphicGroup] = append(byGroup[g.Provenance.MetamorphicGroup], g)
			want, other := originalSel, reissuedSel
			if g.Provenance.Relation == "causal_counterfactual" {
				want, other = reissuedSel, originalSel
				counterfactuals++
			}
			if !strings.Contains(g.Plan.Case.Question, want) {
				t.Fatalf("seed %d case %s (%s) does not select a docket: %s",
					seed, g.Plan.Case.ID, g.Provenance.Relation, g.Plan.Case.Question)
			}
			if strings.Contains(g.Plan.Case.Question, other) {
				t.Fatalf("seed %d case %s selects both dockets: %s", seed, g.Plan.Case.ID, g.Plan.Case.Question)
			}
			// The selector is relational: it still never echoes the alias and
			// never names a printed amount (v12 Gap 3 stays intact).
			if strings.Contains(g.Plan.Case.Question, g.Plan.Case.WritingProtected[0]) {
				t.Fatalf("seed %d case %s names the subject alias: %s", seed, g.Plan.Case.ID, g.Plan.Case.Question)
			}
		}
		if counterfactuals != len(generated)/4 {
			t.Fatalf("seed %d: %d counterfactuals across %d cases", seed, counterfactuals, len(generated))
		}
		// Within a group, the phrase the question selects on appears in exactly
		// one of the two record sets, and the two sets disagree on the answer.
		for groupID, cases := range byGroup {
			for _, g := range cases {
				marker := "original docket"
				if g.Provenance.Relation == "causal_counterfactual" {
					marker = "reissued docket"
				}
				matched, base, counter := 0, "", ""
				for _, other := range cases {
					body := recordBody(other)
					if strings.Contains(body, "Filed on the "+marker) {
						matched++
					}
					if other.Provenance.Relation == "causal_counterfactual" {
						counter = other.Plan.Case.ExpectedAnswer
					} else {
						base = other.Plan.Case.ExpectedAnswer
					}
				}
				// Three variants render the base scenario, one the counterfactual.
				wantMatches := 3
				if g.Provenance.Relation == "causal_counterfactual" {
					wantMatches = 1
				}
				if matched != wantMatches {
					t.Fatalf("seed %d group %s: docket %q appears in %d of the group's record sets, want %d",
						seed, groupID, marker, matched, wantMatches)
				}
				if base == counter {
					t.Fatalf("seed %d group %s: the counterfactual answer does not differ from the base", seed, groupID)
				}
			}
		}
	}
}

// TestV12CounterfactualSelectorIsAmbiguous pins the defect: under v12 the two
// record sets of a group are both selected by the same relational descriptor,
// while their graded answers differ.
func TestV12CounterfactualSelectorIsAmbiguous(t *testing.T) {
	generated, err := universe.GenerateV12Programs(41, 40)
	if err != nil {
		t.Fatal(err)
	}
	subjects := map[string]map[string]bool{}
	answers := map[string]map[string]bool{}
	for _, g := range generated {
		id := g.Provenance.MetamorphicGroup
		if subjects[id] == nil {
			subjects[id] = map[string]bool{}
			answers[id] = map[string]bool{}
		}
		// The subject descriptor is the clause between the opener and the
		// operation verb; comparing the whole selector phrase is enough here.
		for _, form := range []string{
			"the workstream in this batch that has a settled payment on record",
			"the entry whose history logs an amount already paid out",
			"the workstream carrying a cleared disbursement alongside its draft",
			"the account in these records that shows a payment already settled",
		} {
			if strings.Contains(strings.ToLower(g.Plan.Case.Question), form) {
				subjects[id][form] = true
			}
		}
		answers[id][g.Plan.Case.ExpectedAnswer] = true
	}
	for id, forms := range subjects {
		if len(forms) != 1 {
			t.Fatalf("v12 group %s used %d subject descriptors, expected the single shared one", id, len(forms))
		}
		if len(answers[id]) < 2 {
			t.Fatalf("v12 group %s has one answer; the counterfactual should differ", id)
		}
	}
}

func recordBody(g universe.V10GeneratedCase) string {
	var b strings.Builder
	for _, p := range g.Pairs {
		b.WriteString(p.Prompt)
		b.WriteString("\n")
		b.WriteString(p.Response)
		b.WriteString("\n")
	}
	return b.String()
}

// TestV13ProgramsRegenerateExactlyAndKeepV12Frozen proves determinism for v13
// and that routing v12 through the shared code path did not move a byte.
func TestV13ProgramsRegenerateExactlyAndKeepV12Frozen(t *testing.T) {
	a, err := universe.GenerateV13Programs(41, 40)
	if err != nil {
		t.Fatal(err)
	}
	b, err := universe.GenerateV13Programs(41, 40)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatal("same v13 seed did not regenerate identical programs")
	}
	for _, g := range a {
		if g.Provenance.Revision != universe.V13ProvenanceRevision {
			t.Fatalf("v13 case %s carries revision %q", g.Plan.Case.ID, g.Provenance.Revision)
		}
		if g.Plan.Case.BenchVersion != protocol.BenchVersionV13 {
			t.Fatalf("v13 case %s carries bench version %d", g.Plan.Case.ID, g.Plan.Case.BenchVersion)
		}
	}

	// v12 keeps its own revision, version, scenarios and record order: only the
	// closing unit sentence and the docket marker are version-gated.
	v12, err := universe.GenerateV12Programs(41, 40)
	if err != nil {
		t.Fatal(err)
	}
	if len(v12) != len(a) {
		t.Fatalf("v12/v13 case counts differ: %d vs %d", len(v12), len(a))
	}
	for i := range v12 {
		if v12[i].Provenance.Revision != universe.V12ProvenanceRevision {
			t.Fatalf("v12 case %s carries revision %q", v12[i].Plan.Case.ID, v12[i].Provenance.Revision)
		}
		if v12[i].Plan.Case.BenchVersion != protocol.BenchVersionV12 {
			t.Fatalf("v12 case %s carries bench version %d", v12[i].Plan.Case.ID, v12[i].Plan.Case.BenchVersion)
		}
		if v12[i].Plan.Case.ID != a[i].Plan.Case.ID {
			t.Fatalf("v13 changed the case identity at index %d: %s vs %s", i, v12[i].Plan.Case.ID, a[i].Plan.Case.ID)
		}
		if v12[i].Plan.Case.ExpectedAnswer != a[i].Plan.Case.ExpectedAnswer {
			t.Fatalf("v13 changed the graded answer at index %d: %s vs %s",
				i, v12[i].Plan.Case.ExpectedAnswer, a[i].Plan.Case.ExpectedAnswer)
		}
		if !reflect.DeepEqual(v12[i].Provenance.Program, a[i].Provenance.Program) {
			t.Fatalf("v13 changed the program at index %d", i)
		}
	}
}

// TestV13DatasetKeepsV11AndV12Bytes is the no-silent-history guarantee at the
// dataset level.
func TestV13DatasetKeepsV11AndV12Bytes(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV12)
	for _, version := range []int{protocol.BenchVersionV11, protocol.BenchVersionV12, protocol.BenchVersionV13} {
		a, err := GenerateDataset(123456789, prof, version)
		if err != nil {
			t.Fatalf("v%d: %v", version, err)
		}
		b, err := GenerateDataset(123456789, prof, version)
		if err != nil {
			t.Fatalf("v%d: %v", version, err)
		}
		if !reflect.DeepEqual(a, b) {
			t.Fatalf("v%d generation is not deterministic", version)
		}
	}
	v12, err := GenerateDataset(123456789, prof, protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range v12.MemoryCases {
		if c.V10Provenance == nil {
			continue
		}
		if c.V10Provenance.Revision != universe.V12ProvenanceRevision {
			t.Fatalf("v12 dataset case %s carries revision %q", c.ID, c.V10Provenance.Revision)
		}
		if strings.Contains(c.Question, "docket") {
			t.Fatalf("v12 dataset case %s leaked the v13 docket selector: %s", c.ID, c.Question)
		}
	}
	v13, err := GenerateDataset(123456789, prof, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	programs := 0
	for _, c := range v13.MemoryCases {
		if c.V10Provenance == nil {
			continue
		}
		programs++
		if c.V10Provenance.Revision != universe.V13ProvenanceRevision {
			t.Fatalf("v13 dataset case %s carries revision %q", c.ID, c.V10Provenance.Revision)
		}
	}
	if programs != 40 {
		t.Fatalf("v13 dataset carried %d open programs, want 40", programs)
	}
}

// TestLifecycleWriteCategoryUnreachableFromV8 backs the scorer-side repair: the
// category the memory over-call factor excludes is not generated from v8 on, so
// the exclusion protected nothing while the live declarative-acknowledgement
// category went unprotected.
func TestLifecycleWriteCategoryUnreachableFromV8(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV12)
	for _, version := range []int{protocol.BenchVersionV8, protocol.BenchVersionV12, protocol.BenchVersionV13} {
		artifact, err := GenerateDataset(123456789, prof, version)
		if err != nil {
			t.Fatalf("v%d: %v", version, err)
		}
		ack := 0
		for _, c := range artifact.MemoryCases {
			if c.QuestionType == QTLifecycleWrite {
				t.Fatalf("v%d generated a %s case; the scorer's exclusion assumes it cannot", version, QTLifecycleWrite)
			}
			if c.QuestionType == QTDeclarativeAck {
				ack++
			}
		}
		if ack == 0 {
			t.Fatalf("v%d generated no %s case, so the live write-shaped category vanished", version, QTDeclarativeAck)
		}
	}
}
