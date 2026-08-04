package persona

import (
	"encoding/json"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestDeriveQuestionsDeterministic(t *testing.T) {
	p := BuildPlan(42, DefaultOpts())
	a, _ := json.Marshal(DeriveQuestions(p))
	b, _ := json.Marshal(DeriveQuestions(p))
	if string(a) != string(b) {
		t.Fatal("DeriveQuestions is not deterministic for a fixed plan")
	}
}

// TestAskVariantDeterministicAndSeedVarying pins the deterministic
// phrasing-variant contract: a fixed (seed, key) always picks the same variant,
// distinct seeds vary it, and single/empty option sets are handled.
func TestAskVariantDeterministicAndSeedVarying(t *testing.T) {
	opts := []string{"a", "b", "c", "d"}
	if askVariant(7, "rec:city", opts) != askVariant(7, "rec:city", opts) {
		t.Fatal("askVariant not stable for a fixed (seed,key)")
	}
	// Across many seeds the same key should land on more than one variant
	// (surface anti-memorization), not a constant.
	seen := map[string]bool{}
	for s := int64(0); s < 200; s++ {
		seen[askVariant(s, "rec:city", opts)] = true
	}
	if len(seen) < 2 {
		t.Fatalf("askVariant never varied across seeds: %v", seen)
	}
	if askVariant(1, "k", nil) != "" {
		t.Fatal("empty options must yield empty string")
	}
	if askVariant(1, "k", []string{"only"}) != "only" {
		t.Fatal("single option must be returned as-is")
	}
}

// TestQuestionPhrasingVariesAcrossSeeds: two different submission seeds should
// produce at least some differently-worded questions for the same attribute
// (surface variation comes from seeded template selection).
func TestQuestionPhrasingVariesAcrossSeeds(t *testing.T) {
	textByID := func(seed int64) map[string]string {
		m := map[string]string{}
		for _, q := range DeriveQuestions(BuildPlan(seed, DefaultOpts())) {
			m[q.ID] = q.Text
		}
		return m
	}
	a, b := textByID(1), textByID(2)
	diff := 0
	for id, ta := range a {
		if tb, ok := b[id]; ok && ta != tb {
			diff++
		}
	}
	if diff == 0 {
		t.Fatal("no question rephrased between seeds — phrasing variants not seed-varying")
	}
}

// TestQuestionTypeCoverage checks every question type is derivable from a
// default plan: the per-type discrimination gate needs each present.
func TestQuestionTypeCoverage(t *testing.T) {
	p := BuildPlan(42, DefaultOpts())
	qs := DeriveQuestions(p)
	seen := map[string]int{}
	tiers := map[string]int{}
	for _, q := range qs {
		seen[q.Type]++
		tiers[q.Tier]++
	}
	for _, want := range []string{
		QTSingleSession, QTMultiSession, QTTemporal, QTKnowledgeUpdate,
		QTPreference, QTPreferenceApplication, QTContradiction, QTAbstention,
	} {
		if seen[want] == 0 {
			t.Errorf("no %s questions derived", want)
		}
	}
	for _, tier := range []string{TierEasy, TierMedium, TierHard} {
		if tiers[tier] == 0 {
			t.Errorf("no %s-tier questions derived", tier)
		}
	}
}

// TestAnswersMatchGroundTruth verifies each question's canonical answer is the
// actual plan ground truth: recall answers equal the current fact value; the
// count answer equals the number of list items; knowledge-update answers equal
// the CURRENT (not superseded) value.
func TestAnswersMatchGroundTruth(t *testing.T) {
	p := BuildPlan(7, DefaultOpts())
	qs := DeriveQuestions(p)
	for _, q := range qs {
		switch q.Type {
		case QTSingleSession, QTPreference, QTKnowledgeUpdate:
			f, ok := p.FactByID(q.Evidence[0])
			if !ok {
				t.Fatalf("%s: evidence fact %s missing", q.ID, q.Evidence[0])
			}
			// Evidence[0] is the current fact; its value is the answer.
			if q.Type == QTKnowledgeUpdate && !f.Current {
				t.Fatalf("%s: knowledge-update evidence[0] %s is not the current value", q.ID, f.ID)
			}
			if q.Answer != f.Value {
				t.Fatalf("%s: answer %q != fact value %q", q.ID, q.Answer, f.Value)
			}
		case QTMultiSession:
			if !q.Numeric {
				continue
			}
			if strconv.Itoa(len(q.Evidence)) != q.Answer {
				t.Fatalf("%s: count answer %q != evidence count %d", q.ID, q.Answer, len(q.Evidence))
			}
		case QTAbstention:
			if !q.Abstain {
				t.Fatalf("%s: abstention question not marked Abstain", q.ID)
			}
			if len(q.Evidence) != 0 {
				t.Fatalf("%s: abstention question has evidence (should be needle-absent)", q.ID)
			}
		}
	}
}

// TestDomainCoverage checks that every persona is assigned exactly one
// professional domain, that all three domains are reachable across seeds, and
// that a domain's specialist facts flow through DeriveQuestions as real
// questions with correct ground truth (the data-driven-derivation contract).
func TestDomainCoverage(t *testing.T) {
	domainAttrs := map[string][]string{
		"software": {"primary_language", "code_editor", "service"},
		"medical":  {"diagnosis", "medication", "allergy"},
		"legal":    {"practice_area", "bar_admission", "legal_matter"},
		"finance":  {"risk_tolerance", "brokerage", "holding"},
	}
	// attr → its domain, for the exclusivity check.
	attrDomain := map[string]string{}
	for d, attrs := range domainAttrs {
		for _, a := range attrs {
			attrDomain[a] = d
		}
	}

	seenDomain := map[string]bool{}
	for seed := int64(0); seed < 60; seed++ {
		p := BuildPlan(seed, DefaultOpts())

		// exactly one domain present in the plan's facts.
		present := map[string]bool{}
		for _, f := range p.Facts {
			if d, ok := attrDomain[f.Attribute]; ok {
				present[d] = true
			}
		}
		if len(present) != 1 {
			t.Fatalf("seed %d: expected exactly one domain, got %v", seed, present)
		}
		var dom string
		for d := range present {
			dom = d
		}
		seenDomain[dom] = true

		// the domain's scalar attributes must surface as a recall OR
		// knowledge-update question (evidence[0] == the fact) whose answer is the
		// canonical value.
		recallByFact := map[string]Question{}
		for _, q := range DeriveQuestions(p) {
			if (q.Type == QTSingleSession || q.Type == QTKnowledgeUpdate) && len(q.Evidence) > 0 {
				recallByFact[q.Evidence[0]] = q
			}
		}
		for _, f := range p.Facts {
			if attrDomain[f.Attribute] != dom || !f.Current || f.Kind != KindScalar {
				continue
			}
			q, ok := recallByFact[f.ID]
			if !ok {
				t.Fatalf("seed %d: domain scalar %s produced no recall/update question", seed, f.ID)
			}
			if q.Answer != f.Value {
				t.Fatalf("seed %d: %s answer %q != value %q", seed, q.ID, q.Answer, f.Value)
			}
		}
	}
	for d := range domainAttrs {
		if !seenDomain[d] {
			t.Errorf("domain %q never assigned across 60 seeds", d)
		}
	}
}

// TestTrajectoryAndMultiHop verifies the N-state sequence questions against the
// plan's ground truth: the ordered-history answer is the chain values in order,
// the previous-value answer is the second-to-last, and a multi-hop state-at-event
// answer is the chain value that held at the event's session, a genuinely PAST
// (superseded) value, not the current one.
func TestTrajectoryAndMultiHop(t *testing.T) {
	sawTraj, sawMH := false, false
	for seed := int64(0); seed < 40; seed++ {
		p := BuildPlan(seed, DefaultOpts())
		qs := DeriveQuestions(p)
		for _, q := range qs {
			switch {
			case len(q.ID) >= 6 && q.ID[:6] == "q-hist":
				sawTraj = true
				ch := scalarChain(p, chainAttrOf(p, q.Evidence))
				want := make([]string, 0, len(ch))
				for _, f := range ch {
					want = append(want, f.Value)
				}
				if q.Answer != joinComma(want) {
					t.Fatalf("seed %d %s: history %q != chain %v", seed, q.ID, q.Answer, want)
				}
			case len(q.ID) >= 6 && q.ID[:6] == "q-prev":
				ch := scalarChain(p, chainAttrOf(p, q.Evidence))
				if len(ch) < 2 || q.Answer != ch[len(ch)-2].Value {
					t.Fatalf("seed %d %s: previous %q != chain[-2]", seed, q.ID, q.Answer)
				}
			case len(q.ID) >= 4 && q.ID[:4] == "q-mh":
				sawMH = true
				// Evidence = [listFact, stateFact]; stateFact must be non-current.
				sf, ok := p.FactByID(q.Evidence[1])
				if !ok || sf.Current {
					t.Fatalf("seed %d %s: multi-hop answer is not a past value", seed, q.ID)
				}
				if q.Answer != sf.Value {
					t.Fatalf("seed %d %s: answer %q != state value %q", seed, q.ID, q.Answer, sf.Value)
				}
				// Recompute the state at the event session independently.
				lf, _ := p.FactByID(q.Evidence[0])
				ch := scalarChain(p, sf.Attribute)
				var recomputed string
				best := -1
				for _, f := range ch {
					if f.Session <= lf.Session && f.Session > best {
						best, recomputed = f.Session, f.Value
					}
				}
				if recomputed != q.Answer {
					t.Fatalf("seed %d %s: recomputed state %q != answer %q", seed, q.ID, recomputed, q.Answer)
				}
			}
		}
	}
	if !sawTraj {
		t.Error("no trajectory (N-state) questions derived across 40 seeds")
	}
	if !sawMH {
		t.Error("no multi-hop questions derived across 40 seeds")
	}
}

// chainAttrOf returns the attribute of the first evidence fact (a chain member).
func chainAttrOf(p *Plan, evidence []string) string {
	if len(evidence) == 0 {
		return ""
	}
	f, _ := p.FactByID(evidence[0])
	return f.Attribute
}

// TestKnowledgeUpdateAnswerIsLatest guards the latest-value-wins semantics: the
// update answer must be the current value and must differ from the superseded one.
func TestKnowledgeUpdateAnswerIsLatest(t *testing.T) {
	p := BuildPlan(3, DefaultOpts())
	found := false
	for _, q := range DeriveQuestions(p) {
		if q.Type != QTKnowledgeUpdate {
			continue
		}
		found = true
		cur, _ := p.FactByID(q.Evidence[0])
		old, _ := p.FactByID(q.Evidence[1])
		if cur.Value == old.Value {
			t.Fatalf("%s: current and superseded values identical (%q)", q.ID, cur.Value)
		}
		if q.Answer != cur.Value {
			t.Fatalf("%s: answer is not the latest value", q.ID)
		}
	}
	if !found {
		t.Skip("no knowledge-update question in this plan")
	}
}

// TestAssistantRecallIsAssistantSide: every assistant-recall question's answer
// lives in the assistant turn of its evidence beat and NOT the user turn, so it
// genuinely tests recalling what the assistant said.
func TestAssistantRecallIsAssistantSide(t *testing.T) {
	p := BuildPlan(7, DefaultOpts())
	beat := map[string]Beat{}
	for _, s := range p.Sessions {
		for _, b := range s.Beats {
			if b.Kind == BeatFact {
				beat[b.FactID] = b
			}
		}
	}
	n := 0
	for _, q := range DeriveQuestions(p) {
		if q.Type != QTAssistantRecall {
			continue
		}
		n++
		b := beat[q.Evidence[0]]
		if !strings.Contains(b.AsstText, q.Answer) {
			t.Fatalf("%s: answer %q not in assistant text %q", q.ID, q.Answer, b.AsstText)
		}
		if strings.Contains(b.UserText, q.Answer) {
			t.Fatalf("%s: answer %q leaked into user text %q", q.ID, q.Answer, b.UserText)
		}
	}
	if n == 0 {
		t.Fatal("expected assistant-recall questions to be derived")
	}
}

// TestAggregationCountMatchesMentions: the aggregation question's answer equals
// the number of recurring-topic mentions, and those mentions span distinct
// sessions (so it is genuinely a cross-session count).
func TestAggregationCountMatchesMentions(t *testing.T) {
	for _, seed := range []int64{1, 7, 42, 100} {
		p := BuildPlan(seed, DefaultOpts())
		sessOf := map[string]int{}
		mentions := 0
		for _, f := range p.Facts {
			if f.Kind == KindRecurring {
				mentions++
				sessOf[f.ID] = f.Session
			}
		}
		var q *Question
		qs := DeriveQuestions(p)
		for i := range qs {
			if qs[i].Type == QTAggregation {
				qq := qs[i]
				q = &qq
				break
			}
		}
		if mentions < 2 {
			t.Fatalf("seed %d: expected >=2 recurring mentions, got %d", seed, mentions)
		}
		if q == nil {
			t.Fatalf("seed %d: no aggregation question derived", seed)
		}
		if q.Answer != strconv.Itoa(mentions) || len(q.Evidence) != mentions {
			t.Fatalf("seed %d: answer %q / evidence %d != mentions %d", seed, q.Answer, len(q.Evidence), mentions)
		}
		distinct := map[int]bool{}
		for _, id := range q.Evidence {
			distinct[sessOf[id]] = true
		}
		if len(distinct) < 2 {
			t.Fatalf("seed %d: mentions must span >=2 sessions, got %d", seed, len(distinct))
		}
	}
}

// TestSometimesAttributesVaryAcrossSeeds is the anti-hard-coding property of the
// procedural abstention boundary: for a sometimes-present attribute, different
// seeds must produce all three states: self-stated (answerable recall),
// decoy-held (false-premise abstention), and absent (pure abstention). A
// harness therefore cannot statically map a question string to decline-vs-answer.
func TestSometimesAttributesVaryAcrossSeeds(t *testing.T) {
	const attr = "blood_type"
	var sawRecall, sawFP, sawAbs bool
	for seed := int64(0); seed < 120; seed++ {
		qs := DeriveQuestions(BuildPlan(seed, DefaultOpts()))
		for _, q := range qs {
			switch q.ID {
			case "q-rec-" + attr:
				sawRecall = true
				if q.Abstain || q.Answer == "" {
					t.Fatalf("seed %d: self-stated %s should be answerable recall", seed, attr)
				}
			case "q-absfp-" + attr:
				sawFP = true
				if !q.Abstain {
					t.Fatalf("seed %d: decoy-held %s should be an abstention", seed, attr)
				}
			case "q-abs-" + attr:
				sawAbs = true
				if !q.Abstain {
					t.Fatalf("seed %d: absent %s should be an abstention", seed, attr)
				}
			}
		}
		if sawRecall && sawFP && sawAbs {
			return
		}
	}
	t.Fatalf("expected all three states across seeds: recall=%v falsePremise=%v absent=%v",
		sawRecall, sawFP, sawAbs)
}

// TestInjectionPayloadPerSeed checks the payload rotates with the seed, never
// collides with the true answer, and lands verbatim in both the question text
// and the Forbidden field (the compliance tripwire).
func TestInjectionPayloadPerSeed(t *testing.T) {
	payloads := map[string]bool{}
	for seed := int64(0); seed < 20; seed++ {
		qs := DeriveQuestions(BuildPlan(seed, DefaultOpts()))
		for _, q := range qs {
			if q.Type != QTInjection {
				continue
			}
			if q.Forbidden == "" {
				t.Fatalf("seed %d %s: injection question without a Forbidden payload", seed, q.ID)
			}
			if !strings.Contains(q.Text, q.Forbidden) {
				t.Fatalf("seed %d %s: payload %q not embedded in the attack text", seed, q.ID, q.Forbidden)
			}
			if q.Forbidden == q.Answer {
				t.Fatalf("seed %d %s: payload equals the true answer %q", seed, q.ID, q.Answer)
			}
			payloads[q.Forbidden] = true
		}
	}
	if len(payloads) < 10 {
		t.Fatalf("expected many distinct payloads across seeds/attributes, got %d", len(payloads))
	}
}

// TestCanaryPresentAndDistinct checks every plan carries exactly one canary
// question whose answer is the seeded nonce, whose forbidden value is the bait,
// and whose nonce varies across seeds (un-cacheable).
func TestCanaryPresentAndDistinct(t *testing.T) {
	nonces := map[string]bool{}
	for seed := int64(0); seed < 30; seed++ {
		p := BuildPlan(seed, DefaultOpts())
		var cq *Question
		for i := range DeriveQuestions(p) {
			q := DeriveQuestions(p)[i]
			if q.Type == QTCanary {
				cq = &q
				break
			}
		}
		if cq == nil {
			t.Fatalf("seed %d: no canary question", seed)
		}
		if cq.Answer != CanaryNonce(seed) {
			t.Fatalf("seed %d: canary answer %q != nonce %q", seed, cq.Answer, CanaryNonce(seed))
		}
		if cq.Forbidden != canaryBait(seed) || cq.Forbidden == cq.Answer {
			t.Fatalf("seed %d: canary bait wrong or equals answer", seed)
		}
		// The nonce must be seeded into the haystack so it is retrievable.
		var seeded bool
		for _, f := range p.Facts {
			if f.Kind == KindCanary && f.Value == cq.Answer {
				seeded = true
			}
		}
		if !seeded {
			t.Fatalf("seed %d: canary nonce not seeded into the haystack", seed)
		}
		nonces[cq.Answer] = true
	}
	if len(nonces) < 25 {
		t.Fatalf("canary nonce should vary across seeds, got %d distinct", len(nonces))
	}
}

// TestCanaryDecoysNeverShareASession pins the cross-session attribution
// contract: the two attributed canary decoys land in distinct sessions on every
// seed, so surfacing the wrong code always requires crossing a session boundary
// rather than reading two codes off one transcript. Guards the plan-side
// collision fix (the second decoy's session steps off the first's on a draw
// collision).
func TestCanaryDecoysNeverShareASession(t *testing.T) {
	for _, opts := range []Opts{DefaultOpts(), fullOpts()} {
		for seed := int64(1); seed <= 300; seed++ {
			p := BuildPlan(seed, opts)
			s1, s2 := -1, -1
			for _, f := range p.Facts {
				switch f.ID {
				case "f-canary-bait":
					s1 = f.Session
				case "f-canary-bait-2":
					s2 = f.Session
				}
			}
			if s1 < 0 || s2 < 0 {
				t.Fatalf("seed %d: missing canary decoy fact (bait session %d, bait2 session %d)", seed, s1, s2)
			}
			if s1 == s2 {
				t.Fatalf("seed %d: both canary decoys landed in session %d, breaking cross-session attribution", seed, s1)
			}
		}
	}
}

// TestComputedAndDrmModalities checks the DRM lure asks about an unvisited city
// (abstention) and the filtered aggregation counts trips after a job change.
func TestComputedAndDrmModalities(t *testing.T) {
	var sawDRM, sawFiltAgg bool
	for seed := int64(0); seed < 60 && !(sawDRM && sawFiltAgg); seed++ {
		p := BuildPlan(seed, DefaultOpts())
		trips := map[string]bool{}
		for _, f := range p.Facts {
			if f.Kind == KindList && f.Attribute == "trip" {
				trips[f.Value] = true
			}
		}
		for _, q := range DeriveQuestions(p) {
			switch q.ID {
			case "q-drm-trip":
				sawDRM = true
				if !q.Abstain {
					t.Fatalf("seed %d: DRM lure must be an abstention", seed)
				}
			case "q-filtagg-trip-after-job":
				sawFiltAgg = true
				if q.Type != QTComputed || !q.Numeric {
					t.Fatalf("seed %d: filtered-agg must be a numeric computed answer", seed)
				}
				// Recompute the expected count independently.
				changeSession := -1
				for _, f := range p.Facts {
					if f.Attribute == "employer" && f.Entity == "self" && f.Current && f.Supersedes != "" {
						changeSession = f.Session
					}
				}
				want := 0
				for _, f := range p.Facts {
					if f.Kind == KindList && f.Attribute == "trip" && f.Session > changeSession {
						want++
					}
				}
				if q.Answer != strconv.Itoa(want) {
					t.Fatalf("seed %d: filtered-agg answer %q != recomputed %d", seed, q.Answer, want)
				}
			}
		}
	}
	if !sawDRM || !sawFiltAgg {
		t.Fatalf("expected both modalities across seeds: drm=%v filtagg=%v", sawDRM, sawFiltAgg)
	}
}

// TestAnswerKindsAndDistractors pins the judge-free grading contract: every
// question carries the grading data its AnswerKind needs, decline questions
// carry fabrication distractors, and a user's own superseded chain values are
// never distractors (mentioning old state next to the current answer is
// correct, not confusion).
func TestAnswerKindsAndDistractors(t *testing.T) {
	for _, seed := range []int64{7, 42, 123456789} {
		p := BuildPlan(seed, DefaultOpts())
		selfVals := map[string]map[string]bool{} // attr -> own values (whole chain)
		for _, f := range p.Facts {
			if f.Entity == "self" {
				if selfVals[f.Attribute] == nil {
					selfVals[f.Attribute] = map[string]bool{}
				}
				selfVals[f.Attribute][f.Value] = true
			}
		}
		for _, q := range DeriveQuestions(p) {
			switch q.Kind {
			case protocol.AnswerList, protocol.AnswerOrderedList:
				if len(q.Items) == 0 {
					t.Fatalf("seed %d %s: %s kind without items", seed, q.ID, q.Kind)
				}
			case protocol.AnswerReversal:
				if len(q.Items) != 1 {
					t.Fatalf("seed %d %s: reversal must carry the bare value in Items", seed, q.ID)
				}
			case protocol.AnswerDecline:
				if q.ID != "q-drm-trip" && len(q.Distractors) == 0 {
					t.Fatalf("seed %d %s: decline question without fabrication distractors", seed, q.ID)
				}
			case protocol.AnswerNumber:
				if !q.Numeric {
					t.Fatalf("seed %d %s: number kind on a non-numeric question", seed, q.ID)
				}
			}
			for _, d := range q.Distractors {
				if d == q.Answer {
					t.Fatalf("seed %d %s: the expected answer is its own distractor", seed, q.ID)
				}
				// Chain exclusion: no distractor is one of the user's own values
				// for the question's attribute (the id suffix names it).
				for attr, vals := range selfVals {
					if vals[d] && strings.Contains(q.ID, attr) {
						t.Fatalf("seed %d %s: distractor %q is the user's own %s value", seed, q.ID, d, attr)
					}
				}
			}
		}
	}
}

// TestCoinShapedDistinctButSameFamily locks in the grammar-collision invariant:
// within a run every coined token shares ONE shape family (so a shape-keyed
// output scrubber cannot separate forbidden from required tokens) yet the VALUES
// are all distinct (so a required answer, e.g. the canary nonce, is never equal
// to the forbidden injection payload). Regression guard for the LCG-low-bit
// collision that made distinct salts yield near-identical tokens.
func TestCoinShapedDistinctButSameFamily(t *testing.T) {
	family := func(tok string) string {
		switch {
		case len(tok) >= 3 && tok[:3] == "VK-":
			return "vk"
		case contains(tok, '_'):
			return "snake"
		case len(tok) >= 3 && tok[0] >= '0' && tok[0] <= '9' && tok[1] >= '0' && tok[1] <= '9' && tok[2] == '-':
			return "segmented"
		default:
			return "syllable"
		}
	}
	for seed := int64(1); seed <= 300; seed++ {
		toks := map[string]string{
			"nonce":   CanaryNonce(seed),
			"bait":    canaryBait(seed),
			"bait2":   canaryBait2(seed),
			"payload": InjectionPayload(seed),
			"lc-save": CoinShaped(seed, "lc|lc-save"),
			"lc-upd":  CoinShaped(seed, "lc|lc-upd-new"),
			"lc-del":  CoinShaped(seed, "lc|lc-del"),
		}
		seen := map[string]string{}
		var fam string
		for name, v := range toks {
			if v == "" {
				t.Fatalf("seed %d: %s produced an empty token", seed, name)
			}
			if prev, dup := seen[v]; dup {
				t.Fatalf("seed %d: value collision %q shared by %s and %s (grammar collision must be by shape, not value)", seed, v, prev, name)
			}
			seen[v] = name
			f := family(v)
			if fam == "" {
				fam = f
			} else if f != fam {
				t.Fatalf("seed %d: %s token %q is family %q, want shared family %q", seed, name, v, f, fam)
			}
		}
	}
}

func contains(s string, b byte) bool {
	for i := 0; i < len(s); i++ {
		if s[i] == b {
			return true
		}
	}
	return false
}

// TestRedTeamCurrentValueParserFailsPointInTime locks the point-in-time family
// against the most common deterministic shortcut: answering the as-of question
// with the CURRENT value (ignoring the date). The as-of answer is a superseded
// value, so a current-value answer misses it and scores 0, while the correct
// superseded value scores 1. This does not prove the family is un-solvable (a
// correct timeline resolver still solves it — the durable defense there is the
// screener oracle forcing model invocation), but it guards the difficulty from
// regressing to a value the current-state shortcut satisfies.
func TestRedTeamCurrentValueParserFailsPointInTime(t *testing.T) {
	total, differing := 0, 0
	for seed := int64(1); seed <= 40; seed++ {
		p := BuildPlan(seed, Opts{Sessions: 9, Projects: 6, Trips: 4, Pets: 2, UpdateChains: 4, Reversals: 2, DecoyPeople: 6, DomainItems: 3, LongChain: 4})
		qs := DeriveQuestions(p)
		current := map[string]string{}
		for _, f := range p.Facts {
			if f.Entity == "self" && f.Current {
				current[f.Attribute] = f.Value
			}
		}
		for _, q := range qs {
			if q.Type != QTPointInTime {
				continue
			}
			total++
			attr := strings.TrimPrefix(q.ID, "q-pit-")
			cur, ok := current[attr]
			if !ok || cur == q.Answer {
				// as-of == current can happen legitimately for a chain that
				// returned to an earlier value; counted (not skipped) so a
				// generator regression to always-current answers shows up as a
				// collapse in the `differing` fraction asserted below.
				continue
			}
			differing++
			// Grade the case as actually emitted (its real distractors), so the
			// gate reflects the shipped MemoryCase, not a bare stand-in.
			mc := protocol.MemoryCase{
				QuestionType:      q.Type,
				ExpectedAnswer:    q.Answer,
				AnswerKind:        q.Kind,
				DistractorAnswers: q.Distractors,
			}
			// Current-value shortcut: answer with the present value → must miss.
			if v := grade.Memory(mc, protocol.RunResponse{Answer: cur, FinalText: "Your " + attr + " is " + cur + "."}); v.Score != 0 {
				t.Fatalf("seed %d attr %s: current-value shortcut scored %v (as-of=%q current=%q) — point-in-time regressed to a current-state answer",
					seed, attr, v.Score, q.Answer, cur)
			}
			// Correct as-of value scores full.
			if v := grade.Memory(mc, protocol.RunResponse{Answer: q.Answer, FinalText: "At that time it was " + q.Answer + "."}); v.Score != 1 {
				t.Fatalf("seed %d attr %s: correct as-of value %q scored %v", seed, attr, q.Answer, v.Score)
			}
		}
	}
	if total == 0 {
		t.Fatal("no point-in-time cases emitted across 40 seeds: the family has disappeared from question derivation")
	}
	// The generator must predominantly emit SUPERSEDED as-of answers. If it
	// regressed to answering the current value (the family losing its point), the
	// differing fraction collapses and this fails — the generator-side regression
	// the current-value grader check alone cannot see.
	if differing*2 < total {
		t.Fatalf("point-in-time regression: only %d/%d as-of answers differ from the current value — the family collapsed toward current-state answers", differing, total)
	}
	t.Logf("point-in-time gate: %d/%d cases have a superseded as-of answer; current-value shortcut fails all of them", differing, total)
}
