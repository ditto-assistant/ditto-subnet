package scoregates

// Bench v13 CLAIM-SPAN PROVENANCE gate (issue #1849).
//
// Nothing before v13 checked that the served `answer` / `final_text` was
// actually EMITTED by the controlled model. A harness that lets the model do
// the reading and then rewrites the graded value on the way out -- a `/100`
// rescaler that turns the model's "411067" into "$4,110.67", a direction map
// that turns "went up" into "increase", a composed slot assembled from figures
// the model only mentioned, a dumpish draft replaced by a local parser's value
// -- passes every containment grader because the SERVED text is correct. The
// model was consulted; the answer was not the model's.
//
// The gate is deliberately NOT a whole-text substring rule. Requiring the
// served final_text to be a substring of the concatenated completions would
// zero JSON-mode agents (the model returns `{"answer": …}` and the harness
// unwraps it), `final_answer`-tool delivery (the value rides in tool_call
// arguments), formatters ("4110.67 dollars" -> "$4,110.67"), markdown strippers,
// and agents that splice two completions into one reply. Every one of those is
// honest. So the gate is on the GRADED CLAIM SPAN only: the value tokens of the
// served span the grader credited must be contained in the union of the value
// tokens of every model completion the relay attributed to the case, after both
// sides pass through the same published normaliser (NFKC, casefold,
// markdown/label strip, punctuation fold) and the same canonical-number rule.
// "$4,110.67", "4110.67 dollars", "4,110.67" and "**Answer:** $4110.67" all
// canonicalize to the single value token "4110.67"; "411067" does not.
//
// The pure functions in this file take token sets and return verdicts. They
// never see prompt text on the scorer side: the relay records value-token
// HASHES only (cmd/dittobench-api/claim_span_capture.go), so the record can
// neither reproduce a completion nor leak the answer key. The companion causal
// gate (causal_dependence.go) answers the other half of the question -- not
// "did the model say it" but "did the harness tell the model to say it".
//
// Every rule is gated bench_version >= 13 by the callers and by
// AttachClaimProvenance; v9..v12 evidence, digests, and reports stay
// byte-identical.

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

// TokenSet is a set of value-token hashes (see HashToken). Both the relay
// capture and the scorer gates speak in this type so the two sides agree on
// the vocabulary without sharing state.
type TokenSet map[uint64]struct{}

// Add inserts one already-canonical token; empty tokens are ignored.
func (s TokenSet) Add(token string) {
	if token == "" {
		return
	}
	s[HashToken(token)] = struct{}{}
}

// AddText tokenizes text through the published value-token rule and inserts
// every token. It returns whether the per-side ceiling was exceeded.
func (s TokenSet) AddText(text string) bool {
	tokens, truncated := ValueTokenHashes(text)
	for h := range tokens {
		s[h] = struct{}{}
	}
	return truncated
}

// AddSpan normalizes text through the published v13 claim-span rule
// (NormalizeSpan, then the Unicode-aware span tokenizer) and inserts every
// token. It is the v13 counterpart of AddText: every v13 exemption set
// (delivered records, the case question, the validator prompt, served tool
// results) is built with it so a diacritic or non-Latin value folds the same
// way on every side. It returns whether the per-side ceiling was exceeded.
func (s TokenSet) AddSpan(text string) bool {
	tokens, truncated := SpanTokens(text)
	for h := range tokens {
		s[h] = struct{}{}
	}
	return truncated
}

// Has reports whether the hash of token is present.
func (s TokenSet) Has(token string) bool {
	_, ok := s[HashToken(token)]
	return ok
}

// HasHash reports whether the raw hash is present.
func (s TokenSet) HasHash(h uint64) bool {
	_, ok := s[h]
	return ok
}

// Clone returns an independent copy.
func (s TokenSet) Clone() TokenSet {
	out := make(TokenSet, len(s))
	for h := range s {
		out[h] = struct{}{}
	}
	return out
}

// Subset reports whether every hash in s is present in other. The empty set
// is a subset of everything; callers must treat an empty claim as
// not-applicable BEFORE asking this question.
func (s TokenSet) Subset(other TokenSet) bool {
	for h := range s {
		if _, ok := other[h]; !ok {
			return false
		}
	}
	return true
}

// Value-token capture bounds. They are the Bench v12 answer-provenance bounds
// (cmd/dittobench-api/answer_io_capture.go) moved to the shared package so the
// relay capture and the scorer gate cannot drift: a prompt cannot contain more
// DISTINCT value tokens than it has tokens, and it cannot have more tokens than
// the model's context window, so a legitimate single-context prompt (however
// large, however deep its RAG) can never trip MaxValueTokensPerSide. Size is
// never a signal; the ceiling only bounds memory against a pathological body.
const (
	MaxValueTokensPerSide = 262144
	MaxValueTokenLen      = 64
	// MinStringTokenLen bounds which non-numeric tokens are kept. Numbers of any
	// length are always kept; short common words are dropped so a value never
	// matches on a bare stopword.
	MinStringTokenLen = 4
)

// HashToken maps a canonical value token to a 64-bit FNV-1a hash. It is
// seed-free so a relay's capture-time hashes and the scorer's later claim
// hashes agree without sharing any state. Only hashes are ever stored.
func HashToken(tok string) uint64 {
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

// numberPattern matches a currency/number run: optional sign, optional '$',
// digits with grouping commas, optional fractional part.
var numberPattern = regexp.MustCompile(`-?\$?\d[\d,]*(?:\.\d+)?`)

// alnumTokenPattern matches lowercase alphanumeric runs (already lowercased text).
var alnumTokenPattern = regexp.MustCompile(`[a-z0-9]+`)

// CanonicalNumber normalizes a numeric token to a stable canonical string:
// strips '$' and grouping commas, drops an insignificant fractional part and its
// trailing zeros, drops leading zeros, and collapses "-0" to "0". Returns ""
// when the token is not a number after stripping. It is the single number rule
// shared by the relay capture, the claim-span gate, and the causal gate:
// "$4,110.67", "4110.67", "4110.670" -> "4110.67"; "$245.00" -> "245".
func CanonicalNumber(raw string) string {
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

// ValueTokenHashes extracts the bounded set of value-token HASHES from a blob of
// text: every CanonicalNumber, plus lowercase alphanumeric tokens of at least
// MinStringTokenLen that are not pure digit runs, each stored as its HashToken.
// It is byte-for-byte the Bench v12 answer-provenance rule; the v12 relay
// capture delegates here so the two cannot drift. It returns whether the
// per-side ceiling was exceeded so a caller can mark its capture truncated.
func ValueTokenHashes(text string) (TokenSet, bool) {
	tokens := make(TokenSet)
	truncated := false
	add := func(tok string) {
		if tok == "" || len(tok) > MaxValueTokenLen {
			return
		}
		h := HashToken(tok)
		if _, ok := tokens[h]; ok {
			return
		}
		if len(tokens) >= MaxValueTokensPerSide {
			truncated = true
			return
		}
		tokens[h] = struct{}{}
	}
	for _, match := range numberPattern.FindAllString(text, -1) {
		add(CanonicalNumber(match))
	}
	for _, tok := range alnumTokenPattern.FindAllString(strings.ToLower(text), -1) {
		// Pure-digit runs are already covered by the number pass in canonical form;
		// keep only sufficiently long non-numeric-length tokens here.
		if len(tok) >= MinStringTokenLen && strings.IndexFunc(tok, func(r rune) bool { return r < '0' || r > '9' }) >= 0 {
			add(tok)
		}
	}
	return tokens, truncated
}

// labelPrefixPattern strips a leading answer label from a line: "ANSWER:",
// "Final answer -", "A:", "Result:", optionally wrapped in markdown emphasis.
// Labels are harness/model formatting, never claim content.
var labelPrefixPattern = regexp.MustCompile(`(?im)^[\s*_#>\-]*(?:final\s+answer|answer|result|response|output|a)\s*[:\-–—]\s*`)

// listMarkerPattern strips markdown list markers and ordered-list numbers at
// the start of a line. The ordinal of a list item is layout, not a value: a
// model listing "1. Lisbon" did not emit the value 1.
var listMarkerPattern = regexp.MustCompile(`(?m)^\s*(?:[-*+•]|\d{1,3}[.)])\s+`)

// NormalizeSpan is the PUBLISHED request-independent normaliser both sides of
// the claim-span gate apply before tokenizing: Unicode NFD with every combining
// mark (\p{Mn}) dropped, then NFKC (so diacritics fold -- "José" -> "jose",
// "Ōsaka" -> "osaka", "Zürich" -> "zurich" -- and fullwidth digits and
// compatibility forms fold to their ASCII counterparts), lowercase,
// markdown/label stripping (emphasis, code fences, headings, blockquotes, list
// markers, "ANSWER:"-style labels), punctuation folded to spaces except the
// characters that carry numeric meaning inside a number ('$', '.', ',', '-'),
// and whitespace collapsed. Letters of every script are kept (Cyrillic, CJK,
// Greek), so a non-Latin value tokenizes to a claim rather than to nothing. It
// is deterministic and total: any input yields one output, and running it twice
// is a no-op. Miners can run it locally over their own served text and
// completions to check the gate before uploading.
func NormalizeSpan(s string) string {
	s = norm.NFKC.String(stripCombiningMarks(norm.NFD.String(s)))
	s = labelPrefixPattern.ReplaceAllString(s, "")
	s = listMarkerPattern.ReplaceAllString(s, "")
	var b strings.Builder
	b.Grow(len(s))
	prevSpace := true
	for _, r := range s {
		switch {
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			b.WriteString(strings.ToLower(string(r)))
			prevSpace = false
		case r == '$' || r == '.' || r == ',' || r == '-':
			// Numeric-meaning punctuation is kept so the number rule can read
			// "$4,110.67" and "-3"; CanonicalNumber decides what it means.
			b.WriteRune(r)
			prevSpace = false
		default:
			// Everything else -- markdown emphasis, quotes, brackets, colons,
			// slashes, and all whitespace -- becomes one separator.
			if !prevSpace {
				b.WriteByte(' ')
				prevSpace = true
			}
		}
	}
	return strings.TrimSpace(b.String())
}

// stripCombiningMarks drops every non-spacing combining mark from an
// NFD-decomposed string, which is what folds a base letter's diacritics away.
func stripCombiningMarks(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		if unicode.Is(unicode.Mn, r) {
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}

// spanTokenPattern matches runs of letters and digits in ANY script over the
// already-normalized (lowercased) span text. It is the Bench v13 claim-span
// tokenizer; the v12 answer-IO capture keeps its ASCII-only alnumTokenPattern
// so v12 capture bytes never move.
var spanTokenPattern = regexp.MustCompile(`[\p{L}\p{N}]+`)

// SpanTokens applies NormalizeSpan and then the published v13 claim-span token
// rule: every CanonicalNumber, plus every letter/digit run of at least
// MinStringTokenLen RUNES (not bytes, so "Ōsaka" and "Москва" count by letter)
// that is not a pure ASCII digit run, each stored as its HashToken. It shares
// HashToken, CanonicalNumber, and the capture bounds with the v12 rule and
// differs only in the tokenizer's alphabet; the v12 ValueTokenHashes is left
// byte-for-byte because it backs the v12 answer-IO capture. Both the relay
// capture and the served-span extraction call this so the two sides agree
// token for token. It returns whether the per-side ceiling was exceeded.
func SpanTokens(text string) (TokenSet, bool) {
	normalized := NormalizeSpan(text)
	tokens := make(TokenSet)
	truncated := false
	add := func(tok string) {
		if tok == "" || len(tok) > MaxValueTokenLen {
			return
		}
		h := HashToken(tok)
		if _, ok := tokens[h]; ok {
			return
		}
		if len(tokens) >= MaxValueTokensPerSide {
			truncated = true
			return
		}
		tokens[h] = struct{}{}
	}
	for _, match := range numberPattern.FindAllString(normalized, -1) {
		add(CanonicalNumber(match))
	}
	for _, tok := range spanTokenPattern.FindAllString(normalized, -1) {
		if utf8.RuneCountInString(tok) < MinStringTokenLen {
			continue
		}
		// Pure ASCII digit runs are already covered by the number pass in
		// canonical form.
		if strings.IndexFunc(tok, func(r rune) bool { return r < '0' || r > '9' }) < 0 {
			continue
		}
		add(tok)
	}
	return tokens, truncated
}

// ServedClaimTokens extracts the graded CLAIM SPAN from the served text the
// grader credited. groups are the grader's accepted surface forms, one group per
// claim unit: for a value the expected string and its accept set, for a number
// the digits and the English number word, for a money claim the major-unit
// form, for a direction every accepted phrase, and for a list one group per
// item (the item and its alternatives). The claim tokens are the union of the
// token sets of every form that is wholly present in the served span: that is
// exactly the value the grader matched, in the form the harness served it.
// ok=false when no form is present or every present form has an empty token
// set (a value below MinStringTokenLen, or a number word the number rule cannot
// see): the gate then has no claim span to check and MUST fail open rather
// than guess.
func ServedClaimTokens(servedText string, groups [][]string) (TokenSet, bool) {
	served, _ := SpanTokens(servedText)
	claim := make(TokenSet)
	for _, group := range groups {
		for _, alt := range group {
			altTokens, _ := SpanTokens(alt)
			if len(altTokens) == 0 || !altTokens.Subset(served) {
				continue
			}
			for h := range altTokens {
				claim[h] = struct{}{}
			}
		}
	}
	if len(claim) == 0 {
		return nil, false
	}
	return claim, true
}

// claimGroupsEmitted is the per-unit model-emitted verdict behind
// TextProvenance for a grouped claim: for every claim unit with at least one
// form present in the served span, SOME form of that unit (present or not)
// must have a non-empty token set contained in the completion union. The
// grader credits any accepted form, so a harness that renders the model's
// "three" as "3", its "Lisboa" as "Lisbon", or its "went up" as "increase" is an
// honest formatter: the model produced the value, in a form the grader itself
// accepts. A form the grader does not accept ("411067" for a major-unit money
// claim, "moved to a larger amount" for a direction) is never a group member,
// so a rewrite still fails. Units with no present form are not checked: they
// were not credited.
func claimGroupsEmitted(servedText string, groups [][]string, completions TokenSet) bool {
	served, _ := SpanTokens(servedText)
	for _, group := range groups {
		present := false
		for _, alt := range group {
			altTokens, _ := SpanTokens(alt)
			if len(altTokens) != 0 && altTokens.Subset(served) {
				present = true
				break
			}
		}
		if !present {
			continue
		}
		emitted := false
		for _, alt := range group {
			altTokens, _ := SpanTokens(alt)
			if len(altTokens) != 0 && altTokens.Subset(completions) {
				emitted = true
				break
			}
		}
		if !emitted {
			return false
		}
	}
	return true
}

// Claim-span provenance finding names. They appear in per-case evidence
// (protocol.ClaimProvenanceEvidence.Findings), case notes, and the run summary.
const (
	// FindingServedTextNotModelEmitted: the graded claim span is not contained in
	// any completion the relay attributed to the case. The value the harness
	// served is not a value the model produced.
	FindingServedTextNotModelEmitted = "served_text_not_model_emitted"
	// FindingAnswerInPrompt: the graded claim span was authored by the harness
	// into a model prompt before any completion produced it, and it is not a
	// value from a delivered record or tool result (see causal_dependence.go).
	FindingAnswerInPrompt = "answer_in_prompt"
	// FindingClaimProvenanceUnavailable: no relay ledger exists for the case (no
	// v13 broker session, or the session was torn down before the read). Fail open.
	FindingClaimProvenanceUnavailable = "claim_provenance_unavailable"
	// FindingClaimProvenanceIncomplete: a capture bound was hit (a completion
	// or request span the relay could not read or store), so the union may be
	// missing the completion that produced the value. Charged to the relay: the
	// gate fails open.
	FindingClaimProvenanceIncomplete = "claim_provenance_incomplete"
	// FindingClaimProvenanceUnattributedCall: while this case was in flight
	// with other cases, the harness made a chat completion that named no case
	// (no case-scoped inference_base_url, no X-Ditto-Case-Id, and several
	// cases in flight), so the relay could not file it. Charged to the HARNESS:
	// the v13 contract requires attributable calls under concurrency, so under
	// enforce the affected cases fail CLOSED (zero credit) rather than open;
	// under shadow the count is published so operators can read coverage.
	FindingClaimProvenanceUnattributedCall = "claim_provenance_unattributed_call"
	// FindingClaimNotApplicable: the case carries no checkable value claim (a
	// decline, acknowledgement, chit-chat, persistence/reversal stance, a value
	// below the token floor), or the case scored zero so nothing was credited.
	FindingClaimNotApplicable = "claim_not_applicable"
	// FindingNoModelCompletion: the case's evidence is complete and the relay saw
	// NO completion for it, yet a value was credited. Reported as its own finding
	// so the summary can separate "no model call" from "rewritten value"; it also
	// implies served_text_not_model_emitted.
	FindingNoModelCompletion = "no_model_completion"
	// FindingClaimProvenanceZeroed: the case score was zeroed under enforce.
	FindingClaimProvenanceZeroed = "claim_provenance_zeroed"
)

// TextProvenance is the pure claim-span verdict: the served claim tokens must be
// a subset of the union of the case's attributed completion tokens. An empty
// claim is not applicable and passes (the caller reports
// FindingClaimNotApplicable). ok=false with no completions means the value was
// served without any model output at all.
func TextProvenance(claim, completions TokenSet) (modelEmitted bool) {
	if len(claim) == 0 {
		return true
	}
	return claim.Subset(completions)
}

// ClaimProvenancePosture is the ONE posture switch shared by the claim-span
// provenance gate and the causal answer_in_prompt gate: they ship together and
// are enforced together.
type ClaimProvenancePosture string

const (
	// ClaimProvenanceShadow records per-case findings, notes, and the run summary
	// and moves no score. The compiled default (Owner decision -- default taken).
	ClaimProvenanceShadow ClaimProvenancePosture = "shadow"
	// ClaimProvenanceEnforce zeroes a settled flagged case's score in scored
	// scope. Opt-in only, after the honest cohort shows zero false zeros.
	ClaimProvenanceEnforce ClaimProvenancePosture = "enforce"
)

// ParseClaimProvenancePosture maps an operator string to a posture. Anything
// but an exact "enforce" is shadow: the safe end is the default.
func ParseClaimProvenancePosture(raw string) ClaimProvenancePosture {
	if strings.TrimSpace(strings.ToLower(raw)) == string(ClaimProvenanceEnforce) {
		return ClaimProvenanceEnforce
	}
	return ClaimProvenanceShadow
}

// ResultClaimProvenanceFlagged marks a run in which at least one settled claim
// was not model-emitted or was harness-authored into the prompt.
const ResultClaimProvenanceFlagged Result = "claim_provenance_flagged"

// ClaimProvenanceInput is the trusted per-case aggregate the run-level v13
// claim-provenance evidence consumes. AdministeredCases is every memory case in
// the eligible population; EligibleCases is how many carried a settled,
// applicable claim verdict; NotModelEmittedCases and AnswerInPromptCases are the
// flagged subsets (a case can be both). AttributionComplete is the trust bit: it
// is true only when every applicable case settled (no unavailable/incomplete
// ledger). Zeroed is how many cases the enforce posture actually zeroed.
//
// FlaggedCases is the UNION of the two flagged subsets (a case that trips both
// gates counts once), so FlaggedBPS reports the share of eligible cases that
// carry any finding rather than max(not_model_emitted, answer_in_prompt).
// UnattributedCallCases is the subset of UnsettledCases whose ledger was left
// incomplete by a harness completion that named no case while several were in
// flight (FindingClaimProvenanceUnattributedCall); under enforce those cases
// are zeroed too, so ZeroedCases is bounded by FlaggedCases plus
// UnattributedCallCases. AttributionComplete must equal UnsettledCases == 0 in
// both directions.
type ClaimProvenanceInput struct {
	AdministeredCases     int
	EligibleCases         int
	NotModelEmittedCases  int
	AnswerInPromptCases   int
	FlaggedCases          int
	UnattributedCallCases int
	UnsettledCases        int
	ZeroedCases           int
	Posture               ClaimProvenancePosture
	TelemetryComplete     bool
	AttributionComplete   bool
}

// ClaimProvenanceEvidence is the signed, pure run-level evidence for the v13
// claim-span provenance and causal gates. It mirrors the other gate schemas so
// the public projection and validator consume it uniformly. FactorBPS is ALWAYS
// full: the gates act per CLAIM (a flagged case's own score is zeroed under
// enforce), so the run-level factor is an identity term that keeps the evidence
// schema uniform and lets the aggregate be signed and audited. For
// bench_version<13 every field is the zero value and the gate is neither
// canonicalized nor multiplied into the composite.
//
// The Platform-side mirror is ditto_screening_protocol.bench_v9.V13ClaimProvenanceGate;
// its canonical_bytes MUST list these fields in this order, and the bit-paired
// fixture testdata/v13_claim_provenance_evidence.json pins the digest both
// sides must reproduce.
type ClaimProvenanceEvidence struct {
	AdministeredCases     int                    `json:"administered_cases"`
	EligibleCases         int                    `json:"eligible_cases"`
	NotModelEmittedCases  int                    `json:"not_model_emitted_cases"`
	AnswerInPromptCases   int                    `json:"answer_in_prompt_cases"`
	FlaggedCases          int                    `json:"flagged_cases"`
	UnattributedCallCases int                    `json:"unattributed_call_cases"`
	UnsettledCases        int                    `json:"unsettled_cases"`
	ZeroedCases           int                    `json:"zeroed_cases"`
	AttributionComplete   bool                   `json:"attribution_complete"`
	Posture               ClaimProvenancePosture `json:"posture"`
	FlaggedBPS            int                    `json:"flagged_bps"`
	Result                Result                 `json:"result"`
	FactorBPS             int                    `json:"factor_bps"`
}

// administered reports whether the gate was actually run for this evidence. A
// zero-value ClaimProvenanceEvidence (Result unset) means "not administered": it
// is omitted from the canonical bytes and never affects the combined factor, so
// a v13 Evidence built without the gate stays byte-identical to the contract
// without it.
func (e ClaimProvenanceEvidence) administered() bool { return e.Result != "" }

// buildClaimProvenance turns the trusted per-case aggregate into pure evidence.
// Outcome precedence, most conservative first: invalid telemetry is a hard
// error; incomplete attribution publishes insufficient_evidence; no applicable
// claim publishes not_applicable; otherwise the run is passed or flagged. The
// factor is full in every branch (see ClaimProvenanceEvidence).
func buildClaimProvenance(in ClaimProvenanceInput) (ClaimProvenanceEvidence, error) {
	if in.Posture != ClaimProvenanceShadow && in.Posture != ClaimProvenanceEnforce {
		return ClaimProvenanceEvidence{}, invalid("claim-provenance posture must be shadow or enforce")
	}
	if !in.TelemetryComplete {
		return ClaimProvenanceEvidence{}, fmt.Errorf("%w: claim-provenance", ErrTelemetryUnavailable)
	}
	caseCounts := []struct {
		name  string
		count int
	}{
		{"claim_provenance.administered_cases", in.AdministeredCases},
		{"claim_provenance.eligible_cases", in.EligibleCases},
		{"claim_provenance.not_model_emitted_cases", in.NotModelEmittedCases},
		{"claim_provenance.answer_in_prompt_cases", in.AnswerInPromptCases},
		{"claim_provenance.flagged_cases", in.FlaggedCases},
		{"claim_provenance.unattributed_call_cases", in.UnattributedCallCases},
		{"claim_provenance.unsettled_cases", in.UnsettledCases},
		{"claim_provenance.zeroed_cases", in.ZeroedCases},
	}
	for _, item := range caseCounts {
		if err := validateBoundedCases(item.name, item.count); err != nil {
			return ClaimProvenanceEvidence{}, err
		}
	}
	if in.EligibleCases+in.UnsettledCases > in.AdministeredCases {
		return ClaimProvenanceEvidence{}, invalid("claim-provenance eligible and unsettled cases exceed administered_cases")
	}
	if in.NotModelEmittedCases > in.EligibleCases || in.AnswerInPromptCases > in.EligibleCases {
		return ClaimProvenanceEvidence{}, invalid("claim-provenance flagged cases exceed eligible_cases")
	}
	maxFlagged := in.NotModelEmittedCases
	if in.AnswerInPromptCases > maxFlagged {
		maxFlagged = in.AnswerInPromptCases
	}
	if in.FlaggedCases < maxFlagged || in.FlaggedCases > in.NotModelEmittedCases+in.AnswerInPromptCases || in.FlaggedCases > in.EligibleCases {
		return ClaimProvenanceEvidence{}, invalid("claim-provenance flagged_cases must be the union of the two flagged subsets")
	}
	if in.UnattributedCallCases > in.UnsettledCases {
		return ClaimProvenanceEvidence{}, invalid("claim-provenance unattributed_call_cases exceed unsettled_cases")
	}
	if in.ZeroedCases > in.FlaggedCases+in.UnattributedCallCases {
		return ClaimProvenanceEvidence{}, invalid("claim-provenance zeroed_cases exceed the flagged and unattributed cases")
	}
	if in.Posture == ClaimProvenanceShadow && in.ZeroedCases != 0 {
		return ClaimProvenanceEvidence{}, invalid("claim-provenance shadow posture cannot zero cases")
	}
	if in.AttributionComplete != (in.UnsettledCases == 0) {
		return ClaimProvenanceEvidence{}, invalid("claim-provenance attribution_complete must equal unsettled_cases == 0")
	}
	e := ClaimProvenanceEvidence{
		AdministeredCases: in.AdministeredCases, EligibleCases: in.EligibleCases,
		NotModelEmittedCases: in.NotModelEmittedCases, AnswerInPromptCases: in.AnswerInPromptCases,
		FlaggedCases: in.FlaggedCases, UnattributedCallCases: in.UnattributedCallCases,
		UnsettledCases: in.UnsettledCases, ZeroedCases: in.ZeroedCases,
		AttributionComplete: in.AttributionComplete, Posture: in.Posture,
		FactorBPS: BasisPointScale,
	}
	if in.EligibleCases > 0 {
		e.FlaggedBPS = coverageBPS(in.FlaggedCases, in.EligibleCases)
	}
	switch {
	case !in.AttributionComplete:
		// The run-level result is informational (identity factor): an unsettled
		// case publishes insufficient_evidence here whether it failed open (a
		// relay capture bound) or closed (an unattributed harness call under
		// enforce); the per-case findings say which.
		e.Result = ResultInsufficientEvidence
	case in.EligibleCases == 0:
		e.Result = ResultNotApplicable
	case in.FlaggedCases > 0:
		e.Result = ResultClaimProvenanceFlagged
	default:
		e.Result = ResultPassed
	}
	return e, nil
}

// AttachClaimProvenance binds the v13 claim-provenance gate onto an already-built
// v13 Evidence. Keeping it a post-Build attach leaves scoregates.Build and every
// v9..v12 caller source- and byte-identical: the base evidence is assembled
// first, and only bench_version>=13 layers this gate on top. The returned
// Evidence is fully re-validated (its digest now binds the claim-provenance
// evidence too).
func AttachClaimProvenance(e Evidence, in ClaimProvenanceInput) (Evidence, error) {
	if e.BenchVersion < BenchVersionV13 {
		return Evidence{}, invalid("claim-provenance gate is defined only for bench v%d and later", BenchVersionV13)
	}
	provenance, err := buildClaimProvenance(in)
	if err != nil {
		return Evidence{}, err
	}
	e.ClaimProvenance = provenance
	if err := e.Validate(); err != nil {
		return Evidence{}, err
	}
	return e, nil
}

// SortedFindings returns a deterministic, de-duplicated finding list so
// per-case evidence and notes are byte-stable across runs.
func SortedFindings(findings []string) []string {
	seen := make(map[string]struct{}, len(findings))
	out := make([]string, 0, len(findings))
	for _, f := range findings {
		if f == "" {
			continue
		}
		if _, ok := seen[f]; ok {
			continue
		}
		seen[f] = struct{}{}
		out = append(out, f)
	}
	sort.Strings(out)
	return out
}

// ClaimSpanLedger is the per-case token record both gates read. The relay
// builds one per attributed wire case (cmd/dittobench-api/claim_span_capture.go)
// by feeding it the harness-authored and completion spans of every successful
// chat completion in call order, plus every tool_endpoint result it served the
// case; the published vectors build the same ledger from plain strings. It
// holds value-token HASHES only.
//
// Ordering matters for the causal gate: a harness-authored token is
// "harness-first" only when no EARLIER completion of the same case already
// produced it, so a value the model derived and the harness later re-injected
// ("you said 4110.67; now format it") is model-derived, not laundered.
type ClaimSpanLedger struct {
	Completion   TokenSet
	HarnessFirst TokenSet
	ToolResult   TokenSet
	Completions  int
	ToolResults  int
	// Truncated is set when any span exceeded the per-side ceiling; the gate
	// treats the ledger as unsettled (fail open).
	Truncated bool
}

// NewClaimSpanLedger returns an empty ledger.
func NewClaimSpanLedger() *ClaimSpanLedger {
	return &ClaimSpanLedger{Completion: make(TokenSet), HarnessFirst: make(TokenSet), ToolResult: make(TokenSet)}
}

// RequestSpan is one harness-placed span of a chat request with the role it
// was sent under. Assistant is true for a message the harness presented AS THE
// MODEL (an assistant-role turn: a prefill, or carried conversation history).
type RequestSpan struct {
	Text      string
	Assistant bool
}

// RecordCall books one successful chat completion: harness is every
// harness-authored request span (system/developer messages, user template,
// tool-role messages), completion is every model-emitted span (message content,
// tool_call arguments, structured-output fields). The request precedes its own
// completion, so harness tokens are tested against completions of EARLIER calls
// only. It is RecordRequest with every span non-assistant and no session
// history; the published vectors use it.
func (l *ClaimSpanLedger) RecordCall(harness, completion []string) {
	spans := make([]RequestSpan, 0, len(harness))
	for _, span := range harness {
		spans = append(spans, RequestSpan{Text: span})
	}
	l.RecordRequest(spans, completion, nil)
}

// RecordRequest books one successful chat completion with role-aware
// harness-first classification. A non-assistant span's tokens are harness-first
// unless an earlier completion of THIS case produced them. An assistant-role
// span is text the harness attributes to the model, so its tokens are tested
// against sessionCompletions too: every completion the model made anywhere in
// the session (other cases, /seed-time summaries, calls outside any /run
// window). A carried assistant turn or a model-written memory summary that
// contains a value is therefore model-derived, not laundered; an assistant
// prefill carrying a value NO completion ever produced is still harness-first.
// sessionCompletions may be nil.
func (l *ClaimSpanLedger) RecordRequest(spans []RequestSpan, completion []string, sessionCompletions TokenSet) {
	for _, span := range spans {
		tokens, truncated := SpanTokens(span.Text)
		if truncated {
			l.Truncated = true
		}
		for h := range tokens {
			if _, seen := l.Completion[h]; seen {
				continue
			}
			if span.Assistant && sessionCompletions.HasHash(h) {
				continue
			}
			l.HarnessFirst[h] = struct{}{}
		}
	}
	for _, span := range completion {
		tokens, truncated := SpanTokens(span)
		if truncated {
			l.Truncated = true
		}
		for h := range tokens {
			l.Completion[h] = struct{}{}
		}
	}
	l.Completions++
}

// RecordToolResult books one tool_endpoint result the validator served the
// case; its values are exempt from the causal gate.
func (l *ClaimSpanLedger) RecordToolResult(result string) {
	if l.ToolResult.AddSpan(result) {
		l.Truncated = true
	}
	l.ToolResults++
}

// ClaimVerdict is the settled per-claim outcome of both gates.
type ClaimVerdict struct {
	// Applicable is false when the served span carries no checkable claim
	// (ServedClaimTokens found no alternative); ModelEmitted and AnswerInPrompt
	// are then meaningless and the caller reports FindingClaimNotApplicable.
	Applicable     bool
	ClaimTokens    int
	ModelEmitted   bool
	AnswerInPrompt bool
}

// EvaluateClaim runs both gates for one credited claim: served is the span the
// grader credited, groups its accepted canonical forms per claim unit (see
// ServedClaimTokens), ledger the case's relay record, and exemptions the token
// sets subtracted from the harness-first set before the causal test (delivered
// records, the case's own user input, the validator's system prompt; the
// ledger's tool results are always subtracted). The claim-span verdict accepts
// ANY grader-accepted form of each credited unit in the completions
// (claimGroupsEmitted); the causal verdict tests the SERVED form's tokens, the
// value the harness actually put on the wire.
func EvaluateClaim(served string, groups [][]string, ledger *ClaimSpanLedger, exemptions ...TokenSet) ClaimVerdict {
	claim, ok := ServedClaimTokens(served, groups)
	if !ok {
		return ClaimVerdict{}
	}
	residual := ResidualHarnessTokens(ledger.HarnessFirst, append([]TokenSet{ledger.ToolResult}, exemptions...)...)
	return ClaimVerdict{
		Applicable:     true,
		ClaimTokens:    len(claim),
		ModelEmitted:   claimGroupsEmitted(served, groups, ledger.Completion),
		AnswerInPrompt: CausalDependence(claim, residual),
	}
}
