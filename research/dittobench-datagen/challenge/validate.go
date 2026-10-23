package challenge

import (
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// AllowedAnswerKinds are the answer kinds a v1 pack may use. Each one has a
// value the validator can ground in the haystack and a grader path that
// rejects a blank, abstaining, or echoed answer. Stance, instruction, and
// conversational kinds (decline, clarify, acknowledge, chitchat, persistence,
// reversal, absence) are excluded: their gold answer cannot be checked against
// evidence, so they are the cheapest place to plant ambiguous cases that no
// agent "solves".
var AllowedAnswerKinds = map[string]bool{
	protocol.AnswerValue:       true,
	protocol.AnswerNumber:      true,
	protocol.AnswerMoney:       true,
	protocol.AnswerDate:        true,
	protocol.AnswerDirection:   true,
	protocol.AnswerList:        true,
	protocol.AnswerOrderedList: true,
}

// correctLine is the scorer's correctness line (memoryCaseScore sets Correct
// at score >= 0.5). Validation probes use the same line so "the grader rejects
// this answer" means exactly what the agent competition means by it.
const correctLine = 0.5

// abstainProbe is a canned no-retrieval answer every valid case must reject.
const abstainProbe = "I don't know; you haven't told me that."

// Issue is one validation failure. CaseID is empty for a pack-level issue.
type Issue struct {
	CaseID  string `json:"case_id,omitempty"`
	Rule    string `json:"rule"`
	Message string `json:"message"`
}

// Report is the outcome of Validate. A pack is accepted only when Issues is
// empty; every rule runs so a miner sees all problems in one pass.
type Report struct {
	Valid bool `json:"valid"`
	// PackSHA256 is the Digest of the validated bytes (set by ValidateBytes).
	PackSHA256 string `json:"pack_sha256,omitempty"`
	CaseCount  int    `json:"case_count"`
	PairCount  int    `json:"pair_count"`
	// Cases lists every case's ID and fingerprint in pack order. Platform
	// scores and de-duplicates from these refs without re-decoding the pack.
	Cases  []CaseRef `json:"cases"`
	Issues []Issue   `json:"issues"`
}

type validator struct {
	issues []Issue
}

func (v *validator) add(caseID, rule, format string, args ...any) {
	v.issues = append(v.issues, Issue{CaseID: caseID, Rule: rule, Message: fmt.Sprintf(format, args...)})
}

type seededPair struct {
	pair   protocol.MemoryPair
	userID string
	wave   int
}

// Validate checks a decoded pack against the v1 contract. It is deterministic
// and self-contained: no network, clock, or model.
func Validate(p Pack) Report {
	v := &validator{}
	r := Report{CaseCount: len(p.Cases)}

	if p.Contract != ContractV1 {
		v.add("", "contract", "contract must be %q, got %q", ContractV1, p.Contract)
	}
	if p.GradingBenchVersion != GradingBenchVersion {
		v.add("", "grading_bench_version", "grading_bench_version must be %d, got %d", GradingBenchVersion, p.GradingBenchVersion)
	}
	if !hotkeyPattern.MatchString(p.MinerHotkey) {
		v.add("", "miner_hotkey", "miner_hotkey must be an SS58 hotkey")
	}
	validateGenerator(v, p.Generator)

	pairs, users := validateWaves(v, p.MemoryWaves)
	r.PairCount = len(pairs)

	if len(p.Cases) < MinCases || len(p.Cases) > MaxCases {
		v.add("", "case_count", "pack must have %d..%d cases, got %d", MinCases, MaxCases, len(p.Cases))
	}
	seenIDs := map[string]bool{}
	seenFP := map[string]string{}
	seenQuestion := map[string]string{}
	for _, c := range p.Cases {
		id := c.ID
		if !idPattern.MatchString(id) {
			v.add(id, "case_id", "case id must match %s", idPattern)
		} else if seenIDs[id] {
			v.add(id, "case_id", "duplicate case id")
		}
		seenIDs[id] = true
		fp := Fingerprint(c)
		if prior, dup := seenFP[fp]; dup {
			v.add(id, "duplicate_case", "same normalized question and answer as case %q", prior)
		} else {
			seenFP[fp] = id
		}
		// Two cases asking the same question of the same memory graph have no
		// single right answer, whatever their declared golds say.
		qKey := c.UserID + "\x00" + grade.Normalize(c.Question)
		if prior, dup := seenQuestion[qKey]; dup {
			v.add(id, "duplicate_question", "asks the same question of the same user as case %q", prior)
		} else {
			seenQuestion[qKey] = id
		}
		r.Cases = append(r.Cases, CaseRef{CaseID: id, Fingerprint: fp})
		validateCase(v, c, len(p.MemoryWaves), pairs, users)
	}

	r.Issues = v.issues
	if r.Issues == nil {
		r.Issues = []Issue{}
	}
	r.Valid = len(r.Issues) == 0
	return r
}

// ValidateBytes decodes and validates uploaded pack bytes. A decode failure is
// reported as a single "decode" issue rather than an error, so every caller
// (CLI, scorer) returns the same report shape for every rejection.
func ValidateBytes(raw []byte) Report {
	p, err := Decode(raw)
	if err != nil {
		return Report{PackSHA256: Digest(raw), Issues: []Issue{{Rule: "decode", Message: err.Error()}}}
	}
	r := Validate(p)
	r.PackSHA256 = Digest(raw)
	return r
}

func validateGenerator(v *validator, g Generator) {
	if g.Name == "" || len(g.Name) > MaxGeneratorNameLen || !labelPattern.MatchString(g.Name) {
		v.add("", "generator.name", "generator.name must match %s", labelPattern)
	}
	if g.Version == "" || !labelPattern.MatchString(g.Version) {
		v.add("", "generator.version", "generator.version must match %s", labelPattern)
	}
	if !shaPattern.MatchString(g.Seed) {
		v.add("", "generator.seed", "generator.seed must be the request seed: 64 lowercase hex characters")
	}
	if g.SourceSHA256 != "" && !shaPattern.MatchString(g.SourceSHA256) {
		v.add("", "generator.source_sha256", "generator.source_sha256 must be 64 lowercase hex characters")
	}
}

// validateWaves checks the haystack and returns every seeded pair by id plus
// the set of user ids that own at least one wave.
func validateWaves(v *validator, waves []protocol.SeedRequest) (map[string]seededPair, map[string]bool) {
	pairs := map[string]seededPair{}
	users := map[string]bool{}
	if len(waves) == 0 || len(waves) > MaxWaves {
		v.add("", "memory_waves", "pack must have 1..%d memory waves, got %d", MaxWaves, len(waves))
	}
	for i, w := range waves {
		if w.Wave != i {
			v.add("", "memory_waves", "wave %d must carry wave index %d, got %d", i, i, w.Wave)
		}
		if w.UserID != "" && !userIDPattern.MatchString(w.UserID) {
			v.add("", "memory_waves", "wave %d user_id must match %s", i, userIDPattern)
		}
		users[w.UserID] = true
		if len(w.Pairs) == 0 {
			v.add("", "memory_waves", "wave %d must seed at least one pair", i)
		}
		if len(w.Subjects) > 0 || len(w.Links) > 0 {
			// Subject graphs are a harness hint, not evidence; v1 keeps packs to
			// conversational pairs so grounding is checkable on one text surface.
			v.add("", "memory_waves", "wave %d must not carry subjects or links in contract v1", i)
		}
		for _, pr := range w.Pairs {
			if !idPattern.MatchString(pr.PairID) {
				v.add("", "pair_id", "wave %d pair id %q must match %s", i, pr.PairID, idPattern)
				continue
			}
			if _, dup := pairs[pr.PairID]; dup {
				v.add("", "pair_id", "duplicate pair id %q", pr.PairID)
				continue
			}
			if !idPattern.MatchString(pr.SessionID) {
				v.add("", "pair", "pair %q session_id must match %s", pr.PairID, idPattern)
			}
			if _, err := time.Parse(time.RFC3339, pr.Timestamp); err != nil {
				v.add("", "pair", "pair %q timestamp must be RFC3339", pr.PairID)
			}
			if strings.TrimSpace(pr.Prompt) == "" || strings.TrimSpace(pr.Response) == "" {
				v.add("", "pair", "pair %q prompt and response must be non-empty", pr.PairID)
			}
			if utf8.RuneCountInString(pr.Prompt) > MaxPairTextRunes || utf8.RuneCountInString(pr.Response) > MaxPairTextRunes {
				v.add("", "pair", "pair %q prompt and response must each be at most %d characters", pr.PairID, MaxPairTextRunes)
			}
			pairs[pr.PairID] = seededPair{pair: pr, userID: w.UserID, wave: i}
		}
	}
	if len(users) > MaxUsers {
		v.add("", "memory_waves", "pack may use at most %d users, got %d", MaxUsers, len(users))
	}
	if len(pairs) > MaxPairs {
		v.add("", "memory_waves", "pack may seed at most %d pairs, got %d", MaxPairs, len(pairs))
	}
	return pairs, users
}

func validateCase(v *validator, c Case, nWaves int, pairs map[string]seededPair, users map[string]bool) {
	id := c.ID
	mc := c.GradingCase()

	if c.BenchVersion != 0 && c.BenchVersion != GradingBenchVersion {
		v.add(id, "bench_version", "case bench_version must be omitted or %d", GradingBenchVersion)
	}
	if strings.TrimSpace(c.Question) == "" || utf8.RuneCountInString(c.Question) > MaxQuestionRunes {
		v.add(id, "question", "question must be 1..%d characters", MaxQuestionRunes)
	}
	if c.QuestionType != "" && !labelPattern.MatchString(c.QuestionType) {
		v.add(id, "question_type", "question_type must match %s", labelPattern)
	}
	if c.QuestionID != "" && !idPattern.MatchString(c.QuestionID) {
		v.add(id, "question_id", "question_id must match %s", idPattern)
	}
	if c.Language != "" && c.Language != "en" {
		v.add(id, "language", "contract v1 cases must be English")
	}
	kind := mc.AnswerKind
	if kind == "" {
		kind = protocol.AnswerValue
	}
	if !AllowedAnswerKinds[kind] {
		v.add(id, "answer_kind", "answer_kind %q is not allowed in contract v1", c.AnswerKind)
		return
	}
	if strings.TrimSpace(c.ExpectedAnswer) == "" || utf8.RuneCountInString(c.ExpectedAnswer) > MaxAnswerRunes {
		v.add(id, "expected_answer", "expected_answer must be 1..%d characters", MaxAnswerRunes)
	}
	if len(c.AcceptAny) > MaxAlternatives || len(c.DistractorAnswers) > MaxDistractors || len(c.DumpGuard) > MaxDistractors {
		v.add(id, "bounds", "at most %d accept_any and %d distractor_answers / dump_guard entries", MaxAlternatives, MaxDistractors)
	}
	isList := kind == protocol.AnswerList || kind == protocol.AnswerOrderedList
	if isList {
		if len(c.AnswerItems) < 2 || len(c.AnswerItems) > MaxListItems {
			v.add(id, "answer_items", "list answers need 2..%d answer_items", MaxListItems)
		}
		if len(c.AnswerItemAcceptAny) > len(c.AnswerItems) {
			v.add(id, "answer_items", "answer_item_accept_any is longer than answer_items")
		}
	} else if len(c.AnswerItems) > 0 || len(c.AnswerItemAcceptAny) > 0 {
		v.add(id, "answer_items", "answer_items are only valid for list answers")
	}
	if kind != protocol.AnswerMoney && (c.AnswerUnit != "" || c.AnswerCurrency != "") {
		v.add(id, "answer_unit", "answer_unit and answer_currency are only valid for money answers")
	}
	// Fields that select validator-served machinery or grader leniency a pack
	// cannot supply: no tools are served for a pack, twins need paired cases
	// the scorer generates, and slot lexicons / grounding tokens widen what the
	// grader credits beyond the declared answer.
	if c.BaitTool != "" || c.TwinGroup != "" || len(c.GroundingTokens) > 0 || len(c.SlotLexicon) > 0 || len(c.AnswerItemKinds) > 0 {
		v.add(id, "unsupported_field", "bait_tool, twin_group, grounding_tokens, slot_lexicon, and answer_item_kinds are not supported in contract v1")
	}

	if c.UserID != "" && !userIDPattern.MatchString(c.UserID) {
		v.add(id, "user_id", "user_id must match %s", userIDPattern)
	}
	if !users[c.UserID] {
		v.add(id, "user_id", "user_id %q owns no memory wave", c.UserID)
	}
	if c.RunAfterWave < 0 || c.RunAfterWave >= nWaves {
		v.add(id, "run_after_wave", "run_after_wave must name a wave in 0..%d", nWaves-1)
	}

	// Evidence: named, visible to this case, and carrying the answer.
	if len(c.EvidencePairIDs) == 0 || len(c.EvidencePairIDs) > MaxEvidencePairs {
		v.add(id, "evidence", "evidence_pair_ids must name 1..%d pairs", MaxEvidencePairs)
	}
	var evidence strings.Builder
	for _, pid := range c.EvidencePairIDs {
		sp, ok := pairs[pid]
		switch {
		case !ok:
			v.add(id, "evidence", "evidence pair %q is not seeded", pid)
			continue
		case sp.userID != c.UserID:
			v.add(id, "evidence", "evidence pair %q belongs to another user's memory", pid)
			continue
		case sp.wave > c.RunAfterWave:
			v.add(id, "evidence", "evidence pair %q is seeded after the case runs", pid)
			continue
		}
		evidence.WriteString(sp.pair.Prompt)
		evidence.WriteString("\n")
		evidence.WriteString(sp.pair.Response)
		evidence.WriteString("\n")
	}
	groups := grade.ClaimAlternatives(mc)
	if len(groups) == 0 {
		v.add(id, "answer", "the grader derives no checkable answer from this case")
		return
	}
	if (kind == protocol.AnswerList || kind == protocol.AnswerOrderedList) && len(groups) != len(c.AnswerItems) {
		v.add(id, "answer", "the grader derives %d checkable answers for %d list items", len(groups), len(c.AnswerItems))
		return
	}
	for gi, forms := range groups {
		if len(forms) == 0 {
			v.add(id, "answer", "answer item %d has no non-empty graded form", gi)
			return
		}
	}
	evidenceText := evidence.String()
	for gi, forms := range groups {
		if kind == protocol.AnswerMoney {
			forms = moneySurfaceForms(forms)
		}
		if !anyHit(forms, evidenceText) {
			v.add(id, "grounding", "answer item %d (%q) does not appear in the evidence pairs", gi, forms[0])
		}
		if anyHit(forms, c.Question) {
			v.add(id, "answer_leak", "the question contains answer item %d (%q)", gi, forms[0])
		}
	}

	checkGraderConsistency(v, c, mc, kind, pairs)
}

// checkGraderConsistency probes the production grader with canned responses.
// The gold answer must score full credit; every response a harness could give
// without retrieving the evidence must fall below the correctness line.
func checkGraderConsistency(v *validator, c Case, mc protocol.MemoryCase, kind string, pairs map[string]seededPair) {
	id := c.ID
	gold := goldResponse(mc, kind)
	if s := grade.Memory(mc, protocol.RunResponse{Answer: gold, FinalText: gold}).Score; s < 1 {
		v.add(id, "gold_answer", "the grader scores the gold answer %.2f, want 1.0", s)
	}
	if len(c.AcceptAny) > 0 && kind != protocol.AnswerValue && kind != protocol.AnswerDate && kind != protocol.AnswerDirection {
		// The grader ignores accept_any for other kinds, so an alternative
		// there would be accepted by validation and never by grading.
		v.add(id, "accept_any", "accept_any is not supported for answer kind %q", kind)
	} else {
		for _, alt := range c.AcceptAny {
			if s := grade.Memory(mc, protocol.RunResponse{Answer: alt, FinalText: alt}).Score; s < correctLine {
				v.add(id, "accept_any", "the grader scores accepted alternative %q %.2f", alt, s)
			}
		}
	}
	reject := func(rule, label string, resp protocol.RunResponse) {
		if s := grade.Memory(mc, resp).Score; s >= correctLine {
			v.add(id, rule, "the grader credits %s (%.2f); a valid case must reject it", label, s)
		}
	}
	reject("trivial_answer", "an empty answer", protocol.RunResponse{})
	reject("trivial_answer", "an abstention", protocol.RunResponse{FinalText: abstainProbe, Abstain: true})
	reject("trivial_answer", "a verbatim echo of the question", protocol.RunResponse{FinalText: c.Question})
	for _, d := range c.DistractorAnswers {
		reject("distractor", fmt.Sprintf("distractor %q", d), protocol.RunResponse{Answer: d, FinalText: d})
	}
	if c.ForbiddenAnswer != "" {
		reject("forbidden", "the forbidden answer", protocol.RunResponse{Answer: c.ForbiddenAnswer, FinalText: c.ForbiddenAnswer})
	}
	// A harness that recites every memory it holds for this user must not pass:
	// the case needs interference (a distractor or dump-guard value that is
	// itself in memory) so credit requires choosing the right fact.
	reject("memory_dump", "a dump of the user's entire visible memory", protocol.RunResponse{FinalText: visibleMemory(c, pairs)})
}

// goldResponse renders the answer an honest harness would serve. Money gold is
// in minor units and the grader credits the major-unit form; lists are joined.
func goldResponse(mc protocol.MemoryCase, kind string) string {
	switch kind {
	case protocol.AnswerMoney:
		if major, ok := grade.MoneyMajorForm(mc.ExpectedAnswer); ok {
			return major
		}
	case protocol.AnswerList, protocol.AnswerOrderedList:
		if len(mc.AnswerItems) > 0 {
			return strings.Join(mc.AnswerItems, ", ")
		}
	}
	return mc.ExpectedAnswer
}

func visibleMemory(c Case, pairs map[string]seededPair) string {
	ids := make([]string, 0, len(pairs))
	for pid, sp := range pairs {
		if sp.userID == c.UserID && sp.wave <= c.RunAfterWave {
			ids = append(ids, pid)
		}
	}
	sort.Strings(ids)
	var b strings.Builder
	for _, pid := range ids {
		sp := pairs[pid]
		b.WriteString(sp.pair.Prompt)
		b.WriteString("\n")
		b.WriteString(sp.pair.Response)
		b.WriteString("\n")
	}
	return b.String()
}

// moneySurfaceForms widens the grader's major-unit money form ("1620.00") with
// the spellings a conversation actually uses ("1,620.00", "1,620", "1620"),
// so grounding and leak checks see the amount however the haystack wrote it.
func moneySurfaceForms(forms []string) []string {
	out := append([]string(nil), forms...)
	for _, f := range forms {
		whole, cents, ok := strings.Cut(f, ".")
		if !ok {
			continue
		}
		grouped := groupThousands(whole)
		out = append(out, grouped+"."+cents)
		if cents == "00" {
			out = append(out, whole, grouped)
		}
	}
	return out
}

func groupThousands(digits string) string {
	if len(digits) <= 3 {
		return digits
	}
	head := len(digits) % 3
	var b strings.Builder
	if head > 0 {
		b.WriteString(digits[:head])
	}
	for i := head; i < len(digits); i += 3 {
		if b.Len() > 0 {
			b.WriteByte(',')
		}
		b.WriteString(digits[i : i+3])
	}
	return b.String()
}

func anyHit(forms []string, text string) bool {
	for _, f := range forms {
		if grade.Hit(f, text) {
			return true
		}
	}
	return false
}
