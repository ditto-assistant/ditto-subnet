package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestAuditBuckets pins the classification: credited answers and disqualifier
// zeroes stay OFF the sheet; only typed-check zeroes (the one place a grader
// false negative can hide) land on it.
func TestAuditBuckets(t *testing.T) {
	mc := protocol.MemoryCase{
		ID:                "c-value",
		QuestionType:      "single-session-recall",
		Question:          "What's my favorite cuisine?",
		ExpectedAnswer:    "Thai",
		DistractorAnswers: []string{"Ethiopian"},
	}
	cases := []gen.ArtifactCase{
		{MemoryCase: mc},
		{MemoryCase: withID(mc, "c-distractor")},
		{MemoryCase: withID(mc, "c-miss")},
		{MemoryCase: withID(mc, "c-absent")},
	}
	transcripts := map[string]protocol.RunResponse{
		"c-value":      {FinalText: "You said Thai food is your favorite."},
		"c-distractor": {FinalText: "It's Ethiopian, I believe."},
		// A synonymous-but-unmatched answer: the false-negative shape the
		// sheet exists to surface.
		"c-miss": {FinalText: "The cuisine of Thailand."},
	}
	sheet, stats, missing := audit(cases, transcripts)
	if missing != 1 {
		t.Fatalf("missing = %d, want 1", missing)
	}
	if len(sheet) != 1 || sheet[0].CaseID != "c-miss" {
		t.Fatalf("sheet = %+v, want exactly c-miss", sheet)
	}
	s := stats[protocol.AnswerValue]
	if s == nil || s.Graded != 3 || s.Credited != 1 || s.Disqualified != 1 || s.TypedZero != 1 {
		t.Fatalf("stats = %+v, want graded=3 credited=1 disqualified=1 typed_zero=1", s)
	}
}

func withID(mc protocol.MemoryCase, id string) protocol.MemoryCase {
	mc.ID = id
	return mc
}

func TestCannedProbeSuiteIsFixedAndCaseIndependent(t *testing.T) {
	if len(cannedProbes) < 6 {
		t.Fatalf("probe suite too small to cover generic answer kinds: %d", len(cannedProbes))
	}
	seen := map[string]bool{}
	for _, probe := range cannedProbes {
		if probe.Name == "" || seen[probe.Name] {
			t.Fatalf("empty or duplicate probe name %q", probe.Name)
		}
		seen[probe.Name] = true
		resp := probe.Response
		if resp.Answer != "" || len(resp.ToolCalls) != 0 || resp.PromptTokens != 0 || resp.OutputTokens != 0 {
			t.Fatalf("probe %q carries non-canned case/usage data: %+v", probe.Name, resp)
		}
		for _, leaked := range []string{"Lisbon", "GAVOTU", "expected_answer", "question_type", "case_id"} {
			if strings.Contains(resp.FinalText, leaked) {
				t.Fatalf("probe %q contains fixture-specific data %q", probe.Name, leaked)
			}
		}
	}
}

func TestSyntheticRobustnessBankIsVersionedBroadAndAnswerBlind(t *testing.T) {
	if robustnessBankVersion != "v9-2" {
		t.Fatalf("bank version=%q, want v9-2", robustnessBankVersion)
	}
	if len(robustnessProbesV1) < 20 {
		t.Fatalf("robustness probe bank is not broad: %d", len(robustnessProbesV1))
	}
	fixed, promptOnly := 0, 0
	for _, probe := range robustnessProbesV1 {
		if probe.PromptOnly() {
			promptOnly++
			first := probe.Response("FIRST PUBLIC QUESTION")
			second := probe.Response("SECOND PUBLIC QUESTION")
			if !strings.Contains(first.FinalText, "FIRST PUBLIC QUESTION") || strings.Contains(first.FinalText, "SECOND PUBLIC QUESTION") ||
				!strings.Contains(second.FinalText, "SECOND PUBLIC QUESTION") || strings.Contains(second.FinalText, "FIRST PUBLIC QUESTION") {
				t.Fatalf("prompt-only probe %q used data other than its question: first=%q second=%q", probe.Name, first.FinalText, second.FinalText)
			}
		} else {
			fixed++
			if !reflect.DeepEqual(probe.Response("FIRST"), probe.Response("SECOND")) {
				t.Fatalf("fixed probe %q depends on the case", probe.Name)
			}
		}
		for _, rc := range robustnessCasesV1 {
			resp := probe.Response(rc.Question)
			text := resp.Answer + "\n" + resp.FinalText
			if rc.Case.ExpectedAnswer != "" && grade.Hit(rc.Case.ExpectedAnswer, text) {
				t.Fatalf("probe %q contains expected answer %q for case %q", probe.Name, rc.Case.ExpectedAnswer, rc.Name)
			}
			if !probe.PromptOnly() {
				for _, item := range rc.Case.AnswerItems {
					if grade.Hit(item, text) {
						t.Fatalf("fixed probe %q contains answer item %q for case %q", probe.Name, item, rc.Name)
					}
				}
			}
		}
	}
	if fixed < 15 || promptOnly < 5 {
		t.Fatalf("bank lacks independent fixed/prompt-only breadth: fixed=%d prompt_only=%d", fixed, promptOnly)
	}
}

func TestSyntheticRobustnessV1CoversEveryKindAndPinsLimits(t *testing.T) {
	report, err := syntheticRobustnessAudit(protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	if report.BankVersion != robustnessBankVersion || report.BenchVersion != protocol.BenchVersionV9 ||
		report.Cases != 9 || report.Probes != 22 || report.PromptOnly != 5 || report.Evaluations != 198 ||
		report.PositiveChecks != 9 || report.PositiveFailures != 0 || report.NegativeChecks != 10 || report.NegativeFailures != 0 || !report.CoverageOK || !report.WithinLimits {
		t.Fatalf("wrong robustness report identity: %+v", report)
	}
	want := []robustnessKindStat{
		{Kind: protocol.AnswerAcknowledge, Cases: 1, Probes: 22, Evaluations: 22, Passes: 2, PassShare: 1, MeanCredit: 1, AggregatePassShare: 2.0 / 22.0, AggregateMeanCredit: 2.0 / 22.0, WorstPassProbe: "ack-complete", WorstMeanProbe: "ack-complete", MaxPassShare: 1, MaxMeanCredit: 1, WithinLimit: true},
		{Kind: protocol.AnswerChitchat, Cases: 1, Probes: 22, Evaluations: 22, Passes: 22, PassShare: 1, MeanCredit: 0.5, AggregatePassShare: 1, AggregateMeanCredit: 0.5, WorstPassProbe: "neutral-okay", WorstMeanProbe: "neutral-okay", MaxPassShare: 1, MaxMeanCredit: 0.5, WithinLimit: true},
		{Kind: protocol.AnswerDecline, Cases: 1, Probes: 22, Evaluations: 22, Passes: 4, PassShare: 1, MeanCredit: 1, AggregatePassShare: 4.0 / 22.0, AggregateMeanCredit: 4.0 / 22.0, WorstPassProbe: "decline-no-record", WorstMeanProbe: "decline-no-record", MaxPassShare: 1, MaxMeanCredit: 1, WithinLimit: true},
		{Kind: protocol.AnswerList, Cases: 1, Probes: 22, Evaluations: 22, MaxPassShare: 0, MaxMeanCredit: 0, WithinLimit: true},
		{Kind: protocol.AnswerMoney, Cases: 1, Probes: 22, Evaluations: 22, MaxPassShare: 0, MaxMeanCredit: 0, WithinLimit: true},
		{Kind: protocol.AnswerNumber, Cases: 1, Probes: 22, Evaluations: 22, MaxPassShare: 0, MaxMeanCredit: 0, WithinLimit: true},
		{Kind: protocol.AnswerPersistence, Cases: 1, Probes: 22, Evaluations: 22, MaxPassShare: 0.10, MaxMeanCredit: 0.10, WithinLimit: true},
		{Kind: protocol.AnswerReversal, Cases: 1, Probes: 22, Evaluations: 22, MaxPassShare: 0.15, MaxMeanCredit: 0.15, WithinLimit: true},
		{Kind: protocol.AnswerValue, Cases: 1, Probes: 22, Evaluations: 22, MaxPassShare: 0, MaxMeanCredit: 0, WithinLimit: true},
	}
	if !reflect.DeepEqual(report.Kinds, want) {
		t.Fatalf("synthetic per-kind table drifted:\n got %+v\nwant %+v", report.Kinds, want)
	}
}

func TestSyntheticRobustnessNegativeNearMissesStayZero(t *testing.T) {
	for _, negative := range robustnessNegativesV1 {
		t.Run(negative.Name, func(t *testing.T) {
			mc := negative.Case
			mc.BenchVersion = protocol.BenchVersionV9
			if got := grade.Memory(mc, negative.Response); got.Score != 0 {
				t.Fatalf("negative near miss passed: %+v", got)
			}
		})
	}
}

func TestSyntheticRobustnessPositiveControlsAreNonVacuous(t *testing.T) {
	for _, positive := range robustnessCasesV1 {
		t.Run(positive.Name, func(t *testing.T) {
			mc := positive.Case
			mc.BenchVersion = protocol.BenchVersionV9
			mc.Question = positive.Question
			if got := grade.Memory(mc, positive.Positive); got.Score <= 0 {
				t.Fatalf("positive control scored zero: %+v", got)
			}
		})
	}
	money := robustnessCasesV1[len(robustnessCasesV1)-1]
	if money.Case.ExpectedAnswer != "24500" || grade.Memory(protocol.MemoryCase{
		BenchVersion:   protocol.BenchVersionV9,
		AnswerKind:     protocol.AnswerMoney,
		ExpectedAnswer: money.Case.ExpectedAnswer,
	}, money.Positive).Score != 1 {
		t.Fatalf("money positive is not a cents-valued control: %+v", money)
	}
}

func TestSyntheticRobustnessFailsClosedWhenPositiveControlBreaks(t *testing.T) {
	original := robustnessCasesV1[0].Positive
	robustnessCasesV1[0].Positive = protocol.RunResponse{}
	_, err := syntheticRobustnessAudit(protocol.BenchVersionV9)
	robustnessCasesV1[0].Positive = original
	if err == nil || !strings.Contains(err.Error(), "positive control") {
		t.Fatalf("broken positive control did not fail closed: %v", err)
	}
}

func TestSyntheticRobustnessFailsClosedOnMissingKindAndPerKindRegression(t *testing.T) {
	originalCases := robustnessCasesV1
	robustnessCasesV1 = append([]robustnessCase(nil), originalCases[:len(originalCases)-1]...)
	report, err := syntheticRobustnessAudit(protocol.BenchVersionV9)
	robustnessCasesV1 = originalCases
	if err != nil {
		t.Fatal(err)
	}
	if report.CoverageOK || report.WithinLimits {
		t.Fatalf("missing money coverage did not fail closed: %+v", report)
	}

	originalProbes := robustnessProbesV1
	robustnessProbesV1 = make([]robustnessProbe, 22)
	for i := range robustnessProbesV1 {
		robustnessProbesV1[i] = robustnessProbe{Name: fmt.Sprintf("neutral-%02d", i), Fixed: protocol.RunResponse{FinalText: "Okay."}}
	}
	robustnessProbesV1[len(robustnessProbesV1)-1] = robustnessProbe{Name: "one-ack-pass", Fixed: protocol.RunResponse{FinalText: "Done. All set."}}
	originalLimit := robustnessLimitsV1[protocol.AnswerAcknowledge]
	robustnessLimitsV1[protocol.AnswerAcknowledge] = robustnessLimit{MaxPassShare: 0.05, MaxMeanCredit: 0.05}
	report, err = syntheticRobustnessAudit(protocol.BenchVersionV9)
	robustnessLimitsV1[protocol.AnswerAcknowledge] = originalLimit
	robustnessProbesV1 = originalProbes
	if err != nil {
		t.Fatal(err)
	}
	if !report.CoverageOK || report.WithinLimits {
		t.Fatalf("per-kind acknowledgement regression hid behind aggregate: %+v", report)
	}
}

// TestReleaseGateCoversEverySupportedVersionAndPolicyFloor is the #1522
// release gate: every generatable version from the audit floor up and every
// grading-policy floor resolves to a bank owned by its own policy floor, and
// removing a bank fails the gate closed.
func TestReleaseGateCoversEverySupportedVersionAndPolicyFloor(t *testing.T) {
	rows, err := releaseGate()
	if err != nil {
		t.Fatalf("release gate failed: %v\n%+v", err, rows)
	}
	covered := map[int]string{}
	for _, row := range rows {
		if row.Error != "" || row.BankVersion == "" {
			t.Fatalf("uncovered row: %+v", row)
		}
		covered[row.BenchVersion] = row.BankVersion
	}
	for v := grade.AuditBankFloor; v <= protocol.BenchVersionV12; v++ {
		if !protocol.SupportedBenchVersion(v) {
			continue
		}
		if covered[v] == "" {
			t.Fatalf("supported bench_version %d not covered by the release gate", v)
		}
	}
	if covered[protocol.BenchVersionV9] != robustnessBankVersion || covered[protocol.BenchVersionV12] != robustnessBankV12 || covered[protocol.BenchVersionV13] != grade.AuditBankV13().Version {
		t.Fatalf("bank resolution drifted: %+v", covered)
	}
	// Every grading policy floor from the audit floor up owns exactly one bank.
	for _, floor := range grade.GradingPolicyFloors() {
		if floor < grade.AuditBankFloor {
			continue
		}
		n := 0
		for _, bank := range auditBanks() {
			if bank.FloorBenchVersion == floor {
				n++
			}
		}
		if n != 1 {
			t.Fatalf("policy floor %d owns %d banks, want exactly 1", floor, n)
		}
	}
	// A version below the audit floor never resolves.
	if _, err := bankForVersion(protocol.BenchVersionV8); err == nil {
		t.Fatal("bench_version 8 resolved a bank")
	}
	// The CLI form fails closed too.
	var stdout, stderr bytes.Buffer
	if err := run([]string{"-release-gate"}, &stdout, &stderr); err != nil {
		t.Fatalf("release gate CLI: %v", err)
	}
	if !strings.Contains(stdout.String(), "gate\trelease") || !strings.Contains(stdout.String(), grade.AuditBankV13().Version) {
		t.Fatalf("release gate output: %q", stdout.String())
	}
	if err := run([]string{"-release-gate", "-bench-version", "9", "-seeds", "1"}, &stdout, &stderr); err == nil || !strings.Contains(err.Error(), "no other mode flags") {
		t.Fatalf("release gate accepted other modes: %v", err)
	}
}

// TestSyntheticRobustnessV13BankIsCleanAndCoversEveryKind runs the v13-1
// bank under the v13 policy: full kind coverage, every limit respected, every
// hard negative at 0, every reviewed positive at its floor.
func TestSyntheticRobustnessV13BankIsCleanAndCoversEveryKind(t *testing.T) {
	bank := grade.AuditBankV13()
	report, err := syntheticRobustnessAudit(protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	if report.BankVersion != bank.Version || report.BenchVersion != protocol.BenchVersionV13 {
		t.Fatalf("wrong bank: %+v", report)
	}
	if !report.CoverageOK || !report.WithinLimits || report.NegativeFailures != 0 || report.ReviewedPositiveFailures != 0 || report.PositiveFailures != 0 {
		t.Fatalf("v13 bank not clean: failed=%v report=%+v", report.FailedChecks, report)
	}
	if report.NegativeChecks < 50 || report.ReviewedPositives < 40 || report.Cases < 15 || report.PromptOnly < 6 {
		t.Fatalf("v13 bank is not broad: negatives=%d positives=%d cases=%d prompt_only=%d", report.NegativeChecks, report.ReviewedPositives, report.Cases, report.PromptOnly)
	}
	// Every v13 answer kind has a case, a limit, at least one negative, and at
	// least one reviewed positive or positive control.
	for _, kind := range []string{
		protocol.AnswerValue, protocol.AnswerNumber, protocol.AnswerMoney, protocol.AnswerDirection,
		protocol.AnswerList, protocol.AnswerDuration, protocol.AnswerDate, protocol.AnswerDecline,
		protocol.AnswerAbsence, protocol.AnswerClarify, protocol.AnswerAcknowledge, protocol.AnswerChitchat,
		protocol.AnswerPersistence, protocol.AnswerReversal,
	} {
		if _, ok := bank.Limits[kind]; !ok {
			t.Errorf("kind %s has no limit", kind)
		}
		cases, negatives := 0, 0
		for _, c := range bank.Cases {
			if kindOrValue(c.Case.AnswerKind) == kind {
				cases++
			}
		}
		for _, n := range bank.Negatives {
			if kindOrValue(n.Case.AnswerKind) == kind {
				negatives++
			}
		}
		if cases == 0 || negatives == 0 {
			t.Errorf("kind %s: cases=%d negatives=%d", kind, cases, negatives)
		}
	}
	// Claim kinds have a zero worst-strategy exposure; interaction-only kinds
	// publish theirs explicitly.
	for _, stat := range report.Kinds {
		if bank.InteractionOnly(stat.Kind) {
			continue
		}
		if stat.PassShare != 0 || stat.MeanCredit != 0 {
			t.Errorf("claim kind %s exposed: %+v", stat.Kind, stat)
		}
	}
	// The exact v12 minor-unit contradiction is a pinned v13 positive control.
	found := false
	for _, p := range bank.Positives {
		if p.Name == "minor-unit-bare" {
			found = true
			mc := p.Case
			mc.BenchVersion = protocol.BenchVersionV12
			if grade.Memory(mc, p.Response).Score != 0 {
				t.Fatal("the v12 minor-unit contradiction no longer reproduces at v12")
			}
		}
	}
	if !found {
		t.Fatal("minor-unit-bare positive control missing")
	}
}

// TestSyntheticRobustnessV12BankIsClean runs the v12-1 bank under the v12
// policy so the slot-scope contract has fixtures of its own.
func TestSyntheticRobustnessV12BankIsClean(t *testing.T) {
	report, err := syntheticRobustnessAudit(protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	if report.BankVersion != robustnessBankV12 || !report.CoverageOK || !report.WithinLimits || report.NegativeFailures != 0 || report.ReviewedPositiveFailures != 0 {
		t.Fatalf("v12 bank not clean: %+v", report)
	}
	if report.NegativeChecks != len(robustnessNegativesV1)+3 || report.ReviewedPositives != 1 {
		t.Fatalf("v12 bank shape drifted: %+v", report)
	}
}

// TestCannedAuditV13RegradesTheNewestGeneratableCorpus audits the generated v13
// corpus against the published <5% per-claim-kind ceiling. A status such as
// "closed" legitimately accepts "Done"; that is measured exposure, unlike the
// old declarative acknowledgement bug that credited unrelated value answers.
func TestCannedAuditV13RegradesTheNewestGeneratableCorpus(t *testing.T) {
	report, err := cannedAudit(protocol.BenchVersionV13, "small", 3)
	if err != nil {
		t.Fatal(err)
	}
	wantCorpus, _ := corpusVersionFor(protocol.BenchVersionV13)
	if report.BenchVersion != protocol.BenchVersionV13 || report.CorpusBenchVersion != wantCorpus || report.CorpusBenchVersion > protocol.BenchVersionV13 {
		t.Fatalf("corpus fallback drifted: %+v", report)
	}
	if !report.ClaimKindsWithinTarget || len(report.ClaimKindGates) == 0 {
		t.Fatalf("claim-kind gate: %+v", report.ClaimKindGates)
	}
	for _, gate := range report.ClaimKindGates {
		if gate.Kind == protocol.AnswerChitchat || gate.Kind == protocol.AnswerDecline || gate.Kind == protocol.AnswerAcknowledge {
			t.Fatalf("interaction-only kind %s gated as a claim kind", gate.Kind)
		}
		if gate.Target != grade.ClaimKindTargetShare || !gate.Within {
			t.Fatalf("claim kind gate failed: %+v", gate)
		}
	}
	bank, _ := bankForVersion(protocol.BenchVersionV13)
	prof, _ := gen.ProfileForVersion("small", protocol.BenchVersionV13)
	for seed := int64(1); seed <= 3; seed++ {
		a, err := gen.GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range a.MemoryCases {
			if c.QuestionType == gen.QTDeclarativeBehavior {
				if score, probe := bestCannedScore(c.MemoryCase, bank); score >= cannedPassScore {
					t.Fatalf("declarative value received acknowledgement credit: seed=%d probe=%s score=%g", seed, probe, score)
				}
			}
		}
	}
	// Full-profile v9 exposure is frozen: the v9 corpus keeps its pinned table
	// (TestCannedAuditV9FortySeedsBelowLaunchTarget) and its corpus version.
	v9, err := cannedAudit(protocol.BenchVersionV9, "small", 1)
	if err != nil {
		t.Fatal(err)
	}
	if v9.CorpusBenchVersion != protocol.BenchVersionV9 {
		t.Fatalf("v9 corpus version drifted: %+v", v9)
	}
}

func TestSyntheticRobustnessIsDeterministicAndV9Only(t *testing.T) {
	a, err := syntheticRobustnessAudit(protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	b, err := syntheticRobustnessAudit(protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatalf("same bank produced different reports:\n%+v\n%+v", a, b)
	}
	if _, err := syntheticRobustnessAudit(protocol.BenchVersionV8); err == nil || !strings.Contains(err.Error(), "no grader audit bank covers bench_version 8") {
		t.Fatalf("synthetic bank accepted v8: %v", err)
	}
	// v10 and v11 share v9's grading policy and therefore its bank.
	for _, v := range []int{protocol.BenchVersionV10, protocol.BenchVersionV11} {
		bank, err := bankForVersion(v)
		if err != nil || bank.Version != robustnessBankVersion {
			t.Fatalf("v%d resolved bank %q (%v), want %s", v, bank.Version, err, robustnessBankVersion)
		}
	}
}

func TestBestCannedScorePinsGenericKindExposure(t *testing.T) {
	tests := []struct {
		name      string
		mc        protocol.MemoryCase
		wantScore float64
		wantProbe string
	}{
		{
			name:      "v9 chitchat is bounded but passable",
			mc:        protocol.MemoryCase{BenchVersion: protocol.BenchVersionV9, AnswerKind: protocol.AnswerChitchat},
			wantScore: 0.5,
			wantProbe: "generic-okay",
		},
		{
			name:      "v9 acknowledgement does not accept decline first",
			mc:        protocol.MemoryCase{BenchVersion: protocol.BenchVersionV9, AnswerKind: protocol.AnswerAcknowledge},
			wantScore: 1,
			wantProbe: "completed-action",
		},
		{
			name:      "v9 decline remains visibly generic",
			mc:        protocol.MemoryCase{BenchVersion: protocol.BenchVersionV9, AnswerKind: protocol.AnswerDecline},
			wantScore: 1,
			wantProbe: "decline-text",
		},
		{
			name:      "persistence cannot pass without the case item",
			mc:        protocol.MemoryCase{BenchVersion: protocol.BenchVersionV9, AnswerKind: protocol.AnswerPersistence, AnswerItems: []string{"tennis"}},
			wantScore: 0,
		},
		{
			name:      "value cannot pass without the expected value",
			mc:        protocol.MemoryCase{BenchVersion: protocol.BenchVersionV9, AnswerKind: protocol.AnswerValue, ExpectedAnswer: "Lisbon"},
			wantScore: 0,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			score, probe := bestCannedScore(tc.mc, robustnessBankV9())
			if score != tc.wantScore || probe != tc.wantProbe {
				t.Fatalf("best=(%v,%q), want (%v,%q)", score, probe, tc.wantScore, tc.wantProbe)
			}
		})
	}
}

func TestCannedAuditV9FortySeedsBelowLaunchTarget(t *testing.T) {
	report, err := cannedAudit(protocol.BenchVersionV9, "full", 40)
	if err != nil {
		t.Fatal(err)
	}
	if report.BenchVersion != protocol.BenchVersionV9 || report.Seeds != 40 || report.SeedFirst != 1 || report.SeedLast != 40 {
		t.Fatalf("wrong audit identity: %+v", report)
	}
	if report.TotalCases == 0 || report.Passable == 0 {
		t.Fatalf("audit did not exercise canned exposure: %+v", report)
	}
	if !report.WithinTarget || report.Share >= cannedTargetShare {
		t.Fatalf("v9 canned-answer share %.6f does not beat %.6f: %+v", report.Share, cannedTargetShare, report)
	}
	if report.PassScore != cannedPassScore || report.TargetShare != cannedTargetShare {
		t.Fatalf("thresholds not published: %+v", report)
	}
	wantKinds := []cannedKindStat{
		{Kind: protocol.AnswerChitchat, Cases: 120, Passable: 120, Share: 1, ScoreMass: 60, MeanCredit: 0.5},
		{Kind: protocol.AnswerList, Cases: 1040, Passable: 1, Share: 1.0 / 1040.0, ScoreMass: 0.5, MeanCredit: 0.5 / 1040.0},
		{Kind: protocol.AnswerMoney, Cases: 2305},
		{Kind: protocol.AnswerNumber, Cases: 1682},
		{Kind: protocol.AnswerValue, Cases: 4893, Passable: 120, Share: 120.0 / 4893.0, ScoreMass: 120, MeanCredit: 120.0 / 4893.0},
	}
	if !reflect.DeepEqual(report.Kinds, wantKinds) {
		t.Fatalf("v9 40-seed per-kind table drifted:\n got %+v\nwant %+v", report.Kinds, wantKinds)
	}
	if report.TotalCases != 10040 || report.Passable != 241 || report.ScoreMass != 180.5 ||
		report.Share != 241.0/10040.0 || report.MeanCredit != 180.5/10040.0 {
		t.Fatalf("v9 40-seed overall table drifted: %+v", report)
	}
}

func TestCannedAuditAndOutputAreDeterministic(t *testing.T) {
	a, err := cannedAudit(protocol.BenchVersionV9, "small", 3)
	if err != nil {
		t.Fatal(err)
	}
	b, err := cannedAudit(protocol.BenchVersionV9, "small", 3)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatalf("same inputs produced different reports:\n%+v\n%+v", a, b)
	}
	for _, asJSON := range []bool{false, true} {
		var first, second bytes.Buffer
		if err := writeCannedReport(&first, a, asJSON); err != nil {
			t.Fatal(err)
		}
		if err := writeCannedReport(&second, b, asJSON); err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(first.Bytes(), second.Bytes()) {
			t.Fatalf("asJSON=%t output drift:\n%s\n%s", asJSON, first.Bytes(), second.Bytes())
		}
		if first.Len() == 0 {
			t.Fatalf("asJSON=%t wrote no output", asJSON)
		}
	}
}

func TestRunRejectsInvalidModeAndBounds(t *testing.T) {
	tests := []struct {
		name string
		args []string
		want string
	}{
		{"no mode", nil, "usage:"},
		{"version without seeds", []string{"-bench-version", "9"}, "requires both"},
		{"seeds without version", []string{"-seeds", "2"}, "requires both"},
		{"zero seeds", []string{"-bench-version", "9", "-seeds", "-1"}, "between 1 and 1000"},
		{"too many seeds", []string{"-bench-version", "9", "-seeds", "1001"}, "between 1 and 1000"},
		{"unknown version", []string{"-bench-version", "99", "-seeds", "1"}, "unsupported bench_version"},
		{"unknown run size", []string{"-bench-version", "9", "-seeds", "1", "-run-size", "huge"}, "unsupported run_size"},
		{"artifact only", []string{"-artifact", "x"}, "requires both"},
		{"transcript only", []string{"-transcripts", "x"}, "requires both"},
		{"mixed modes", []string{"-artifact", "x", "-transcripts", "y", "-bench-version", "9", "-seeds", "1"}, "mutually exclusive"},
		{"positional", []string{"-bench-version", "9", "-seeds", "1", "extra"}, "unexpected positional"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			err := run(tc.args, &stdout, &stderr)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error=%v, want substring %q (stderr=%q)", err, tc.want, stderr.String())
			}
		})
	}
}

func TestRunGeneratedJSONIsDeterministicAndParseable(t *testing.T) {
	args := []string{"-bench-version", "9", "-seeds", "2", "-run-size", "small", "-json"}
	var first, second bytes.Buffer
	firstErr := run(args, &first, &bytes.Buffer{})
	secondErr := run(args, &second, &bytes.Buffer{})
	// A deliberately undersized audit may miss the launch target. It must still
	// emit its complete machine-readable evidence before returning a failing
	// status, and both the evidence and the error must be deterministic.
	if firstErr == nil || secondErr == nil || firstErr.Error() != secondErr.Error() || !strings.Contains(firstErr.Error(), "exceeds target") {
		t.Fatalf("nondeterministic or missing target error: first=%v second=%v", firstErr, secondErr)
	}
	if !bytes.Equal(first.Bytes(), second.Bytes()) {
		t.Fatalf("CLI output drifted:\n%s\n%s", first.Bytes(), second.Bytes())
	}
	var report generatedAndRobustnessReport
	if err := json.Unmarshal(first.Bytes(), &report); err != nil {
		t.Fatalf("invalid JSON %q: %v", first.String(), err)
	}
	if report.GeneratedCorpus.BenchVersion != 9 || report.GeneratedCorpus.Seeds != 2 || report.GeneratedCorpus.RunSize != "small" ||
		len(report.GeneratedCorpus.Probes) != len(generatedCannedProbes(robustnessBankV9())) || report.Synthetic.BankVersion != robustnessBankVersion || !report.Synthetic.CoverageOK {
		t.Fatalf("missing report identity: %+v", report)
	}
	generatedNames := map[string]bool{}
	for _, name := range report.GeneratedCorpus.Probes {
		generatedNames[name] = true
	}
	for _, name := range []string{"prompt-echo", "prompt-ack", "prompt-decline", "prompt-persistence", "prompt-reversal"} {
		if !generatedNames[name] {
			t.Errorf("generated-corpus gate omitted prompt-only strategy %q", name)
		}
	}
}

func TestRunTranscriptModeRemainsBackwardCompatible(t *testing.T) {
	tmp := t.TempDir()
	artifactPath := filepath.Join(tmp, "dataset.json")
	transcriptPath := filepath.Join(tmp, "transcripts.jsonl")
	artifact := gen.DatasetArtifact{MemoryCases: []gen.ArtifactCase{{MemoryCase: protocol.MemoryCase{
		ID: "c1", QuestionType: "recall", Question: "Where?", ExpectedAnswer: "Lisbon",
	}}}}
	ab, err := json.Marshal(artifact)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(artifactPath, ab, 0o600); err != nil {
		t.Fatal(err)
	}
	tl := transcriptLine{CaseID: "c1", Response: protocol.RunResponse{FinalText: "Porto"}}
	tb, err := json.Marshal(tl)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(transcriptPath, append(tb, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	if err := run([]string{"-artifact", artifactPath, "-transcripts", transcriptPath}, &stdout, &stderr); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout.String(), "case_id\tquestion_type") || !strings.Contains(stdout.String(), "c1\trecall\tvalue") {
		t.Fatalf("legacy TSV output changed: %q", stdout.String())
	}
	if !strings.Contains(stderr.String(), "1 candidate false negatives") {
		t.Fatalf("legacy summary changed: %q", stderr.String())
	}
}
