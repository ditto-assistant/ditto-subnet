// Command graderaudit builds the grader false-negative labeling sheet (prod
// hardening P3, in the spirit of the SWE-bench Verified / WebArena Verified
// grader audits).
//
// Deterministic grading's weak spot is a correct answer phrased outside the
// normalization tables. This tool finds the cases worth a human look: every
// memory case that survived the disqualifying scans (forbidden value,
// distractor, wrong abstention) but scored 0 at the typed answer check. Those
// are the only place a grader false negative can hide; a zero from a
// disqualifier is the adversarial machinery working as designed and is
// excluded from the sheet.
//
// Usage:
//
//	graderaudit -artifact dataset.json -transcripts transcripts.jsonl [-json]
//	graderaudit -bench-version 9 -seeds 40 [-run-size full] [-json]
//
// The artifact is the canonical DatasetArtifact JSON (generate -out). The
// transcript file is JSONL, one line per case:
//
//	{"case_id": "c1a2b3c4d5e6f7a8", "response": {<protocol.RunResponse>}}
//
// Output: a labeling sheet on stdout (TSV by default, JSONL with -json) with
// one row per candidate false negative: case id, question type, answer kind,
// question, expected answer, list items, and the harness response. A summary
// of per-answer-kind counts (graded, credited, disqualified, typed zeroes)
// goes to stderr, so repeated audits can publish the measured rate per kind.
//
// The sheet is for HUMAN labeling: a row is a real false negative only when a
// human reads the response as correct. Measured rates feed the public docs
// per bench version; fixes land as normalization-table changes in the grade
// package, which alter grading (not dataset bytes) and are re-runnable over
// recorded transcripts.
//
// Generated mode runs two public deterministic gates: fixed canned responses
// over the requested generated corpus, plus a versioned synthetic per-kind bank
// of generic paraphrases, prompt-only templates, and negative near misses. The
// banks measure grader exposure; they are auditable public fixtures, not secret
// anti-overfit probes.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// transcriptLine is one JSONL record tying a case to the harness response it
// received.
type transcriptLine struct {
	CaseID   string               `json:"case_id"`
	Response protocol.RunResponse `json:"response"`
}

// outcome classifies one graded case for the audit.
type outcome string

const (
	outCredited     outcome = "credited"     // score > 0
	outDisqualified outcome = "disqualified" // forbidden/distractor/wrong-abstain zero
	outTypedZero    outcome = "typed_zero"   // survived scans, failed the typed check
	outEmpty        outcome = "empty"        // empty response
	outMissing      outcome = "missing"      // no transcript for the case
)

// candidate is one labeling-sheet row.
type candidate struct {
	CaseID       string   `json:"case_id"`
	QuestionType string   `json:"question_type"`
	AnswerKind   string   `json:"answer_kind"`
	Question     string   `json:"question"`
	Expected     string   `json:"expected_answer"`
	Items        []string `json:"answer_items,omitempty"`
	Answer       string   `json:"answer,omitempty"`
	FinalText    string   `json:"final_text"`
	Notes        []string `json:"notes,omitempty"`
}

// kindStat is the per-answer-kind tally for the stderr summary.
type kindStat struct {
	Graded       int
	Credited     int
	Disqualified int
	TypedZero    int
}

const (
	cannedPassScore       = 0.5
	cannedTargetShare     = 0.05
	robustnessBankVersion = "v9-2"
	robustnessBankV12     = "v12-1"
)

// cannedProbe is deliberately independent of the generated case. The suite is
// public and fixed: it measures how much credit a harness can bank by routing a
// coarse question shape to a canned wire response without retrieving anything.
type cannedProbe struct {
	Name     string
	Response protocol.RunResponse
}

var cannedProbes = []cannedProbe{
	{Name: "generic-okay", Response: protocol.RunResponse{FinalText: "Okay."}},
	{Name: "generic-greeting", Response: protocol.RunResponse{FinalText: "Hello!"}},
	{Name: "generic-acknowledgement", Response: protocol.RunResponse{FinalText: "Got it."}},
	{Name: "completed-action", Response: protocol.RunResponse{FinalText: "Done. All set."}},
	{Name: "decline-text", Response: protocol.RunResponse{FinalText: "I don't have that on record."}},
	{Name: "decline-slot", Response: protocol.RunResponse{FinalText: "I don't know.", Abstain: true}},
	{Name: "generic-persistence", Response: protocol.RunResponse{FinalText: "I would still like it."}},
	{Name: "generic-reversal", Response: protocol.RunResponse{FinalText: "I no longer do that."}},
}

// The bank fixture types are the public grade.Audit* types so every versioned
// bank (v9-2 and v12-1 here, v13-1 in grade/audit_v13_bank.go) shares one
// evaluator.
type (
	robustnessProbe    = grade.AuditProbe
	robustnessCase     = grade.AuditCase
	robustnessNegative = grade.AuditNegative
	robustnessLimit    = grade.AuditLimit
)

// robustnessProbesV1 deliberately extends beyond cannedProbes. It includes
// held-out paraphrases and prompt-only templates so the launch check cannot be
// made green merely by special-casing the short CLI probe list. This is still a
// public deterministic grader-exposure test, not a secret anti-overfit defense.
var robustnessProbesV1 = []robustnessProbe{
	{Name: "neutral-okay", Fixed: protocol.RunResponse{FinalText: "Okay."}},
	{Name: "neutral-understood", Fixed: protocol.RunResponse{FinalText: "Understood."}},
	{Name: "neutral-thanks", Fixed: protocol.RunResponse{FinalText: "Thanks for letting me know."}},
	{Name: "greeting-hello", Fixed: protocol.RunResponse{FinalText: "Hello!"}},
	{Name: "greeting-check-in", Fixed: protocol.RunResponse{FinalText: "Good to hear from you — how are things?"}},
	{Name: "ack-complete", Fixed: protocol.RunResponse{FinalText: "Done. All set."}},
	{Name: "ack-handled", Fixed: protocol.RunResponse{FinalText: "That has been handled."}},
	{Name: "ack-finished", Fixed: protocol.RunResponse{FinalText: "Finished as requested."}},
	{Name: "decline-no-record", Fixed: protocol.RunResponse{FinalText: "I don't have that on record."}},
	{Name: "decline-cannot-find", Fixed: protocol.RunResponse{FinalText: "I couldn't find anything about that."}},
	{Name: "decline-unsure", Fixed: protocol.RunResponse{FinalText: "I'm not sure."}},
	{Name: "decline-wire", Fixed: protocol.RunResponse{FinalText: "I don't know.", Abstain: true}},
	{Name: "generic-persistence", Fixed: protocol.RunResponse{FinalText: "That remains one of your preferences."}},
	{Name: "generic-continuation", Fixed: protocol.RunResponse{FinalText: "You continue to prefer it."}},
	{Name: "generic-reversal", Fixed: protocol.RunResponse{FinalText: "You gave that up."}},
	{Name: "generic-cessation", Fixed: protocol.RunResponse{FinalText: "That is no longer current."}},
	{Name: "generic-explanation", Fixed: protocol.RunResponse{FinalText: "Based on the information available, that is the best answer."}},
	{Name: "prompt-echo", PromptPrefix: "You asked: ", PromptSuffix: ""},
	{Name: "prompt-ack", PromptPrefix: "Done as requested: ", PromptSuffix: ""},
	{Name: "prompt-decline", PromptPrefix: "I don't have an answer for: ", PromptSuffix: ""},
	{Name: "prompt-persistence", PromptPrefix: "You remain a big fan of this: ", PromptSuffix: ""},
	{Name: "prompt-reversal", PromptPrefix: "You gave up this: ", PromptSuffix: ""},
}

// robustnessCasesV1 is synthetic by design: generated V9 currently does not
// emit every affected answer kind. These cases keep every kind under a
// stable gate instead of letting absent kinds disappear into the aggregate.
var robustnessCasesV1 = []robustnessCase{
	{Name: "chat", Question: "How is your afternoon going?", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "GAVOTU-8841"}, Positive: protocol.RunResponse{FinalText: "Good afternoon!"}},
	{Name: "ack-delete", Question: "Please remove the old hiking preference.", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerAcknowledge}, Positive: protocol.RunResponse{FinalText: "Done. All set."}},
	{Name: "decline-pet", Question: "What name did I give my childhood cat?", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerDecline}, Positive: protocol.RunResponse{FinalText: "I don't know.", Abstain: true}},
	{Name: "persist-tennis", Question: "Do I still play tennis?", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerPersistence, AnswerItems: []string{"tennis"}}, Positive: protocol.RunResponse{FinalText: "You remain a big fan of tennis."}},
	{Name: "reverse-climbing", Question: "Did I stop climbing?", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerReversal, AnswerItems: []string{"climbing"}}, Positive: protocol.RunResponse{FinalText: "You gave up climbing."}},
	{Name: "value-city", Question: "Where did I say I live?", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "Lisbon"}, Positive: protocol.RunResponse{FinalText: "Lisbon"}},
	{Name: "list-offices", Question: "Which two offices did I shortlist?", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerList, AnswerItems: []string{"Osaka", "Lima"}}, Positive: protocol.RunResponse{FinalText: "Osaka and Lima"}},
	{Name: "number-attendees", Question: "How many attendees did I count?", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerNumber, ExpectedAnswer: "17"}, Positive: protocol.RunResponse{FinalText: "17"}},
	{Name: "money-budget", Question: "What was the approved budget?", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerMoney, ExpectedAnswer: "24500"}, Positive: protocol.RunResponse{FinalText: "$245.00"}},
}

var robustnessNegativesV1 = []robustnessNegative{
	{Name: "chitchat-forbidden-leak", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "GAVOTU-8841"}, Response: protocol.RunResponse{FinalText: "GAVOTU-8841"}},
	{Name: "acknowledge-is-not-decline", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerAcknowledge}, Response: protocol.RunResponse{FinalText: "I don't know.", Abstain: true}},
	{Name: "decline-is-not-completion", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerDecline}, Response: protocol.RunResponse{FinalText: "Done. All set."}},
	{Name: "persistence-rejects-bare-still-like", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerPersistence, AnswerItems: []string{"tennis"}}, Response: protocol.RunResponse{FinalText: "I would still like to check tennis later."}},
	{Name: "persistence-rejects-cessation-hedge", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerPersistence, AnswerItems: []string{"tennis"}}, Response: protocol.RunResponse{FinalText: "You remain a tennis fan, but you stopped playing."}},
	{Name: "reversal-rejects-future-interest", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerReversal, AnswerItems: []string{"climbing"}}, Response: protocol.RunResponse{FinalText: "You might resume climbing next month."}},
	{Name: "value-wrong-slot-not-laundered", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "Lisbon"}, Response: protocol.RunResponse{Answer: "Porto", FinalText: "The answer is Lisbon."}},
	{Name: "list-unrelated", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerList, AnswerItems: []string{"Osaka", "Lima"}}, Response: protocol.RunResponse{FinalText: "Cairo and Rome."}},
	{Name: "number-boundary", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerNumber, ExpectedAnswer: "17"}, Response: protocol.RunResponse{FinalText: "117"}},
	{Name: "money-wrong-amount", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerMoney, ExpectedAnswer: "24500"}, Response: protocol.RunResponse{FinalText: "$2.45"}},
}

var robustnessLimitsV1 = map[string]robustnessLimit{
	protocol.AnswerChitchat: {MaxPassShare: 1, MaxMeanCredit: 0.5},
	// These interaction-only kinds intentionally accept a generic response;
	// their exposure is explicit rather than hidden in an average.
	protocol.AnswerAcknowledge: {MaxPassShare: 1, MaxMeanCredit: 1},
	protocol.AnswerDecline:     {MaxPassShare: 1, MaxMeanCredit: 1},
	protocol.AnswerPersistence: {MaxPassShare: 0.10, MaxMeanCredit: 0.10},
	protocol.AnswerReversal:    {MaxPassShare: 0.15, MaxMeanCredit: 0.15},
	protocol.AnswerValue:       {MaxPassShare: 0, MaxMeanCredit: 0},
	protocol.AnswerList:        {MaxPassShare: 0, MaxMeanCredit: 0},
	protocol.AnswerNumber:      {MaxPassShare: 0, MaxMeanCredit: 0},
	protocol.AnswerMoney:       {MaxPassShare: 0, MaxMeanCredit: 0},
}

// robustnessBankV9 wraps the frozen v9-2 fixtures. It is built on demand so a
// test that swaps one fixture variable audits the swapped bank.
func robustnessBankV9() grade.AuditBank {
	return grade.AuditBank{
		Version:              robustnessBankVersion,
		FloorBenchVersion:    protocol.BenchVersionV9,
		Probes:               robustnessProbesV1,
		Cases:                robustnessCasesV1,
		Negatives:            robustnessNegativesV1,
		Limits:               robustnessLimitsV1,
		InteractionOnlyKinds: []string{protocol.AnswerChitchat, protocol.AnswerAcknowledge, protocol.AnswerDecline},
	}
}

// robustnessBankV12Bank is the v12 policy bank: the v9-2 strategy set plus
// the slot-scoped distractor vectors the v12 policy introduced, so the v12
// grading floor owns a bank of its own (the release gate requires one per
// policy floor). Its exposure limits are the v9 limits: v12 changed the
// negative scan scope, not what a generic strategy can pass.
func robustnessBankV12Bank() grade.AuditBank {
	bank := robustnessBankV9()
	bank.Version = robustnessBankV12
	bank.FloorBenchVersion = protocol.BenchVersionV12
	reasoned := protocol.MemoryCase{ExpectedAnswer: "Lisbon", DistractorAnswers: []string{"Oslo"}}
	bank.Negatives = append(append([]robustnessNegative(nil), robustnessNegativesV1...),
		robustnessNegative{Name: "v12-distractor-in-slot", Case: reasoned, Response: protocol.RunResponse{Answer: "Oslo", FinalText: "You live in Oslo."}},
		robustnessNegative{Name: "v12-slot-enumerates-distractor", Case: reasoned, Response: protocol.RunResponse{Answer: "Lisbon or Oslo", FinalText: "not sure which"}},
		robustnessNegative{Name: "v12-prose-shotgun-without-slot", Case: reasoned, Response: protocol.RunResponse{FinalText: "It's either Lisbon or Oslo."}},
	)
	bank.Positives = []grade.AuditPositive{
		{Name: "v12-slot-protects-shown-reasoning", Case: reasoned, Response: protocol.RunResponse{Answer: "Lisbon", FinalText: "You used to live in Oslo, but you moved to Lisbon last year."}},
	}
	return bank
}

// auditBanks lists every versioned bank, ascending by policy floor.
func auditBanks() []grade.AuditBank {
	return []grade.AuditBank{robustnessBankV9(), robustnessBankV12Bank(), grade.AuditBankV13()}
}

// bankForVersion resolves the bank auditing the grading policy that governs
// benchVersion. It fails closed when that policy floor owns no bank, so a new
// grading policy cannot ship without its robustness fixtures.
func bankForVersion(benchVersion int) (grade.AuditBank, error) {
	if !knownBenchVersion(benchVersion) {
		return grade.AuditBank{}, fmt.Errorf("unsupported bench_version %d", benchVersion)
	}
	if benchVersion < grade.AuditBankFloor {
		return grade.AuditBank{}, fmt.Errorf("no grader audit bank covers bench_version %d (banks start at bench_version %d)", benchVersion, grade.AuditBankFloor)
	}
	floor := grade.GradingPolicyFloor(benchVersion)
	for _, bank := range auditBanks() {
		if bank.FloorBenchVersion == floor {
			return bank, nil
		}
	}
	return grade.AuditBank{}, fmt.Errorf("grading policy floor bench_version %d (governing bench_version %d) has no audit bank", floor, benchVersion)
}

// knownBenchVersion reports whether v is a generatable contract or a grader
// policy floor that exists ahead of its generation contract. Anything else is
// not a bench version and is refused rather than silently mapped to a floor.
func knownBenchVersion(v int) bool {
	if protocol.SupportedBenchVersion(v) {
		return true
	}
	for _, floor := range grade.GradingPolicyFloors() {
		if floor == v {
			return true
		}
	}
	return false
}

// releaseGateVersions are the versions the release gate must cover: every
// generator-supported version from the audit floor up, plus every grading
// policy floor from the audit floor up (a grader-only version such as v13
// before its generation contract lands).
func releaseGateVersions() []int {
	floors := grade.GradingPolicyFloors()
	seen := map[int]bool{}
	var out []int
	add := func(v int) {
		if v >= grade.AuditBankFloor && !seen[v] {
			seen[v] = true
			out = append(out, v)
		}
	}
	for v := grade.AuditBankFloor; v <= floors[len(floors)-1]+64; v++ {
		if protocol.SupportedBenchVersion(v) {
			add(v)
		}
	}
	for _, v := range floors {
		add(v)
	}
	sort.Ints(out)
	return out
}

type releaseGateRow struct {
	BenchVersion int    `json:"bench_version"`
	PolicyFloor  int    `json:"policy_floor"`
	BankVersion  string `json:"bank_version,omitempty"`
	Generatable  bool   `json:"generatable"`
	Error        string `json:"error,omitempty"`
}

// releaseGate fails closed unless every release-gate version resolves to a
// bank whose floor is its own grading policy floor.
func releaseGate() ([]releaseGateRow, error) {
	var rows []releaseGateRow
	var firstErr error
	for _, v := range releaseGateVersions() {
		row := releaseGateRow{BenchVersion: v, PolicyFloor: grade.GradingPolicyFloor(v), Generatable: protocol.SupportedBenchVersion(v)}
		bank, err := bankForVersion(v)
		if err != nil {
			row.Error = err.Error()
			if firstErr == nil {
				firstErr = err
			}
		} else {
			row.BankVersion = bank.Version
		}
		rows = append(rows, row)
	}
	return rows, firstErr
}

type cannedKindStat struct {
	Kind       string  `json:"kind"`
	Cases      int     `json:"cases"`
	Passable   int     `json:"passable"`
	Share      float64 `json:"share"`
	ScoreMass  float64 `json:"score_mass"`
	MeanCredit float64 `json:"mean_credit"`
}

// claimKindGate is the per-kind generated-corpus gate for one claim kind: the
// share of cases a canned or public-question-only strategy can pass.
type claimKindGate struct {
	Kind     string  `json:"kind"`
	Cases    int     `json:"cases"`
	Passable int     `json:"passable"`
	Share    float64 `json:"share"`
	Target   float64 `json:"target_share"`
	Within   bool    `json:"within_target"`
}

type cannedReport struct {
	BenchVersion int `json:"bench_version"`
	// CorpusBenchVersion is the version the corpus was GENERATED at. It equals
	// BenchVersion when that version is generatable; for a grader-only version
	// (a grading policy that landed before its generation contract) the newest
	// generatable corpus below it is regraded under the requested policy.
	CorpusBenchVersion int              `json:"corpus_bench_version"`
	RunSize            string           `json:"run_size"`
	Seeds              int              `json:"seeds"`
	SeedFirst          int64            `json:"seed_first"`
	SeedLast           int64            `json:"seed_last"`
	PassScore          float64          `json:"pass_score"`
	TargetShare        float64          `json:"target_share"`
	WithinTarget       bool             `json:"within_target"`
	TotalCases         int              `json:"total_cases"`
	Passable           int              `json:"passable"`
	Share              float64          `json:"share"`
	ScoreMass          float64          `json:"score_mass"`
	MeanCredit         float64          `json:"mean_credit"`
	Probes             []string         `json:"probes"`
	Kinds              []cannedKindStat `json:"kinds"`
	// ClaimKindGates publish the per-kind gate for every claim kind that appears
	// in the corpus; ClaimKindsWithinTarget is their conjunction.
	ClaimKindGates         []claimKindGate `json:"claim_kind_gates,omitempty"`
	ClaimKindsWithinTarget bool            `json:"claim_kinds_within_target"`
}

type cannedAccumulator struct {
	Cases     int
	Passable  int
	ScoreMass float64
}

type robustnessKindStat struct {
	Kind                string  `json:"kind"`
	Cases               int     `json:"cases"`
	Probes              int     `json:"probes"`
	Evaluations         int     `json:"evaluations"`
	Passes              int     `json:"passes"`
	PassShare           float64 `json:"worst_strategy_pass_share"`
	MeanCredit          float64 `json:"worst_strategy_mean_credit"`
	AggregatePassShare  float64 `json:"aggregate_pass_share"`
	AggregateMeanCredit float64 `json:"aggregate_mean_credit"`
	WorstPassProbe      string  `json:"worst_pass_probe,omitempty"`
	WorstMeanProbe      string  `json:"worst_mean_probe,omitempty"`
	MaxPassShare        float64 `json:"max_pass_share"`
	MaxMeanCredit       float64 `json:"max_mean_credit"`
	WithinLimit         bool    `json:"within_limit"`
}

type robustnessReport struct {
	BankVersion              string               `json:"bank_version"`
	BenchVersion             int                  `json:"bench_version"`
	Cases                    int                  `json:"cases"`
	Probes                   int                  `json:"probes"`
	PromptOnly               int                  `json:"prompt_only_probes"`
	Evaluations              int                  `json:"evaluations"`
	PositiveChecks           int                  `json:"positive_checks"`
	PositiveFailures         int                  `json:"positive_failures"`
	NegativeChecks           int                  `json:"negative_checks"`
	NegativeFailures         int                  `json:"negative_failures"`
	ReviewedPositives        int                  `json:"reviewed_positives"`
	ReviewedPositiveFailures int                  `json:"reviewed_positive_failures"`
	FailedChecks             []string             `json:"failed_checks,omitempty"`
	CoverageOK               bool                 `json:"coverage_ok"`
	WithinLimits             bool                 `json:"within_limits"`
	Kinds                    []robustnessKindStat `json:"kinds"`
}

type generatedAndRobustnessReport struct {
	GeneratedCorpus cannedReport     `json:"generated_corpus"`
	Synthetic       robustnessReport `json:"synthetic_robustness"`
	WithinLimits    bool             `json:"within_limits"`
}

// classify grades one case and buckets the verdict. It keys the disqualifier
// bucket on the grader's own note strings, which is safe here because tool
// and grader live in one module and change together.
func classify(mc protocol.MemoryCase, resp protocol.RunResponse) (outcome, grade.Verdict) {
	v := grade.Memory(mc, resp)
	if v.Score > 0 {
		return outCredited, v
	}
	if len(v.Notes) == 0 {
		return outTypedZero, v
	}
	n := v.Notes[0]
	switch {
	case n == "empty response":
		return outEmpty, v
	case strings.HasPrefix(n, "no deterministic "):
		return outTypedZero, v
	default:
		return outDisqualified, v
	}
}

// audit runs the classification over every memory case with a transcript and
// returns the labeling sheet plus per-kind stats.
func audit(cases []gen.ArtifactCase, transcripts map[string]protocol.RunResponse) ([]candidate, map[string]*kindStat, int) {
	stats := map[string]*kindStat{}
	statFor := func(kind string) *kindStat {
		if kind == "" {
			kind = protocol.AnswerValue
		}
		if stats[kind] == nil {
			stats[kind] = &kindStat{}
		}
		return stats[kind]
	}
	var sheet []candidate
	missing := 0
	for _, ac := range cases {
		resp, ok := transcripts[ac.ID]
		if !ok {
			missing++
			continue
		}
		st := statFor(ac.AnswerKind)
		st.Graded++
		out, v := classify(ac.MemoryCase, resp)
		switch out {
		case outCredited:
			st.Credited++
		case outDisqualified:
			st.Disqualified++
		case outTypedZero, outEmpty:
			st.TypedZero++
			sheet = append(sheet, candidate{
				CaseID:       ac.ID,
				QuestionType: ac.QuestionType,
				AnswerKind:   kindOrValue(ac.AnswerKind),
				Question:     ac.Question,
				Expected:     ac.ExpectedAnswer,
				Items:        ac.AnswerItems,
				Answer:       resp.Answer,
				FinalText:    resp.FinalText,
				Notes:        v.Notes,
			})
		}
	}
	return sheet, stats, missing
}

func kindOrValue(k string) string {
	if k == "" {
		return protocol.AnswerValue
	}
	return k
}

// cannedAudit generates seeds 1..seedCount and measures the best score each
// case awards to a fixed canned response or a template using only its public
// question. No probe receives the expected answer, answer items, case id, seed,
// or generated memory.
func cannedAudit(benchVersion int, runSize string, seedCount int) (cannedReport, error) {
	bank, err := bankForVersion(benchVersion)
	if err != nil {
		return cannedReport{}, err
	}
	corpusVersion, ok := corpusVersionFor(benchVersion)
	if !ok {
		return cannedReport{}, fmt.Errorf("unsupported bench_version %d", benchVersion)
	}
	prof, ok := gen.ProfileForVersion(runSize, corpusVersion)
	if !ok {
		return cannedReport{}, fmt.Errorf("unsupported run_size %q", runSize)
	}
	if seedCount < 1 || seedCount > 1000 {
		return cannedReport{}, fmt.Errorf("seeds must be between 1 and 1000")
	}

	byKind := map[string]*cannedAccumulator{}
	total := cannedAccumulator{}
	for seed := int64(1); seed <= int64(seedCount); seed++ {
		artifact, err := gen.GenerateDataset(seed, prof, corpusVersion)
		if err != nil {
			return cannedReport{}, fmt.Errorf("generate seed %d: %w", seed, err)
		}
		for _, ac := range artifact.MemoryCases {
			if corpusVersion >= protocol.BenchVersionV8 && ac.BenchVersion != corpusVersion {
				return cannedReport{}, fmt.Errorf("case %s stamps bench_version %d, want %d", ac.ID, ac.BenchVersion, corpusVersion)
			}
			mc := ac.MemoryCase
			if corpusVersion != benchVersion {
				// Regrade the fallback corpus under the requested grading policy.
				mc.BenchVersion = benchVersion
			}
			best, _ := bestCannedScore(mc, bank)
			kind := kindOrValue(ac.AnswerKind)
			acc := byKind[kind]
			if acc == nil {
				acc = &cannedAccumulator{}
				byKind[kind] = acc
			}
			acc.Cases++
			acc.ScoreMass += best
			total.Cases++
			total.ScoreMass += best
			if best >= cannedPassScore {
				acc.Passable++
				total.Passable++
			}
		}
	}

	kinds := make([]string, 0, len(byKind))
	for kind := range byKind {
		kinds = append(kinds, kind)
	}
	sort.Strings(kinds)
	probes := generatedCannedProbes(bank)
	report := cannedReport{
		BenchVersion:           benchVersion,
		CorpusBenchVersion:     corpusVersion,
		RunSize:                runSize,
		Seeds:                  seedCount,
		SeedFirst:              1,
		SeedLast:               int64(seedCount),
		PassScore:              cannedPassScore,
		TargetShare:            cannedTargetShare,
		TotalCases:             total.Cases,
		Passable:               total.Passable,
		ScoreMass:              total.ScoreMass,
		Probes:                 make([]string, 0, len(probes)),
		Kinds:                  make([]cannedKindStat, 0, len(kinds)),
		ClaimKindsWithinTarget: true,
	}
	for _, probe := range probes {
		report.Probes = append(report.Probes, probe.Name)
	}
	for _, kind := range kinds {
		acc := byKind[kind]
		stat := cannedKindStat{Kind: kind, Cases: acc.Cases, Passable: acc.Passable, ScoreMass: acc.ScoreMass}
		if acc.Cases > 0 {
			stat.Share = float64(acc.Passable) / float64(acc.Cases)
			stat.MeanCredit = acc.ScoreMass / float64(acc.Cases)
		}
		report.Kinds = append(report.Kinds, stat)
		if bank.InteractionOnly(kind) {
			continue
		}
		gate := claimKindGate{Kind: kind, Cases: acc.Cases, Passable: acc.Passable, Share: stat.Share, Target: grade.ClaimKindTargetShare}
		gate.Within = gate.Share < gate.Target
		if !gate.Within {
			report.ClaimKindsWithinTarget = false
		}
		report.ClaimKindGates = append(report.ClaimKindGates, gate)
	}
	if total.Cases > 0 {
		report.Share = float64(total.Passable) / float64(total.Cases)
		report.MeanCredit = total.ScoreMass / float64(total.Cases)
	}
	report.WithinTarget = report.Share < report.TargetShare
	return report, nil
}

// corpusVersionFor returns the version whose corpus audits a grading policy:
// the version itself when generatable, else the newest generatable version
// below it. ok is false when nothing below it is generatable either.
func corpusVersionFor(benchVersion int) (int, bool) {
	for v := benchVersion; v >= protocol.BenchVersionV2; v-- {
		if protocol.SupportedBenchVersion(v) {
			return v, true
		}
	}
	return 0, false
}

func generatedCannedProbes(bank grade.AuditBank) []robustnessProbe {
	probes := make([]robustnessProbe, 0, len(cannedProbes)+len(bank.Probes))
	for _, probe := range cannedProbes {
		probes = append(probes, robustnessProbe{Name: probe.Name, Fixed: probe.Response})
	}
	for _, probe := range bank.Probes {
		if probe.PromptOnly() {
			probes = append(probes, probe)
		}
	}
	return probes
}

func bestCannedScore(mc protocol.MemoryCase, bank grade.AuditBank) (float64, string) {
	best := 0.0
	bestProbe := ""
	for _, probe := range generatedCannedProbes(bank) {
		if score := grade.Memory(mc, probe.Response(mc.Question)).Score; score > best {
			best = score
			bestProbe = probe.Name
		}
	}
	return best, bestProbe
}

func syntheticRobustnessAudit(benchVersion int) (robustnessReport, error) {
	bank, err := bankForVersion(benchVersion)
	if err != nil {
		return robustnessReport{}, err
	}
	return runRobustnessBank(bank, benchVersion)
}

func runRobustnessBank(bank grade.AuditBank, benchVersion int) (robustnessReport, error) {
	type acc struct {
		Cases, Evaluations, Passes int
		ScoreMass                  float64
	}
	byKind := map[string]*acc{}
	byKindProbe := map[string]map[string]*acc{}
	caseNames := map[string]bool{}
	probeNames := map[string]bool{}
	promptOnly := 0
	for _, probe := range bank.Probes {
		if probe.Name == "" || probeNames[probe.Name] {
			return robustnessReport{}, fmt.Errorf("robustness bank %s has empty or duplicate probe %q", bank.Version, probe.Name)
		}
		probeNames[probe.Name] = true
		if probe.PromptOnly() {
			promptOnly++
		}
	}
	for _, rc := range bank.Cases {
		if rc.Name == "" || caseNames[rc.Name] {
			return robustnessReport{}, fmt.Errorf("robustness bank %s has empty or duplicate case %q", bank.Version, rc.Name)
		}
		caseNames[rc.Name] = true
		kind := kindOrValue(rc.Case.AnswerKind)
		if _, ok := bank.Limits[kind]; !ok {
			return robustnessReport{}, fmt.Errorf("robustness bank %s case %q has unbounded kind %q", bank.Version, rc.Name, kind)
		}
		a := byKind[kind]
		if a == nil {
			a = &acc{}
			byKind[kind] = a
		}
		a.Cases++
		mc := rc.Case
		mc.BenchVersion = benchVersion
		mc.ID = "robustness-" + rc.Name
		mc.Question = rc.Question
		if score := grade.Memory(mc, rc.Positive).Score; score <= 0 {
			return robustnessReport{}, fmt.Errorf("robustness bank %s positive control %q scored %.3f", bank.Version, rc.Name, score)
		}
		for _, probe := range bank.Probes {
			resp := probe.Response(rc.Question)
			responseText := resp.Answer + "\n" + resp.FinalText
			if mc.ExpectedAnswer != "" && grade.Hit(mc.ExpectedAnswer, responseText) {
				return robustnessReport{}, fmt.Errorf("robustness probe %q includes expected answer for case %q", probe.Name, rc.Name)
			}
			for _, item := range mc.AnswerItems {
				if grade.Hit(item, responseText) && (!probe.PromptOnly() || !grade.Hit(item, rc.Question)) {
					return robustnessReport{}, fmt.Errorf("robustness probe %q includes non-public answer item for case %q", probe.Name, rc.Name)
				}
			}
			score := grade.Memory(mc, resp).Score
			a.Evaluations++
			a.ScoreMass += score
			probeByName := byKindProbe[kind]
			if probeByName == nil {
				probeByName = map[string]*acc{}
				byKindProbe[kind] = probeByName
			}
			pa := probeByName[probe.Name]
			if pa == nil {
				pa = &acc{}
				probeByName[probe.Name] = pa
			}
			pa.Cases++
			pa.Evaluations++
			pa.ScoreMass += score
			if score >= cannedPassScore {
				a.Passes++
				pa.Passes++
			}
		}
	}

	report := robustnessReport{
		BankVersion: bank.Version, BenchVersion: benchVersion,
		Cases: len(bank.Cases), Probes: len(bank.Probes), PromptOnly: promptOnly,
		PositiveChecks: len(bank.Cases), NegativeChecks: len(bank.Negatives), ReviewedPositives: len(bank.Positives),
		CoverageOK: true, WithinLimits: true,
	}
	kinds := make([]string, 0, len(bank.Limits))
	for kind := range bank.Limits {
		kinds = append(kinds, kind)
	}
	sort.Strings(kinds)
	for _, kind := range kinds {
		a := byKind[kind]
		limit := bank.Limits[kind]
		stat := robustnessKindStat{Kind: kind, Probes: len(bank.Probes), MaxPassShare: limit.MaxPassShare, MaxMeanCredit: limit.MaxMeanCredit}
		if a == nil || a.Cases == 0 || a.Evaluations == 0 {
			report.CoverageOK = false
			report.WithinLimits = false
			report.FailedChecks = append(report.FailedChecks, "coverage:"+kind)
			report.Kinds = append(report.Kinds, stat)
			continue
		}
		stat.Cases, stat.Evaluations, stat.Passes = a.Cases, a.Evaluations, a.Passes
		stat.AggregatePassShare = float64(a.Passes) / float64(a.Evaluations)
		stat.AggregateMeanCredit = a.ScoreMass / float64(a.Evaluations)
		for _, probe := range bank.Probes {
			pa := byKindProbe[kind][probe.Name]
			if pa == nil || pa.Evaluations == 0 {
				continue
			}
			passShare := float64(pa.Passes) / float64(pa.Evaluations)
			meanCredit := pa.ScoreMass / float64(pa.Evaluations)
			if passShare > stat.PassShare {
				stat.PassShare = passShare
				stat.WorstPassProbe = probe.Name
			}
			if meanCredit > stat.MeanCredit {
				stat.MeanCredit = meanCredit
				stat.WorstMeanProbe = probe.Name
			}
		}
		stat.WithinLimit = stat.PassShare <= stat.MaxPassShare && stat.MeanCredit <= stat.MaxMeanCredit
		if !stat.WithinLimit {
			report.WithinLimits = false
			report.FailedChecks = append(report.FailedChecks, "limit:"+kind+":"+stat.WorstPassProbe)
		}
		report.Evaluations += stat.Evaluations
		report.Kinds = append(report.Kinds, stat)
	}
	for _, negative := range bank.Negatives {
		mc := negative.Case
		mc.BenchVersion = benchVersion
		if grade.Memory(mc, negative.Response).Score != 0 {
			report.NegativeFailures++
			report.WithinLimits = false
			report.FailedChecks = append(report.FailedChecks, "negative:"+negative.Name)
		}
	}
	for _, positive := range bank.Positives {
		mc := positive.Case
		mc.BenchVersion = benchVersion
		want := positive.MinScore
		if want <= 0 {
			want = 1
		}
		if grade.Memory(mc, positive.Response).Score < want {
			report.ReviewedPositiveFailures++
			report.WithinLimits = false
			report.FailedChecks = append(report.FailedChecks, "positive:"+positive.Name)
		}
	}
	return report, nil
}

func main() {
	if err := run(os.Args[1:], os.Stdout, os.Stderr); err != nil {
		fmt.Fprintf(os.Stderr, "graderaudit: %v\n", err)
		os.Exit(2)
	}
}

func run(args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("graderaudit", flag.ContinueOnError)
	fs.SetOutput(stderr)
	artifactPath := fs.String("artifact", "", "path to the canonical DatasetArtifact JSON")
	transcriptPath := fs.String("transcripts", "", "path to JSONL transcripts ({case_id, response} per line)")
	benchVersion := fs.Int("bench-version", 0, "generate and audit this benchmark version")
	seedCount := fs.Int("seeds", 0, "number of deterministic generated seeds to audit (1..N)")
	runSize := fs.String("run-size", "full", "generated audit profile: small | medium | full")
	asJSON := fs.Bool("json", false, "emit machine-readable JSON")
	releaseGateMode := fs.Bool("release-gate", false, "verify every supported bench version and grading policy floor owns an audit bank (fails closed)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return fmt.Errorf("unexpected positional arguments: %s", strings.Join(fs.Args(), " "))
	}

	transcriptMode := *artifactPath != "" || *transcriptPath != ""
	cannedMode := *benchVersion != 0 || *seedCount != 0
	switch {
	case *releaseGateMode && (transcriptMode || cannedMode):
		return fmt.Errorf("-release-gate takes no other mode flags")
	case *releaseGateMode:
		rows, gateErr := releaseGate()
		if err := writeReleaseGate(stdout, rows, *asJSON); err != nil {
			return err
		}
		return gateErr
	case transcriptMode && cannedMode:
		return fmt.Errorf("artifact/transcript mode and generated canned-response mode are mutually exclusive")
	case transcriptMode:
		if *artifactPath == "" || *transcriptPath == "" {
			return fmt.Errorf("artifact mode requires both -artifact and -transcripts")
		}
		return runTranscriptAudit(*artifactPath, *transcriptPath, *asJSON, stdout, stderr)
	case cannedMode:
		if *benchVersion == 0 || *seedCount == 0 {
			return fmt.Errorf("generated mode requires both -bench-version and -seeds")
		}
		generated, err := cannedAudit(*benchVersion, *runSize, *seedCount)
		if err != nil {
			return err
		}
		synthetic, err := syntheticRobustnessAudit(*benchVersion)
		if err != nil {
			return err
		}
		report := generatedAndRobustnessReport{
			GeneratedCorpus: generated,
			Synthetic:       synthetic,
			WithinLimits: generated.WithinTarget && generated.ClaimKindsWithinTarget && synthetic.CoverageOK &&
				synthetic.WithinLimits && synthetic.NegativeFailures == 0 && synthetic.ReviewedPositiveFailures == 0,
		}
		if err := writeGeneratedAndRobustnessReport(stdout, report, *asJSON); err != nil {
			return err
		}
		if !generated.WithinTarget {
			return fmt.Errorf("generated-corpus canned-response share %.4f exceeds target %.4f", generated.Share, generated.TargetShare)
		}
		if !generated.ClaimKindsWithinTarget {
			return fmt.Errorf("generated-corpus public-question-only passable share reached %.4f for a claim kind", grade.ClaimKindTargetShare)
		}
		if !synthetic.CoverageOK {
			return fmt.Errorf("synthetic robustness bank %s lacks required answer-kind coverage", synthetic.BankVersion)
		}
		if !synthetic.WithinLimits || synthetic.NegativeFailures != 0 || synthetic.ReviewedPositiveFailures != 0 {
			return fmt.Errorf("synthetic robustness bank %s failed %d check(s): %s", synthetic.BankVersion, len(synthetic.FailedChecks), strings.Join(synthetic.FailedChecks, ", "))
		}
		return nil
	default:
		return fmt.Errorf("usage: graderaudit -artifact dataset.json -transcripts transcripts.jsonl [-json], or -bench-version 9 -seeds 40 [-run-size full] [-json], or -release-gate [-json]")
	}
}

func writeGeneratedAndRobustnessReport(w io.Writer, report generatedAndRobustnessReport, asJSON bool) error {
	if asJSON {
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		if err := enc.Encode(report); err != nil {
			return fmt.Errorf("encode combined grader audit report: %w", err)
		}
		return nil
	}
	if _, err := fmt.Fprintln(w, "gate\tgenerated_corpus"); err != nil {
		return fmt.Errorf("write generated-corpus gate header: %w", err)
	}
	if err := writeCannedReport(w, report.GeneratedCorpus, false); err != nil {
		return err
	}
	if _, err := fmt.Fprintf(w, "gate\tsynthetic_robustness\nbank_version\tbench_version\tcases\tprobes\tprompt_only\tevaluations\tpositive_checks\tpositive_failures\tnegative_checks\tnegative_failures\treviewed_positives\treviewed_positive_failures\tcoverage_ok\twithin_limits\n%s\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%t\t%t\n",
		report.Synthetic.BankVersion, report.Synthetic.BenchVersion, report.Synthetic.Cases,
		report.Synthetic.Probes, report.Synthetic.PromptOnly, report.Synthetic.Evaluations,
		report.Synthetic.PositiveChecks, report.Synthetic.PositiveFailures,
		report.Synthetic.NegativeChecks, report.Synthetic.NegativeFailures,
		report.Synthetic.ReviewedPositives, report.Synthetic.ReviewedPositiveFailures,
		report.Synthetic.CoverageOK, report.Synthetic.WithinLimits); err != nil {
		return fmt.Errorf("write synthetic robustness summary: %w", err)
	}
	if _, err := fmt.Fprintln(w, "answer_kind\tcases\tprobes\tevaluations\tpasses\tworst_strategy_pass_share\tworst_strategy_mean_credit\tworst_pass_probe\tworst_mean_probe\taggregate_pass_share\taggregate_mean_credit\tmax_pass_share\tmax_mean_credit\twithin_limit"); err != nil {
		return fmt.Errorf("write synthetic robustness header: %w", err)
	}
	for _, stat := range report.Synthetic.Kinds {
		if _, err := fmt.Fprintf(w, "%s\t%d\t%d\t%d\t%d\t%.6f\t%.6f\t%s\t%s\t%.6f\t%.6f\t%.6f\t%.6f\t%t\n",
			stat.Kind, stat.Cases, stat.Probes, stat.Evaluations, stat.Passes,
			stat.PassShare, stat.MeanCredit, stat.WorstPassProbe, stat.WorstMeanProbe,
			stat.AggregatePassShare, stat.AggregateMeanCredit,
			stat.MaxPassShare, stat.MaxMeanCredit, stat.WithinLimit); err != nil {
			return fmt.Errorf("write synthetic robustness kind %s: %w", stat.Kind, err)
		}
	}
	return nil
}

func runTranscriptAudit(artifactPath, transcriptPath string, asJSON bool, stdout, stderr io.Writer) error {
	ab, err := os.ReadFile(artifactPath)
	if err != nil {
		return fmt.Errorf("read artifact: %w", err)
	}
	var artifact gen.DatasetArtifact
	if err := json.Unmarshal(ab, &artifact); err != nil {
		return fmt.Errorf("parse artifact: %w", err)
	}

	tf, err := os.Open(transcriptPath)
	if err != nil {
		return fmt.Errorf("open transcripts: %w", err)
	}
	defer tf.Close()
	transcripts := map[string]protocol.RunResponse{}
	sc := bufio.NewScanner(tf)
	sc.Buffer(make([]byte, 0, 1<<20), 1<<24)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var tl transcriptLine
		if err := json.Unmarshal([]byte(line), &tl); err != nil {
			return fmt.Errorf("parse transcript line: %w", err)
		}
		transcripts[tl.CaseID] = tl.Response
	}
	if err := sc.Err(); err != nil {
		return fmt.Errorf("scan transcripts: %w", err)
	}

	sheet, stats, missing := audit(artifact.MemoryCases, transcripts)

	w := bufio.NewWriter(stdout)
	if asJSON {
		enc := json.NewEncoder(w)
		for _, c := range sheet {
			if err := enc.Encode(c); err != nil {
				return fmt.Errorf("encode row: %w", err)
			}
		}
	} else {
		fmt.Fprintln(w, "case_id\tquestion_type\tanswer_kind\tquestion\texpected\tresponse")
		for _, c := range sheet {
			fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\n",
				c.CaseID, c.QuestionType, c.AnswerKind,
				tsv(c.Question), tsv(c.Expected+joinItems(c.Items)), tsv(c.Answer+" | "+c.FinalText))
		}
	}
	if err := w.Flush(); err != nil {
		return fmt.Errorf("flush output: %w", err)
	}

	kinds := make([]string, 0, len(stats))
	for k := range stats {
		kinds = append(kinds, k)
	}
	sort.Strings(kinds)
	fmt.Fprintf(stderr, "graderaudit: %d candidate false negatives across %d graded cases (%d without transcripts)\n",
		len(sheet), totalGraded(stats), missing)
	for _, k := range kinds {
		s := stats[k]
		fmt.Fprintf(stderr, "  %-14s graded=%-4d credited=%-4d disqualified=%-4d typed_zero=%d\n",
			k, s.Graded, s.Credited, s.Disqualified, s.TypedZero)
	}
	return nil
}

func writeCannedReport(w io.Writer, report cannedReport, asJSON bool) error {
	if asJSON {
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		if err := enc.Encode(report); err != nil {
			return fmt.Errorf("encode canned-response report: %w", err)
		}
		return nil
	}
	if _, err := fmt.Fprintf(w, "bench_version\tcorpus_bench_version\trun_size\tseeds\tpass_score\ttarget_share\ttotal_cases\tpassable\tshare\tmean_credit\twithin_target\tclaim_kinds_within_target\n%d\t%d\t%s\t%d\t%.2f\t%.6f\t%d\t%d\t%.6f\t%.6f\t%t\t%t\n",
		report.BenchVersion, report.CorpusBenchVersion, report.RunSize, report.Seeds, report.PassScore, report.TargetShare,
		report.TotalCases, report.Passable, report.Share, report.MeanCredit, report.WithinTarget, report.ClaimKindsWithinTarget); err != nil {
		return fmt.Errorf("write canned-response summary: %w", err)
	}
	if _, err := fmt.Fprintln(w, "answer_kind\tcases\tpassable\tshare\tmean_credit"); err != nil {
		return fmt.Errorf("write canned-response header: %w", err)
	}
	for _, stat := range report.Kinds {
		if _, err := fmt.Fprintf(w, "%s\t%d\t%d\t%.6f\t%.6f\n", stat.Kind, stat.Cases, stat.Passable, stat.Share, stat.MeanCredit); err != nil {
			return fmt.Errorf("write canned-response kind %s: %w", stat.Kind, err)
		}
	}
	if len(report.ClaimKindGates) > 0 {
		if _, err := fmt.Fprintln(w, "claim_kind\tcases\tpassable\tshare\ttarget_share\twithin_target"); err != nil {
			return fmt.Errorf("write claim-kind gate header: %w", err)
		}
		for _, gate := range report.ClaimKindGates {
			if _, err := fmt.Fprintf(w, "%s\t%d\t%d\t%.6f\t%.6f\t%t\n", gate.Kind, gate.Cases, gate.Passable, gate.Share, gate.Target, gate.Within); err != nil {
				return fmt.Errorf("write claim-kind gate %s: %w", gate.Kind, err)
			}
		}
	}
	return nil
}

func writeReleaseGate(w io.Writer, rows []releaseGateRow, asJSON bool) error {
	if asJSON {
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		if err := enc.Encode(rows); err != nil {
			return fmt.Errorf("encode release gate: %w", err)
		}
		return nil
	}
	if _, err := fmt.Fprintln(w, "gate\trelease\nbench_version\tpolicy_floor\tbank_version\tgeneratable\terror"); err != nil {
		return fmt.Errorf("write release gate header: %w", err)
	}
	for _, row := range rows {
		if _, err := fmt.Fprintf(w, "%d\t%d\t%s\t%t\t%s\n", row.BenchVersion, row.PolicyFloor, row.BankVersion, row.Generatable, row.Error); err != nil {
			return fmt.Errorf("write release gate row: %w", err)
		}
	}
	return nil
}

func totalGraded(stats map[string]*kindStat) int {
	n := 0
	for _, s := range stats {
		n += s.Graded
	}
	return n
}

func joinItems(items []string) string {
	if len(items) == 0 {
		return ""
	}
	return " [" + strings.Join(items, "; ") + "]"
}

// tsv flattens a field for the TSV sheet.
func tsv(s string) string {
	s = strings.ReplaceAll(s, "\t", " ")
	return strings.ReplaceAll(s, "\n", " ")
}
