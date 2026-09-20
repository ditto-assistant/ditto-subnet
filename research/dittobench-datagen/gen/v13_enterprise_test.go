package gen

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestV13EnterpriseScoredGroups(t *testing.T) {
	formats := map[string]bool{}
	ops := map[string]bool{}
	for seed := int64(-3); seed < 15; seed++ {
		cases, err := universe.GenerateV13EnterprisePrograms(seed, 12)
		if err != nil {
			t.Fatal(err)
		}
		again, err := universe.GenerateV13EnterprisePrograms(seed, 12)
		if err != nil || !reflect.DeepEqual(cases, again) {
			t.Fatal("seed replay changed")
		}
		ids := map[string]bool{}
		for i, c := range cases {
			formats[string(c.Provenance.Renderer)] = true
			ops[c.Provenance.Program.Op] = true
			for _, p := range c.Pairs {
				if ids[p.PairID] {
					t.Fatal("evidence identity reused across scopes")
				}
				ids[p.PairID] = true
				if strings.Contains(p.Prompt, c.Provenance.MetamorphicGroup) || strings.Contains(p.Prompt, "counterfactual") {
					t.Fatal("trusted relation leaked to evidence")
				}
			}
			mc := c.Plan.Case
			for _, answer := range []string{mc.ExpectedAnswer, "The answer is " + mc.ExpectedAnswer + "."} {
				if got := grade.Memory(mc, protocol.RunResponse{Answer: answer}); got.Score != 1 {
					t.Fatalf("honest answer %q rejected: %+v", answer, got)
				}
			}
			if got := grade.Memory(mc, protocol.RunResponse{Answer: "unsupported"}); got.Score != 0 {
				t.Fatal("wrong answer accepted")
			}
			if got := grade.Memory(mc, protocol.RunResponse{Answer: "The answer is not " + mc.ExpectedAnswer + "."}); got.Score != 0 {
				t.Fatalf("negated answer accepted: %s %+v", mc.ExpectedAnswer, got)
			}
			base := cases[(i/4)*4].Plan.Case
			if i%4 == 3 {
				if mc.ExpectedAnswer == base.ExpectedAnswer || mc.TwinGroup != "" {
					t.Fatal("ineffective counterfactual")
				}
				if got := grade.Memory(mc, protocol.RunResponse{Answer: base.ExpectedAnswer}); got.Score != 0 {
					t.Fatal("stale answer accepted")
				}
			} else if mc.ExpectedAnswer != base.ExpectedAnswer || mc.TwinGroup != base.TwinGroup {
				t.Fatal("invariance broken")
			}
		}
	}
	if len(formats) != 6 || len(ops) != 3 {
		t.Fatalf("coverage: formats=%v ops=%v", formats, ops)
	}
	for _, count := range []int{-1, 0, 1, 5, 16} {
		if _, err := universe.GenerateV13EnterprisePrograms(1, count); err == nil {
			t.Fatalf("accepted count %d", count)
		}
	}
}

type enterpriseDestructiveTranslation struct{}

func (enterpriseDestructiveTranslation) Translate(int64, uint64, string, string) string {
	return "corrupted"
}

func TestV13EnterpriseArtifactPreservesTypedSurfaces(t *testing.T) {
	for _, runSize := range []string{"small", "medium", "full"} {
		prof, _ := ProfileForVersion(runSize, 13)
		rng, _ := NewRNGForVersion(41, 13)
		suite, err := GenerateMemorySuiteForVersion(rng, 41, prof.Mem, prof.Waves, prof.RawPairsFrac, 13)
		if err != nil {
			t.Fatal(err)
		}
		a, err := BuildArtifactForVersionWithSurface(41, 13, nil, suite.Cases, suite.Waves,
			SurfaceOptions{Salt: 17, Translation: enterpriseDestructiveTranslation{}})
		if err != nil {
			t.Fatal(err)
		}
		original := map[string]protocol.MemoryPair{}
		for _, wave := range suite.Waves {
			for _, p := range wave.Pairs {
				original[p.PairID] = p
			}
		}
		protected := map[string]bool{}
		count := 0
		for i, c := range a.MemoryCases {
			if c.QuestionType != universe.V13EnterpriseQuestionType {
				continue
			}
			count++
			if c.Question != suite.Cases[i].Case.Question {
				t.Fatal("typed question rewritten")
			}
			for _, id := range c.V10EvidencePairIDs {
				protected[id] = true
			}
		}
		want := 4
		if runSize == "full" {
			want = 12
		}
		if count != want {
			t.Fatalf("%s: %d enterprise cases, want %d", runSize, count, want)
		}
		seen := map[string]bool{}
		for _, wave := range a.MemoryWaves {
			for _, p := range wave.Pairs {
				if !protected[p.PairID] {
					continue
				}
				seen[p.PairID] = true
				if p.Prompt != original[p.PairID].Prompt || p.Response != original[p.PairID].Response {
					t.Fatal("structured evidence rewritten")
				}
			}
		}
		if len(seen) != len(protected) {
			t.Fatal("missing evidence")
		}
		b, _ := json.Marshal(a)
		if !strings.Contains(string(b), universe.V13EnterpriseRevision) {
			t.Fatal("artifact lacks generation identity")
		}
	}
}
