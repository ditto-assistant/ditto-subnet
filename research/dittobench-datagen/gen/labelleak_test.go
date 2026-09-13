package gen

import (
	"fmt"
	"math/rand"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// #1827: /seed used to hand labels over for free. MemoryPair.session_id carried
// "story-04-outcome", "people-11-d", "project-03-ledger", "v10-email-2",
// "isolation-person-02-a", "v10-tool-route-07"; timestamps stepped 137 hours
// from one epoch for world pairs and sat on "2026-01-0<i>T0<9+i>" /
// "2026-02-<n>T<8+n>:15" / "2026-03-<n>T<8+n>:20" grids for the program,
// divergence, and compiler families. A harness could classify every record's
// generator family, entity, and slot before reading a word.
//
// These tests hold the v13 wire to chance: a feature classifier over the
// session id and timestamp fields, trained on 30 seeds and scored on 10
// held-out seeds in each of four folds, must not beat a shuffled-label null on
// family, story arc slot, or program record slot. The same probe is run against v12 and must
// succeed there, proving the probe detects exactly the leak v13 closes.

var opaqueSessionShape = regexp.MustCompile(`^c[0-9a-f]{16}$`)

// labeledPair is one seeded record with the generator-side truth the wire
// must not reveal.
type labeledPair struct {
	pair   protocol.MemoryPair
	family string
	slot   int // story part (0-2) or program record index (0-5); -1 otherwise
}

const (
	familyWorldPerson     = "world-person"
	familyWorldProject    = "world-project"
	familyWorldTrip       = "world-trip"
	familyWorldStory      = "world-story"
	familyWorldPreference = "world-preference"
	familyWorldIntegrity  = "world-integrity"
	familyProgram         = "program"
	familyDivergence      = "parser-divergence"
	familyCompiler        = "family-compiler"
	familyIsolation       = "isolation"
	familyToolPrereq      = "tool-prerequisite"
)

// labeledArtifactPairs generates the full profile for (seed, version) and
// labels every wire pair from generator truth.
func labeledArtifactPairs(t *testing.T, seed int64, version int) []labeledPair {
	t.Helper()
	prof, ok := ProfileForVersion("full", version)
	if !ok {
		t.Fatalf("no full profile for v%d", version)
	}
	artifact, err := GenerateDataset(seed, prof, version)
	if err != nil {
		t.Fatalf("seed %d v%d: %v", seed, version, err)
	}
	scale, _ := v8WorldProfile(prof.Mem)
	world := universe.GenerateForVersion(seed, scale, version)
	family := map[string]string{}
	slot := map[string]int{}
	for _, p := range world.People {
		for _, id := range []string{p.IdentityPairID, p.WorkPairID, p.EmailPairID, p.CorrectionPairID, p.ToolNotePairID} {
			family[id] = familyWorldPerson
		}
	}
	for _, p := range world.Projects {
		for _, id := range []string{p.ContextPairID, p.LedgerPairID, p.CorrectionPairID, p.ToolNotePairID} {
			family[id] = familyWorldProject
		}
	}
	for _, trip := range world.Trips {
		for _, id := range []string{trip.ContextPairID, trip.PlanPairID, trip.CorrectionPairID} {
			family[id] = familyWorldTrip
		}
	}
	for _, arc := range world.StoryArcs {
		for part, id := range arc.StoryPairIDs {
			family[id] = familyWorldStory
			slot[id] = part
		}
	}
	for _, preference := range world.Preferences {
		family[preference.PairID] = familyWorldPreference
	}
	family[world.BusinessPairID] = familyWorldIntegrity
	for _, id := range world.Integrity.CanaryPairIDs {
		family[id] = familyWorldIntegrity
	}
	for _, c := range artifact.MemoryCases {
		switch {
		case c.V10Provenance != nil:
			for i, id := range c.V10Provenance.EvidencePairIDs {
				family[id] = familyProgram
				slot[id] = i
			}
		case strings.HasPrefix(c.QuestionType, "parser-divergence"):
			for _, id := range c.V10EvidencePairIDs {
				family[id] = familyDivergence
			}
		default:
			for _, id := range c.V10EvidencePairIDs {
				if _, known := family[id]; !known {
					family[id] = familyCompiler
				}
			}
		}
	}
	var out []labeledPair
	add := func(pair protocol.MemoryPair, fallback string) {
		label, ok := family[pair.PairID]
		if !ok {
			label = fallback
		}
		s, ok := slot[pair.PairID]
		if !ok {
			s = -1
		}
		out = append(out, labeledPair{pair: pair, family: label, slot: s})
	}
	for _, wave := range artifact.MemoryWaves {
		fallback := familyCompiler
		if wave.UserID == SecondaryUser {
			fallback = familyIsolation
		}
		for _, pair := range wave.Pairs {
			if wave.UserID == SecondaryUser {
				out = append(out, labeledPair{pair: pair, family: familyIsolation, slot: -1})
				continue
			}
			add(pair, fallback)
		}
	}
	for _, tc := range artifact.ToolCases {
		for _, pair := range tc.PrerequisitePairs {
			add(pair, familyToolPrereq)
		}
	}
	return out
}

// wireFeatures are the only things a /seed reader can see before reading
// prose: the session id's shape and the timestamp's calendar fields.
func wireFeatures(pair protocol.MemoryPair) map[string]string {
	shape := regexp.MustCompile(`[0-9]+`).ReplaceAllString(pair.SessionID, "#")
	shape = regexp.MustCompile(`[a-f]`).ReplaceAllString(shape, "h")
	at, err := time.Parse(time.RFC3339, pair.Timestamp)
	if err != nil {
		return map[string]string{"session-shape": shape, "session-len": fmt.Sprint(len(pair.SessionID))}
	}
	return map[string]string{
		"session-shape":  shape,
		"session-len":    fmt.Sprint(len(pair.SessionID)),
		"session-prefix": strings.SplitN(pair.SessionID, "-", 2)[0],
		"year":           fmt.Sprint(at.Year()),
		"month":          fmt.Sprint(at.Month()),
		"day":            fmt.Sprint(at.Day()),
		"weekday":        at.Weekday().String(),
		"hour":           fmt.Sprint(at.Hour()),
		"minute":         fmt.Sprint(at.Minute()),
		"minute-zero":    fmt.Sprint(at.Minute() == 0),
		"year-month":     at.Format("2006-01"),
		"hour-minute":    at.Format("15:04"),
	}
}

// featureClassifier predicts a label from one wire feature by the majority
// label seen for that feature value in training; unseen values fall back to
// the global majority. Scoring the BEST feature (an optimistic adversary) is
// what must stay at chance.
type featureClassifier struct {
	byValue  map[string]map[string]int
	global   map[string]int
	majority string
}

func trainFeature(pairs []labeledPair, feature string, label func(labeledPair) string, keep func(labeledPair) bool) featureClassifier {
	c := featureClassifier{byValue: map[string]map[string]int{}, global: map[string]int{}}
	for _, lp := range pairs {
		if !keep(lp) {
			continue
		}
		value := wireFeatures(lp.pair)[feature]
		if c.byValue[value] == nil {
			c.byValue[value] = map[string]int{}
		}
		c.byValue[value][label(lp)]++
		c.global[label(lp)]++
	}
	c.majority = argmax(c.global)
	return c
}

func (c featureClassifier) predict(pair protocol.MemoryPair, feature string) string {
	if counts, ok := c.byValue[wireFeatures(pair)[feature]]; ok {
		return argmax(counts)
	}
	return c.majority
}

func argmax(counts map[string]int) string {
	best, bestCount := "", -1
	for label, count := range counts {
		if count > bestCount || (count == bestCount && label < best) {
			best, bestCount = label, count
		}
	}
	return best
}

var leakFeatures = []string{"session-shape", "session-len", "session-prefix", "year", "month", "day", "weekday", "hour", "minute", "minute-zero", "year-month", "hour-minute"}

// leakProbe trains one classifier per wire feature on the training seeds and
// returns the best held-out accuracy together with the held-out majority
// baseline for the given label function.
func leakProbe(train, held []labeledPair, label func(labeledPair) string, keep func(labeledPair) bool) (bestFeature string, bestAccuracy, baseline float64) {
	heldCounts := map[string]int{}
	total := 0
	for _, lp := range held {
		if keep(lp) {
			heldCounts[label(lp)]++
			total++
		}
	}
	if total == 0 {
		return "", 0, 0
	}
	baseline = float64(heldCounts[argmax(heldCounts)]) / float64(total)
	for _, feature := range leakFeatures {
		c := trainFeature(train, feature, label, keep)
		correct := 0
		for _, lp := range held {
			if keep(lp) && c.predict(lp.pair, feature) == label(lp) {
				correct++
			}
		}
		if accuracy := float64(correct) / float64(total); accuracy > bestAccuracy {
			bestFeature, bestAccuracy = feature, accuracy
		}
	}
	return bestFeature, bestAccuracy, baseline
}

// labeledSeeds generates and labels the pinned 40 qualification seeds once;
// the folds below reuse them so the probe's cost is generation, not scoring.
func labeledSeeds(t *testing.T, version int) [][]labeledPair {
	t.Helper()
	out := make([][]labeledPair, 0, 40)
	for seed := int64(1); seed <= 40; seed++ {
		out = append(out, labeledArtifactPairs(t, seed, version))
	}
	return out
}

// foldSplit holds out every fourth seed starting at fold and trains on the
// rest, so each of the four folds scores ten unseen seeds.
func foldSplit(seeds [][]labeledPair, fold int) (train, held []labeledPair) {
	for i, pairs := range seeds {
		if i%4 == fold {
			held = append(held, pairs...)
		} else {
			train = append(train, pairs...)
		}
	}
	return train, held
}

func familyOf(lp labeledPair) string { return lp.family }
func slotOf(lp labeledPair) string   { return fmt.Sprint(lp.slot) }
func every(labeledPair) bool         { return true }
func storyOnly(lp labeledPair) bool  { return lp.family == familyWorldStory }
func programOnly(lp labeledPair) bool {
	return lp.family == familyProgram
}

// chanceMargin is how far above the shuffled-label null the best single-feature
// classifier may land, averaged over four held-out folds of ten seeds each,
// before the wire is considered to carry a label. The null repeats the whole
// procedure (train the best of twelve features, score held-out) on labels
// permuted within the same pairs, so it already absorbs the optimism of picking
// the best feature and the noise of a 600-value feature scored on a few hundred
// pairs; a real label must clear it by more than sampling jitter. hardCap bounds
// the absolute lift over the majority class regardless of the null.
const (
	chanceMargin = 0.02
	hardCap      = 0.06
)

// shuffledLabels returns copies of the pairs with the family and slot labels
// permuted deterministically among pairs of the same keep-set, preserving the
// label marginals so the null shares the majority baseline.
func shuffledLabels(pairs []labeledPair, keep func(labeledPair) bool, salt int64) []labeledPair {
	out := append([]labeledPair(nil), pairs...)
	var kept []int
	for i, lp := range out {
		if keep(lp) {
			kept = append(kept, i)
		}
	}
	r := rand.New(rand.NewSource(salt))
	perm := r.Perm(len(kept))
	for i, j := range perm {
		out[kept[i]].family, out[kept[i]].slot = pairs[kept[j]].family, pairs[kept[j]].slot
	}
	return out
}

// assertAtChance runs the probe on the real labels and on a shuffled-label
// null over four folds and fails when the real wire's mean held-out accuracy
// beats the null's by more than chanceMargin (or the majority class by more
// than hardCap).
func assertAtChance(t *testing.T, task string, seeds [][]labeledPair, label func(labeledPair) string, keep func(labeledPair) bool) {
	t.Helper()
	var realSum, nullSum, baseSum float64
	features := map[string]int{}
	for fold := 0; fold < 4; fold++ {
		train, held := foldSplit(seeds, fold)
		feature, accuracy, baseline := leakProbe(train, held, label, keep)
		features[feature]++
		realSum += accuracy
		baseSum += baseline
		salt := int64(fold + 1)
		_, nullAccuracy, _ := leakProbe(shuffledLabels(train, keep, salt), shuffledLabels(held, keep, salt+100), label, keep)
		nullSum += nullAccuracy
	}
	accuracy, null, baseline := realSum/4, nullSum/4, baseSum/4
	t.Logf("v13 %s: best features %v mean accuracy %.3f vs majority %.3f, shuffled-label null %.3f", task, features, accuracy, baseline, null)
	if accuracy > null+chanceMargin || accuracy > baseline+hardCap {
		t.Fatalf("v13 wire predicts the %s: features %v mean accuracy %.3f vs majority %.3f, null %.3f", task, features, accuracy, baseline, null)
	}
}

// TestV13SeedWireCarriesNoFamilyArcOrSlotLabel is the #1827 acceptance test:
// on 40 v13 seeds, no session-id or timestamp feature predicts a pair's
// generator family, its story slot, or its program record slot above chance.
func TestV13SeedWireCarriesNoFamilyArcOrSlotLabel(t *testing.T) {
	seeds := labeledSeeds(t, protocol.BenchVersionV13)
	var all []labeledPair
	for _, pairs := range seeds {
		all = append(all, pairs...)
	}
	for _, lp := range all {
		if !opaqueSessionShape.MatchString(lp.pair.SessionID) {
			t.Fatalf("v13 pair %s (%s) session %q is not an OpaqueCaseID", lp.pair.PairID, lp.family, lp.pair.SessionID)
		}
		for _, leaked := range []string{"story-", "people-", "project-", "trip-", "v10-", "v11-", "isolation-person-", "preferences-", "event-checkin", "business-import", "accountant-", "current-context-", "v10-tool-route-"} {
			if strings.HasPrefix(lp.pair.SessionID, leaked) {
				t.Fatalf("v13 pair %s session %q carries the %q label", lp.pair.PairID, lp.pair.SessionID, leaked)
			}
		}
	}
	assertAtChance(t, "generator family", seeds, familyOf, every)
	assertAtChance(t, "story slot", seeds, slotOf, storyOnly)
	assertAtChance(t, "program record slot", seeds, slotOf, programOnly)
	// Chronology inside a story arc survives (the oracles rely on it), but the
	// order is only recoverable once the pairs are already grouped by content.
	for seed := int64(1); seed <= 5; seed++ {
		world := universe.GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		for _, arc := range world.StoryArcs {
			if arc.StoryPairIDs[0] == "" {
				t.Fatalf("seed %d arc %s has no story pairs", seed, arc.ID)
			}
		}
	}
}

// TestLeakProbeDetectsTheFrozenV12Labels validates the probe: on the frozen
// v12 contract the same classifier recovers the family from the session id
// alone, so a v13 pass is evidence of closure rather than of a blind probe.
func TestLeakProbeDetectsTheFrozenV12Labels(t *testing.T) {
	var train, held []labeledPair
	for seed := int64(1); seed <= 8; seed++ {
		pairs := labeledArtifactPairs(t, seed, protocol.BenchVersionV12)
		if seed <= 6 {
			train = append(train, pairs...)
		} else {
			held = append(held, pairs...)
		}
	}
	feature, accuracy, baseline := leakProbe(train, held, familyOf, every)
	t.Logf("v12 family: best feature %q accuracy %.3f vs majority %.3f", feature, accuracy, baseline)
	if accuracy < 0.9 || accuracy < baseline+0.25 {
		t.Fatalf("probe failed to recover the v12 family labels: feature %q accuracy %.3f vs majority %.3f", feature, accuracy, baseline)
	}
	feature, accuracy, baseline = leakProbe(train, held, slotOf, storyOnly)
	if accuracy < 0.99 {
		t.Fatalf("probe failed to recover the v12 story slot: feature %q accuracy %.3f vs majority %.3f", feature, accuracy, baseline)
	}
	feature, accuracy, baseline = leakProbe(train, held, slotOf, programOnly)
	if accuracy < 0.99 {
		t.Fatalf("probe failed to recover the v12 program slot: feature %q accuracy %.3f vs majority %.3f", feature, accuracy, baseline)
	}
}

// TestV13SeedSubjectLinksDoNotEnumerateTheStoryJoin is the synthesizeSubjects /
// partitionWaves audit from #1827. The v8+ world path never emits prepared
// subjects or subject links: the harness receives raw pairs and must build its
// own index, so /seed cannot enumerate the three-record join a story oracle
// needs. The v2-v7 persona path (synthesizeSubjects) is frozen and unreachable
// at v13.
func TestV13SeedSubjectLinksDoNotEnumerateTheStoryJoin(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	for seed := int64(1); seed <= 10; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		for _, wave := range artifact.MemoryWaves {
			if len(wave.Subjects) != 0 || len(wave.Links) != 0 {
				t.Fatalf("seed %d wave %d emits %d subjects and %d links: /seed would hand over a prepared join", seed, wave.Wave, len(wave.Subjects), len(wave.Links))
			}
		}
	}
}
