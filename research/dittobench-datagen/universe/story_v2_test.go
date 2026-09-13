package universe

import (
	"fmt"
	"math/rand"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Story v2 (bench_version >= 13) contract tests. The v8 script tests in
// story_test.go keep pinning the frozen v8–v12 path; everything here runs the
// world under BenchVersionV13 and must hold for every seed, because a story
// plan that fails validation fails generation.

func v13World(seed int64) World { return GenerateForVersion(seed, 3, protocol.BenchVersionV13) }

func v13StoryPlans(t *testing.T, w World) []QuestionPlan {
	t.Helper()
	plans := w.storyQuestionCandidatesV13()
	for _, plan := range plans {
		if err := w.validatePlan(plan); err != nil {
			t.Fatalf("seed %d %s: %v", w.Seed, plan.oracleKind, err)
		}
	}
	return plans
}

func TestStoryV2ArcsAreTypedEventDAGsWithRevisions(t *testing.T) {
	kindsSeen := map[StoryEventKind]bool{}
	for seed := int64(1); seed <= 20; seed++ {
		w := v13World(seed)
		if len(w.StoryArcs) != 13 {
			t.Fatalf("seed %d arcs=%d, want 13", seed, len(w.StoryArcs))
		}
		business, personal, revised := 0, 0, 0
		for i, arc := range w.StoryArcs {
			v2 := arc.V2
			if v2 == nil {
				t.Fatalf("seed %d arc %d has no v2 state", seed, i)
			}
			if v2.Kind == StoryBusiness {
				business++
			} else {
				personal++
			}
			if v2.HasRevision {
				revised++
			}
			if arc.BaseBudgetCents != 0 || arc.CurrentBalanceCents != 0 || arc.StoryPairIDs[0] != "" {
				t.Fatalf("seed %d arc %d still carries v8 script state", seed, i)
			}
			events := v2.Events
			if events[len(events)-1].Kind == EventOutcomeDisputed {
				if !v2.Disagree {
					t.Fatalf("seed %d arc %d has a disputed record without the disagree flag", seed, i)
				}
				events = events[:len(events)-1]
			}
			if len(events) < 5 || len(events) > 9 {
				t.Fatalf("seed %d arc %d has %d events, want 5..9", seed, i, len(events))
			}
			if !isRootEvent(events[0].Kind) || (events[1].Kind != EventQuote && events[1].Kind != EventBooking) || events[len(events)-1].Kind != EventOutcome {
				t.Fatalf("seed %d arc %d timeline shape wrong: %v", seed, i, eventKinds(events))
			}
			seen := map[StoryEventKind]int{}
			for _, e := range events {
				kindsSeen[e.Kind] = true
				for _, need := range eventSpecFor(e.Kind).requires {
					if !preconditionMet(seen, need) {
						t.Fatalf("seed %d arc %d: %s precedes precondition %s", seed, i, e.Kind, need)
					}
				}
				if e.Revision && seen[e.Kind] == 0 && e.Kind != EventCorrection {
					t.Fatalf("seed %d arc %d: %s flagged as a revision with nothing to revise", seed, i, e.Kind)
				}
				seen[e.Kind]++
			}
			if v2.Memories < 3 || v2.Memories > 5 || v2.Sessions < 2 {
				t.Fatalf("seed %d arc %d memories=%d sessions=%d", seed, i, v2.Memories, v2.Sessions)
			}
			if v2.Owner == v2.OwnerInitial || v2.Owner == arc.PersonIndex {
				t.Fatalf("seed %d arc %d owner never moved off the initial owner or is the anchor person", seed, i)
			}
			if v2.Sequence[0] == "" || v2.Sequence[1] == "" || v2.Sequence[0] == v2.Sequence[1] {
				t.Fatalf("seed %d arc %d has no ordered replacement pair", seed, i)
			}
			if (v2.Quantity == nil) == (v2.Lesson == nil) {
				t.Fatalf("seed %d arc %d must carry exactly one rotating slot (quantity xor lesson)", seed, i)
			}
		}
		if business != 7 || personal != 6 {
			t.Fatalf("seed %d business=%d personal=%d, want 7/6", seed, business, personal)
		}
		if revised*2 < len(w.StoryArcs) {
			t.Fatalf("seed %d only %d/13 arcs carry a revision", seed, revised)
		}
		plans := v13StoryPlans(t, w)
		if len(plans) != 78 {
			t.Fatalf("seed %d story plans=%d, want 78", seed, len(plans))
		}
	}
	for _, kind := range []StoryEventKind{EventKickoff, EventQuote, EventApprovalCapped, EventContactRouteChanged, EventVendorSwapped, EventIncident, EventCorrection, EventHandoffAssigned, EventFollowUp, EventOutcome, EventBooking, EventProviderSwapped} {
		if !kindsSeen[kind] {
			t.Errorf("event kind %s never drawn across 20 seeds", kind)
		}
	}
	for _, theme := range personalThemes {
		if !kindsSeen[theme] {
			t.Errorf("personal theme %s never drawn across 20 seeds", theme)
		}
	}
}

func eventKinds(events []StoryEvent) []StoryEventKind {
	out := make([]StoryEventKind, 0, len(events))
	for _, e := range events {
		out = append(out, e.Kind)
	}
	return out
}

// unfilled matches a grammar slot ({who}) or symbol (#lead#) that survived
// rendering.
var unfilled = regexp.MustCompile(`\{[a-z0-9]+\}|#[a-z0-9]+#`)

func TestStoryV2EnvelopeIsRetainedAndBothKindsPresent(t *testing.T) {
	for _, tc := range []struct{ scale, arcs int }{{1, 1}, {2, 6}, {3, 13}} {
		for seed := int64(1); seed <= 12; seed++ {
			w := GenerateForVersion(seed, tc.scale, protocol.BenchVersionV13)
			if len(w.StoryArcs) != tc.arcs {
				t.Fatalf("scale %d seed %d arcs=%d, want %d", tc.scale, seed, len(w.StoryArcs), tc.arcs)
			}
			pairs := storyPairMap(w)
			kinds := map[StoryKind]bool{}
			for _, story := range w.Stories {
				pair := pairs[story.PairID]
				size := len(pair.Prompt) + len(pair.Response)
				if size < 1_800 || size > 3_300 {
					t.Fatalf("scale %d seed %d story %s size=%d, want 1800..3300", tc.scale, seed, story.ID, size)
				}
				if err := validateStoryStructure(story); err != nil {
					t.Fatalf("story %s has invalid structured state: %v", story.ID, err)
				}
				kinds[story.Kind] = true
				seenParagraph := map[string]bool{}
				for _, paragraph := range strings.Split(pair.Prompt, "\n\n") {
					normalized := strings.Join(strings.Fields(strings.ToLower(paragraph)), " ")
					if normalized == "" {
						t.Fatalf("seed %d story %s has an empty paragraph", seed, story.ID)
					}
					if seenParagraph[normalized] {
						t.Fatalf("seed %d story %s repeats paragraph %q", seed, story.ID, paragraph)
					}
					seenParagraph[normalized] = true
				}
				lower := strings.ToLower(pair.Prompt)
				for _, tell := range []string{"%+d", " -> ", "->", "%s"} {
					if strings.Contains(lower, tell) {
						t.Fatalf("seed %d story %s carries the format tell %q", seed, story.ID, tell)
					}
				}
				if unfilled.MatchString(pair.Prompt) {
					t.Fatalf("seed %d story %s carries an unfilled grammar slot: %q", seed, story.ID, unfilled.FindString(pair.Prompt))
				}
			}
			if tc.scale > 1 && (!kinds[StoryPersonal] || !kinds[StoryBusiness]) {
				t.Fatalf("scale %d seed %d story kinds=%v, want personal+business", tc.scale, seed, kinds)
			}
			want := 0
			for _, arc := range w.StoryArcs {
				want += len(arc.V2.PairIDs) + 1 // memories plus the near-name decoy
			}
			if len(w.Stories) != want {
				t.Fatalf("scale %d seed %d stories=%d, want %d", tc.scale, seed, len(w.Stories), want)
			}
		}
	}
}

func TestStoryV2SessionIDsAndTimestampsDoNotPredictArcOrSlot(t *testing.T) {
	grid := time.Date(2024, 1, 8, 9, 0, 0, 0, time.UTC)
	for seed := int64(1); seed <= 10; seed++ {
		w := v13World(seed)
		pairs := storyPairMap(w)
		sessionArc := map[string]int{}
		onGrid, minutes := 0, map[int]bool{}
		for _, story := range w.Stories {
			pair := pairs[story.PairID]
			if strings.HasPrefix(pair.SessionID, "story-") || strings.Contains(pair.SessionID, "origin") || strings.Contains(pair.SessionID, "outcome") {
				t.Fatalf("seed %d story pair %s leaks its slot through session id %q", seed, pair.PairID, pair.SessionID)
			}
			if len(pair.SessionID) != len(protocol.OpaqueCaseID(seed, "x", 0)) || pair.SessionID[0] != 'c' {
				t.Fatalf("seed %d story session id %q is not an opaque case id", seed, pair.SessionID)
			}
			if arc, ok := sessionArc[pair.SessionID]; ok && arc != story.ArcIndex {
				t.Fatalf("seed %d session %s is shared by arcs %d and %d", seed, pair.SessionID, arc, story.ArcIndex)
			}
			sessionArc[pair.SessionID] = story.ArcIndex
			ts, err := time.Parse(time.RFC3339, pair.Timestamp)
			if err != nil {
				t.Fatalf("seed %d story pair %s timestamp %q: %v", seed, pair.PairID, pair.Timestamp, err)
			}
			if ts.Sub(grid)%(137*time.Hour) == 0 {
				onGrid++
			}
			minutes[ts.Minute()] = true
		}
		// The v8 grid puts every pair exactly on base + n*137h; jittered story
		// instants land there only by coincidence.
		if onGrid*10 > len(w.Stories) || len(minutes) < 10 {
			t.Fatalf("seed %d story timestamps look positional: %d/%d on the 137h grid, %d distinct minute values", seed, onGrid, len(w.Stories), len(minutes))
		}
		for i, arc := range w.StoryArcs {
			// Chronology is preserved inside an arc (latest-by-time is meaningful)
			// while the memory index is not recoverable from the session id.
			var last time.Time
			for c, id := range arc.V2.PairIDs {
				if c >= arc.V2.Memories {
					break
				}
				ts, _ := time.Parse(time.RFC3339, pairs[id].Timestamp)
				if ts.Before(last) {
					t.Fatalf("seed %d arc %d memory %d is earlier than its predecessor", seed, i, c)
				}
				last = ts
			}
			if arc.V2.SessionIDs[0] == arc.V2.SessionIDs[1] {
				t.Fatalf("seed %d arc %d root and hop share a session", seed, i)
			}
		}
	}
}

func TestStoryV2PlansValidatePerEventTypeAndAreCausal(t *testing.T) {
	for seed := int64(21); seed <= 32; seed++ {
		w := v13World(seed)
		pairs := storyPairMap(w)
		storyIDs := map[string]bool{}
		decoys := map[string]bool{}
		for _, story := range w.Stories {
			storyIDs[story.PairID] = true
			if story.PairID == w.StoryArcs[story.ArcIndex].V2.DecoyPairID {
				decoys[story.PairID] = true
			}
		}
		for _, story := range w.Stories {
			pair := pairs[story.PairID]
			if len(story.Facts) == 0 {
				t.Fatalf("seed %d story %s has no planted facts", seed, story.ID)
			}
			for _, fact := range story.Facts {
				pos := strings.Index(pair.Prompt, fact.Value)
				if pos < 0 {
					t.Fatalf("seed %d story %s omits %s=%q", seed, story.ID, fact.Key, fact.Value)
				}
				if ratio := float64(pos) / float64(len(pair.Prompt)); ratio < 0.15 || ratio > 0.85 {
					t.Fatalf("seed %d story %s fact %s at %.2f, want interior", seed, story.ID, fact.Key, ratio)
				}
				if strings.Contains(pair.Response, fact.Value) {
					t.Fatalf("seed %d story %s response duplicates %s", seed, story.ID, fact.Key)
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
		for _, plan := range v13StoryPlans(t, w) {
			if len(plan.RequiredPairIDs) < 3 {
				t.Fatalf("seed %d plan %s evidence=%d, want >= 3", seed, plan.oracleKind, len(plan.RequiredPairIDs))
			}
			available := map[string]bool{}
			for _, id := range plan.RequiredPairIDs {
				available[id] = true
				if decoys[id] {
					t.Fatalf("seed %d plan %s requires the decoy thread", seed, plan.oracleKind)
				}
			}
			if got, ok := w.resolveWithEvidence(plan, available); !ok || got != plan.Case.ExpectedAnswer {
				t.Fatalf("seed %d plan %s resolves %q, want %q", seed, plan.oracleKind, got, plan.Case.ExpectedAnswer)
			}
			for _, omitted := range plan.RequiredPairIDs {
				delete(available, omitted)
				if got, ok := w.resolveWithEvidence(plan, available); ok && got == plan.Case.ExpectedAnswer {
					t.Fatalf("seed %d plan %s still resolves without %s", seed, plan.oracleKind, omitted)
				}
				available[omitted] = true
			}
			if len(plan.Claims) == 0 {
				t.Fatalf("seed %d plan %s emits no typed claims", seed, plan.oracleKind)
			}
			for _, claim := range plan.Claims {
				if claim.Kind == "" || claim.Expected == "" || claim.Weight <= 0 {
					t.Fatalf("seed %d plan %s has an under-specified claim %+v", seed, plan.oracleKind, claim)
				}
			}
		}
	}
}

// TestStoryV2AnchorsResolveUniquelyUnderTypoEdits: every question anchor
// (nickname, subject alias) still resolves to exactly one arc after 1–3 typo
// edits on either token, and never to the near-name decoy, so a clarifying
// question is never the only honest move on a value-expecting story case. The
// multilingual render is 0% at v13.0 (#1831) and is covered when it lands.
func TestStoryV2AnchorsResolveUniquelyUnderTypoEdits(t *testing.T) {
	for seed := int64(1); seed <= 15; seed++ {
		w := v13World(seed)
		r := rand.New(rand.NewSource(seed))
		type candidate struct {
			nick, alias string
			arc         int
			decoy       bool
		}
		candidates := make([]candidate, 0, 2*len(w.StoryArcs))
		for i, arc := range w.StoryArcs {
			nick := w.People[arc.PersonIndex].Nickname
			candidates = append(candidates, candidate{nick, arc.V2.SubjectAlias, i, false}, candidate{nick, arc.V2.DecoyAlias, i, true})
		}
		for i, arc := range w.StoryArcs {
			nick := w.People[arc.PersonIndex].Nickname
			alias := arc.V2.SubjectAlias
			for trial := 0; trial < 12; trial++ {
				edits := 1 + r.Intn(3)
				qNick, qAlias := nick, alias
				if trial%2 == 0 {
					qNick = typoEdit(r, nick, edits)
				} else {
					qAlias = typoEdit(r, alias, edits)
				}
				best, bestDistance, ties := -1, 1<<30, 0
				bestDecoy := false
				for _, c := range candidates {
					d := levenshtein(c.nick, qNick) + levenshtein(c.alias, qAlias)
					switch {
					case d < bestDistance:
						best, bestDistance, ties, bestDecoy = c.arc, d, 1, c.decoy
					case d == bestDistance && (c.arc != best || c.decoy != bestDecoy):
						ties++
					}
				}
				if best != i || ties != 1 || bestDecoy {
					t.Fatalf("seed %d arc %d anchor (%q,%q) with %d edits -> (%q,%q) resolves to arc %d decoy=%v ties=%d", seed, i, nick, alias, edits, qNick, qAlias, best, bestDecoy, ties)
				}
			}
		}
	}
}

// typoEdit applies n single-character edits (substitution, deletion,
// transposition) to the letters of s.
func typoEdit(r *rand.Rand, s string, n int) string {
	b := []byte(s)
	for i := 0; i < n && len(b) > 2; i++ {
		pos := 1 + r.Intn(len(b)-1)
		switch r.Intn(3) {
		case 0:
			b[pos] = byte('a' + r.Intn(26))
		case 1:
			b = append(b[:pos], b[pos+1:]...)
		default:
			if pos+1 < len(b) {
				b[pos], b[pos+1] = b[pos+1], b[pos]
			}
		}
	}
	return string(b)
}

// TestStoryOraclesAreTypedAndLessonClaimSetsAccept replaces the v8
// TestStoryBalanceIsComputedAndLessonAcceptsEquivalentPhrasing: every story v2
// oracle grades a correct typed answer (including the seed's status surface,
// the canonical synonyms, an owner nickname, a paraphrased lesson concept, a
// human money format, and a records-disagree reply) at 1 under today's grader
// and grades its distractors at 0; the mix is 78 cases with <= 5 money-bearing
// and <= 1 lesson per arc.
func TestStoryOraclesAreTypedAndLessonClaimSetsAccept(t *testing.T) {
	for seed := int64(44332211); seed <= 44332214; seed++ {
		w := v13World(seed)
		plans := v13StoryPlans(t, w)
		if len(plans) != 78 {
			t.Fatalf("seed %d story cases=%d, want 78", seed, len(plans))
		}
		moneyCases := 0
		perArc := map[int]map[string]int{}
		seenKinds := map[string]bool{}
		for _, plan := range plans {
			seenKinds[plan.oracleKind] = true
			if perArc[plan.oracleIndex] == nil {
				perArc[plan.oracleIndex] = map[string]int{}
			}
			perArc[plan.oracleIndex][plan.oracleKind]++
			arc := w.StoryArcs[plan.oracleIndex]
			v2 := arc.V2
			if plan.Case.BenchVersion == 0 {
				plan.Case.BenchVersion = protocol.BenchVersionV13
			}
			correct := []string{}
			switch plan.oracleKind {
			case oracleStoryOwnerCurrent:
				owner := w.People[v2.Owner]
				correct = append(correct, owner.Name, "It sits with "+owner.Nickname+" now.")
			case oracleStoryStatusCurrent:
				correct = append(correct, v2.Status, "As of the last note it is "+v2.StatusSurface+".", storyStatusVocabulary[v2.Status][0])
			case oracleStoryStatusDisagree:
				correct = append(correct, fmt.Sprintf("The records disagree: one says %s, the other says %s.", v2.StatusSurface, v2.StatusAltSurface))
			case oracleStoryOrder:
				correct = append(correct, fmt.Sprintf("First %s, then %s took over.", v2.Sequence[0], v2.Sequence[1]))
				if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: fmt.Sprintf("%s then %s", v2.Sequence[1], v2.Sequence[0])}); verdict.Score != 0 {
					t.Fatalf("seed %d reversed order graded %+v", seed, verdict)
				}
			case oracleStoryNextAction:
				who := w.People[v2.Next.Who]
				correct = append(correct, fmt.Sprintf("%s will %s by %s.", who.Nickname, v2.Next.WhatAccept[0], storyChannelAccept(v2.Next.Channel)[len(storyChannelAccept(v2.Next.Channel))-1]))
			case oracleStoryQuantity:
				if v2.Quantity.Kind == "money" {
					moneyCases++
					correct = append(correct, "That leaves "+money(v2.Quantity.Value)+".")
				} else {
					correct = append(correct, fmt.Sprintf("%d %s in total.", v2.Quantity.Value, v2.Quantity.Kind))
				}
			case oracleStoryLessonClaims:
				paraphrase := []string{}
				for _, concept := range v2.Lesson.concepts {
					paraphrase = append(paraphrase, concept.accept[len(concept.accept)-1])
				}
				correct = append(correct, "Roughly: "+strings.Join(paraphrase, ", and ")+".")
			case oracleStoryOwnerEmail:
				correct = append(correct, w.People[v2.Owner].Email)
			}
			for _, answer := range correct {
				if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: answer}); verdict.Score != 1 {
					t.Fatalf("seed %d %s correct answer %q graded %+v (case %+v)", seed, plan.oracleKind, answer, verdict, plan.Case)
				}
			}
			for _, distractor := range plan.Case.DistractorAnswers {
				if verdict := grade.Memory(plan.Case, protocol.RunResponse{Answer: distractor}); verdict.Score != 0 {
					t.Fatalf("seed %d %s distractor %q graded %+v", seed, plan.oracleKind, distractor, verdict)
				}
			}
		}
		if moneyCases > 5 {
			t.Fatalf("seed %d has %d money-bearing story cases, want <= 5", seed, moneyCases)
		}
		for arcIndex, kinds := range perArc {
			if kinds[oracleStoryLessonClaims] > 1 {
				t.Fatalf("seed %d arc %d has %d lesson oracles", seed, arcIndex, kinds[oracleStoryLessonClaims])
			}
			core := kinds[oracleStoryOwnerCurrent] + kinds[oracleStoryStatusCurrent] + kinds[oracleStoryStatusDisagree] + kinds[oracleStoryNextAction] + kinds[oracleStoryOrder]
			if core < 3 {
				t.Fatalf("seed %d arc %d has only %d of the owner/status/next-action/ordering oracles", seed, arcIndex, core)
			}
			if kinds[oracleStoryOwnerEmail] != 1 {
				t.Fatalf("seed %d arc %d lacks its cross-record oracle", seed, arcIndex)
			}
		}
		for _, kind := range []string{oracleStoryOwnerCurrent, oracleStoryStatusCurrent, oracleStoryStatusDisagree, oracleStoryOrder, oracleStoryNextAction, oracleStoryQuantity, oracleStoryLessonClaims, oracleStoryOwnerEmail} {
			if !seenKinds[kind] {
				t.Fatalf("seed %d did not exercise story program %s", seed, kind)
			}
		}
	}
}

func TestStoryV2QuestionsLeakNoHiddenValueOrRawSeed(t *testing.T) {
	const seed int64 = 639_284_517_306_122_941
	w := v13World(seed)
	needle := strconv.FormatInt(seed, 10)
	for _, plan := range v13StoryPlans(t, w) {
		v2 := w.StoryArcs[plan.oracleIndex].V2
		question := strings.ToLower(plan.Case.Question)
		for _, hidden := range []string{v2.JoinKey1, v2.JoinKey2, v2.Sequence[0], v2.Sequence[1], v2.DecoyAlias, needle} {
			if strings.Contains(question, strings.ToLower(hidden)) {
				t.Fatalf("question %q leaks %q", plan.Case.Question, hidden)
			}
		}
		if len(plan.Constraints) != 2 || !contains(plan.Constraints, w.People[w.StoryArcs[plan.oracleIndex].PersonIndex].Nickname) {
			t.Fatalf("plan %s constraints=%v", plan.oracleKind, plan.Constraints)
		}
	}
	for _, pair := range w.Pairs {
		if strings.Contains(pair.Prompt+" "+pair.Response, needle) {
			t.Fatalf("pair %s exposes raw seed", pair.PairID)
		}
	}
}

func TestStoryV2LessonPoolHasReviewedKeyConcepts(t *testing.T) {
	if len(storyV13Lessons) < 30 {
		t.Fatalf("v13 lesson pool=%d, want >= 30", len(storyV13Lessons))
	}
	for _, lesson := range storyV13Lessons {
		if len(lesson.concepts) < 2 {
			t.Fatalf("lesson %q has %d key concepts, want >= 2", lesson.canonical, len(lesson.concepts))
		}
		for _, concept := range lesson.concepts {
			if concept.term == "" || len(concept.accept) < 2 {
				t.Fatalf("lesson %q concept %+v is under-reviewed", lesson.canonical, concept)
			}
		}
	}
	for canonical := range lessonKeyConcepts {
		found := false
		for _, lesson := range storyLessons {
			if strings.EqualFold(lesson.canonical, canonical) {
				found = true
			}
		}
		if !found {
			t.Fatalf("key-concept lesson %q is not in the frozen corpus", canonical)
		}
	}
}

func TestStoryV2DoesNotDisturbTheV8Script(t *testing.T) {
	for seed := int64(1); seed <= 5; seed++ {
		v8 := Generate(seed, 3)
		v12 := GenerateForVersion(seed, 3, protocol.BenchVersionV12)
		if len(v8.Pairs) != len(v12.Pairs) {
			t.Fatalf("seed %d v8/v12 pair counts differ", seed)
		}
		for i := range v8.Pairs {
			if v8.Pairs[i] != v12.Pairs[i] {
				t.Fatalf("seed %d pair %d differs between the v8 and v12 worlds", seed, i)
			}
		}
		if v8.StoryArcs[0].V2 != nil || !strings.HasPrefix(v8.Stories[0].SessionID, "story-") {
			t.Fatalf("seed %d v8 world carries story v2 state", seed)
		}
	}
}
