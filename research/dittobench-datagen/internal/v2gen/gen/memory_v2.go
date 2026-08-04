package gen

import (
	"math/rand"
	"sort"
	"strconv"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/v2gen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// StagedCase pairs a memory case with the seeding wave after which it becomes
// answerable (the max wave among its evidence facts). Abstention cases carry
// wave 0 (a grounded decline is correct regardless of what has been seeded).
type StagedCase struct {
	Case         protocol.MemoryCase
	RunAfterWave int
	// UserID is the memory graph the case must be answered under (multi-graph
	// isolation). Empty means the primary graph (PrimaryUser); isolation
	// cases set it explicitly so the pipeline scopes RunRequest.UserID per case.
	UserID string
}

// MemorySuite is the full v2 memory dataset: the ordered seeding waves (Tier C),
// the cases with their unlock wave, the retained paraphrase-stats field, and the
// Tier-B count.
type MemorySuite struct {
	Waves        []protocol.SeedRequest
	Cases        []StagedCase
	Stats        protocol.ParaphraseStats
	TierBCases   int
	SeedingWaves int
	// LifecycleCases counts the write-then-read lifecycle cases in the suite
	// (instruction + read halves; see gen/lifecycle.go). Advisory telemetry.
	LifecycleCases int
	// LexicalGap is the query↔needle overlap telemetry (NoLiMa): how much
	// content wording the emitted questions share with their evidence, before and
	// after the low-overlap rewrite.
	LexicalGap protocol.LexicalGapStats
}

// GenerateMemorySuite is the DittoBench v2 memory generator. It builds a
// procedural persona universe (seed-only plan → LLM-realized
// haystack → questions derived from ground truth), stratify-samples to the case
// quota, and lays the result out across seeding tiers:
//
//   - Tier A (prepared seeding): a synthesized subject per persona attribute
//     links the pairs, so retrieval is tested in isolation.
//   - Tier B (raw-pairs seeding): for a rawPairsFrac slice of questions, the
//     evidence pairs are seeded WITHOUT prepared subjects: the harness must
//     build its own subject index (the memory-construction test).
//   - Tier C (staged ingestion): the haystack is split into nWaves waves by
//     attribute cluster; the pipeline seeds a wave, runs the cases it unlocks,
//     then seeds the next, so memory is built incrementally. nWaves=1 → single seed.
//
// The plan is a pure function of the master `seed`; sampling uses the shared rng
// `r`. Fully non-LLM and reproducible from (seed, bench_version): the haystack
// and questions come from the plan's deterministic template surfaces + seeded
// phrasing variants, not a paraphrase pass.
func GenerateMemorySuite(r *rand.Rand, seed int64, n int, nWaves int, rawPairsFrac float64) MemorySuite {
	suite, _ := GenerateMemorySuiteForVersion(r, seed, n, nWaves, rawPairsFrac, protocol.BenchVersionV2)
	return suite
}

// GenerateMemorySuiteForVersion generates memory data for an explicit
// benchmark contract. The caller must seed r with the same version.
func GenerateMemorySuiteForVersion(r *rand.Rand, seed int64, n int, nWaves int, rawPairsFrac float64, benchVersion int) (MemorySuite, error) {
	if nWaves < 1 {
		nWaves = 1
	}
	suite := MemorySuite{SeedingWaves: nWaves}
	if n <= 0 {
		suite.Waves = []protocol.SeedRequest{{UserID: "miner"}}
		return suite, nil
	}

	plan, err := persona.BuildPlanForVersion(seed, personaOptsFor(n), benchVersion)
	if err != nil {
		return MemorySuite{}, err
	}
	pairs, evidence := RenderHaystack(plan)

	questions := persona.DeriveQuestions(plan)

	// Split into: the always-included canary integrity probe, the abstention
	// share, and the main pool stratified by type. The canary is never sampled
	// out: its whole point is a guaranteed per-run integrity check.
	var absPool, mainPool, canaryPool, twinPool []persona.Question
	for _, q := range questions {
		switch {
		case q.Type == persona.QTCanary:
			canaryPool = append(canaryPool, q)
		case q.TwinGroup != "":
			twinPool = append(twinPool, q) // both invariance twins kept together
		case q.Abstain:
			absPool = append(absPool, q)
		default:
			mainPool = append(mainPool, q)
		}
	}
	// Keep a run-size-appropriate number of whole metamorphic families. More
	// families for larger runs smooth the metamorphic-consistency factor (a single
	// family makes it a per-run coin flip); small/medium keep one to stay in
	// budget. Selected before mainQuota so the twin reservation is exact.
	twinPool = selectTwinFamilies(twinPool, twinFamiliesFor(n))
	nAbs := abstentionQuota(n)
	if nAbs > len(absPool) {
		nAbs = len(absPool)
	}
	// Write-then-read lifecycle chains (gen/lifecycle.go): like the canary,
	// they are never sampled out; their count is seed-independent per run size.
	lc := buildLifecycle(r, seed, plan, lifecycleChainsFor(n, nWaves), nWaves)
	suite.LifecycleCases = len(lc.Cases)
	// Reserve room for the always-included canary, twin, and lifecycle cases so
	// the total case count stays at n.
	mainQuota := n - nAbs - len(canaryPool) - len(twinPool) - len(lc.Cases)
	if mainQuota < 0 {
		mainQuota = 0
	}
	selected := stratifyByType(r, mainPool, mainQuota)
	selected = append(selected, canaryPool...)
	selected = append(selected, twinPool...)
	if nAbs > 0 {
		perm := r.Perm(len(absPool))
		for i := 0; i < nAbs; i++ {
			selected = append(selected, absPool[perm[i]])
		}
	}

	// Tier-B selection: a rawPairsFrac slice of the non-abstention
	// questions is chosen; the evidence of each is seeded WITHOUT a prepared
	// subject, so a subjects-first retriever cannot route to it; the harness
	// must have built its own index. Pass 1 marks every fact claimed by a
	// NON-Tier-B (Tier A) question; pass 2 promotes a rolled-B question to Tier B
	// only if ALL its evidence is free of Tier-A claims, so Tier-A retrieval never
	// loses its scaffolding to a fact shared with a Tier-B question.
	rollB := map[string]bool{}
	tierAFacts := map[string]bool{}
	for _, q := range selected {
		isB := !q.Abstain && rawPairsFrac > 0 && r.Float64() < rawPairsFrac
		rollB[q.ID] = isB
		if !isB {
			for _, fid := range q.Evidence {
				tierAFacts[fid] = true
			}
		}
	}
	rawFacts := map[string]bool{}
	for _, q := range selected {
		if !rollB[q.ID] || len(q.Evidence) == 0 {
			continue
		}
		free := true
		for _, fid := range q.Evidence {
			if tierAFacts[fid] {
				free = false
				break
			}
		}
		if free {
			suite.TierBCases++
			for _, fid := range q.Evidence {
				rawFacts[fid] = true
			}
		}
	}

	// pairText: rendered text of each haystack pair, keyed by pair ID. It is the
	// needle side of the query↔needle overlap measurement.
	pairText := make(map[string]string, len(pairs))
	for _, p := range pairs {
		pairText[p.PairID] = p.Prompt + " " + p.Response
	}
	evidenceText := func(q persona.Question) string {
		var sb strings.Builder
		for _, fid := range q.Evidence {
			if pid, ok := evidence[fid]; ok {
				if t, ok := pairText[pid]; ok {
					sb.WriteString(t)
					sb.WriteByte(' ')
				}
			}
		}
		return sb.String()
	}

	// realizeQuestion emits the final question text. Generation is non-LLM: the
	// question's surface was already varied by the seeded phrasing variant in
	// persona.DeriveQuestions, so this only records the query↔needle lexical
	// overlap telemetry (NoLiMa gap) for evidence-bearing questions. One rng roll
	// is consumed per question to keep the draw-count stable regardless of type.
	var gapBeforeSum, gapAfterSum float64
	realizeQuestion := func(q persona.Question) string {
		ev := evidenceText(q)
		measured := !q.Abstain && ev != ""
		// Consume one roll per question (keeps the RNG draw-count independent of
		// question type, so two runs of a seed stay identical).
		_ = r.Float64()
		if measured {
			overlap := lexicalOverlap(q.Text, ev)
			suite.LexicalGap.Questions++
			gapBeforeSum += overlap
			gapAfterSum += overlap
		}
		return q.Text
	}

	fw := factWaves(plan, nWaves)
	staged := make([]StagedCase, 0, len(selected))
	for i, q := range selected {
		expected := q.Answer
		if q.Abstain {
			expected = abstentionExpectedAnswer
		}
		staged = append(staged, StagedCase{
			Case: protocol.MemoryCase{
				ID:                protocol.OpaqueCaseID(seed, "mem", i),
				QuestionID:        q.ID,
				QuestionType:      q.Type,
				Question:          realizeQuestion(q),
				ExpectedAnswer:    expected,
				AnswerKind:        q.Kind,
				AnswerItems:       q.Items,
				DistractorAnswers: q.Distractors,
				ForbiddenAnswer:   q.Forbidden,
				TwinGroup:         q.TwinGroup,
			},
			RunAfterWave: caseUnlockWave(q, fw),
		})
	}
	if suite.LexicalGap.Questions > 0 {
		suite.LexicalGap.MeanBefore = gapBeforeSum / float64(suite.LexicalGap.Questions)
		suite.LexicalGap.MeanAfter = gapAfterSum / float64(suite.LexicalGap.Questions)
	}
	staged = append(staged, lc.Cases...)
	r.Shuffle(len(staged), func(i, j int) { staged[i], staged[j] = staged[j], staged[i] })
	suite.Cases = staged

	// Subjects: synthesize for every self fact EXCEPT the raw-pairs slice.
	subjects, links := synthesizeSubjects(plan, evidence, rawFacts)
	suite.Waves = partitionWaves(plan, pairs, subjects, links, evidence, fw, nWaves)
	// Lifecycle seeded facts (update/delete chains) join wave 0 as raw pairs:
	// no prepared subject, so the harness indexes them itself (Tier-B style).
	if len(lc.Pairs) > 0 {
		suite.Waves[0].Pairs = append(suite.Waves[0].Pairs, lc.Pairs...)
	}
	return suite, nil
}

// GenerateMemoryV2 is the single-wave, all-Tier-A view of the suite, retained
// for callers/tests that want a flat SeedRequest + cases (no staging).
func GenerateMemoryV2(r *rand.Rand, seed int64, n int) (protocol.SeedRequest, []protocol.MemoryCase, protocol.ParaphraseStats, error) {
	suite := GenerateMemorySuite(r, seed, n, 1, 0)
	cases := make([]protocol.MemoryCase, 0, len(suite.Cases))
	for _, sc := range suite.Cases {
		cases = append(cases, sc.Case)
	}
	seedReq := protocol.SeedRequest{UserID: "miner"}
	if len(suite.Waves) > 0 {
		seedReq = suite.Waves[0]
	}
	return seedReq, cases, suite.Stats, nil
}

// caseUnlockWave is the wave after which a case is answerable: the max wave among
// its evidence facts (abstention / evidence-free → 0).
func caseUnlockWave(q persona.Question, fw map[string]int) int {
	w := 0
	for _, fid := range q.Evidence {
		if v, ok := fw[fid]; ok && v > w {
			w = v
		}
	}
	return w
}

// factWaves assigns each fact a staging wave BY ATTRIBUTE CLUSTER, so a subject
// and all its pairs (including an update chain or a reversal, which share the
// attribute) always land in one wave: no dangling cross-wave subject links, and
// ground truth stays exact (a knowledge-update question only runs once both
// values are seeded). Attribute order is the facts' timeline order (deterministic).
func factWaves(plan *persona.Plan, nWaves int) map[string]int {
	var attrOrder []string
	seen := map[string]bool{}
	for _, f := range plan.Facts {
		if !seen[f.Attribute] {
			seen[f.Attribute] = true
			attrOrder = append(attrOrder, f.Attribute)
		}
	}
	attrWave := map[string]int{}
	for i, a := range attrOrder {
		w := 0
		if len(attrOrder) > 0 {
			w = i * nWaves / len(attrOrder)
		}
		if w >= nWaves {
			w = nWaves - 1
		}
		attrWave[a] = w
	}
	fw := map[string]int{}
	for _, f := range plan.Facts {
		fw[f.ID] = attrWave[f.Attribute]
	}
	return fw
}

// partitionWaves splits the haystack into nWaves SeedRequests: each pair lands in
// its fact's wave (noise pairs by their session bucket); each subject + its links
// land in the subject's attribute wave (== all its pairs' wave). Empty leading
// waves are preserved as index placeholders so Wave numbers stay stable.
func partitionWaves(plan *persona.Plan, pairs []protocol.MemoryPair, subjects []protocol.Subject, links []protocol.SubjectLink, evidence map[string]string, fw map[string]int, nWaves int) []protocol.SeedRequest {
	nSessions := len(plan.Sessions)
	pairWave := map[string]int{}
	for _, f := range plan.Facts {
		if pid, ok := evidence[f.ID]; ok {
			pairWave[pid] = fw[f.ID]
		}
	}
	waves := make([]protocol.SeedRequest, nWaves)
	for i := range waves {
		waves[i] = protocol.SeedRequest{UserID: "miner", Wave: i}
	}
	for _, p := range pairs {
		w, ok := pairWave[p.PairID]
		if !ok { // noise pair: bucket by its session
			w = sessionBucket(sessionOf(p.SessionID), nSessions, nWaves)
		}
		waves[w].Pairs = append(waves[w].Pairs, p)
	}
	// Subjects: subj-<attr> → attribute wave (via any linked pair's wave).
	subjWave := map[string]int{}
	for _, l := range links {
		if w, ok := pairWave[l.PairID]; ok {
			subjWave[l.SubjectID] = w
		}
	}
	for _, s := range subjects {
		w := subjWave[s.ID] // defaults to 0 if unlinked (shouldn't happen)
		waves[w].Subjects = append(waves[w].Subjects, s)
	}
	for _, l := range links {
		w := pairWave[l.PairID]
		waves[w].Links = append(waves[w].Links, l)
	}
	return waves
}

// sessionOf parses the integer index out of a "sess-<i>" SessionID (−1 on
// malformed input, which buckets to wave 0).
func sessionOf(sessionID string) int {
	s := strings.TrimPrefix(sessionID, "sess-")
	n, err := strconv.Atoi(s)
	if err != nil {
		return -1
	}
	return n
}

func sessionBucket(session, nSessions, nWaves int) int {
	if session < 0 || nSessions <= 0 || nWaves <= 1 {
		return 0
	}
	w := session * nWaves / nSessions
	if w >= nWaves {
		w = nWaves - 1
	}
	return w
}

// personaOptsFor scales the plan size to the memory-case quota so the question
// pool comfortably exceeds n at every run_size.
func personaOptsFor(n int) persona.Opts {
	switch {
	case n <= 8:
		return persona.Opts{Sessions: 5, Projects: 4, Trips: 3, Pets: 2, UpdateChains: 2, Reversals: 1, DecoyPeople: 4, DomainItems: 3, LongChain: 3}
	case n <= 25:
		return persona.DefaultOpts()
	default:
		return persona.Opts{Sessions: 9, Projects: 10, Trips: 8, Pets: 5, UpdateChains: 4, Reversals: 3, DecoyPeople: 10, DomainItems: 4, LongChain: 4}
	}
}

// memoryTypeWeight biases the v2 memory mix toward the types that actually
// discriminate among competent harnesses (multi-session synthesis, applied
// preference, real temporal, contradiction) and away from single-fact recall a
// good retriever always aces. Types not listed default to weight 1. The mix stays
// seed-independent: only the emphasis changes, not per-seed reproducibility.
var memoryTypeWeight = map[string]int{
	persona.QTMultiSession:          3,
	persona.QTTemporal:              3,
	persona.QTPreferenceApplication: 3,
	persona.QTContradiction:         2,
	persona.QTKnowledgeUpdate:       2,
	persona.QTInjection:             2,
	persona.QTAssistantRecall:       2,
	persona.QTAggregation:           2,
	persona.QTPointInTime:           2,
	persona.QTSingleSession:         1,
	persona.QTPreference:            1,
}

func typeWeight(t string) int {
	if w, ok := memoryTypeWeight[t]; ok && w > 0 {
		return w
	}
	return 1
}

// weightedTypeQuota splits n across types proportional to memoryTypeWeight,
// capped by availability, filling any shortfall (from flooring or caps)
// preferring higher-weight types with spare capacity. Deterministic: the fill
// order is (weight desc, name asc), independent of the run seed.
func weightedTypeQuota(types []string, avail map[string]int, n int) map[string]int {
	quota := make(map[string]int, len(types))
	if n <= 0 || len(types) == 0 {
		return quota
	}
	total := 0
	for _, t := range types {
		total += typeWeight(t)
	}
	assigned := 0
	for _, t := range types {
		q := n * typeWeight(t) / total
		if q > avail[t] {
			q = avail[t]
		}
		quota[t] = q
		assigned += q
	}
	fillOrder := append([]string(nil), types...)
	sort.SliceStable(fillOrder, func(i, j int) bool {
		wi, wj := typeWeight(fillOrder[i]), typeWeight(fillOrder[j])
		if wi != wj {
			return wi > wj
		}
		return fillOrder[i] < fillOrder[j]
	})
	for assigned < n {
		progressed := false
		for _, t := range fillOrder {
			if assigned >= n {
				break
			}
			if quota[t] < avail[t] {
				quota[t]++
				assigned++
				progressed = true
			}
		}
		if !progressed {
			break
		}
	}
	return quota
}

// stratifyByType selects up to n questions with a fixed, seed-independent
// per-type quota (like the tool suite's category stratification): WHICH
// questions within a type varies by seed; HOW MANY of each type does not.
func stratifyByType(r *rand.Rand, pool []persona.Question, n int) []persona.Question {
	if n <= 0 || len(pool) == 0 {
		return nil
	}
	byType := map[string][]persona.Question{}
	var typeOrder []string
	for _, q := range pool {
		if _, seen := byType[q.Type]; !seen {
			typeOrder = append(typeOrder, q.Type)
		}
		byType[q.Type] = append(byType[q.Type], q)
	}
	sort.Strings(typeOrder)
	avail := make(map[string]int, len(typeOrder))
	for t, qs := range byType {
		avail[t] = len(qs)
	}
	quota := weightedTypeQuota(typeOrder, avail, n)

	var out []persona.Question
	for _, t := range typeOrder {
		qs := byType[t]
		perm := r.Perm(len(qs))
		for i := 0; i < quota[t] && i < len(qs); i++ {
			out = append(out, qs[perm[i]])
		}
	}
	return out
}

// synthesizeSubjects builds Tier-A prepared seeding: one subject per persona
// attribute, linking the evidence pairs of the (non-distractor) self facts of
// that attribute, EXCEPT facts in rawFacts (Tier B), whose pairs are seeded
// without any subject scaffolding. Deterministic (timeline attribute order).
func synthesizeSubjects(plan *persona.Plan, evidence map[string]string, rawFacts map[string]bool) ([]protocol.Subject, []protocol.SubjectLink) {
	var attrOrder []string
	seenAttr := map[string]bool{}
	linksByAttr := map[string][]protocol.SubjectLink{}

	for _, f := range plan.Facts {
		if f.Entity != "self" || f.Kind == persona.KindDistractor || rawFacts[f.ID] {
			continue
		}
		pid, ok := evidence[f.ID]
		if !ok {
			continue
		}
		if !seenAttr[f.Attribute] {
			seenAttr[f.Attribute] = true
			attrOrder = append(attrOrder, f.Attribute)
		}
		sid := "subj-" + f.Attribute
		linksByAttr[f.Attribute] = append(linksByAttr[f.Attribute], protocol.SubjectLink{SubjectID: sid, PairID: pid})
	}

	subjects := make([]protocol.Subject, 0, len(attrOrder))
	var links []protocol.SubjectLink
	for _, attr := range attrOrder {
		label := titleAttr(attr)
		subjects = append(subjects, protocol.Subject{
			ID:              "subj-" + attr,
			SubjectText:     label,
			DescriptionText: "The user's recorded facts about their " + strings.ToLower(label) + ".",
		})
		links = append(links, linksByAttr[attr]...)
	}
	return subjects, links
}

// twinFamiliesFor returns how many metamorphic invariance families a run of n
// memory cases carries. One family makes the metamorphic-consistency factor a
// per-run coin flip (a single split flips the whole rate); several let the factor
// average, cutting its run-to-run noise ~1/sqrt(families). Scaled so twin cases
// stay a bounded share of the budget (~n/16: full=3, medium/small=1).
func twinFamiliesFor(n int) int {
	g := n / 16
	if g < 1 {
		g = 1
	}
	return g
}

// selectTwinFamilies keeps the first g whole families (grouped by TwinGroup, in
// first-seen order) from the pool and drops the rest. Families are kept intact so
// every sibling of a scored group is present; first-seen order is the seed-keyed
// attribute order invarianceTwins emitted.
func selectTwinFamilies(pool []persona.Question, g int) []persona.Question {
	if g <= 0 {
		return nil
	}
	order := make([]string, 0)
	seen := map[string]bool{}
	for _, q := range pool {
		if !seen[q.TwinGroup] {
			seen[q.TwinGroup] = true
			order = append(order, q.TwinGroup)
		}
	}
	if g >= len(order) {
		return pool
	}
	keep := map[string]bool{}
	for _, grp := range order[:g] {
		keep[grp] = true
	}
	out := make([]persona.Question, 0, len(pool))
	for _, q := range pool {
		if keep[q.TwinGroup] {
			out = append(out, q)
		}
	}
	return out
}

// titleAttr renders an attribute key ("favorite_cuisine") as a subject label
// ("Favorite Cuisine").
func titleAttr(attr string) string {
	words := strings.Split(attr, "_")
	for i, w := range words {
		if w == "" {
			continue
		}
		words[i] = strings.ToUpper(w[:1]) + w[1:]
	}
	return strings.Join(words, " ")
}
