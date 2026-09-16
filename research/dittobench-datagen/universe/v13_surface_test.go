package universe

import (
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV13WorldBelowV13IsByteIdentical pins that GenerateForVersion renders the
// exact Generate world for every earlier contract.
func TestV13WorldBelowV13IsByteIdentical(t *testing.T) {
	for _, seed := range []int64{1, 7, 123456789} {
		plain := Generate(seed, 3)
		for _, version := range []int{protocol.BenchVersionV8, protocol.BenchVersionV12} {
			versioned := GenerateForVersion(seed, 3, version)
			versioned.BenchVersion = 0
			if !reflect.DeepEqual(plain, versioned) {
				t.Fatalf("seed %d v%d world differs from the unversioned world", seed, version)
			}
		}
		if reflect.DeepEqual(plain.Pairs, GenerateForVersion(seed, 3, protocol.BenchVersionV13).Pairs) {
			t.Fatalf("seed %d v13 world rendered the v12 pairs", seed)
		}
	}
}

// TestV13ProjectIdentityPermutesClausesAndOmitsVendor checks the identity
// record grammar: every record still binds alias, formal name, client, owner
// and AP record; the vendor clause is omitted on a share of records; clause
// order varies; and the alias/formal equivalence is stated in more than one way.
func TestV13ProjectIdentityPermutesClausesAndOmitsVendor(t *testing.T) {
	records, withoutVendor := 0, 0
	openers := map[string]bool{}
	equivalence := map[string]bool{}
	for seed := int64(1); seed <= 20; seed++ {
		w := GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		byID := map[string]protocol.MemoryPair{}
		for _, pair := range w.Pairs {
			byID[pair.PairID] = pair
		}
		for _, p := range w.Projects {
			lead := w.People[p.Lead]
			prompt := byID[p.ContextPairID].Prompt
			records++
			for _, want := range []string{"“" + p.Alias + "”", p.Name, p.Client, lead.Name, p.RecordID} {
				if !strings.Contains(prompt, want) {
					t.Fatalf("seed %d project %q identity record lost %q: %q", seed, p.Alias, want, prompt)
				}
			}
			if !strings.Contains(prompt, p.Vendor) {
				withoutVendor++
			}
			openers[strings.Fields(prompt)[0]] = true
			switch {
			case strings.Contains(prompt, "When I say"):
				equivalence["when-i-say"] = true
			case strings.Contains(prompt, "one and the same"), strings.Contains(prompt, "are the same project"):
				equivalence["same"] = true
			case strings.Contains(prompt, "shorthand"), strings.Contains(prompt, "Internally we call"):
				equivalence["shorthand"] = true
			}
		}
		for i, p := range w.People {
			prompt := byID[p.IdentityPairID].Prompt
			for _, want := range []string{p.Name, p.Relation, "“" + p.Nickname} {
				if !strings.Contains(prompt, want) {
					t.Fatalf("seed %d person %d identity record lost %q: %q", seed, i, want, prompt)
				}
			}
		}
	}
	if withoutVendor == 0 || withoutVendor == records {
		t.Fatalf("vendor clause omitted on %d/%d records, want a proper share", withoutVendor, records)
	}
	if len(openers) < 6 || len(equivalence) < 3 {
		t.Fatalf("identity records barely vary: openers=%d equivalence=%v", len(openers), equivalence)
	}
}

// TestV13QuestionPlansValidateAndVaryFrames generates the full v13 question set
// for many seeds (validatePlan proves each frame answerable, leak-free and
// constraint-complete) and checks every oracle renders several distinct frames.
func TestV13QuestionPlansValidateAndVaryFrames(t *testing.T) {
	frames := map[string]map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		w := GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		plans, err := w.QuestionPlans(153)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		for _, plan := range plans {
			kind := plan.oracleKind
			if frames[kind] == nil {
				frames[kind] = map[string]bool{}
			}
			frame := plan.Case.Question
			for _, constraint := range plan.Constraints {
				frame = strings.ReplaceAll(frame, constraint, "_")
			}
			frames[kind][frame] = true
			if got := renderedConstraintCount(plan); got < 2 {
				t.Fatalf("seed %d %s rendered %d constraints: %q", seed, kind, got, plan.Case.Question)
			}
		}
	}
	for _, kind := range []string{
		oracleContactCurrent, oracleContactPrevious, oracleProjectOutstanding, oracleProjectLeadCurrent,
		oracleProjectLeadPrevious, oracleTripCurrent, oracleTripChangedLegPrevious, oracleTripChangedLegCurrent,
		oracleTripLongestCurrent, oracleStoryBalanceCurrent, oracleStoryBudgetDelta, oracleStoryPostApproval,
		oracleStoryLaterNetChange, oracleStoryContactCurrent, oracleStoryLesson, oracleStoryOutcomeSummary,
	} {
		if len(frames[kind]) < 5 {
			t.Errorf("v13 oracle %s rendered only %d distinct frames across 40 seeds", kind, len(frames[kind]))
		}
	}
}

// TestV13StoryNoiseUsesTypoV2 checks the story compiler switched projectors:
// fact values survive, and the rendering differs from the v12 one.
func TestV13StoryNoiseUsesTypoV2(t *testing.T) {
	for seed := int64(1); seed <= 10; seed++ {
		v12 := GenerateForVersion(seed, 3, protocol.BenchVersionV12)
		v13 := GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		if len(v12.Stories) != len(v13.Stories) {
			t.Fatalf("seed %d story count moved", seed)
		}
		byID := map[string]protocol.MemoryPair{}
		for _, pair := range v13.Pairs {
			byID[pair.PairID] = pair
		}
		v12ByID := map[string]protocol.MemoryPair{}
		for _, pair := range v12.Pairs {
			v12ByID[pair.PairID] = pair
		}
		changed := 0
		for _, story := range v13.Stories {
			prompt := byID[story.PairID].Prompt
			for _, fact := range story.Facts {
				if !strings.Contains(prompt, fact.Value) {
					t.Fatalf("seed %d story %s lost fact %q", seed, story.ID, fact.Value)
				}
			}
			if prompt != v12ByID[story.PairID].Prompt {
				changed++
			}
		}
		if changed == 0 {
			t.Fatalf("seed %d v13 stories are byte-identical to v12", seed)
		}
	}
}
