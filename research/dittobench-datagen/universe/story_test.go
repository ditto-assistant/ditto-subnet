package universe

import (
	"fmt"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestStoryProfilesHaveFixedLargeMemoryEnvelope(t *testing.T) {
	for _, tc := range []struct {
		scale   int
		stories int
	}{{1, 3}, {2, 18}, {3, 39}} {
		for seed := int64(1); seed <= 12; seed++ {
			w := Generate(seed, tc.scale)
			if len(w.Stories) != tc.stories {
				t.Fatalf("scale %d seed %d stories=%d, want %d", tc.scale, seed, len(w.Stories), tc.stories)
			}
			hasRealCents := false
			for _, arc := range w.StoryArcs {
				hasRealCents = hasRealCents || arc.BaseBudgetCents%100 != 0 || arc.BudgetDeltaCents%100 != 0 || arc.PaidCents%100 != 0
			}
			if !hasRealCents {
				t.Fatalf("scale %d seed %d story finances collapsed to whole-dollar grid", tc.scale, seed)
			}
			pairs := storyPairMap(w)
			kinds := map[StoryKind]bool{}
			for _, story := range w.Stories {
				pair := pairs[story.PairID]
				size := len(pair.Prompt) + len(pair.Response)
				if size < 1_800 || size > 4_600 {
					t.Fatalf("scale %d seed %d story %s size=%d, want 1800..4600", tc.scale, seed, story.ID, size)
				}
				if len(story.Beginning.Events) < 2 || len(story.Middle.Events) < 5 || len(story.End.Events) < 2 {
					t.Fatalf("story %s lacks structured beginning/middle/end: %+v", story.ID, story)
				}
				if len(story.Themes) < 2 || len(story.LessonsLearned) == 0 {
					t.Fatalf("story %s lacks themes/lessons: %+v", story.ID, story)
				}
				if err := validateStoryStructure(story); err != nil {
					t.Fatalf("story %s has invalid structured state: %v", story.ID, err)
				}
				kinds[story.Kind] = true
			}
			if !kinds[StoryPersonal] || !kinds[StoryBusiness] {
				t.Fatalf("scale %d seed %d story kinds=%v, want personal+business", tc.scale, seed, kinds)
			}
		}
	}
}

func TestStoryCompilerIsLinearHumanAndNonRepeating(t *testing.T) {
	banned := []string{
		"later retelling", "a terse note", "worth a mention", "nothing urgent",
		"not something about you", "complete version", "chronology matters",
		"the phrase i carried", "what i remember most clearly", "i nearly left this out",
		"a practical problem slipped into the story", "everyone from that part of my life calls",
		"ref-", "dec-",
	}
	for seed := int64(1); seed <= 20; seed++ {
		w := Generate(seed, 3)
		for _, story := range w.Stories {
			draft, _ := story.renderDraft(seed)
			lower := strings.ToLower(draft)
			for _, phrase := range banned {
				if strings.Contains(lower, phrase) {
					t.Fatalf("seed %d story %s contains benchmark-like phrase %q", seed, story.ID, phrase)
				}
			}
			seenParagraph := map[string]bool{}
			for _, paragraph := range strings.Split(draft, "\n\n") {
				normalized := strings.Join(strings.Fields(strings.ToLower(paragraph)), " ")
				if seenParagraph[normalized] {
					t.Fatalf("seed %d story %s repeats paragraph %q", seed, story.ID, paragraph)
				}
				seenParagraph[normalized] = true
			}
			sections := map[string]StorySection{"beginning": story.Beginning, "middle": story.Middle, "end": story.End}
			for _, section := range sections {
				for _, event := range section.Events {
					if strings.Count(draft, event) != 1 {
						t.Fatalf("seed %d story %s event rendered %d times: %q", seed, story.ID, strings.Count(draft, event), event)
					}
				}
			}
			for _, fact := range story.Facts {
				section, ok := sections[fact.Phase]
				if !ok || fact.AfterEvent < 0 || fact.AfterEvent >= len(section.Events) {
					t.Fatalf("seed %d story %s fact %s has invalid placement %s/%d", seed, story.ID, fact.Key, fact.Phase, fact.AfterEvent)
				}
				if strings.Count(draft, fact.Value) != 1 {
					t.Fatalf("seed %d story %s fact %s rendered %d times", seed, story.ID, fact.Key, strings.Count(draft, fact.Value))
				}
			}
		}
	}
}

func TestStoryFactsAreInteriorAndExclusiveToLongMemories(t *testing.T) {
	for seed := int64(31); seed <= 40; seed++ {
		w := Generate(seed, 3)
		pairs := storyPairMap(w)
		storyIDs := map[string]bool{}
		for _, story := range w.Stories {
			storyIDs[story.PairID] = true
		}
		for _, story := range w.Stories {
			pair := pairs[story.PairID]
			for _, fact := range story.Facts {
				pos := strings.Index(pair.Prompt, fact.Value)
				if pos < 0 {
					t.Fatalf("seed %d story %s omits %s=%q", seed, story.ID, fact.Key, fact.Value)
				}
				ratio := float64(pos) / float64(len(pair.Prompt))
				if ratio < 0.15 || ratio > 0.85 {
					t.Fatalf("seed %d story %s fact %s at %.2f, want interior", seed, story.ID, fact.Key, ratio)
				}
				if strings.Contains(pair.Response, fact.Value) {
					t.Fatalf("seed %d story %s response duplicates %s=%q", seed, story.ID, fact.Key, fact.Value)
				}
				for _, other := range w.Pairs {
					if storyIDs[other.PairID] {
						continue
					}
					if strings.Contains(other.Prompt+" "+other.Response, fact.Value) {
						t.Fatalf("seed %d story-only fact %s=%q appears in short pair %s", seed, fact.Key, fact.Value, other.PairID)
					}
				}
			}
		}
	}
}

func TestStoryQuestionsRequireThreeLongMemoriesAcrossDomains(t *testing.T) {
	w := Generate(123456789, 3)
	pairs := storyPairMap(w)
	plans, err := w.QuestionPlans(190)
	if err != nil {
		t.Fatal(err)
	}
	storyCount := 0
	for _, plan := range plans {
		if !isStoryOracle(plan.oracleKind) {
			continue
		}
		storyCount++
		if len(plan.RequiredPairIDs) != 3 {
			t.Fatalf("story plan %s evidence=%d, want 3", plan.Case.ID, len(plan.RequiredPairIDs))
		}
		kinds := map[StoryKind]bool{}
		available := map[string]bool{}
		for _, id := range plan.RequiredPairIDs {
			available[id] = true
			story, ok := storyByPair(w, id)
			if !ok {
				t.Fatalf("story plan %s uses non-story evidence %s", plan.Case.ID, id)
			}
			kinds[story.Kind] = true
			pair := pairs[id]
			if len(pair.Prompt)+len(pair.Response) < 1_800 {
				t.Fatalf("story plan %s evidence %s is not large", plan.Case.ID, id)
			}
		}
		if !kinds[StoryPersonal] || !kinds[StoryBusiness] {
			t.Fatalf("story plan %s does not cross personal and business stories", plan.Case.ID)
		}
		for _, omitted := range plan.RequiredPairIDs {
			delete(available, omitted)
			if got, ok := w.resolveWithEvidence(plan, available); ok && got == plan.Case.ExpectedAnswer {
				t.Fatalf("story plan %s still resolves without %s", plan.Case.ID, omitted)
			}
			available[omitted] = true
		}
	}
	if storyCount != 91 {
		t.Fatalf("full story cases=%d, want fixed 91", storyCount)
	}
}

func TestStoryQuestionQuotaIsStableAcrossSeeds(t *testing.T) {
	for _, tc := range []struct {
		scale int
		count int
		want  int
	}{{2, 77, 42}, {3, 229, 91}} {
		for seed := int64(101); seed <= 120; seed++ {
			plans, err := Generate(seed, tc.scale).QuestionPlans(tc.count)
			if err != nil {
				t.Fatalf("scale %d seed %d: %v", tc.scale, seed, err)
			}
			got := 0
			for _, plan := range plans {
				if isStoryOracle(plan.oracleKind) {
					got++
				}
			}
			if got != tc.want {
				t.Fatalf("scale %d seed %d story cases=%d, want %d", tc.scale, seed, got, tc.want)
			}
		}
	}
}

func TestStoryProgramsAreInterpersonalComposedAndAnswerSafe(t *testing.T) {
	w := Generate(8128, 3)
	for _, plan := range w.storyQuestionCandidates() {
		arc := w.StoryArcs[plan.oracleIndex]
		person := w.People[arc.PersonIndex]
		for _, anchor := range plan.Constraints {
			if !strings.Contains(strings.ToLower(plan.Case.Question), strings.ToLower(anchor)) {
				t.Fatalf("story plan %s omits interpersonal anchor %q", plan.Case.ID, anchor)
			}
		}
		if !contains(plan.Constraints, person.Nickname) {
			t.Fatalf("story plan %s lacks the natural person anchor %q", plan.Case.ID, person.Nickname)
		}
		for _, hidden := range []string{arc.CaseID, arc.PurchaseOrder, arc.CurrentContact} {
			if strings.Contains(plan.Case.Question, hidden) {
				t.Fatalf("story plan %s leaks hidden value %q", plan.Case.ID, hidden)
			}
		}
		if len(plan.Facts) < 5 || len(plan.Constraints) != 2 || len(plan.Operations) < 4 {
			t.Fatalf("under-composed story plan %s: facts=%d constraints=%d operations=%d", plan.Case.ID, len(plan.Facts), len(plan.Constraints), len(plan.Operations))
		}
	}
}

func TestStoryBalanceIsComputedAndLessonAcceptsEquivalentPhrasing(t *testing.T) {
	w := Generate(44332211, 3)
	pairs := storyPairMap(w)
	seen := map[string]bool{}
	for _, plan := range w.storyQuestionCandidates() {
		seen[plan.oracleKind] = true
		arc := w.StoryArcs[plan.oracleIndex]
		switch plan.oracleKind {
		case oracleStoryBalanceCurrent:
			for _, id := range plan.RequiredPairIDs {
				body := pairs[id].Prompt + " " + pairs[id].Response
				if strings.Contains(body, plan.Case.ExpectedAnswer) {
					t.Fatalf("computed balance %q appears verbatim in story %s", plan.Case.ExpectedAnswer, id)
				}
			}
			if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: money(arc.CurrentBalanceCents)}); verdict.Score != 1 {
				t.Fatalf("computed answer did not grade: %+v", verdict)
			}
		case oracleStoryBudgetDelta:
			if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: money(absInt(arc.BudgetDeltaCents))}); verdict.Score != 1 {
				t.Fatalf("budget delta did not grade: %+v", verdict)
			}
		case oracleStoryPostApproval:
			post := arc.BaseBudgetCents + arc.BudgetDeltaCents - arc.PaidCents
			if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: money(post)}); verdict.Score != 1 {
				t.Fatalf("post-approval balance did not grade: %+v", verdict)
			}
		case oracleStoryLaterNetChange:
			net := arc.BudgetDeltaCents - arc.UnexpectedCostCents + arc.CreditCents
			direction := "increase"
			if net < 0 {
				direction = "decrease"
			}
			if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: direction + "; " + money(absInt(net))}); verdict.Score != 1 {
				t.Fatalf("later net change did not grade: %+v", verdict)
			}
		case oracleStoryLesson:
			for _, accepted := range plan.Case.AcceptAny {
				if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: accepted}); verdict.Score != 1 {
					t.Fatalf("lesson equivalent %q did not grade: %+v", accepted, verdict)
				}
			}
			if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: plan.Case.DistractorAnswers[0]}); verdict.Score != 0 {
				t.Fatalf("lesson distractor graded nonzero: %+v", verdict)
			}
		case oracleStoryOutcomeSummary:
			answer := strings.Join([]string{arc.CurrentContact, money(arc.CurrentBalanceCents), arc.Lesson}, "; ")
			if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: answer}); verdict.Score != 1 {
				t.Fatalf("outcome summary did not grade fully: %+v", verdict)
			}
			if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: plan.Case.DistractorAnswers[0]}); verdict.Score != 0 {
				t.Fatalf("outcome distractor graded nonzero: %+v", verdict)
			}
		}
	}
	for _, kind := range []string{oracleStoryBalanceCurrent, oracleStoryBudgetDelta, oracleStoryPostApproval, oracleStoryLaterNetChange, oracleStoryContactCurrent, oracleStoryLesson, oracleStoryOutcomeSummary} {
		if !seen[kind] {
			t.Fatalf("seed did not exercise story program %s", kind)
		}
	}
}

func TestStoryTransportDoesNotExposeRawSeed(t *testing.T) {
	const seed int64 = 639_284_517_306_122_941
	w := Generate(seed, 3)
	needle := strconv.FormatInt(seed, 10)
	for _, pair := range w.Pairs {
		if strings.Contains(pair.Prompt+" "+pair.Response, needle) {
			t.Fatalf("pair %s exposes raw seed %s", pair.PairID, needle)
		}
	}
	for _, plan := range w.storyQuestionCandidates() {
		if strings.Contains(plan.Case.Question, needle) {
			t.Fatalf("question %s exposes raw seed %s", plan.Case.ID, needle)
		}
		if plan.Case.ID == needle || plan.Case.QuestionID == needle {
			t.Fatalf("case identity exposes raw seed %s: %+v", needle, plan.Case)
		}
	}
	if fmt.Sprint(w.Seed) != needle {
		t.Fatal("test seed formatting drifted")
	}
}

func storyPairMap(w World) map[string]protocol.MemoryPair {
	out := make(map[string]protocol.MemoryPair, len(w.Stories))
	for _, pair := range w.Pairs {
		out[pair.PairID] = pair
	}
	return out
}

func storyByPair(w World, pairID string) (Story, bool) {
	for _, story := range w.Stories {
		if story.PairID == pairID {
			return story, true
		}
	}
	return Story{}, false
}
