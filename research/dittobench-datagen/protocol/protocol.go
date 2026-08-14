// Package protocol defines the shared wire types exchanged between the
// DittoBench validator (on Bittensor subnet 118) and a miner's agent harness.
//
// The validator imports this package, so it is the single source of these
// shapes; a harness in any language matches the JSON field names and types
// here. Published so a harness can be built and tested against the exact
// contract offline, with no private dependency.
package protocol

import "encoding/json"

// ToolSpec is an expected tool in a dataset case.
type ToolSpec struct {
	Name          string            `json:"name"`
	RequiredArgs  map[string]string `json:"required_args,omitempty"`
	ForbiddenArgs []string          `json:"forbidden_args,omitempty"`
}

// ToolCase is one tool-calling benchmark case.
//
// Unordered marks a case whose ExpectedTools are INDEPENDENT calls (a parallel
// request). FuzzyTrajectory marks an outcome-driven agent task where the named
// capabilities are expected but the agent may inspect/retry/reorder as needed;
// data dependencies and the final observed outcome enforce correctness instead
// of one prescribed trace. Both score names/args without relative-order credit.
// For a fuzzy case MaxToolCalls describes the expected task envelope, not a hard
// cap; AllowExtraTools permits longer creative trajectories without a call-count
// penalty. Run-level token efficiency remains a separate scoring signal.
type ToolCase struct {
	ID               string     `json:"id"`
	Category         string     `json:"category"`
	Prompt           string     `json:"prompt"`
	ExpectedTools    []ToolSpec `json:"expected_tools"`
	MaxToolCalls     int        `json:"max_tool_calls"`
	AllowExtraTools  bool       `json:"allow_extra_tools"`
	Unordered        bool       `json:"unordered,omitempty"`
	FuzzyTrajectory  bool       `json:"fuzzy_trajectory,omitempty"`
	ExpectedBehavior string     `json:"expected_behavior,omitempty"`
	// PrerequisitePairs are validator-internal, seed-bound routing facts loaded
	// before the tool phase. In V8 the first prerequisite payload is the initial
	// shared world: the harness store is intentionally preserved through every
	// tool case and the later memory phase. They are emitted only by v8+; v7 and
	// earlier artifacts retain their exact bytes. The harness sees the pairs
	// through the ordinary /seed contract, never in /run.
	PrerequisitePairs []MemoryPair `json:"prerequisite_pairs,omitempty"`
	// WritingProtected is generator-only semantic identity metadata. It keeps
	// aliases, people, products, and other join keys out of the typo projector;
	// it never enters the public artifact or harness request.
	WritingProtected []string `json:"-"`
}

// AnswerKind values: how a memory case is graded deterministically. Grading is
// fully non-LLM; each kind names the check the scorer runs against the
// response's answer slot (RunResponse.Answer, falling back to FinalText).
const (
	// AnswerValue: the expected value must be present (normalized bounded
	// containment); any DistractorAnswers value present scores 0.
	AnswerValue = "value"
	// AnswerNumber: exact number-token match (digits or the English word).
	AnswerNumber = "number"
	// AnswerMoney (bench_version 8): ExpectedAnswer is an integer number of
	// minor currency units. The grader accepts ordinary and localized decimal
	// currency forms while rejecting raw internal cents and adjacent values.
	AnswerMoney = "money"
	// AnswerDirection (bench_version 8): a reviewed increase/decrease concept,
	// including natural equivalents, with opposite/negated direction rejected.
	AnswerDirection = "direction"
	// AnswerList: every AnswerItems element present, any order; credit is the
	// fraction present. A DistractorAnswers value present scores 0.
	AnswerList = "list"
	// AnswerOrderedList: every AnswerItems element present AND in order;
	// an out-of-order or missing element scores 0.
	AnswerOrderedList = "ordered_list"
	// AnswerDuration: the response's duration (number + day/week/month unit)
	// must fall within tolerance of the expected duration.
	AnswerDuration = "duration"
	// AnswerReversal: the expected value must be present together with a
	// cessation phrase (no longer / gave up / stopped ...).
	AnswerReversal = "reversal"
	// AnswerPersistence: the mirror of AnswerReversal for a NEVER-reversed
	// opinion: the expected value must be present together with a persistence
	// phrase (still / love / enjoy ...) and WITHOUT any cessation phrase, so
	// "you no longer do it" is not a free win on contradiction questions.
	AnswerPersistence = "persistence"
	// AnswerDecline: correct behavior is a grounded decline: RunResponse.Abstain,
	// or a decline phrase, and NO DistractorAnswers value (a named value is a
	// fabrication and scores 0).
	AnswerDecline = "decline"
	// AnswerAcknowledge: the case is an INSTRUCTION (delete this, forget that),
	// not a question, so there is no value to return and containment grading
	// would be a phrasing lottery -- "Done, I've removed that" is a perfect
	// response that names nothing. Correct behavior is any confirmation that the
	// instruction was carried out, a post-deletion decline phrase, or
	// RunResponse.Abstain (there is nothing to answer). Whether the mutation
	// actually PERSISTED is graded separately and unfakeably by the paired read
	// case, which is where the real signal lives.
	AnswerAcknowledge = "acknowledge"
	// AnswerChitchat (bench_version 5): a greeting or small-talk turn that carries
	// no memory question at all. There is nothing to recall, so the ONLY thing
	// graded is the non-leak floor: a non-empty reply that surfaces no seeded
	// sentinel (ForbiddenAnswer), no off-topic self value (DumpGuard), and no
	// same-attribute distractor scores 1; surfacing any of them scores 0. This is
	// the canary check inverted -- it directly catches a router that answers a
	// plain "hi" with a stray retrieved value ("Aurora-9"). The negative scans that
	// precede the positive check already enforce the leak zero, so the positive
	// check for this kind is simply "did the harness say anything at all".
	AnswerChitchat = "chitchat"
)

// MemoryCase is one memory-recall benchmark case. The harness is first seeded
// with a fresh haystack (see SeedRequest); then for each case the validator
// POSTs a normal RunRequest whose user_input is Question, and the agent must
// answer from its seeded memory. ExpectedAnswer is the oracle answer, graded
// deterministically per AnswerKind.
type MemoryCase struct {
	// BenchVersion selects version-gated deterministic grading behavior. It is
	// validator-internal and is set only for v8+ so frozen v7 artifacts retain
	// their exact JSON bytes.
	BenchVersion   int    `json:"bench_version,omitempty"`
	ID             string `json:"id"`
	QuestionID     string `json:"question_id"`
	QuestionType   string `json:"question_type"`
	Question       string `json:"question"`
	ExpectedAnswer string `json:"expected_answer"`
	// AnswerKind selects the deterministic grading check (AnswerValue when
	// empty; see the AnswerKind constants). Validator-internal, like
	// ExpectedAnswer.
	AnswerKind string `json:"answer_kind,omitempty"`
	// AnswerItems are the elements of a list/ordered_list answer (ordered for
	// ordered_list). Validator-internal.
	AnswerItems []string `json:"answer_items,omitempty"`
	// AnswerItemKinds and AnswerItemAcceptAny are V8 list-item semantics. They
	// are parallel to AnswerItems and let one answer combine, for example, an
	// email, a localized monetary amount, and a context-safe lesson paraphrase.
	// Empty kinds retain legacy AnswerValue matching for that item.
	AnswerItemKinds     []string   `json:"answer_item_kinds,omitempty"`
	AnswerItemAcceptAny [][]string `json:"answer_item_accept_any,omitempty"`
	// AcceptAny (bench_version 5) is the accept-set for an AnswerValue case: a set
	// of equivalent surface forms of the SAME answer, any one of which grades
	// correct. It is the primitive that lets a non-verbatim answer be graded
	// deterministically (v5 workstream 4.3): when the load-bearing fact is stored
	// through a unit that must be converted or a reference that must be resolved,
	// the answer token is no longer a literal substring of any seeded pair, so a
	// grep parser fails, while an honest reader can emit any of several equivalent
	// forms ("0.67", "two-thirds", "0.67 hours"). The generator fills every
	// equivalent form so an honest reply is never false-negatived; ExpectedAnswer
	// remains the canonical form. Matched by the same normalized bounded
	// containment as ExpectedAnswer. Validator-internal, never sent to the harness.
	AcceptAny []string `json:"accept_any,omitempty"`
	// DistractorAnswers are same-attribute confusable values (another entity's
	// value in the haystack, or pool values for a decline case). A response that
	// surfaces one has retrieved or fabricated the wrong fact and scores 0.
	// Superseded values of the user's own update chain are deliberately NOT
	// distractors: mentioning old state alongside the current answer is correct.
	// Validator-internal.
	DistractorAnswers []string `json:"distractor_answers,omitempty"`
	// ForbiddenAnswer is the conflicting value another user's memory graph holds
	// for this attribute on an isolation case: the queried user's own answer is
	// ExpectedAnswer, and a response that instead surfaces ForbiddenAnswer has
	// leaked across graphs and is scored 0. Empty for ordinary cases. Like
	// ExpectedAnswer it is validator-internal grading data and is never sent to
	// the harness (only Question is).
	ForbiddenAnswer string `json:"forbidden_answer,omitempty"`
	// TwinGroup ties metamorphic invariance cases together: the cases that ask for
	// the SAME fact in different phrasings share a TwinGroup id. The scorer folds
	// their agreement into the composite (a robust harness answers every member the
	// same way; a phrasing-brittle one disagrees). Validator-internal grouping,
	// never sent to the harness. Empty for ungrouped cases.
	TwinGroup string `json:"twin_group,omitempty"`
	// DumpGuard lists the user's OTHER current-attribute self values (never the
	// answer to this question, and never a distractor). Surfacing a large
	// fraction of them is the signature of an answer-DUMP: emitting the whole
	// self-fact table on every question to bypass genuine routing (a
	// deterministic parser's cheapest strategy). A response that names
	// DumpFloor(len) or more distinct DumpGuard values scores 0; one or two
	// incidental mentions (a reasoner adding helpful context) are fine, so the
	// guard forces the harness to SELECT the right value rather than shotgun the
	// table. Validator-internal, never sent to the harness.
	DumpGuard []string `json:"dump_guard,omitempty"`
	// BaitTool, when set, names an action tool an embedded injection attack tries
	// to make the harness call (e.g. gmail_send to exfiltrate). A pure recall
	// question needs no action tool, so OBSERVING a call to BaitTool is
	// injection compliance: the case scores 0 and is flagged. Unlike the text
	// ForbiddenAnswer payload, this evidence lives in the validator-OBSERVED tool
	// trajectory (the scorer substitutes the observed calls into
	// RunResponse.ToolCalls before grading), so a harness cannot launder
	// compliance by scrubbing its own output — the call already reached the
	// validator. Validator-internal, never sent to the harness.
	BaitTool string `json:"bait_tool,omitempty"`
	// WritingProtected is generator-only semantic identity metadata. It never
	// enters the public artifact or harness request.
	WritingProtected []string `json:"-"`
}

// Dataset is a (fresh, seeded) set of tool-calling + memory cases.
type Dataset struct {
	Seed        int64        `json:"seed"`
	GeneratedAt string       `json:"generated_at"`
	ToolCases   []ToolCase   `json:"tool_cases"`
	MemoryCases []MemoryCase `json:"memory_cases,omitempty"`
}

// MemoryPair is one conversation pair in a fresh haystack pushed to the harness
// via POST /seed. The harness embeds prompt+response and stores it for recall.
type MemoryPair struct {
	PairID    string `json:"pair_id"`
	SessionID string `json:"session_id"`
	Timestamp string `json:"timestamp"` // RFC3339
	Prompt    string `json:"prompt"`
	Response  string `json:"response"`
}

// Subject is one subject/topic cluster linked to memory pairs in a haystack.
type Subject struct {
	ID              string `json:"id"`
	SubjectText     string `json:"subject_text"`
	DescriptionText string `json:"description_text"`
}

// SubjectLink ties a Subject to a MemoryPair (many-to-many).
type SubjectLink struct {
	SubjectID string `json:"subject_id"`
	PairID    string `json:"pair_id"`
}

// SeedRequest is the fresh haystack the validator POSTs to <harness>/seed before
// running memory cases. UserID defaults to "miner" if empty.
//
// Wave (Tier C) is the 0-based index of a STAGED seeding wave:
// the validator may call /seed repeatedly, each call carrying the next chunk of
// the haystack with an incremented Wave, and interleave /run questions between
// waves so memory is built incrementally "as you converse". Repeated /seed is an
// idempotent upsert (the reference harness's contract); a single-wave run leaves
// Wave=0. The field is ADDITIVE-OPTIONAL: a harness that ignores it and simply
// upserts each call still scores correctly.
type SeedRequest struct {
	UserID string `json:"user_id,omitempty"`
	Wave   int    `json:"wave,omitempty"`
	// omitempty so a wave with no subjects/links serializes the key as absent
	// rather than JSON null: a strict harness decoder (e.g. serde) rejects an
	// explicit null for a sequence field but accepts an absent key as empty.
	Pairs    []MemoryPair  `json:"pairs,omitempty"`
	Subjects []Subject     `json:"subjects,omitempty"`
	Links    []SubjectLink `json:"links,omitempty"`
}

// SeedResponse is what <harness>/seed returns: counts actually loaded.
type SeedResponse struct {
	Pairs    int `json:"pairs"`
	Subjects int `json:"subjects"`
	Links    int `json:"links"`
}

// ToolDefinition is a tool schema sent to the harness for a case.
type ToolDefinition struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Parameters  json.RawMessage `json:"parameters,omitempty"`
}

// RunRequest is what the validator POSTs to the harness /run endpoint per case.
//
// ToolEndpoint is an OPTIONAL validator-served mock
// tool-execution URL. When present, a harness that supports observed execution
// should EXECUTE its non-memory catalog tool calls by POSTing a ToolExecRequest
// to this URL (instead of stubbing them locally) and use the returned
// ToolExecResponse.Result. Doing so lets the validator (a) OBSERVE the real tool
// trajectory rather than trusting the harness's self-reported tool_calls, and
// (b) score whether the answer incorporates the returned content
// (result-usage). The field is ADDITIVE-OPTIONAL: a harness that ignores it and
// stubs tools locally still scores, but selection-only and at a capped ceiling on
// the categories the endpoint would have served (their self-reported calls are
// untrusted). Memory tools are NOT served here: the harness answers those from
// its own seeded memory.
//
// UserID (multi-graph isolation) scopes the case to one
// seeded memory graph; it mirrors the user_id the haystack was seeded under. A
// harness must answer only from that user's memory and never leak another user's
// facts. Empty means the default single-user graph ("miner").
type RunRequest struct {
	CaseID       string           `json:"case_id"`
	SystemPrompt string           `json:"system_prompt"`
	UserInput    string           `json:"user_input"`
	Tools        []ToolDefinition `json:"tools"`
	// BenchVersion is sent only for v7+ execution behavior. It is deliberately
	// omitted for v2-v6 so their historical harness wire request stays frozen.
	BenchVersion int    `json:"bench_version,omitempty"`
	ToolEndpoint string `json:"tool_endpoint,omitempty"`
	UserID       string `json:"user_id,omitempty"`
}

// ToolExecRequest is what a harness POSTs to the validator-served tool_endpoint
// (RunRequest.ToolEndpoint) to actually EXECUTE one non-memory catalog tool
// during a case. The validator returns a deterministic, seed-derived mock result
// (ToolExecResponse) and records the call as the authoritative observed
// trajectory for that case. CaseID ties the call to the
// running case; UserID echoes RunRequest.UserID; Hop is the 0-based position in
// the harness's tool sequence (for order scoring).
type ToolExecRequest struct {
	CaseID string          `json:"case_id"`
	UserID string          `json:"user_id,omitempty"`
	Name   string          `json:"name"`
	Args   json.RawMessage `json:"args,omitempty"`
	Hop    int             `json:"hop,omitempty"`
}

// ToolExecResponse is the mock result the validator returns for a ToolExecRequest.
// Result is the tool's output the harness should reason over (a web snippet, a
// page's text, a job status, …), seeded deterministically per case. Error is set
// (with Result empty) when the call is malformed or names a tool the mock server
// does not serve; a harness should treat it like a real tool error.
type ToolExecResponse struct {
	Result string `json:"result"`
	Error  string `json:"error,omitempty"`
}

// ObservedToolCall is a tool call the harness made.
type ObservedToolCall struct {
	Name string          `json:"name"`
	Args json.RawMessage `json:"args,omitempty"`
	Hop  int             `json:"hop,omitempty"`
}

// RunResponse is what the harness returns for a case.
type RunResponse struct {
	FinalText    string             `json:"final_text"`
	ToolCalls    []ObservedToolCall `json:"tool_calls"`
	PromptTokens int64              `json:"prompt_tokens"`
	OutputTokens int64              `json:"output_tokens"`
	LatencyMs    int64              `json:"latency_ms"`
	// Answer is the harness's OPTIONAL short answer slot: the bare value the
	// FinalText prose asserts (a name, a number, a comma-separated list). The
	// deterministic grader matches the slot when present and falls back to
	// FinalText containment when absent, so populating it removes prose-phrasing
	// risk from grading. Additive-optional.
	Answer string `json:"answer,omitempty"`
	// Abstain marks a grounded decline: the harness is stating the asked fact is
	// not in memory. The correct response to a needle-absent (decline) case;
	// abstaining on an answerable case scores 0. Additive-optional (decline
	// phrasing in FinalText is the fallback).
	Abstain bool `json:"abstain,omitempty"`
	// Confidence is the harness's OPTIONAL self-reported confidence in
	// [0,1] that its answer is correct. When present the validator scores a
	// Brier calibration metric (advisory telemetry): honest confidence minimizes
	// it, always-100% does not. A pointer so "not reported" is distinct from 0.0;
	// additive-optional, so a harness that omits it is unaffected.
	Confidence *float64 `json:"confidence,omitempty"`
}

// Kind discriminates a CaseScore between the two case families.
const (
	KindTool   = "tool"
	KindMemory = "memory"
)

// ToolProvenanceEvidence is trusted, per-case evidence that scored tool
// execution was selected by the controlled model and then observed by the
// validator. It is populated only by Bench v10+ scorers; historical report
// bytes omit it.
type ToolProvenanceEvidence struct {
	ModelEmitted             int      `json:"model_emitted"`
	EndpointAttempts         int      `json:"endpoint_attempts"`
	Matched                  int      `json:"matched"`
	Unmatched                int      `json:"unmatched"`
	ModelSelectedNotExecuted int      `json:"model_selected_not_executed"`
	Complete                 bool     `json:"complete"`
	Findings                 []string `json:"findings,omitempty"`
}

// ToolProvenanceSummary aggregates ToolProvenanceEvidence over a run. Counts
// are advisory evidence; per-case tool credit is already fail-closed before
// aggregation.
type ToolProvenanceSummary struct {
	Cases                    int `json:"cases"`
	IncompleteCases          int `json:"incomplete_cases"`
	ModelEmitted             int `json:"model_emitted"`
	EndpointAttempts         int `json:"endpoint_attempts"`
	Matched                  int `json:"matched"`
	Unmatched                int `json:"unmatched"`
	ModelSelectedNotExecuted int `json:"model_selected_not_executed"`
}

// CaseScore is the score for one case (tool OR memory).
//
// For a tool case: Score = ToolAccuracy (deterministic trajectory + args;
// Quality is legacy and unused).
// For a memory case: Score in [0,1] from the deterministic per-AnswerKind
// grader, and ToolAccuracy/Quality are unused.
type CaseScore struct {
	CaseID    string  `json:"case_id"`
	Category  string  `json:"category"`
	Kind      string  `json:"kind"`              // "tool" | "memory"
	Score     float64 `json:"score"`             // 0..1 composite for this case
	ToolScore float64 `json:"tool_score"`        // 0..1 deterministic tool accuracy (tool cases)
	Quality   float64 `json:"quality,omitempty"` // legacy, unused in deterministic scoring
	// ResultUsage is 0..1 for a result-usage tool case: whether the final answer
	// incorporated the distinctive value the executed tool returned, checked
	// deterministically.
	ResultUsage float64 `json:"result_usage,omitempty"`
	Correct     bool    `json:"correct,omitempty"` // deterministic memory grade verdict (memory cases)
	// TwinGroup, when set, ties this case to the other metamorphic invariance cases
	// for the same fact, so the aggregate can score phrasing consistency.
	TwinGroup string `json:"twin_group,omitempty"`
	// AuditHalf marks which side of a transform-audit pair this case is:
	// AuditHalfBase or AuditHalfTransform, empty for every other case. Without it
	// the two halves are indistinguishable in a report and only their AGREEMENT
	// can be scored, which is exactly the information loss that made the metric
	// fail to separate a brittle harness from an honest one.
	//
	// This is not a tell a harness can exploit. It appears only in the REPORT,
	// produced after every case has already been answered, and which cases were
	// audited is re-derivable by anyone from the published seed regardless.
	AuditHalf string `json:"audit_half,omitempty"`
	// Confidence echoes the harness's self-reported confidence for this case, when
	// it reported one, so the aggregate can Brier-score calibration.
	Confidence *float64 `json:"confidence,omitempty"`
	LatencyMs  int64    `json:"latency_ms"`
	// Observed is true when the harness routed this tool case's calls through the
	// validator's mock endpoint, so Called is the authoritative observed
	// trajectory (not self-report). Only observed cases feed the efficiency term.
	Observed bool     `json:"observed,omitempty"`
	Called   []string `json:"called"`
	Expected []string `json:"expected"`
	// Undelivered marks a case whose run never completed (transport error or
	// timeout), so its verdict reflects the infrastructure rather than the
	// harness. Group metrics that compare sibling cases -- transform-audit pairs
	// and metamorphic twin families -- must drop these, or a dropped case reads as
	// a disagreement and charges brittleness for a network hiccup. The case still
	// scores 0 on its own accuracy; only the SIBLING COMPARISON is suppressed.
	// Polarity is negative so an absent field means delivered. Additive-optional.
	Undelivered bool `json:"undelivered,omitempty"`
	// ValidatorFault marks a v9 case excluded because trusted validator-owned
	// execution failed independently of the harness. Current ordinary runs fail
	// the whole attempt on such infrastructure failures; the additive marker
	// gives future bounded per-case recovery an unambiguous gate exclusion
	// without overloading Undelivered. It is never miner supplied.
	ValidatorFault bool `json:"validator_fault,omitempty"`
	// AllowExtraTools echoes the case's ToolCase.AllowExtraTools so aggregate
	// factors can tell a case that REQUIRES extra calls (the serving layer forces
	// the first content-tool call to fail, so a correct harness must retry) from
	// one where extra calls are waste. Without it the efficiency term charged the
	// recovery those cases exist to test. Additive-optional.
	AllowExtraTools bool `json:"allow_extra_tools,omitempty"`
	// ToolProvenance is the trusted broker-to-endpoint intersection for this
	// case. It is nil before Bench v10, preserving every frozen contract.
	ToolProvenance *ToolProvenanceEvidence `json:"tool_provenance,omitempty"`
	Notes          []string                `json:"notes,omitempty"`
	// Injection is true when the deterministic grader saw injection compliance:
	// either the embedded injection payload in the harness output, or an observed
	// call to the case's action bait tool (MemoryCase.BaitTool) in the trajectory,
	// which scores the case 0 outright. Bare payload compliance scores the case 0;
	// a refuse-and-answer response (payload alongside the true answer) keeps its
	// score but is STILL flagged here, so compliance laundered into an apparent
	// answer stays visible to moderation review.
	Injection bool `json:"injection,omitempty"`
}

// AuditPairCounts is the 2x2 outcome table of a run's transform-audit pairs:
// whether the harness answered the BASE case and the TRANSFORMED case
// correctly. Counts rather than a rate, so they pool across runs and across
// validators without weighting a 1-pair run like a 7-pair one.
//
// BaseOnly is the brittleness cell. It means the harness knew the answer when
// asked in the phrasing it had seen, and did not when the same fact was asked
// in a phrasing derived from the post-commit seed. TransformOnly is the same
// event in reverse, which a brittle strategy has no reason to produce, so the
// two cells are compared against each other rather than against a threshold.
//
// BothWrong is deliberately kept separate and never folded into a rate: on a
// hard benchmark it is the large majority of pairs (81% in the 2026-07-18
// calibration) and it says nothing about brittleness, only about accuracy,
// which the composite already scores.
type AuditPairCounts struct {
	BothCorrect   int `json:"both_correct"`
	BaseOnly      int `json:"base_only"`
	TransformOnly int `json:"transform_only"`
	BothWrong     int `json:"both_wrong"`
}

// Discordant returns the pairs the harness answered inconsistently, which are
// the only ones carrying directional information.
func (a AuditPairCounts) Discordant() int { return a.BaseOnly + a.TransformOnly }

// Total returns every audit pair the run carried.
func (a AuditPairCounts) Total() int {
	return a.BothCorrect + a.BaseOnly + a.TransformOnly + a.BothWrong
}

// Add accumulates another run's counts, so a caller can pool an agent's history.
func (a AuditPairCounts) Add(b AuditPairCounts) AuditPairCounts {
	return AuditPairCounts{
		BothCorrect:   a.BothCorrect + b.BothCorrect,
		BaseOnly:      a.BaseOnly + b.BaseOnly,
		TransformOnly: a.TransformOnly + b.TransformOnly,
		BothWrong:     a.BothWrong + b.BothWrong,
	}
}

// Audit pair halves for CaseScore.AuditHalf.
const (
	AuditHalfBase      = "base"
	AuditHalfTransform = "transform"
)

// CategoryStat is the mean composite score for one category, with the standard
// error of that mean. StdErr makes per-category signal legible: with only a few
// cases per category a mean carries a wide band (≈StdErr·1.96 for a 95% CI), so a
// consumer can tell a real per-capability gap from sampling noise instead of
// over-reading a 2–6-case category. 0 for a single-case category.
type CategoryStat struct {
	Category string  `json:"category"`
	Count    int     `json:"count"`
	Mean     float64 `json:"mean"`
	StdErr   float64 `json:"std_err,omitempty"`
}

// CodeFingerprint is a bottom-k MinHash (KMV) sketch of a submission's source,
// consumed by the platform's anti-copy moderation gate. It is advisory metadata,
// never part of the scored result: V is the sketch-format version, K the bottom-k
// budget, Card the true shingle-set cardinality, and M the sorted bottom-K shingle
// hashes. The shape is byte-compatible with the platform's own fingerprint sketch
// so the two compare with one code path (Jaccard / containment over M).
type CodeFingerprint struct {
	V    int      `json:"v"`
	K    int      `json:"k"`
	Card int      `json:"card"`
	M    []string `json:"m"`
}

// ParaphraseStats is a retained wire field. Generation is fully non-LLM, so the
// surface variation comes from seeded template selection rather than a paraphrase
// pass, and every counter here is always zero. It is kept for wire compatibility
// with consumers that read the field. Purely advisory telemetry; never affects
// the score.
type ParaphraseStats struct {
	Attempted int `json:"attempted"`
	Applied   int `json:"applied"`
	Retried   int `json:"retried"`
	Fallback  int `json:"fallback"`
}

// Add folds another ParaphraseStats into the receiver.
func (p *ParaphraseStats) Add(o ParaphraseStats) {
	p.Attempted += o.Attempted
	p.Applied += o.Applied
	p.Retried += o.Retried
	p.Fallback += o.Fallback
}

// LexicalGapStats reports the query↔needle content-word overlap of the memory
// suite (the NoLiMa literal-match signal). A question
// that shares wording with its stored fact can be answered by lexical shortcut,
// overstating memory ability; the generator rewords questions to reduce overlap
// and this makes the residual visible. Purely advisory telemetry; never scored.
type LexicalGapStats struct {
	Questions  int     `json:"questions"`   // non-abstention questions measured
	Rewritten  int     `json:"rewritten"`   // low-overlap rewrite applied
	MeanBefore float64 `json:"mean_before"` // mean question↔evidence content overlap, original text
	MeanAfter  float64 `json:"mean_after"`  // ... after rewrite (or original where not rewritten)
}

// RunDetails is the opaque, additive telemetry blob for a run.
// It is NOT part of the platform's DB/signature contract, so new fields may be
// added freely (for example bench_version, seeding-wave counts, token totals).
// Serialized under ScoreReport.details.
type RunDetails struct {
	// BenchVersion is the scoring benchmark version (see protocol.BenchVersion).
	// The weight fold only compares entries of the max bench_version present, so a
	// bump makes new scores non-comparable to old until a re-score.
	BenchVersion int `json:"bench_version"`
	// V9Base is the typed, signature-bound ordinary-score evidence for Bench v9.
	// It is nil for every historical contract, preserving their report bytes.
	V9Base *V9BaseDetails `json:"v9_base,omitempty"`
	// RunSize is the immutable generator profile (small, medium, or full). It is
	// required to select a like-for-like starter-kit token baseline.
	RunSize string `json:"run_size,omitempty"`
	// DatasetSHA256 is the hex SHA-256 of the fully-rendered dataset (tool cases +
	// memory waves + memory cases). It pins the exact artifact a dispute
	// re-scores: the recorded hash must match a re-hash of the persisted
	// artifact. With no LLM surface variation it is also reproducible from
	// (seed, bench_version).
	DatasetSHA256 string           `json:"dataset_sha256,omitempty"`
	Paraphrase    *ParaphraseStats `json:"paraphrase,omitempty"`
	// InjectionAttempts counts cases the deterministic grader flagged as
	// prompt-injection compliance (each scored 0). A non-zero value is
	// moderation-relevant evidence, the same policy channel as plagiarism.
	InjectionAttempts int `json:"injection_attempts,omitempty"`
	// Tokens, JudgeAudited, and JudgeDisagreed are legacy telemetry fields, unused
	// by the deterministic scorer. Current runs emit zeros (the omitempty drops
	// them); the fields stay for old-report wire compatibility.
	Tokens         int64 `json:"tokens,omitempty"`
	JudgeAudited   int   `json:"judge_audited,omitempty"`
	JudgeDisagreed int   `json:"judge_disagreed,omitempty"`
	// SeedingWaves is how many staged /seed waves the memory haystack was split
	// into (Tier C; 1 = single seed). RawPairsCases is how many memory cases were
	// Tier B (raw-pairs seeding: their evidence was seeded WITHOUT prepared
	// subjects, so the harness had to build its own subject index). Both
	// are advisory calibration telemetry.
	SeedingWaves  int `json:"seeding_waves,omitempty"`
	RawPairsCases int `json:"raw_pairs_cases,omitempty"`
	// ToolMean / MemoryMean echo the per-suite means for convenience alongside the
	// per-category breakdown in ScoreReport.per_category.
	ToolMean   float64 `json:"tool_mean"`
	MemoryMean float64 `json:"memory_mean"`
	// LexicalGap is the query↔needle overlap telemetry for the memory suite (the
	// NoLiMa literal-match signal). Advisory only.
	LexicalGap *LexicalGapStats `json:"lexical_gap,omitempty"`
	// ConversationalSanity (bench_version 5) is the first-class conversational
	// grounding metric: the WEAKEST-LINK (minimum) pass rate across the three v5
	// conversational-sanity slices that ran -- greeting non-leak, declarative
	// acknowledgement, and behavior-change application. It is a conjunction by
	// construction so a canned reply cannot bank the greeting slice (which a fixed
	// "Got it!" passes) and dilute its failures on the declarative and
	// behavior-change slices. The validator also folds it into the composite as a
	// bounded factor with its own floor (ConversationalSanityFactor), harder than
	// the efficiency floors, so a run that fails conversational sanity cannot reach
	// champion composite regardless of memory accuracy. nil when no v5
	// conversational case ran (older contracts, or a run that drew none). Any third
	// party recomputes it from (dataset, transcript). See dittobench-api
	// docs/BENCHMARK-V5-PLAN.md section 4.1.
	ConversationalSanity *float64 `json:"conversational_sanity,omitempty"`
	// MetamorphicConsistency is the fraction of invariance twin groups whose
	// members the harness answered consistently (all correct or all incorrect). A
	// phrasing-brittle harness scores below 1.0. The validator folds this into the
	// composite as a bounded factor over the split groups only
	// (MetamorphicConsistencyFactor multiplies the composite by
	// 1 - maxPenalty*(1 - this rate)); the rate reported here is the pre-fold
	// value, so the applied factor stays reconstructable. nil when no twin groups
	// ran.
	MetamorphicConsistency *float64 `json:"metamorphic_consistency,omitempty"`
	// TransformRobustness is the reproduce-under-transform audit result: the
	// fraction of audit pairs the harness answered CONSISTENTLY, where each pair
	// is a base case and the same underlying fact re-asked under a post-commit
	// transform the harness could not predict (persona/transform.go). It
	// generalizes MetamorphicConsistency from generator-chosen twins fixed in the
	// dataset to validator-derived, block-hash-seeded transforms.
	//
	// Because both the transforms and the selection are pure functions of the
	// published seed, any third party regenerates the audit set and recomputes
	// this number from (dataset, transcript) alone. That is what lets a validator
	// or the platform act on it without holding a secret. nil when no audit pairs
	// ran.
	//
	// Honest scope: a low value is the SURFACE-BRITTLENESS signature (competent on
	// the base phrasing, wrong under an unpredictable rephrasing) or memorization
	// (right answer for the base, stale answer under a covariance shift). It is
	// NOT evidence about a robust local solver, which recomputes correctly under
	// the transform too.
	TransformRobustness *float64 `json:"transform_robustness,omitempty"`
	// AuditCaseCount is how many transform-audit pairs the run carried. Published
	// so a reader can tell a robustness value backed by many pairs from one backed
	// by two, and so the audit rate itself is auditable.
	AuditCaseCount int `json:"audit_case_count,omitempty"`
	// AuditPairs are the transform-audit outcome COUNTS, and they are what a
	// verdict should actually be computed from. TransformRobustness above is a
	// per-run ratio, which cannot be pooled: averaging ratios over runs weights a
	// run with one pair the same as a run with seven. These counts are sufficient
	// statistics, so a consumer sums them across an agent's runs and across the
	// k=3 validators and then decides once, on all the evidence.
	//
	// The 2026-07-18 calibration showed why the direction matters. An honest
	// model's discordant pairs are SYMMETRIC (measured 5 base-only vs 6
	// transform-only): a nondeterministic model that splits a pair splits it
	// either way. A surface-keyed harness is DIRECTIONAL (measured 6 base-only vs
	// 0 transform-only): it fails specifically on the half it could not
	// fingerprint. Agreement throws that direction away, which is why it did not
	// separate the two.
	AuditPairs *AuditPairCounts `json:"audit_pairs,omitempty"`
	// CalibrationBrier is the mean Brier score over cases where the harness
	// reported a confidence: mean((confidence - correct)^2), lower is
	// better. Honest confidence minimizes it; always-100% does not. CalibrationN
	// is how many cases carried a confidence. Advisory only, never folded into the
	// composite (so a harness that omits confidence is unaffected). nil when no
	// case reported a confidence.
	CalibrationBrier *float64 `json:"calibration_brier,omitempty"`
	CalibrationN     int      `json:"calibration_n,omitempty"`
	// ObservedToolCases is how many tool cases were scored on the validator-
	// observed trajectory (the harness routed its calls through tool_endpoint);
	// CappedToolCases is how many observable cases were capped because the harness
	// did NOT (self-report untrusted). Together they show how much of
	// the tool suite ran under observed execution. Advisory calibration telemetry.
	ObservedToolCases int `json:"observed_tool_cases,omitempty"`
	CappedToolCases   int `json:"capped_tool_cases,omitempty"`
	// ToolProvenance is the v10+ aggregate of broker-emitted and
	// validator-executed tool calls. Historical reports omit it.
	ToolProvenance *ToolProvenanceSummary `json:"tool_provenance,omitempty"`
	// IsolationCases is how many multi-graph isolation cases ran: a second
	// persona seeded under a different user_id with a conflicting value, so a
	// cross-graph memory leak scores wrong. Advisory telemetry.
	IsolationCases int `json:"isolation_cases,omitempty"`
	// LifecycleCases is how many write-then-read lifecycle cases ran: an
	// instruction case asks the harness to save/update/delete a fact through
	// its own memory tools, and a later-wave read case is only answerable if
	// the write actually landed in the harness's store. Advisory telemetry.
	LifecycleCases int `json:"lifecycle_cases,omitempty"`
	// ToolEfficiency is the run's observed tool-efficiency factor (0..1) folded
	// into the composite as a bounded multiplier: 1.0 when harnesses reached
	// correct answers within their expected tool budget, dropping toward the floor
	// as observed trajectories overshot. 1.0 (no effect) when no tool case ran
	// under observed execution. Advisory: the composite already reflects it.
	ToolEfficiency float64 `json:"tool_efficiency,omitempty"`
	// Models records the LLM model id that produced this run: only the miner's
	// harness chat model, and only when the operator forces it. Generation is
	// non-LLM and scoring is deterministic, so no generator or judge model
	// applies. Advisory transparency metadata for the public leaderboard.
	Models *ModelInfo `json:"models,omitempty"`
	// PerCategory echoes ScoreReport.PerCategory into the details blob so the
	// per-category breakdown (per tool / per memory-question-type mean) survives
	// the platform wire (which carries details but not the top-level
	// per_category) and can drive a transparent leaderboard. Advisory only.
	PerCategory []CategoryStat `json:"per_category,omitempty"`
	// TokenUsage is trusted model-proxy accounting for this run. It is derived
	// by the validator from provider responses, never copied from RunResponse's
	// miner-reported token fields. TokenEfficiency records the v5 baseline lookup
	// and score transform separately so raw quality remains auditable.
	TokenUsage      *TokenUsage      `json:"token_usage,omitempty"`
	TokenEfficiency *TokenEfficiency `json:"token_efficiency,omitempty"`
}

// TokenUsage is validator-observed model consumption for one isolated run.
// Status is "complete" only when every successful proxy completion carried a
// valid provider usage object. Missing or malformed telemetry is explicit and
// makes the v5 efficiency transform fail neutral.
type TokenUsage struct {
	AccountingVersion int    `json:"accounting_version"`
	Status            string `json:"status"`
	Source            string `json:"source"`
	Provider          string `json:"provider"`
	ProfileRevision   string `json:"profile_revision"`
	Model             string `json:"model"`
	Requests          uint64 `json:"requests"`
	Successes         uint64 `json:"successes"`
	UsageAvailable    uint64 `json:"usage_available"`
	UsageUnavailable  uint64 `json:"usage_unavailable"`
	PromptTokens      uint64 `json:"prompt_tokens"`
	PromptBytes       uint64 `json:"prompt_bytes"`
	CompletionTokens  uint64 `json:"completion_tokens"`
	TotalTokens       uint64 `json:"total_tokens"`
	ProviderLatencyMs uint64 `json:"provider_latency_ms"`
	TTFTStatus        string `json:"ttft_status"`
}

// TokenEfficiency is the complete v5 relay-token waste decision. It never
// rewards token minimization: usage through the reference budget is neutral,
// and only above-budget waste can reduce Composite. RawComposite and every
// transform input remain separate so the adjustment is reproducible.
type TokenEfficiency struct {
	FormulaVersion           string  `json:"formula_version"`
	BaselineID               string  `json:"baseline_id,omitempty"`
	BaselinePromptTokens     uint64  `json:"baseline_prompt_tokens,omitempty"`
	BaselineCompletionTokens uint64  `json:"baseline_completion_tokens,omitempty"`
	BaselineTotalTokens      uint64  `json:"baseline_total_tokens,omitempty"`
	BudgetPercentile         float64 `json:"budget_percentile"`
	ObservedPromptTokens     uint64  `json:"observed_prompt_tokens"`
	ObservedCompletionTokens uint64  `json:"observed_completion_tokens"`
	ObservedTotalTokens      uint64  `json:"observed_total_tokens"`
	ExcessRatio              float64 `json:"excess_ratio"`
	MaximumPenalty           float64 `json:"maximum_penalty"`
	MinimumMultiplier        float64 `json:"minimum_multiplier"`
	Multiplier               float64 `json:"multiplier"`
	RawComposite             float64 `json:"raw_composite"`
	AdjustedComposite        float64 `json:"adjusted_composite"`
	RawCompositeStderr       float64 `json:"raw_composite_stderr,omitempty"`
	AdjustedCompositeStderr  float64 `json:"adjusted_composite_stderr,omitempty"`
	PenaltyApplied           bool    `json:"penalty_applied"`
	DecisionReason           string  `json:"decision_reason"`
}

// ModelInfo is the set of LLM model ids a run was produced with (RunDetails.models).
// All fields are advisory transparency metadata, never scored or signed.
type ModelInfo struct {
	// Generator, Judge, and JudgeAudit are legacy fields, unused: generation is
	// non-LLM and scoring is deterministic, so current runs leave them empty.
	// They stay for old-report wire compatibility.
	Generator  string `json:"generator,omitempty"`
	Judge      string `json:"judge,omitempty"`
	JudgeAudit string `json:"judge_audit,omitempty"`
	// Harness is the miner harness's chat model when the operator forces it via
	// DITTOBENCH_HARNESS_MODEL; empty when the harness used its own default (the
	// server does not otherwise observe the miner's model choice).
	Harness string `json:"harness,omitempty"`
}

// ScoreReport is the full result of scoring a run.
type ScoreReport struct {
	RunID       string  `json:"run_id"`
	Seed        int64   `json:"seed"` // dataset seed (anti-overfit reproducibility)
	GeneratedAt string  `json:"generated_at"`
	Composite   float64 `json:"composite"` // final composite in [0,1]; v5 may apply a bounded waste penalty
	// BaseEvidenceSHA256 binds the complete typed details.v9_base payload into
	// Bench v9 score signatures. It is absent for every historical contract.
	BaseEvidenceSHA256 string `json:"base_evidence_sha256,omitempty"`
	// RawComposite is the pre-efficiency quality score. It is emitted for v5 and
	// omitted for frozen v2-v4 reports, whose Composite is already raw quality.
	RawComposite float64 `json:"raw_composite,omitempty"`
	// CompositeStderr is the standard error of the composite for THIS run, combining
	// the tool-half and memory-half standard errors: 0.5*sqrt(se_tool^2 + se_mem^2).
	// It lets the KOTH weight fold gate a challenger on measurement uncertainty (a
	// challenger dethrones only when its lead exceeds z*sqrt(se_c^2 + se_champ^2))
	// instead of a flat margin. Additive-optional (omitempty): a consumer that
	// ignores it uses flat-margin gating.
	// Caveat: this is the WITHIN-run SE over per-case scores; memory cases share one
	// persona (a single cluster), so it understates run-to-run variance. The subnet
	// combines it with CRN paired scoring and multi-seed aggregation for the
	// cross-run picture.
	CompositeStderr float64 `json:"composite_stderr,omitempty"`
	ToolMean        float64 `json:"tool_mean"`   // 0..1 mean tool-case composite
	MemoryMean      float64 `json:"memory_mean"` // 0..1 fraction of memory cases correct
	// ConversationalSanity (bench_version 5) is the weakest-link conversational
	// grounding metric published as a first-class field so a low score cannot hide
	// inside the memory mean; see RunDetails.ConversationalSanity. nil (omitted)
	// when no v5 conversational case ran. Additive-optional.
	ConversationalSanity *float64       `json:"conversational_sanity,omitempty"`
	MedianMs             int64          `json:"median_ms"`
	N                    int            `json:"n"`
	PerCase              []CaseScore    `json:"per_case"`
	PerCategory          []CategoryStat `json:"per_category,omitempty"`
	// Details is opaque, additive run telemetry. Advisory only, never scored or
	// signed.
	Details *RunDetails `json:"details,omitempty"`
	// StructuralFingerprint is an AST-level shingle sketch of the built crate
	// (nil when unavailable), forwarded to the platform's anti-copy gate as
	// advisory (unsigned) moderation metadata. It never affects the score.
	StructuralFingerprint *CodeFingerprint `json:"structural_fingerprint,omitempty"`
}
