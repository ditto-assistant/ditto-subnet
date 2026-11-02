package universe

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV13CounterfactualRecordsHaveUnambiguousScopes(t *testing.T) {
	for _, domain := range []string{"business", "personal"} {
		for seed := int64(1); seed <= 40; seed++ {
			build := GenerateV13Programs
			if domain == "personal" {
				build = GenerateV13PersonalPrograms
			}
			cases, err := build(seed, 28)
			if err != nil {
				t.Fatal(err)
			}
			seen := map[string]bool{}
			for group := 0; group < len(cases)/4; group++ {
				baseScope := v13ProgramRecordScope(seed, domain, group, protocol.RelationBase)
				counterScope := v13ProgramRecordScope(seed, domain, group, protocol.RelationCausalCounterfactual)
				if baseScope == counterScope || seen[baseScope] || seen[counterScope] {
					t.Fatal("record scope collision")
				}
				seen[baseScope], seen[counterScope] = true, true
				for variant := 0; variant < 4; variant++ {
					c := cases[group*4+variant]
					scope, excluded := baseScope, counterScope
					if variant == 3 {
						scope, excluded = counterScope, baseScope
					}
					if !strings.HasPrefix(c.Plan.Case.Question, "Use only case file "+scope+".") || strings.Contains(c.Plan.Case.Question, excluded) {
						t.Fatalf("%s seed %d: query selects wrong evidence", domain, seed)
					}
					for _, pair := range c.Pairs {
						if !strings.Contains(pair.Prompt, "Case file "+scope+":") || strings.Contains(pair.Prompt, excluded) {
							t.Fatalf("%s seed %d: unscoped or mixed evidence", domain, seed)
						}
					}
					protected := false
					for _, value := range c.Plan.Case.WritingProtected {
						protected = protected || value == scope
					}
					if !protected {
						t.Fatal("scope may be corrupted by surface pass")
					}
				}
				if cases[group*4].Plan.Case.ExpectedAnswer == cases[group*4+3].Plan.Case.ExpectedAnswer {
					t.Fatal("counterfactual answer did not change")
				}
			}
		}
	}
}
