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

// moneyMarkers are the surfaces a monetary case or record would carry. v13
// programs must carry none of them.
var moneyMarkers = []string{"$", "€", "£", " cents", "minor unit", "USD", "CAD", "EUR", "GBP", "invoice", "paid", "balance"}

func assertNoMoney(t *testing.T, label string, cases []StagedCase, pairs []protocol.MemoryPair) {
	t.Helper()
	for _, sc := range cases {
		if sc.Case.AnswerKind == protocol.AnswerMoney {
			t.Fatalf("%s: case %s grades money", label, sc.Case.ID)
		}
		for _, kind := range sc.Case.AnswerItemKinds {
			if kind == protocol.AnswerMoney {
				t.Fatalf("%s: case %s carries a money list item", label, sc.Case.ID)
			}
		}
		for _, marker := range moneyMarkers {
			if strings.Contains(sc.Case.Question, marker) {
				t.Fatalf("%s: question carries money marker %q: %s", label, marker, sc.Case.Question)
			}
		}
	}
	for _, p := range pairs {
		for _, marker := range moneyMarkers {
			if strings.Contains(p.Prompt, marker) {
				t.Fatalf("%s: record carries money marker %q: %s", label, marker, p.Prompt)
			}
		}
	}
}

// TestV13ContractProgramsCoverEveryFamilyWithZeroMoney: 28 cases per full
// seed, seven complete metamorphic groups, every family exactly once, no
// monetary group, and a typed claim spec on every member.
func TestV13ContractProgramsCoverEveryFamilyWithZeroMoney(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		cases, pairs, err := V13BusinessProgramCases(seed, v13ProgramCaseCount(225))
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if len(cases) != 28 {
			t.Fatalf("seed %d: %d cases, want 28", seed, len(cases))
		}
		assertNoMoney(t, "business", cases, pairs)
		groups := map[string][]StagedCase{}
		for i, sc := range cases {
			groups[sc.V10Provenance.MetamorphicGroup] = append(groups[sc.V10Provenance.MetamorphicGroup], sc)
			if len(sc.Case.Claims) == 0 {
				t.Fatalf("seed %d case %s has no claims", seed, sc.Case.ID)
			}
			weight := 0.0
			for _, c := range sc.Case.Claims {
				weight += c.Weight
				if c.Kind == "" || c.Expected == "" {
					t.Fatalf("seed %d case %s has a malformed claim %+v", seed, sc.Case.ID, c)
				}
			}
			if weight < 0.999 || weight > 1.001 {
				t.Fatalf("seed %d case %s claim weights sum to %.3f", seed, sc.Case.ID, weight)
			}
			if sc.Case.QuestionType != universe.V13ProgramQuestionType {
				t.Fatalf("seed %d case %d question type %q", seed, i, sc.Case.QuestionType)
			}
		}
		if len(groups) != 7 {
			t.Fatalf("seed %d: %d groups, want 7", seed, len(groups))
		}
		seen := map[universe.V13Family]bool{}
		for i := 0; i < len(cases); i += 4 {
			seen[universe.V13FamilyOf(i)] = true
		}
		if len(seen) != len(universe.V13Families) {
			t.Fatalf("seed %d: families covered %v", seed, seen)
		}
	}
}

// TestV13ContractMetamorphicRelations: within every group the base, renderer
// invariant, and distractor invariant share one answer and one TwinGroup; the
// causal counterfactual changes the answer and carries no TwinGroup; the
// renderer invariant uses a different renderer; the distractor invariant's
// evidence is a superset carrying the decoy.
func TestV13ContractMetamorphicRelations(t *testing.T) {
	check := func(t *testing.T, label string, cases []StagedCase) {
		t.Helper()
		for g := 0; g+3 < len(cases); g += 4 {
			base, rend, dist, cf := cases[g], cases[g+1], cases[g+2], cases[g+3]
			want := []string{protocol.RelationBase, protocol.RelationRendererInvariant, protocol.RelationDistractorInvariant, protocol.RelationCausalCounterfactual}
			for i, sc := range []StagedCase{base, rend, dist, cf} {
				if sc.V10Provenance == nil || sc.V10Provenance.Relation != want[i] {
					t.Fatalf("%s group %d member %d relation %+v, want %s", label, g/4, i, sc.V10Provenance, want[i])
				}
			}
			same := func(a, b StagedCase) bool {
				return a.Case.ExpectedAnswer == b.Case.ExpectedAnswer && strings.Join(a.Case.AnswerItems, "|") == strings.Join(b.Case.AnswerItems, "|")
			}
			if !same(base, rend) || !same(base, dist) {
				t.Fatalf("%s group %d: invariant members disagree on the answer", label, g/4)
			}
			if same(base, cf) {
				t.Fatalf("%s group %d: counterfactual kept the answer %q", label, g/4, base.Case.ExpectedAnswer)
			}
			if base.Case.TwinGroup == "" || base.Case.TwinGroup != rend.Case.TwinGroup || base.Case.TwinGroup != dist.Case.TwinGroup || cf.Case.TwinGroup != "" {
				t.Fatalf("%s group %d: twin groups %q/%q/%q/%q", label, g/4, base.Case.TwinGroup, rend.Case.TwinGroup, dist.Case.TwinGroup, cf.Case.TwinGroup)
			}
			if base.V10Provenance.Renderer == rend.V10Provenance.Renderer {
				t.Fatalf("%s group %d: renderer invariant reused renderer %q", label, g/4, base.V10Provenance.Renderer)
			}
			if len(dist.Case.DistractorAnswers) < len(base.Case.DistractorAnswers) {
				t.Fatalf("%s group %d: distractor invariant lost distractors", label, g/4)
			}
		}
	}
	for seed := int64(1); seed <= 25; seed++ {
		business, _, err := V13BusinessProgramCases(seed, 28)
		if err != nil {
			t.Fatal(err)
		}
		check(t, "business", business)
		personal, _, err := GenerateV13PersonalPrograms(seed, 24)
		if err != nil {
			t.Fatal(err)
		}
		check(t, "personal", personal)
	}
}

// TestV13ContractOracleSynonymsAndDistractorsGrade is the grader proof: the
// canonical answer scores 1, every accepted equivalent (the seed's status alias
// term, a plain-English synonym, a date or time rendering) scores 1, and the
// planted decoy scores 0 — while records-disagree, an exempt claim set, still
// credits a response that names the decoy alongside both recorded values.
func TestV13ContractOracleSynonymsAndDistractorsGrade(t *testing.T) {
	for seed := int64(1); seed <= 25; seed++ {
		business, _, err := V13BusinessProgramCases(seed, 28)
		if err != nil {
			t.Fatal(err)
		}
		personal, _, err := GenerateV13PersonalPrograms(seed, 24)
		if err != nil {
			t.Fatal(err)
		}
		for _, sc := range append(business, personal...) {
			mc := sc.Case
			oracle := v13OracleResponse(mc)
			if v := grade.Memory(mc, oracle); v.Score != 1 {
				t.Fatalf("seed %d case %s (%s): oracle %q scored %.2f (%v)", seed, mc.ID, mc.QuestionType, oracle.Answer, v.Score, v.Notes)
			}
			for _, alt := range mc.AcceptAny {
				if v := grade.Memory(mc, protocol.RunResponse{Answer: alt, FinalText: "It is " + alt + "."}); v.Score != 1 {
					t.Fatalf("seed %d case %s: accepted form %q scored %.2f (%v)", seed, mc.ID, alt, v.Score, v.Notes)
				}
			}
			for _, d := range mc.DistractorAnswers {
				if v := grade.Memory(mc, protocol.RunResponse{Answer: d, FinalText: "It is " + d + "."}); v.Score != 0 {
					t.Fatalf("seed %d case %s: distractor %q scored %.2f (%v)", seed, mc.ID, d, v.Score, v.Notes)
				}
			}
			if mc.AnswerKind == protocol.AnswerList && len(mc.DistractorAnswers) == 0 {
				// records-disagree: exempt from distractor scanning.
				answer := strings.Join(mc.AnswerItems[:2], " and ") + " — the records conflict"
				if v := grade.Memory(mc, protocol.RunResponse{Answer: answer}); v.Score != 1 {
					t.Fatalf("seed %d case %s: conflict synonym scored %.2f (%v)", seed, mc.ID, v.Score, v.Notes)
				}
				if v := grade.Memory(mc, protocol.RunResponse{Answer: mc.AnswerItems[0]}); v.Score <= 0 || v.Score >= 1 {
					t.Fatalf("seed %d case %s: one value of a claim set scored %.2f, want partial", seed, mc.ID, v.Score)
				}
			}
		}
	}
}

// v13OracleResponse renders the answer an honest reader emits.
func v13OracleResponse(mc protocol.MemoryCase) protocol.RunResponse {
	if mc.AnswerKind == protocol.AnswerList {
		return protocol.RunResponse{Answer: strings.Join(mc.AnswerItems, "; ")}
	}
	return protocol.RunResponse{Answer: mc.ExpectedAnswer}
}

// TestV13ContractQuestionsBindSubjectByRemit: no business question names the
// workstream alias; the subject is bound by its remit through the binding
// record, and the question never contains the graded answer.
func TestV13ContractQuestionsBindSubjectByRemit(t *testing.T) {
	for seed := int64(1); seed <= 20; seed++ {
		generated, err := universe.GenerateV13Programs(seed, 28)
		if err != nil {
			t.Fatal(err)
		}
		for _, g := range generated {
			alias := g.Plan.Case.WritingProtected[0]
			q := strings.ToLower(g.Plan.Case.Question)
			if strings.Contains(q, strings.ToLower(alias)) {
				t.Fatalf("seed %d: question names the alias %q: %s", seed, alias, g.Plan.Case.Question)
			}
			if g.Plan.Case.AnswerKind != protocol.AnswerList && grade.Hit(g.Plan.Case.ExpectedAnswer, g.Plan.Case.Question) {
				t.Fatalf("seed %d: question leaks the answer %q: %s", seed, g.Plan.Case.ExpectedAnswer, g.Plan.Case.Question)
			}
			for _, banned := range []string{"base", "counterfactual", "distractor", "renderer", "metamorphic", "v13"} {
				if strings.Contains(q, banned) {
					t.Fatalf("seed %d: question carries generator marker %q", seed, banned)
				}
			}
			if strings.HasPrefix(g.Plan.Case.ID, "v13") || strings.Contains(g.Pairs[0].SessionID, "v13") {
				t.Fatalf("seed %d: ids are not opaque: %s / %s", seed, g.Plan.Case.ID, g.Pairs[0].SessionID)
			}
		}
	}
}

// TestV13ProgramClaimsNeverReachArtifactOrWire: Claims are tagged json:"-",
// so neither a marshalled v13 program MemoryCase nor its ArtifactCase (the
// hashed artifact row) carries a claims key, and a claims key on input is
// ignored.
func TestV13ProgramClaimsNeverReachArtifactOrWire(t *testing.T) {
	cases, _, err := V13BusinessProgramCases(7, 28)
	if err != nil {
		t.Fatal(err)
	}
	for _, sc := range cases {
		raw, err := json.Marshal(sc.Case)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(strings.ToLower(string(raw)), `"claims"`) {
			t.Fatalf("MemoryCase JSON carries claims: %s", raw)
		}
		artifact := ArtifactCase{MemoryCase: sc.Case, V10Provenance: sc.V10Provenance}
		raw, err = json.Marshal(artifact)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(strings.ToLower(string(raw)), `"claims"`) {
			t.Fatalf("ArtifactCase JSON carries claims: %s", raw)
		}
		var decoded map[string]any
		if err := json.Unmarshal(raw, &decoded); err != nil {
			t.Fatal(err)
		}
		if _, ok := decoded["claims"]; ok {
			t.Fatal("decoded artifact carries a claims key")
		}
	}
	var back protocol.MemoryCase
	if err := json.Unmarshal([]byte(`{"id":"x","claims":[{"Kind":"person","Expected":"a"}]}`), &back); err != nil {
		t.Fatal(err)
	}
	if len(back.Claims) != 0 {
		t.Fatalf("claims key was decoded from the wire: %+v", back.Claims)
	}
}

// TestV13ContractStagingIsDeterministicAndOpaque: same seed, same staged
// cases and pairs; wave pairs carry the evidence every case requires.
func TestV13ContractStagingIsDeterministicAndOpaque(t *testing.T) {
	for _, seed := range []int64{1, 42, 123456789} {
		a, ap, err := V13BusinessProgramCases(seed, 28)
		if err != nil {
			t.Fatal(err)
		}
		b, bp, err := V13BusinessProgramCases(seed, 28)
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(a, b) || !reflect.DeepEqual(ap, bp) {
			t.Fatalf("seed %d: staging not deterministic", seed)
		}
		ids := map[string]bool{}
		for _, p := range ap {
			ids[p.PairID] = true
		}
		for _, sc := range a {
			for _, id := range sc.RequiredPairIDs {
				if !ids[id] {
					t.Fatalf("seed %d case %s requires pair %s that is not seeded", seed, sc.Case.ID, id)
				}
			}
		}
	}
	if _, _, err := V13BusinessProgramCases(1, 6); err == nil {
		t.Fatal("a non-multiple-of-four count was accepted")
	}
}

// TestV13ClaimCriticalityMarksOnlyTheLoadBearingClaim pins the spec the v13
// grader consumes: on every case from every v13 family generator, either
// exactly one claim is Critical and it carries the whole case weight, or no
// claim is Critical and the partial weights sum to one. A partial-weight claim
// marked Critical would let the grader zero a case the pinned partial-credit
// vectors (direction 0.5, half a set 0.5, one of two disagree names) score
// positive.
func TestV13ClaimCriticalityMarksOnlyTheLoadBearingClaim(t *testing.T) {
	for seed := int64(1); seed <= 20; seed++ {
		var cases []protocol.MemoryCase
		business, _, err := V13BusinessProgramCases(seed, 28)
		if err != nil {
			t.Fatal(err)
		}
		personal, _, err := GenerateV13PersonalPrograms(seed, 24)
		if err != nil {
			t.Fatal(err)
		}
		injection, err := BuildV13WorldInjection(seed, v13InjectionWorld(seed))
		if err != nil {
			t.Fatal(err)
		}
		for _, group := range [][]StagedCase{business, personal, injection.Cases} {
			for _, sc := range group {
				cases = append(cases, sc.Case)
			}
		}
		for _, fc := range BuildFamilyCompilerV13(seed, 16) {
			cases = append(cases, fc.Staged.Case)
		}
		if len(cases) != 28+24+V13WorldInjectionCaseCount+16 {
			t.Fatalf("seed %d: %d cases collected", seed, len(cases))
		}
		for _, mc := range cases {
			if len(mc.Claims) == 0 {
				t.Fatalf("seed %d case %s (%s) has no claims", seed, mc.ID, mc.QuestionType)
			}
			critical, weight := 0, 0.0
			for _, c := range mc.Claims {
				weight += c.Weight
				if c.Weight <= 0 || c.Weight > 1 {
					t.Fatalf("seed %d case %s claim %+v has weight outside (0, 1]", seed, mc.ID, c)
				}
				if c.Critical {
					critical++
					if c.Weight < 1 {
						t.Fatalf("seed %d case %s (%s): partial-weight claim marked critical: %+v", seed, mc.ID, mc.QuestionType, c)
					}
				}
			}
			if weight < 0.999 || weight > 1.001 {
				t.Fatalf("seed %d case %s claim weights sum to %.3f", seed, mc.ID, weight)
			}
			switch {
			case critical == 1 && len(mc.Claims) == 1:
			case critical == 0 && len(mc.Claims) > 1:
			default:
				t.Fatalf("seed %d case %s (%s): %d critical of %d claims", seed, mc.ID, mc.QuestionType, critical, len(mc.Claims))
			}
		}
	}
}
