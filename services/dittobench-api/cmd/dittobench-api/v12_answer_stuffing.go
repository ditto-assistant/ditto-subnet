package main

// Bench v12 Class-D answer-stuffing detection: the SCORER side that settles the
// two trusted per-case telemetry fields the answer-stuffing gate reads
// (runner.CaseExecution.AnswerStuffObserved / AnswerStuffed).
//
// A Class-D harness computes a case's answer in engine code and feeds the
// finished value into the model's INPUT with a copy-exactly directive so the
// model parrots it. The answer routes THROUGH the model, so the causal-ablation
// (model_dependence) gate misses it. This pass catches it by VALUE PROVENANCE
// over the clean-pass model I/O the trusted broker recorded (bounded/normalized,
// answer-key-free): for every COMPUTED-answer, model-reached, non-excluded scored
// case it asks whether the finished answer value appeared in a model INPUT the
// harness sent BEFORE it appeared in any model COMPLETION.
//
//   - STUFFED: the value's earliest occurrence is an INPUT (the harness handed
//     the model its own computed answer) -> flagged.
//   - CLEAN: the value only ever appears in a COMPLETION (the model derived it),
//     or its earliest occurrence is a completion that a later input reuses (legit
//     multi-turn) -> not flagged.
//
// Only COMPUTED cases are eligible: a case whose expected answer is verbatim
// present in its seeded evidence is verbatim-recall and is EXCLUDED before any
// I/O is inspected (the answer-stuffing gate never looks at normal RAG). The
// scorer holds the dataset answer and does this check post-run; the broker only
// recorded bounded value tokens and never saw the answer key. This pass writes
// ONLY the two per-case telemetry fields on the aligned transcript slice --
// never perCase, the report, the transcript Response bytes, or any accounting.

import (
	"encoding/json"
	"log"
	"regexp"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// memoryAnswerIsComputed decides whether a memory case's expected answer is
// COMPUTED (in the v12 answer-stuffing slice) or verbatim-recall (excluded). It
// is computed only when the answer is non-empty, the case declares seeded
// evidence, every evidence pin resolves, and the answer is NOT verbatim present
// in that evidence. Any missing answer, missing/unresolved evidence, or verbatim
// presence yields false -- the conservative direction that keeps normal RAG and
// unresolvable cases out of the flagged slice.
func memoryAnswerIsComputed(mc protocol.MemoryCase, requiredPairIDs []string, evidenceByPairID map[string]string) bool {
	if strings.TrimSpace(mc.ExpectedAnswer) == "" || len(requiredPairIDs) == 0 {
		return false
	}
	var evidence strings.Builder
	for _, pairID := range requiredPairIDs {
		text, ok := evidenceByPairID[pairID]
		if !ok {
			// Unresolved evidence: cannot prove the answer is computed -> exclude.
			return false
		}
		evidence.WriteByte(' ')
		evidence.WriteString(text)
	}
	return !gen.AnswerVerbatimInEvidence(evidence.String(), mc.ExpectedAnswer)
}

// seededMemoryValueSet is the union of candidate answer-value token HASHES across
// ALL retrievable seeded memory for the run: every seeded pair's prompt+response
// text (main.go seededEvidenceByPairID: primary waves + secondary isolation
// graph), run through the SAME valueTokenSet extractor the provenance pass uses on
// the model I/O. It is the "a legitimate retrieval could have surfaced this value"
// oracle behind Change-1 provability: if an answer value appears ANYWHERE here,
// some honest retrieval could have produced it, so the case is NOT provable
// answer-stuffing and is excluded from the flagged slice. This is what makes the
// operand-equals-answer coincidence safe -- an answer that numerically equals an
// operand is present in memory as that operand -- and it never stores raw prose or
// token text (only hashes), so it cannot leak the seeded memory.
type seededMemoryValueSet struct {
	tokens map[uint64]struct{}
}

// buildSeededMemoryValueSet folds every seeded evidence blob into one value-token
// hash union. The per-side ceiling inside valueTokenSet is irrelevant here: each
// seeded pair is an ordinary-sized body, and the union only grows to the actual
// distinct-value-token count of the whole seeded memory.
func buildSeededMemoryValueSet(evidenceByPairID map[string]string) seededMemoryValueSet {
	tokens := make(map[uint64]struct{})
	for _, text := range evidenceByPairID {
		set, _ := valueTokenSet(text)
		for h := range set {
			tokens[h] = struct{}{}
		}
	}
	return seededMemoryValueSet{tokens: tokens}
}

// containsAnswer reports whether ANY accepted answer surface form is fully present
// in the seeded-memory value token set (every value-token hash of some candidate
// form appears in memory). It reuses candidatePresent -- the SAME containment the
// provenance check applies to a model input -- so "the answer value exists in
// memory" means exactly what "the answer value appears in an input" means: same
// canonicalization, same hashing. An empty candidate form is never matched
// (fail-open direction). A nil/empty set (pre-v12 or no seeded memory) contains
// nothing, so no case is excluded on that basis.
func (m seededMemoryValueSet) containsAnswer(candidates []string) bool {
	forms := make([]map[uint64]struct{}, 0, len(candidates))
	for _, form := range candidates {
		if tokens := answerCandidateTokens(form); len(tokens) > 0 {
			forms = append(forms, tokens)
		}
	}
	return candidatePresent(m.tokens, forms)
}

// memoryAnswerIsProvablyComputed is the strengthened (STRICT + PROVABLE) Change-1
// flag condition. A memory case's answer is in the v12 answer-stuffing slice IFF
// BOTH hold:
//
//  1. it is COMPUTED -- not verbatim present in the case's OWN declared evidence
//     (memoryAnswerIsComputed, the existing verbatim-recall / normal-RAG guard); AND
//  2. its value appears NOWHERE in the entire run's retrievable seeded memory
//     (!memoryValues.containsAnswer) -- so no legitimate retrieval could ever
//     surface it.
//
// Together with the provenance check (answer in a model INPUT before any
// COMPLETION) this makes each flag PROVABLE: a value no retrieval could produce and
// the model did not derive, yet fed into the model input, was computed and injected
// by the harness. Condition 2 is precisely what excludes the operand-equals-answer
// coincidence (that value is present in memory as the operand) and any real
// large-RAG answer, with no reliance on a share threshold.
func memoryAnswerIsProvablyComputed(
	mc protocol.MemoryCase,
	requiredPairIDs []string,
	evidenceByPairID map[string]string,
	memoryValues seededMemoryValueSet,
	candidates []string,
) bool {
	if !memoryAnswerIsComputed(mc, requiredPairIDs, evidenceByPairID) {
		return false
	}
	return !memoryValues.containsAnswer(candidates)
}

// v12AnswerStuffingConfigFromEnv returns the compiled defaults with optional
// operator overrides, following the broker's envOr/envIntDefault conventions. The
// posture defaults to ENFORCE (a computed answer in the harness's model-input is a
// deliberate injection); an operator may flip it to review, and an unknown value
// falls back to the safe compiled default.
func v12AnswerStuffingConfigFromEnv() v9base.AnswerStuffingGateConfig {
	cfg := v9base.DefaultAnswerStuffingGateConfig()
	cfg.MinCases = envIntDefault("DITTOBENCH_V12_ANSWER_STUFFING_MIN_CASES", cfg.MinCases)
	cfg.ShareBPS = envIntDefault("DITTOBENCH_V12_ANSWER_STUFFING_SHARE_BPS", cfg.ShareBPS)
	// The loose systematic-review bar (default ~4000 = 40%): the share of computed
	// answers fed into a model input above which the run routes to human review (never
	// an auto-zero). Zero disables the loose-review signal.
	cfg.ReviewShareBPS = envIntDefault("DITTOBENCH_V12_ANSWER_STUFFING_REVIEW_SHARE_BPS", cfg.ReviewShareBPS)
	switch strings.ToLower(strings.TrimSpace(envOr("DITTOBENCH_V12_ANSWER_STUFFING_POSTURE", string(cfg.Posture)))) {
	case string(scoregates.AnswerStuffingEnforce):
		cfg.Posture = scoregates.AnswerStuffingEnforce
	case string(scoregates.AnswerStuffingReview):
		cfg.Posture = scoregates.AnswerStuffingReview
	}
	return cfg
}

// answerIO capture bounds. Size is NOT a signal: a legitimate deep-RAG agent is
// expected to use the model's full context window, and the gate judges purely on
// whether the finished answer VALUE was fed into a model input -- never on how
// large the prompt is. The per-side ceiling is therefore set so high that a real
// context-window-sized prompt can never reach it (see answerIOMaxTokensPerSide);
// the truncation-marks-the-capture path is only a last-resort safety net that
// bounds a truly malformed/malicious body far beyond any real context window, and
// the scorer routes such a residual truncation to human REVIEW (never fail-open,
// never auto-zero).
const (
	answerIOMaxCalls = 64
	// answerIOMaxTokensPerSide is the maximum distinct normalized value-token
	// HASHES captured per side (input / completion) of a single model call. It is
	// set to twice gpt-oss-20b's full 131072-token context window: a prompt cannot
	// contain more DISTINCT value tokens than it has tokens, and it cannot have more
	// tokens than the context window, so a legitimate single-context prompt (however
	// large, however deep its RAG) can NEVER trip this bound. It exists solely to
	// bound the in-memory hash set against a pathological body that claims more
	// distinct value tokens than the model could ever accept; reaching it routes the
	// case to REVIEW, not to a pass or a zero. At 8 bytes per uint64 key the ceiling
	// is ~2 MB of raw keys per side, and the set only ever grows to the ACTUAL
	// distinct-value-token count of the prompt, so normal full-context runs cost a
	// few MB total (see the memory note below), not the ceiling.
	answerIOMaxTokensPerSide = 262144
	answerIOMaxTokenLen      = 64
	// answerIOMinStringTokenLen bounds which non-numeric tokens are kept. Numbers
	// of any length are always kept (the v12 program families are money/number
	// answers); short common words are dropped so an answer never matches on a
	// bare stopword. String answers shorter than this are conservatively not
	// matched (a fail-open direction: a stuffer escapes rather than an honest run
	// being flagged).
	answerIOMinStringTokenLen = 4
)

// hashToken maps a normalized value token to a 64-bit FNV-1a hash. The capture
// stores ONLY these hashes (never the token text), so the recorded I/O can neither
// reproduce the prompt nor leak the answer key, and a full-context prompt costs a
// flat 8 bytes per distinct value token. FNV-1a is seed-free, so the broker's
// capture-time hashes and the scorer's post-run candidate hashes agree without
// sharing any state; the scorer hashes the expected answer through this same
// function and tests set membership + input-before-completion ordering.
func hashToken(tok string) uint64 {
	const (
		offset64 = 14695981039346656037
		prime64  = 1099511628211
	)
	h := uint64(offset64)
	for i := 0; i < len(tok); i++ {
		h ^= uint64(tok[i])
		h *= prime64
	}
	return h
}

// caseModelCall is one bounded, normalized clean-pass model call: the set of
// candidate answer-value token HASHES the harness placed in the model INPUT and
// the set the model returned in its COMPLETION. Only value-token hashes are kept
// (never raw prose, never the token text), so the record can neither reproduce the
// prompt nor leak the answer key.
type caseModelCall struct {
	inputTokens      map[uint64]struct{}
	completionTokens map[uint64]struct{}
}

// caseModelIOLog is the ordered clean-pass model I/O the broker recorded for one
// case, plus a truncation bit. The bit is set only when a body was unparseable or
// the per-side capture ceiling (far above the model's full context window) was
// exceeded -- an event that a legitimate full-context prompt can never cause. For
// a COMPUTED case the scorer routes such a residual truncation to human REVIEW
// (never fail-open, never auto-zero); an unparseable/unavailable body remains a
// genuine capture gap that fails OPEN.
type caseModelIOLog struct {
	calls     []caseModelCall
	truncated bool
}

// caseModelIOSource yields the trusted clean-pass model I/O the broker recorded
// for one case. The interface is the seam that keeps the deterministic
// provenance logic unit-testable: production wires the live broker
// (brokerCaseModelIOSource); tests model the harness archetypes.
type caseModelIOSource interface {
	CaseModelIO(caseID string) (caseModelIOLog, bool)
}

// answerStuffingCaseSpec is the per-case answer-key data the scorer needs to
// judge provenance. It is captured during the clean pass (where the oracle
// MemoryCase and its seeded evidence are in scope) and aligned index-for-index
// with the transcript slice. computed is true only when the expected answer is
// NOT verbatim present in the case's seeded evidence (rule #1: verbatim-recall is
// excluded). answerCandidates are the accepted answer surface forms
// (ExpectedAnswer + AcceptAny, plus a money major-unit rendering); the value is
// "present" in a call when any candidate's normalized token set is contained in
// that call's token set.
type answerStuffingCaseSpec struct {
	caseID string
	// computed is the PROVABLE-computed flag (memoryAnswerIsProvablyComputed): the
	// answer is not verbatim in the case's own evidence AND its value appears nowhere
	// in the run's retrievable seeded memory. It drives the provable auto-gate.
	computed bool
	// computedLoose is the LOOSE-computed flag (memoryAnswerIsComputed): the answer is
	// simply not verbatim-recall (not verbatim in the case's own evidence),
	// REGARDLESS of whether its value also appears elsewhere in seeded memory. It is a
	// SUPERSET of computed and drives the loose systematic-review signal, catching a
	// coinciding-value stuffer the provable auto-gate cannot prove against RAG.
	computedLoose    bool
	answerCandidates []string
}

func (s answerStuffingCaseSpec) populated() bool { return s.caseID != "" }

// answerStuffingSummary is returned for logging/reporting only; it never feeds a
// score. Administered = model-reached cases whose provenance the pass settled
// (whether or not they enter the tally). Stuffed/Clean are the computed-case
// tally; Excluded counts settled cases whose answer is verbatim-recall (kept out
// of the denominator); Review counts COMPUTED cases whose capture overflowed the
// per-side ceiling (routed to human review, never fail-open, never auto-zero);
// Pending counts model-reached cases whose I/O the broker could not supply at all
// (a genuine capture gap that fails open).
type answerStuffingSummary struct {
	Eligible     int
	Administered int
	Stuffed      int
	Clean        int
	Excluded     int
	Review       int
	Pending      int
	// LooseEligible / LooseStuffed are the loose systematic-review tally over the
	// COMPUTED (not verbatim-recall) population regardless of memory presence:
	// LooseStuffed is how many fed the finished answer into a model input before any
	// completion. LooseEligible is a superset of Eligible. Logging/reporting only.
	LooseEligible int
	LooseStuffed  int
}

// numberPattern matches a currency/number run: optional sign, optional '$',
// digits with grouping commas, optional fractional part.
var numberPattern = regexp.MustCompile(`-?\$?\d[\d,]*(?:\.\d+)?`)

// alnumTokenPattern matches lowercase alphanumeric runs (already lowercased text).
var alnumTokenPattern = regexp.MustCompile(`[a-z0-9]+`)

// canonicalNumber normalizes a numeric token to a stable canonical string:
// strips '$' and grouping commas, drops an insignificant fractional part and its
// trailing zeros, drops leading zeros, and collapses "-0" to "0". Returns ""
// when the token is not a number after stripping.
func canonicalNumber(raw string) string {
	s := strings.TrimSpace(raw)
	neg := strings.HasPrefix(s, "-")
	s = strings.TrimPrefix(s, "-")
	s = strings.ReplaceAll(s, "$", "")
	s = strings.ReplaceAll(s, ",", "")
	if s == "" {
		return ""
	}
	intPart, fracPart := s, ""
	if dot := strings.IndexByte(s, '.'); dot >= 0 {
		intPart, fracPart = s[:dot], s[dot+1:]
	}
	fracPart = strings.TrimRight(fracPart, "0")
	intPart = strings.TrimLeft(intPart, "0")
	if intPart == "" {
		intPart = "0"
	}
	for _, r := range intPart + fracPart {
		if r < '0' || r > '9' {
			return ""
		}
	}
	out := intPart
	if fracPart != "" {
		out = intPart + "." + fracPart
	}
	if neg && out != "0" {
		out = "-" + out
	}
	return out
}

// valueTokenSet extracts the bounded set of candidate answer-value token HASHES
// from a blob of message/completion text: every canonical number, plus lowercase
// alphanumeric tokens of at least answerIOMinStringTokenLen, each stored as its
// hashToken. It returns whether the per-side ceiling was exceeded so the caller
// can mark the log truncated -- an event a real context-window-sized prompt can
// never cause. It never stores raw prose or token text -- only value-token hashes
// -- so it cannot reproduce the prompt or leak the answer key.
func valueTokenSet(text string) (map[uint64]struct{}, bool) {
	tokens := make(map[uint64]struct{})
	truncated := false
	add := func(tok string) {
		if tok == "" || len(tok) > answerIOMaxTokenLen {
			return
		}
		h := hashToken(tok)
		if _, ok := tokens[h]; ok {
			return
		}
		if len(tokens) >= answerIOMaxTokensPerSide {
			truncated = true
			return
		}
		tokens[h] = struct{}{}
	}
	for _, match := range numberPattern.FindAllString(text, -1) {
		add(canonicalNumber(match))
	}
	for _, tok := range alnumTokenPattern.FindAllString(strings.ToLower(text), -1) {
		// Pure-digit runs are already covered by the number pass in canonical form;
		// keep only sufficiently long non-numeric-length tokens here.
		if len(tok) >= answerIOMinStringTokenLen && strings.IndexFunc(tok, func(r rune) bool { return r < '0' || r > '9' }) >= 0 {
			add(tok)
		}
	}
	return tokens, truncated
}

// answerCandidateTokens normalizes one accepted answer surface form to its value
// token hash set with the SAME extractor the broker used on the model I/O, so "the
// answer appears" means exactly the same thing (same canonicalization, same hash)
// on both sides. A form with no value tokens (e.g. a short string below the min
// length) yields an empty set and is never matched (fail-open direction).
func answerCandidateTokens(candidate string) map[uint64]struct{} {
	tokens, _ := valueTokenSet(candidate)
	return tokens
}

// candidatePresent reports whether any accepted answer form is fully contained in
// a call's token hash set: every value-token hash of some non-empty candidate form
// is present. A single-token numeric answer (the v12 money case) reduces to plain
// membership.
func candidatePresent(callTokens map[uint64]struct{}, candidates []map[uint64]struct{}) bool {
	for _, form := range candidates {
		if len(form) == 0 {
			continue
		}
		contained := true
		for tok := range form {
			if _, ok := callTokens[tok]; !ok {
				contained = false
				break
			}
		}
		if contained {
			return true
		}
	}
	return false
}

// answerStuffed applies the value-provenance rule to one case's recorded I/O: the
// answer value appears in an INPUT before it appears in any COMPLETION. Input of
// call k ranks before completion of call k (input position 2k < completion
// position 2k+1), so an answer the harness placed in a call's input that the same
// call's model then echoes is correctly read as stuffed. An answer that only ever
// appears in completions, or first appears in a completion and is reused in a
// later input (legit multi-turn), is clean.
func answerStuffed(io caseModelIOLog, candidates []map[uint64]struct{}) bool {
	minInputPos, minCompletionPos := -1, -1
	for k, call := range io.calls {
		if minInputPos < 0 && candidatePresent(call.inputTokens, candidates) {
			minInputPos = 2 * k
		}
		if minCompletionPos < 0 && candidatePresent(call.completionTokens, candidates) {
			minCompletionPos = 2*k + 1
		}
	}
	if minInputPos < 0 {
		return false // the finished answer never appears in any model input
	}
	if minCompletionPos < 0 {
		return true // in an input, never in a completion -> harness-authored
	}
	return minInputPos < minCompletionPos
}

// populateV12AnswerStuffing runs the passive answer-stuffing provenance pass and
// writes the per-case telemetry fields the v12 answer-stuffing gate reads. It is a
// no-op for bench_version<12. For every model-reached, non-excluded case it pulls
// the broker's recorded clean-pass I/O and settles it as one of:
//
//   - PENDING (AnswerStuffObserved=false): the I/O log is unavailable -- a genuine
//     capture gap the agent cannot cause -- so the gate reader treats attribution
//     as incomplete and fails OPEN.
//   - EXCLUDED (Observed=true, verdict nil, review=false): a verbatim-recall
//     (non-computed) answer, kept out of the tally -- even if its capture truncated.
//   - REVIEW (Observed=true, verdict nil, AnswerStuffReviewRequired=true): a
//     COMPUTED case whose capture overflowed the per-side ceiling (a pathological
//     prompt beyond the model's full context window). The run routes to human
//     review -- never fail-open, never auto-zero.
//   - SETTLED verdict (Observed=true, AnswerStuffed=&bool): a fully-captured
//     computed case gets a true/false stuffed verdict.
//
// The per-side capture ceiling sits above the model's full context window, so a
// legitimate deep-RAG agent using its entire context is always fully captured and
// judged on provenance alone -- prompt size is never itself a signal.
func populateV12AnswerStuffing(
	runID string,
	benchVersion int,
	perCase []protocol.CaseScore,
	transcripts []transcriptCase,
	specs []answerStuffingCaseSpec,
	source caseModelIOSource,
) answerStuffingSummary {
	var summary answerStuffingSummary
	if benchVersion < protocol.BenchVersionV12 || source == nil {
		return summary
	}
	if len(perCase) != len(transcripts) || len(specs) != len(transcripts) {
		log.Printf("run %s: v12 answer-stuffing skipped: misaligned per-case inputs", runID)
		return summary
	}
	for i, score := range perCase {
		if v9base.ExcludedFromModelPopulation(score) {
			continue
		}
		if !transcripts[i].Execution.ModelInferenceObserved {
			// Never reached the model: a fast deterministic/retrieval answer is not a
			// stuffing case and is not part of the slice.
			continue
		}
		spec := specs[i]
		if !spec.populated() {
			// No spec captured for this model-reached case: cannot settle provenance,
			// so it stays pending and the gate fails open.
			summary.Pending++
			continue
		}
		io, ok := source.CaseModelIO(spec.caseID)
		if !ok {
			// No capture log at all for this model-reached case: a genuine capture gap
			// (a broker/relay fault, NOT anything the agent's prompt size can cause --
			// every model-reached v12 case opens a log). Leave pending so the gate fails
			// OPEN: a detection gate never penalizes an honest run for a validator gap.
			summary.Pending++
			continue
		}
		if io.truncated {
			if !spec.computed {
				// A verbatim-recall (non-computed) case is excluded from the tally
				// regardless of capture completeness -- its answer legitimately sits in
				// the retrieved evidence, so it is never a stuffing vector. Settle it as
				// excluded rather than dragging the whole run to fail-open.
				transcripts[i].Execution.AnswerStuffObserved = true
				summary.Administered++
				summary.Excluded++
				continue
			}
			// A COMPUTED case whose capture overflowed the per-side ceiling. This bound
			// sits above the model's full context window, so a legitimate deep-RAG
			// prompt can never reach it; a residual truncation here is a pathological,
			// oversized body. We can neither clear the case (pass) nor prove it stuffed,
			// so route the RUN to human review: settle the case as observed-but-review
			// (never pending/fail-open, never auto-zero). Size is never itself a signal
			// -- this is a last-resort safety net, not a size penalty.
			transcripts[i].Execution.AnswerStuffObserved = true
			transcripts[i].Execution.AnswerStuffReviewRequired = true
			summary.Administered++
			summary.Review++
			continue
		}
		// The provenance for this case is SETTLED.
		transcripts[i].Execution.AnswerStuffObserved = true
		summary.Administered++
		// Build the accepted answer surface forms once for both the loose and the
		// provable provenance checks (both apply the SAME value-provenance rule).
		var candidates []map[uint64]struct{}
		if spec.computed || spec.computedLoose {
			candidates = make([]map[uint64]struct{}, 0, len(spec.answerCandidates))
			for _, form := range spec.answerCandidates {
				if tokens := answerCandidateTokens(form); len(tokens) > 0 {
					candidates = append(candidates, tokens)
				}
			}
		}
		// LOOSE systematic-review signal: for a COMPUTED (not verbatim-recall) case,
		// record whether the finished answer value appeared in a model INPUT before any
		// COMPLETION, REGARDLESS of whether the value also appears in seeded memory.
		// This is a superset of the provable slice below and only ever routes a
		// systematic pattern to review (never an auto-zero); an honest RAG/legit-compute
		// agent's computed answers appear in completions, not inputs, so this is ~0.
		if spec.computedLoose {
			looseStuffed := len(candidates) > 0 && answerStuffed(io, candidates)
			transcripts[i].Execution.AnswerStuffLoose = &looseStuffed
			summary.LooseEligible++
			if looseStuffed {
				summary.LooseStuffed++
			}
		}
		if !spec.computed {
			// Not PROVABLY computed (verbatim-recall, or a computed answer whose value
			// also appears in seeded memory): excluded from the provable auto-gate tally,
			// kept out of its denominator. AnswerStuffed stays nil. The loose verdict
			// above (if computedLoose) still governs the systematic-review signal.
			summary.Excluded++
			continue
		}
		summary.Eligible++
		stuffed := len(candidates) > 0 && answerStuffed(io, candidates)
		transcripts[i].Execution.AnswerStuffed = &stuffed
		if stuffed {
			summary.Stuffed++
		} else {
			summary.Clean++
		}
	}
	log.Printf("run %s: v12 answer-stuffing: administered=%d eligible=%d stuffed=%d clean=%d excluded=%d review=%d pending=%d loose_eligible=%d loose_stuffed=%d",
		runID, summary.Administered, summary.Eligible, summary.Stuffed, summary.Clean, summary.Excluded, summary.Review, summary.Pending, summary.LooseEligible, summary.LooseStuffed)
	return summary
}

// answerStuffingCandidates builds the accepted answer surface forms for a memory
// case: the canonical ExpectedAnswer, every AcceptAny form, and -- for a money
// answer given in minor units -- the major-unit rendering, so a model I/O that
// prints "$12.34" still matches an expected "1234". The finished value a Class-D
// harness copies is the engine-computed value itself, which equals ExpectedAnswer
// in its own format, so the primary match is exact.
func answerStuffingCandidates(mc protocol.MemoryCase) []string {
	candidates := make([]string, 0, len(mc.AcceptAny)+2)
	seen := make(map[string]struct{})
	add := func(v string) {
		v = strings.TrimSpace(v)
		if v == "" {
			return
		}
		if _, ok := seen[v]; ok {
			return
		}
		seen[v] = struct{}{}
		candidates = append(candidates, v)
	}
	add(mc.ExpectedAnswer)
	for _, alt := range mc.AcceptAny {
		add(alt)
	}
	if mc.AnswerKind == protocol.AnswerMoney {
		if major := moneyMajorUnits(mc.ExpectedAnswer); major != "" {
			add(major)
		}
	}
	return candidates
}

// moneyMajorUnits renders an integer minor-unit money answer (e.g. "1234") as its
// two-decimal major-unit form ("12.34"), returning "" when the answer is not a
// plain integer.
func moneyMajorUnits(minor string) string {
	s := strings.TrimSpace(minor)
	neg := strings.HasPrefix(s, "-")
	s = strings.TrimPrefix(s, "-")
	for _, r := range s {
		if r < '0' || r > '9' {
			return ""
		}
	}
	if s == "" {
		return ""
	}
	for len(s) < 3 {
		s = "0" + s
	}
	out := s[:len(s)-2] + "." + s[len(s)-2:]
	if neg {
		out = "-" + out
	}
	return out
}

// parseChatInputText extracts the concatenated string message contents from a
// normalized OpenAI chat request body. Only message CONTENT is read (never role
// labels, the model field, or tool metadata), so spurious numbers from the
// envelope never enter the value token set. ok=false means the body was
// unparseable and the caller should mark the capture truncated (fail open).
func parseChatInputText(body []byte) (string, bool) {
	var req struct {
		Messages []struct {
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if json.Unmarshal(body, &req) != nil {
		return "", false
	}
	var b strings.Builder
	for _, msg := range req.Messages {
		appendRawContent(&b, msg.Content)
	}
	return b.String(), true
}

// parseChatCompletionText extracts the concatenated choice message contents from
// a chat completion body. Only message content is read (never usage token counts
// or the model field). ok=false means unparseable.
func parseChatCompletionText(body []byte) (string, bool) {
	var resp struct {
		Choices []struct {
			Message struct {
				Content json.RawMessage `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if json.Unmarshal(body, &resp) != nil {
		return "", false
	}
	var b strings.Builder
	for _, choice := range resp.Choices {
		appendRawContent(&b, choice.Message.Content)
	}
	return b.String(), true
}

// appendRawContent appends an OpenAI message content field, which is either a
// string or an array of typed parts, as plain text.
func appendRawContent(b *strings.Builder, raw json.RawMessage) {
	if len(raw) == 0 {
		return
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		b.WriteByte(' ')
		b.WriteString(s)
		return
	}
	var parts []struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &parts) == nil {
		for _, part := range parts {
			b.WriteByte(' ')
			b.WriteString(part.Text)
		}
	}
}
