package gen

import (
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestV12GenerationIsExplicitAndNotActivated(t *testing.T) {
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatalf("v12 scaffold changed active version to %d", protocol.CurrentBenchVersion)
	}
	if !protocol.SupportedBenchVersion(protocol.BenchVersionV12) {
		t.Fatal("v12 deterministic generation is not supported")
	}
	for _, runSize := range []string{"small", "medium", "full"} {
		v10, _ := ProfileForVersion(runSize, protocol.BenchVersionV10)
		v12, ok := ProfileForVersion(runSize, protocol.BenchVersionV12)
		if !ok || v10 != v12 {
			t.Errorf("v12 %s profile=(%+v,%v), want the v10 envelope %+v", runSize, v12, ok, v10)
		}
	}
}

func TestV12ProgramsSampleShapesAndRegenerateExactly(t *testing.T) {
	a, err := universe.GenerateV12Programs(41, 40)
	if err != nil {
		t.Fatal(err)
	}
	b, err := universe.GenerateV12Programs(41, 40)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatal("same v12 seed did not regenerate identical programs")
	}
	ops := map[string]bool{}
	for _, generated := range a {
		collectOps(generated.Provenance.Program, ops)
		if generated.Provenance.Revision != universe.V12ProvenanceRevision {
			t.Fatalf("case %s carries revision %q", generated.Plan.Case.ID, generated.Provenance.Revision)
		}
	}
	if !ops["subtract"] || (!ops["add"] && !ops["max"]) {
		t.Fatalf("v12 groups did not sample varied program shapes: %v", ops)
	}
}

func TestV12AnswersPositiveAndDistractorFree(t *testing.T) {
	for seed := int64(1); seed <= 25; seed++ {
		generated, err := universe.GenerateV12Programs(seed, 40)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		for _, g := range generated {
			answer, err := strconv.Atoi(g.Plan.Case.ExpectedAnswer)
			if err != nil || answer <= 0 {
				t.Fatalf("seed %d case %s expected answer %q", seed, g.Plan.Case.ID, g.Plan.Case.ExpectedAnswer)
			}
			for _, d := range g.Plan.Case.DistractorAnswers {
				if d == g.Plan.Case.ExpectedAnswer {
					t.Fatalf("seed %d case %s distractor collides with answer %q", seed, g.Plan.Case.ID, d)
				}
			}
		}
	}
}

// TestV12SubstrateNotPositionallyBindable is the core Gap-1 proof: no record
// exposes a `label=amount` ledger row, the record order is genuinely shuffled
// per seed, and every question binds the subject descriptively (never by its
// alias).
func TestV12SubstrateNotPositionallyBindable(t *testing.T) {
	generated, err := universe.GenerateV12Programs(123456789, 40)
	if err != nil {
		t.Fatal(err)
	}
	// No `=` in any record is immediately followed by a number: the monetary
	// KV ledger a template-fitter parsed in v11 no longer exists.
	kvAmount := regexp.MustCompile(`=\s*-?\d`)
	bindingSlots := map[int]bool{}
	descriptorHit := 0
	for _, g := range generated {
		alias := g.Plan.Case.WritingProtected[0]
		q := g.Plan.Case.Question
		if strings.Contains(q, alias) {
			t.Fatalf("v12 question names the subject alias %q instead of binding descriptively: %s", alias, q)
		}
		if strings.Contains(q, "workstream") || strings.Contains(q, "entry") || strings.Contains(q, "account") {
			descriptorHit++
		}
		for slot, pair := range g.Pairs {
			body := pair.Prompt + "\n" + pair.Response
			if kvAmount.MatchString(body) {
				t.Fatalf("v12 record exposes a key=amount ledger token (positionally bindable): %q", body)
			}
			// The alias-binding record is the only one carrying `=`; record its
			// slot so we can prove the order is shuffled across groups.
			if strings.Contains(pair.Prompt, alias+"=") || strings.Contains(pair.Prompt, "="+alias) {
				bindingSlots[slot] = true
			}
		}
	}
	if descriptorHit != len(generated) {
		t.Fatalf("v12 descriptive binding not universal: %d of %d questions used a role descriptor", descriptorHit, len(generated))
	}
	if len(bindingSlots) < 2 {
		t.Fatalf("v12 record order is not shuffled: the binding record only ever landed in slot(s) %v", bindingSlots)
	}
}

// TestV12DropsV11FormatTells proves Gap 2: no record leaks the program shape
// through a `%+d` sign or a `->` correction token.
func TestV12DropsV11FormatTells(t *testing.T) {
	for seed := int64(1); seed <= 20; seed++ {
		generated, err := universe.GenerateV12Programs(seed, 40)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		for _, g := range generated {
			for _, pair := range g.Pairs {
				body := pair.Prompt + "\n" + pair.Response
				if strings.Contains(body, "->") {
					t.Fatalf("seed %d: v12 record kept the latest-correction `->` tell: %q", seed, body)
				}
				if strings.Contains(body, "=+") || strings.Contains(body, "; +") {
					t.Fatalf("seed %d: v12 record kept a signed `%%+d` adjustment tell: %q", seed, body)
				}
			}
		}
	}
}

func TestV12DatasetRewritesMarkersAndKeepsV11Frozen(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV12)
	v12, err := GenerateDataset(123456789, prof, protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	v12b, err := GenerateDataset(123456789, prof, protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(v12, v12b) {
		t.Fatal("v12 generation is not deterministic")
	}

	v11a, err := GenerateDataset(123456789, prof, protocol.BenchVersionV11)
	if err != nil {
		t.Fatal(err)
	}
	v11b, err := GenerateDataset(123456789, prof, protocol.BenchVersionV11)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(v11a, v11b) {
		t.Fatal("v11 generation stopped being deterministic")
	}

	// v12 memory cases still bind provenance and complete metamorphic groups.
	groups := map[string]int{}
	v12Cases := 0
	for _, c := range v12.MemoryCases {
		if c.V10Provenance == nil {
			continue
		}
		v12Cases++
		groups[c.V10Provenance.MetamorphicGroup]++
		if c.V10Provenance.Revision != universe.V12ProvenanceRevision {
			t.Fatalf("case %s revision %q", c.ID, c.V10Provenance.Revision)
		}
	}
	if v12Cases != 40 || len(groups) != 10 {
		t.Fatalf("v12 program coverage cases/groups=%d/%d, want 40/10", v12Cases, len(groups))
	}

	// The surface pass rewrote the fixed stored-directive marker, and it did so
	// compositionally: the v11 variant bank no longer covers the replacement.
	v11Variants := map[string]bool{
		"[MIRROR]": true, "((state-sync))": true, "[[refresh]]": true, "sync//note:": true, "[RECONCILE]": true,
	}
	for _, wave := range v12.MemoryWaves {
		for _, pair := range wave.Pairs {
			if strings.Contains(pair.Prompt, "[SYNC]") {
				t.Fatalf("v12 kept the fixed [SYNC] injection marker: %s", pair.Prompt)
			}
			for variant := range v11Variants {
				if strings.Contains(pair.Prompt, variant) {
					t.Fatalf("v12 reused a finite v11 marker variant %q: %s", variant, pair.Prompt)
				}
			}
		}
	}
}
