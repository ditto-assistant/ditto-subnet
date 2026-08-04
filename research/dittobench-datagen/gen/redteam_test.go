package gen

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// This file is the grep-baseline red-team GATE (audit recommendation #4). It runs
// a non-reasoning "parser" harness against freshly generated suites and asserts
// it cannot score, so a regression that re-opens a lexical shortcut fails CI. The
// canonical baseline is the OMNISCIENT DUMPER: a harness that returns the entire
// seeded haystack verbatim, i.e. the theoretical maximum of "I possess every
// word of the context." A robust benchmark must score it near zero, because
// possession is not selection and not reasoning.

// haystackText joins every seeded pair into the raw text a parser harness holds.
func haystackText(seed protocol.SeedRequest) string {
	var b strings.Builder
	for _, p := range seed.Pairs {
		b.WriteString(p.Prompt)
		b.WriteByte(' ')
		b.WriteString(p.Response)
		b.WriteByte('\n')
	}
	return b.String()
}

// dumperResponse is the omniscient dumper's answer to every question: the whole
// haystack. It carries every verbatim value, so if containment alone could pass,
// this would score 1 everywhere.
func dumperResponse(hay string) protocol.RunResponse {
	return protocol.RunResponse{FinalText: hay}
}

// TestRedTeamOmniscientDumperScoresNearZero is the headline gate: a harness that
// echoes the entire haystack must not pass. This proves the grader rewards
// SELECTION (routing to the asked fact, declining when absent, computing counts)
// rather than verbatim possession — the property the string-table exploit broke.
func TestRedTeamOmniscientDumperScoresNearZero(t *testing.T) {
	prof, _ := ProfileFor("full")
	var total float64
	var n int
	perType := map[string][]float64{}
	for seed := int64(1); seed <= 25; seed++ {
		sreq, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		hay := haystackText(sreq)
		resp := dumperResponse(hay)
		for _, c := range cases {
			s := grade.Memory(c, resp).Score
			total += s
			n++
			perType[c.QuestionType] = append(perType[c.QuestionType], s)
		}
	}
	mean := total / float64(n)
	t.Logf("omniscient-dumper mean score over %d cases: %.3f", n, mean)
	types := make([]string, 0, len(perType))
	for k := range perType {
		types = append(types, k)
	}
	sort.Strings(types)
	for _, k := range types {
		xs := perType[k]
		var sum float64
		for _, v := range xs {
			sum += v
		}
		t.Logf("  %-24s n=%3d mean=%.3f", k, len(xs), sum/float64(len(xs)))
	}
	// The dumper possesses every verbatim value yet must be well below a passing
	// score: the dump-guard and distractor scans zero value/list questions, and
	// numeric/temporal/abstention answers are not present to be echoed.
	if mean >= 0.20 {
		t.Fatalf("omniscient dumper scored %.3f (>=0.20): verbatim possession is passing the benchmark", mean)
	}
}

// TestRedTeamLiteralLabelCountUndercounts is the V4 gate: for every aggregation
// (recurring-mention count) case, a parser that counts literal occurrences of
// the topic label in the haystack must UNDERCOUNT the true answer, so a lexical
// counter is wrong. Coreference makes the full label appear once (the anchor).
//
// SCOPE: this gate proves the *label-only* counter fails, not that counting is
// model-required. The oblique referent is a fixed per-topic phrase, so a parser
// that greps `label OR coref` still recovers the count (see docs/anti-gaming.md
// "Deterministic solver families"). The model-forcing defense is the screener
// oracle plus the on-chain transform audit; this gate only locks the difficulty
// against a regression back to a label-only-countable surface.
func TestRedTeamLiteralLabelCountUndercounts(t *testing.T) {
	prof, _ := ProfileFor("full")
	checked := 0
	for seed := int64(1); seed <= 25; seed++ {
		sreq, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		hay := strings.ToLower(haystackText(sreq))
		for _, c := range cases {
			if c.QuestionType != "aggregation-count" {
				continue
			}
			// The topic label is the longest quoted-ish noun phrase in the question
			// that also occurs in the haystack; approximate by scanning the known
			// recurring labels present in the question text.
			label := recurringLabelInQuestion(c.Question)
			if label == "" {
				continue
			}
			literalCount := strings.Count(hay, strings.ToLower(label))
			// The literal count must be strictly less than the true answer, so the
			// cheap "count the label" strategy produces a wrong number.
			trueCount := c.ExpectedAnswer
			lc := fmt.Sprintf("%d", literalCount)
			if lc == trueCount {
				t.Fatalf("aggregation case %s: literal label count %d equals the true count %s — V4 coreference regressed (question %q)",
					c.ID, literalCount, trueCount, c.Question)
			}
			// And grading the literal count as the answer must score 0.
			if v := grade.Memory(c, protocol.RunResponse{FinalText: fmt.Sprintf("You brought it up %d times.", literalCount)}); v.Score == 1 {
				t.Fatalf("aggregation case %s: literal-count answer %d scored 1 (true %s) — V4 regressed", c.ID, literalCount, trueCount)
			}
			checked++
		}
	}
	if checked == 0 {
		t.Skip("no aggregation-count cases sampled")
	}
	t.Logf("V4 gate: %d aggregation cases, literal-label counting undercounts in all", checked)
}

// recurringLabelInQuestion returns the recurring-topic label named in an
// aggregation question, or "". Mirrors the persona recurringSpecs labels; kept
// here (not imported) so the gate reads the surface exactly as a parser would.
func recurringLabelInQuestion(q string) string {
	for _, lbl := range []string{"my ongoing back pain", "the Barton account", "my sourdough starter", "my thesis revisions"} {
		if strings.Contains(q, lbl) {
			return lbl
		}
	}
	return ""
}

// TestRedTeamReversalNotLexical is the V5 gate: the haystack evidence for an
// opinion reversal must NOT contain the classic hard-cessation lexicon (the
// first-generation tokens: "no longer"/"stopped"/"quit"/…), so a parser
// hard-coded to grep those cannot decide reversal-vs-standing.
//
// SCOPE: this gate proves the OLD hard lexicon is absent; it does NOT prove the
// classification is model-required. Deterministic grading credits a fixed
// sentiment lexicon (grade.cessationPhrases negative-stance group) and that
// lexicon appears verbatim in the evidence, so a source-holder greps the
// sentiment stems and classifies by membership. Any fixed credited lexicon is
// enumerable by a source-holder; the durable defense is the oracle + transform
// audit (docs/anti-gaming.md). This gate locks difficulty against a regression
// to the hard-lexical surface only.
func TestRedTeamReversalNotLexical(t *testing.T) {
	// The grader's cessation tokens (grade.cessationPhrases) a lexical parser
	// would grep for. Kept in sync deliberately; the gate fails if a reversal
	// statement reintroduces one into the haystack.
	cessation := []string{
		"no longer", "anymore", "any more", "gave it up", "given it up",
		"gave up", "given up", "stopped", "quit", "used to", "went off",
		"gone off", "lost interest", "don't do", "doesn't do", "not into",
	}
	prof, _ := ProfileFor("full")
	checkedSeeds := 0
	for seed := int64(1); seed <= 25; seed++ {
		sreq, _, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		// A reversal pair is one whose user text conveys a cooled stance. We can't
		// see fact kinds from the wire haystack, so assert the STRONGER property:
		// NO pair in the haystack uses a bare cessation token in the USER turn
		// (the persona reversalStmts/reversalAcks were rewritten to sentiment).
		for _, p := range sreq.Pairs {
			low := strings.ToLower(p.Prompt)
			for _, tok := range cessation {
				if containsWord(low, tok) {
					t.Fatalf("seed %d: haystack user turn contains cessation token %q (%q) — V5 lexical-reversal shortcut reopened",
						seed, tok, p.Prompt)
				}
			}
		}
		checkedSeeds++
	}
	t.Logf("V5 gate: %d seeds, no cessation-lexicon tokens in any haystack user turn", checkedSeeds)
}

// containsWord reports whether phrase occurs in text on word boundaries (so
// "quit" does not fire inside "quite").
func containsWord(text, phrase string) bool {
	for i := 0; ; {
		j := strings.Index(text[i:], phrase)
		if j < 0 {
			return false
		}
		j += i
		before := j == 0 || !isWordByte(text[j-1])
		after := j+len(phrase) >= len(text) || !isWordByte(text[j+len(phrase)])
		if before && after {
			return true
		}
		i = j + 1
	}
}

func isWordByte(b byte) bool {
	return b == '\'' || (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
}

// shapeClass classifies a coined token into its persona.CoinShaped family by
// structure, exactly as a token-shape output scrubber would. Used to prove the
// v3 GRAMMAR-COLLISION property: forbidden and required coined tokens share one
// family per run, so no shape rule separates "delete the injection payload" from
// "keep the canary/lifecycle answer".
func shapeClass(tok string) string {
	switch {
	case strings.HasPrefix(tok, "VK-"):
		return "vk"
	case strings.Contains(tok, "_"):
		return "snake"
	case len(tok) >= 3 && tok[0] >= '0' && tok[0] <= '9' && tok[1] >= '0' && tok[1] <= '9' && tok[2] == '-':
		return "segmented"
	default:
		return "syllable"
	}
}

// TestRedTeamShapeScrubberCannotSeparate is the grammar-collision gate: within a
// run the injection payload, the canary nonce, and a lifecycle answer token all
// share one shape family, so a scrubber keyed on token shape cannot delete the
// payload without also deleting required answers. AND the payload is planted in
// the haystack, so the alternative "delete coined tokens absent from my context"
// scrub keeps it. Either shape-keyed strategy therefore fails.
func TestRedTeamShapeScrubberCannotSeparate(t *testing.T) {
	prof, _ := ProfileFor("full")
	for seed := int64(1); seed <= 25; seed++ {
		payload := persona.InjectionPayload(seed)
		nonce := persona.CanaryNonce(seed)
		lcTok := persona.CoinShaped(seed, "lc|lc-save") // an actual lifecycle read-answer token
		if shapeClass(payload) != shapeClass(nonce) || shapeClass(nonce) != shapeClass(lcTok) {
			t.Fatalf("seed %d: coined tokens split across shape families (payload=%q nonce=%q lc=%q) — grammar collision broke; a shape scrubber could separate forbidden from required",
				seed, payload, nonce, lcTok)
		}
		// Context-membership scrub defense: the payload is planted once in the
		// haystack, so a "keep only tokens present in my context" rule keeps it.
		sreq, _, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		if !strings.Contains(haystackText(sreq), payload) {
			t.Fatalf("seed %d: injection payload %q not planted in the haystack — the context-membership scrub is no longer defeated", seed, payload)
		}
	}
}

// TestRedTeamNonceScrubDestroysCanary is the concrete consequence: a harness
// that scrubs coined-shaped tokens from its output to hide injection compliance
// also scrubs the canary answer, so it cannot pass the integrity probe. Proves
// the scrub is self-harming, not a free defense.
func TestRedTeamNonceScrubDestroysCanary(t *testing.T) {
	prof, _ := ProfileFor("full")
	checked := 0
	for seed := int64(1); seed <= 25; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		nonce := persona.CanaryNonce(seed)
		for _, c := range cases {
			if c.QuestionType != "canary" {
				continue
			}
			// A correct answer names the nonce; the shape scrubber removes every
			// token of the nonce's family, including the nonce itself.
			correct := "Your verification code is " + nonce + "."
			scrubbed := stripShapeFamily(correct, shapeClass(nonce))
			if grade.Memory(c, protocol.RunResponse{FinalText: scrubbed, Answer: scrubbed}).Score != 0 {
				t.Fatalf("seed %d: canary survived nonce scrubbing (%q) — the scrub is not self-harming", seed, scrubbed)
			}
			checked++
		}
	}
	if checked == 0 {
		t.Skip("no canary cases sampled")
	}
	t.Logf("nonce-scrub gate: %d canary cases, scrubbing coined tokens zeros every one", checked)
}

// stripShapeFamily removes whitespace-delimited tokens of the given shape family
// from text, mimicking a token-shape output scrubber.
func stripShapeFamily(text, family string) string {
	fields := strings.Fields(text)
	kept := fields[:0]
	for _, f := range fields {
		trimmed := strings.Trim(f, ".,!?;:\"'")
		if shapeClass(trimmed) == family && looksCoined(trimmed) {
			continue
		}
		kept = append(kept, f)
	}
	return strings.Join(kept, " ")
}

// looksCoined reports whether a token is plausibly a coined high-entropy token
// (has a digit or an internal delimiter and is not an ordinary word), so the
// scrubber does not strip ordinary prose words that happen to fall in a family.
func looksCoined(tok string) bool {
	hasDigit, hasDelim := false, false
	for i := 0; i < len(tok); i++ {
		switch {
		case tok[i] >= '0' && tok[i] <= '9':
			hasDigit = true
		case tok[i] == '-' || tok[i] == '_':
			hasDelim = true
		}
	}
	return hasDigit || hasDelim || strings.HasPrefix(tok, "VK-")
}

// TestRedTeamSelfProfileDumpScoresZero is the dump-guard gate against the
// archetype-extractor shortcut: a parser that cannot route to the asked
// attribute and instead emits the user's whole current self-profile (the answer
// plus every other current scalar value) must be zeroed by the dump guard on
// value-recall cases that carry one.
func TestRedTeamSelfProfileDumpScoresZero(t *testing.T) {
	prof, _ := ProfileFor("full")
	guarded, tripped := 0, 0
	for seed := int64(1); seed <= 25; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		for _, c := range cases {
			if len(c.DumpGuard) < grade.DumpFloor(len(c.DumpGuard)) {
				continue
			}
			// The self-table dump: the asked value plus every other current self
			// value, exactly what an attribute-blind extractor emits every turn.
			dump := c.ExpectedAnswer + ". Also: " + strings.Join(c.DumpGuard, ", ") + "."
			guarded++
			if grade.Memory(c, protocol.RunResponse{FinalText: dump, Answer: dump}).Score == 0 {
				tripped++
			} else {
				t.Fatalf("case %s: whole-self-profile dump scored nonzero — dump guard did not catch the archetype-extractor shortcut", c.ID)
			}
		}
	}
	if guarded == 0 {
		t.Skip("no dump-guarded cases sampled")
	}
	t.Logf("self-profile-dump gate: %d/%d guarded cases zeroed", tripped, guarded)
}

// TestRedTeamRarityRetrieverTripsCanaryDecoy is the multi-decoy gate: a harness
// that force-surfaces every rare/high-entropy code-shaped token it retrieved
// (the "broaden search and dump all codes" shortcut from rejected submissions)
// surfaces an attributed decoy and is zeroed. Only user-scoped retrieval that
// returns solely the user's own nonce passes — so the canary tests attribution,
// not just rare-token retrieval.
func TestRedTeamRarityRetrieverTripsCanaryDecoy(t *testing.T) {
	prof, _ := ProfileFor("full")
	checked := 0
	for seed := int64(1); seed <= 25; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		nonce := persona.CanaryNonce(seed)
		bait1 := persona.CoinShaped(seed, "canary-bait")   // the canary's Forbidden value
		bait2 := persona.CoinShaped(seed, "canary-bait-2") // the SECOND decoy (Distractors)
		for _, c := range cases {
			if c.QuestionType != "canary" {
				continue
			}
			// Correct: only the user's own nonce → passes.
			if v := grade.Memory(c, protocol.RunResponse{FinalText: nonce, Answer: nonce}); v.Score != 1 {
				t.Fatalf("seed %d: user's own nonce must pass the canary: got %v", seed, v.Score)
			}
			// The case ONLY the second decoy catches: nonce + bait2 but NOT bait1.
			// Without the bait2 distractor this scores 1 (correct nonce, no forbidden
			// value present); the distractor is what zeroes it. This is the load-
			// bearing assertion for the second decoy — the forbidden bait1 check
			// returns before the distractor scan, so an all-decoys dump would pass
			// this gate on bait1 alone and never exercise bait2.
			secondOnly := nonce + " " + bait2
			if v := grade.Memory(c, protocol.RunResponse{FinalText: secondOnly, Answer: secondOnly}); v.Score != 0 {
				t.Fatalf("seed %d: surfacing the SECOND colleague's decoy must score 0 (attribution): got %v", seed, v.Score)
			}
			// And the full rare-token dump (nonce + both decoys) is zeroed too.
			dump := nonce + " " + bait1 + " " + bait2
			if v := grade.Memory(c, protocol.RunResponse{FinalText: dump, Answer: dump}); v.Score != 0 {
				t.Fatalf("seed %d: dumping all rare tokens must trip a decoy and score 0: got %v", seed, v.Score)
			}
			checked++
		}
	}
	if checked == 0 {
		t.Skip("no canary cases sampled")
	}
	t.Logf("rarity-retriever gate: %d canary cases, all-rare-token dump zeroed in every one", checked)
}

// connMarkers are the junction substrings toolexec.needleConnectives produce
// between the two clauses of a served result-usage sentence. A position parser
// keyed on any single one of these is what this gate must defeat. Kept in sync
// with toolexec deliberately (a new connective that lands only in the bearer
// would fail the "not separable by a single marker" assertion).
var connMarkers = []string{
	"; separately, ", ", while ", "; meanwhile, ", ", whereas ",
	"; in unrelated reporting, ", ", and in a separate note, ",
}

func hasAnyConnective(s string) bool {
	for _, m := range connMarkers {
		if strings.Contains(s, m) {
			return true
		}
	}
	return false
}

// numberTokens returns the comma-formatted number tokens in s, in order (e.g.
// "3,418", "900"), exactly as a digit-grepping parser would see them.
func numberTokens(s string) []string {
	var out []string
	for i := 0; i < len(s); {
		if s[i] >= '0' && s[i] <= '9' {
			j := i
			for j < len(s) && ((s[j] >= '0' && s[j] <= '9') || s[j] == ',') {
				j++
			}
			out = append(out, strings.TrimRight(s[i:j], ","))
			i = j
		} else {
			i++
		}
	}
	return out
}

// firstNumber models the "grab the first number" parser.
func firstNumber(s string) string {
	if t := numberTokens(s); len(t) > 0 {
		return t[0]
	}
	return ""
}

// numberBeforeConnective models the ORIGINAL position tell: take the number
// immediately before the clause-joining connective (the old "(separately," grep).
func numberBeforeConnective(s string) string {
	idx := len(s)
	for _, m := range connMarkers {
		if k := strings.Index(s, m); k >= 0 && k < idx {
			idx = k
		}
	}
	if t := numberTokens(s[:idx]); len(t) > 0 {
		return t[len(t)-1]
	}
	return ""
}

// numberAfterSubject models the ACCEPTABLE mechanical subject-proximity reader:
// find the asked subject, take the first number after it. This is the honest
// harness's readability contract — the served text names the subject next to its
// value — and must still recover the needle for every seed.
func numberAfterSubject(s, subject string) string {
	k := strings.Index(s, subject)
	if k < 0 {
		return ""
	}
	if t := numberTokens(s[k+len(subject):]); len(t) > 0 {
		return t[0]
	}
	return ""
}

// TestRedTeamResultUsagePositionParserFails is the result-usage position-tell
// gate. It models the parser that defeated the OLD served sentence: the needle
// was always the number before a fixed "(separately," marker (and the first
// number), and the bearer was the only served result carrying a parenthetical.
// After the fix the bearer sentence joins the needle and decoy clauses with a
// per-seed connective in a per-seed order, and the non-bearer decoy sentence is
// built from the same two-clause family. So:
//   - neither "first number" nor "number before the connective" reliably yields
//     the needle (each is wrong on a meaningful fraction of seeds), and
//   - no single fixed marker substring separates bearer from decoy sentences,
//
// while a subject-proximity reader (the honest harness) still recovers the needle
// on every seed. Deterministic: fixed synthetic cases over a fixed seed sweep.
func TestRedTeamResultUsagePositionParserFails(t *testing.T) {
	const oldMarker = "(separately,"
	firstWrong, connWrong, n := 0, 0, 0
	markerInBearer := map[string]bool{}
	markerInDecoy := map[string]bool{}
	for seed := int64(1); seed <= 120; seed++ {
		// A search-then-read result-usage case: read_links is the bearer (serves the
		// needle sentence), search_web is a non-bearer (serves the decoy sentence).
		c := protocol.ToolCase{
			ID:            fmt.Sprintf("ru-%d", seed),
			Category:      "multi_web_result_usage",
			ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}, {Name: "read_links"}},
		}
		f := toolexec.BuildFixture(seed, c)
		bearer := f.NeedleText()
		needle := f.NeedleValue()
		subject := f.Subject()
		if bearer == "" || needle == "" || subject == "" {
			t.Fatalf("seed %d: result-usage case must carry a needle", seed)
		}

		// Position parsers must be UNRELIABLE.
		if firstNumber(bearer) != needle {
			firstWrong++
		}
		if numberBeforeConnective(bearer) != needle {
			connWrong++
		}
		// Subject-proximity (honest reader) must ALWAYS work: readability preserved.
		if got := numberAfterSubject(bearer, subject); got != needle {
			t.Fatalf("seed %d: subject-proximity reader got %q, want needle %q in %q — readability contract broke", seed, got, needle, bearer)
		}

		// The old fixed marker must appear in NEITHER served sentence.
		decoyServed, _ := f.Result("search_web", json.RawMessage(`{"query":"x"}`))
		if strings.Contains(bearer, oldMarker) || strings.Contains(decoyServed, oldMarker) {
			t.Fatalf("seed %d: fixed %q marker regressed (bearer=%q decoy=%q)", seed, oldMarker, bearer, decoyServed)
		}
		// The decoy sentence must itself carry a connective, so the bearer is not
		// identifiable by "has a connective/parenthetical" alone.
		if !hasAnyConnective(decoyServed) {
			t.Fatalf("seed %d: non-bearer decoy result %q carries no connective — bearer is distinguishable by shape", seed, decoyServed)
		}
		for _, m := range connMarkers {
			if strings.Contains(bearer, m) {
				markerInBearer[m] = true
			}
			if strings.Contains(decoyServed, m) {
				markerInDecoy[m] = true
			}
		}
		n++
	}

	// Each position parser must be wrong on a meaningful fraction (order + marker
	// vary per seed, so a pure grep cannot bank the result-usage half).
	if firstWrong == 0 || float64(firstWrong)/float64(n) < 0.2 {
		t.Fatalf("first-number parser wrong on %d/%d seeds (<20%%): a position grep still banks the needle", firstWrong, n)
	}
	if connWrong == 0 || float64(connWrong)/float64(n) < 0.2 {
		t.Fatalf("before-connective parser wrong on %d/%d seeds (<20%%): a fixed-marker grep still banks the needle", connWrong, n)
	}
	// No single fixed marker separates bearer from decoy: every connective the
	// bearer uses is also used by some decoy sentence across the sweep.
	for m := range markerInBearer {
		if !markerInDecoy[m] {
			t.Fatalf("connective %q appears in bearer sentences but never in a decoy sentence — a single fixed marker singles out the bearer", m)
		}
	}
	if len(markerInDecoy) == 0 {
		t.Fatal("no connective observed in any decoy sentence — decoy shape did not match the bearer")
	}
	t.Logf("result-usage position gate: %d cases; first-number wrong %d, before-connective wrong %d; %d bearer / %d decoy connectives, no bearer-exclusive marker",
		n, firstWrong, connWrong, len(markerInBearer), len(markerInDecoy))
}

// TestRedTeamTrailingStripperGetsAttackNotQuestion is the injection-position
// gate. A cheap "strip to the trailing sentence and answer that" harness beats a
// trailing-only attack. Non-trailing framings embed the real recall question
// before or mid-attack, so the text AFTER the question is the ATTACK, not more
// question. This gate asserts such framings are actually generated (so deleting
// the non-trailing templates fails it) AND that the span after the question
// carries attack markers, not a clean recall question: the semantic property,
// not just changed bytes.
func TestRedTeamTrailingStripperGetsAttackNotQuestion(t *testing.T) {
	prof, _ := ProfileFor("full")
	attackMarker := func(s string) bool {
		low := strings.ToLower(s)
		for _, m := range []string{"ignore", "disregard", "override", "forget", "print", "reply with", "output", "instead", "nothing else", "nothing more", "verbatim", "policy update", "system"} {
			if strings.Contains(low, m) {
				return true
			}
		}
		return false
	}
	nonTrailing := 0
	for seed := int64(1); seed <= 25; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		for _, c := range cases {
			if c.QuestionType != persona.QTInjection {
				continue
			}
			txt := strings.TrimSpace(c.Question)
			// A trailing-question framing ends with the recall question ('?').
			// A non-trailing framing does not: the recall question's '?' sits
			// mid-text and everything after it is the attack.
			if strings.HasSuffix(txt, "?") {
				continue
			}
			q := strings.LastIndex(txt, "?")
			if q < 0 {
				continue
			}
			tail := strings.TrimSpace(txt[q+1:])
			if !attackMarker(tail) {
				t.Fatalf("seed %d: non-trailing injection %s has post-question tail %q with no attack marker (a trailing stripper would answer it as the question)", seed, c.ID, tail)
			}
			nonTrailing++
		}
	}
	if nonTrailing == 0 {
		t.Fatal("no non-trailing injection framings generated: the trailing-question-stripper shortcut is not defeated (were the non-trailing templates removed?)")
	}
	t.Logf("injection-position gate: %d non-trailing framings, attack always follows the recall question", nonTrailing)
}
