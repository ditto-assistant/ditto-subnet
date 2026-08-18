package main

// Tests for the Bench v12 counterfactual RUNTIME (the relay side that populates
// the two per-case telemetry fields the v12 dependence gate reads). The gate
// itself is proven in v12_dependence_gate_test.go; here we prove the driver that
// FEEDS it under the CORRECTNESS-based verdict:
//
//   - a modeled genuine agent whose correctness COLLAPSES under full model
//     ablation settles every clean-correct case as dependent ->
//     SliceAttributionComplete=true -> the real gate assembly passes and the
//     composite is preserved;
//   - a modeled compute-then-launder harness that stays CORRECT under full model
//     ablation (a non-model re-insertion path) settles every case as independent
//     -> 0 dependent -> the gate floors the composite to 0.00;
//   - the pass is deterministic: same seed -> identical administration order and
//     identical verdicts;
//   - a case the relay cannot settle stays pending -> the gate fails closed.
//
// The modeled runner grades exactly the way the production grade closure does: a
// response with the expected answer scores 1.0 (correct), anything else 0.0.

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Sentinel errors the modeled runner uses to drive the administered-drain vs
// transport-gap discriminator in settleAblatedRun.
var (
	errModeledDrain        = errors.New("modeled retry-on-empty budget drain")
	errModeledTransportGap = errors.New("modeled relay/transport gap")
)

// counterfactualExpectedAnswer is the per-case correct answer the modeled grade
// closure checks for. A response carrying it grades correct (1.0).
func counterfactualExpectedAnswer(caseID string) string { return "answer-" + caseID }

// modeledCounterfactualRunner models the two harness archetypes by the CORRECTNESS
// of their answer under FULL model ablation. It draws the ablated completion from
// the SAME seeded ablation.Responder the production broker lease would serve to
// the harness (via AblatedChat, the empty completion), so its determinism is the
// real responder's determinism.
type modeledCounterfactualRunner struct {
	genuine  bool
	failCase string
	// drainCase models a retry-on-empty agent: the broker DOES serve it the ablated
	// completion (ChatApplied>0), but the harness then drains its budget / times out
	// and the re-run returns an error with no usable answer. The administered-drain
	// discriminator must settle it as ablated-incorrect -> dependent, not pending.
	drainCase string
	// transportGapCase models a genuine relay/transport gap: the counterfactual is
	// never administered (no ablated completion is served, ChatApplied==0) and the
	// re-run errors. The discriminator must leave it PENDING (fail open).
	transportGapCase string

	mu    sync.Mutex
	order []string
}

func (m *modeledCounterfactualRunner) CounterfactualResponse(_ context.Context, spec counterfactualCaseSpec, responder *ablation.Responder) (protocol.RunResponse, bool) {
	m.mu.Lock()
	m.order = append(m.order, spec.caseID)
	m.mu.Unlock()
	if spec.caseID == m.failCase {
		return protocol.RunResponse{}, false
	}
	if spec.caseID == m.transportGapCase {
		// The relay never served a single ablated completion: ChatApplied stays 0.
		// The re-run errored, so the discriminator keeps the case pending.
		return settleAblatedRun(protocol.RunResponse{}, errModeledTransportGap, responder.Snapshot().ChatApplied)
	}
	completion, err := responder.AblatedChat("test-model", uint64(len(spec.prompt))+1)
	if err != nil {
		return protocol.RunResponse{}, false
	}
	if spec.caseID == m.drainCase {
		// The ablated completion WAS served (ChatApplied>0), then the harness drained
		// its budget / timed out and the re-run errored without a usable answer.
		return settleAblatedRun(protocol.RunResponse{}, errModeledDrain, responder.Snapshot().ChatApplied)
	}
	if m.genuine {
		// A genuine agent has no model output to reason from (AblatedChat serves an
		// empty completion), so its answer collapses to that empty content ->
		// graded INCORRECT -> dependent.
		return protocol.RunResponse{Answer: strings.TrimSpace(completion.Choices[0].Message.Content)}, true
	}
	// Compute-then-launder: a non-model path re-inserts the deterministically
	// derived correct answer regardless of the (empty) completion, so the agent
	// stays CORRECT under ablation -> independent -> floored.
	return protocol.RunResponse{Answer: counterfactualExpectedAnswer(spec.caseID)}, true
}

// modelReachedRun builds N model-reached, non-excluded scored cases whose CLEAN
// response is correct for both archetypes (the counterfactual denominator is the
// clean-correct population). Each spec carries a grade closure that scores 1.0
// iff the response Answer is the case's expected answer. No counterfactual fields
// are set — the driver populates them.
func modelReachedRun(t *testing.T, n int, genuine bool) ([]protocol.CaseScore, []transcriptCase, []counterfactualCaseSpec) {
	t.Helper()
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]counterfactualCaseSpec, 0, n)
	for i := 0; i < n; i++ {
		caseID := fmt.Sprintf("case-%02d", i)
		prompt := "kv" + caseID + "=1"
		expected := counterfactualExpectedAnswer(caseID)
		grade := func(r protocol.RunResponse) float64 {
			if strings.TrimSpace(r.Answer) == expected {
				return 1.0
			}
			return 0.0
		}
		perCase = append(perCase, protocol.CaseScore{CaseID: caseID})
		transcripts = append(transcripts, transcriptCase{
			CaseID: caseID,
			// Clean response is correct for both archetypes.
			Response: protocol.RunResponse{Answer: expected},
			Execution: runner.CaseExecution{
				ModelInferenceObserved:   true,
				ModelAttributionComplete: true,
			},
		})
		specs = append(specs, counterfactualCaseSpec{caseID: caseID, prompt: prompt, grade: grade})
	}
	return perCase, transcripts, specs
}

// assembleFromTranscripts drives the REAL production gate assembly over the
// (now counterfactual-populated) per-case telemetry, mirroring assembleV12.
func assembleFromTranscripts(t *testing.T, perCase []protocol.CaseScore, transcripts []transcriptCase) (protocol.V9BaseDetails, scoregates.Score) {
	t.Helper()
	dependence := v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)
	gates, err := v9base.BuildGateEvidence(protocol.BenchVersionV12, perCase, passingModelTelemetry(len(perCase)), true, dependence...)
	if err != nil {
		t.Fatalf("BuildGateEvidence(v12) error = %v", err)
	}
	details, _, effective, err := v9base.Build(v9base.Inputs{
		RunID: "v12-cf-run", BenchVersion: protocol.BenchVersionV12,
		ArtifactSHA256:   "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		DatasetSHA256:    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		TranscriptSHA256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		Ordinary:         scoregates.Score{Composite: 0.997012, CompositeStderr: 0.01},
		Gates:            gates,
	})
	if err != nil {
		t.Fatalf("v9base.Build(v12) error = %v", err)
	}
	if err := v9base.Validate(details); err != nil {
		t.Fatalf("v9base.Validate(v12) error = %v", err)
	}
	return details, effective
}

func TestV12CounterfactualGenuineAgentPasses(t *testing.T) {
	const n = 12
	perCase, transcripts, specs := modelReachedRun(t, n, true)
	summary := populateV12Counterfactual(context.Background(), "run-genuine", 4242, protocol.BenchVersionV12, perCase, transcripts, specs, &modeledCounterfactualRunner{genuine: true})
	if summary.Eligible != n || summary.Administered != n || summary.Dependent != n || summary.Independent != 0 || summary.Excluded != 0 {
		t.Fatalf("genuine summary = %+v, want eligible=administered=dependent=%d, independent=excluded=0", summary, n)
	}

	dep := v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)
	if len(dep) != 1 || !dep[0].SliceAttributionComplete || dep[0].DependentCases != n || dep[0].EligibleCases != n {
		t.Fatalf("genuine dependence telemetry = %+v", dep)
	}
	details, effective := assembleFromTranscripts(t, perCase, transcripts)
	gdep := details.ScoreGates.ModelDependence
	if gdep.DependentCases != n || gdep.Result != string(scoregates.ResultPassed) || gdep.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("genuine gate did not pass: %+v", gdep)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("genuine composite not preserved: %v", effective.Composite)
	}
}

func TestV12CounterfactualLaundererFloored(t *testing.T) {
	const n = 12
	perCase, transcripts, specs := modelReachedRun(t, n, false)
	summary := populateV12Counterfactual(context.Background(), "run-launder", 4242, protocol.BenchVersionV12, perCase, transcripts, specs, &modeledCounterfactualRunner{genuine: false})
	if summary.Eligible != n || summary.Administered != n || summary.Dependent != 0 || summary.Independent != n {
		t.Fatalf("launderer summary = %+v, want eligible=administered=independent=%d dependent=0", summary, n)
	}

	details, effective := assembleFromTranscripts(t, perCase, transcripts)
	dep := details.ScoreGates.ModelDependence
	if dep == (protocol.V9ModelDependenceGateEvidence{}) {
		t.Fatal("v12 evidence omitted the model-dependence gate")
	}
	if dep.DependentCases != 0 || dep.IndependentCases != n || dep.Result != string(scoregates.ResultBelowThreshold) || dep.FactorBPS != 0 {
		t.Fatalf("launderer dependence not gated: %+v", dep)
	}
	if details.SemanticGateFactorBPS != 0 || details.AppliedGateFactorBPS != 0 {
		t.Fatalf("launderer composite not zeroed: semantic=%d applied=%d", details.SemanticGateFactorBPS, details.AppliedGateFactorBPS)
	}
	if effective != (scoregates.Score{}) {
		t.Fatalf("launderer effective score = %+v, want zero", effective)
	}
}

// A case whose CLEAN run was already incorrect carries no dependence evidence
// either way: it is administered (so slice attribution stays complete) but kept
// out of the dependent/independent tally. A genuine agent with one such case
// still passes on the remaining clean-correct, collapse-under-ablation cases.
func TestV12CounterfactualCleanIncorrectExcludedFromTally(t *testing.T) {
	const n = 10
	perCase, transcripts, specs := modelReachedRun(t, n, true)
	// Make case-03's clean response incorrect (empty answer grades 0).
	transcripts[3].Response = protocol.RunResponse{Answer: ""}
	summary := populateV12Counterfactual(context.Background(), "run-excluded", 55, protocol.BenchVersionV12, perCase, transcripts, specs, &modeledCounterfactualRunner{genuine: true})
	if summary.Administered != n || summary.Excluded != 1 || summary.Dependent != n-1 || summary.Independent != 0 {
		t.Fatalf("excluded summary = %+v, want administered=%d excluded=1 dependent=%d independent=0", summary, n, n-1)
	}
	if !transcripts[3].Execution.ModelCounterfactualObserved || transcripts[3].Execution.ModelCounterfactualDependent != nil {
		t.Fatalf("clean-incorrect case must be administered with a nil verdict: %+v", transcripts[3].Execution)
	}
	dep := v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)
	if len(dep) != 1 || !dep[0].SliceAttributionComplete || dep[0].EligibleCases != n-1 || dep[0].DependentCases != n-1 {
		t.Fatalf("excluded-case dependence telemetry = %+v, want eligible=dependent=%d, attribution complete", dep, n-1)
	}
	details, effective := assembleFromTranscripts(t, perCase, transcripts)
	gdep := details.ScoreGates.ModelDependence
	if gdep.Result != string(scoregates.ResultPassed) || gdep.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("excluded-case genuine agent did not pass: %+v", gdep)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("excluded-case composite not preserved: %v", effective.Composite)
	}
}

// A case the relay cannot settle stays pending, so SliceAttributionComplete is
// false and the gate fails closed even for an otherwise-genuine agent.
func TestV12CounterfactualUnsettledCaseFailsClosed(t *testing.T) {
	const n = 8
	perCase, transcripts, specs := modelReachedRun(t, n, true)
	summary := populateV12Counterfactual(context.Background(), "run-pending", 99, protocol.BenchVersionV12, perCase, transcripts, specs,
		&modeledCounterfactualRunner{genuine: true, failCase: "case-03"})
	if summary.Administered != n-1 {
		t.Fatalf("expected one unsettled case, got administered=%d", summary.Administered)
	}
	dep := v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)
	if len(dep) != 1 || dep[0].SliceAttributionComplete {
		t.Fatalf("pending case must leave slice attribution incomplete: %+v", dep)
	}
	gates, err := v9base.BuildGateEvidence(protocol.BenchVersionV12, perCase, passingModelTelemetry(n), true, dep...)
	if err != nil {
		t.Fatalf("BuildGateEvidence error = %v", err)
	}
	if gates.ModelDependence.Result != scoregates.ResultInsufficientEvidence || gates.ModelDependence.FactorBPS != 0 {
		t.Fatalf("unsettled v12 run not fail-closed: %+v", gates.ModelDependence)
	}
}

// Determinism: the same seed draws the same administration order and the same
// verdicts across independent runs.
func TestV12CounterfactualIsDeterministic(t *testing.T) {
	const n = 16
	verdicts := func() ([]string, []bool) {
		perCase, transcripts, specs := modelReachedRun(t, n, true)
		runner := &modeledCounterfactualRunner{genuine: true}
		populateV12Counterfactual(context.Background(), "run-det", 7, protocol.BenchVersionV12, perCase, transcripts, specs, runner)
		got := make([]bool, n)
		for i := range transcripts {
			if d := transcripts[i].Execution.ModelCounterfactualDependent; d != nil {
				got[i] = *d
			}
		}
		return runner.order, got
	}
	orderA, verdictsA := verdicts()
	orderB, verdictsB := verdicts()
	if len(orderA) != n || len(orderB) != n {
		t.Fatalf("administration count changed: %d vs %d", len(orderA), len(orderB))
	}
	for i := range orderA {
		if orderA[i] != orderB[i] {
			t.Fatalf("administration order differs at %d: %q vs %q", i, orderA[i], orderB[i])
		}
	}
	for i := range verdictsA {
		if verdictsA[i] != verdictsB[i] {
			t.Fatalf("verdict differs at case %d: %v vs %v", i, verdictsA[i], verdictsB[i])
		}
	}
	// A different seed must (with overwhelming probability) permute the order.
	perCase, transcripts, specs := modelReachedRun(t, n, true)
	other := &modeledCounterfactualRunner{genuine: true}
	populateV12Counterfactual(context.Background(), "run-det2", 8, protocol.BenchVersionV12, perCase, transcripts, specs, other)
	if fmt.Sprint(other.order) == fmt.Sprint(orderA) {
		t.Fatalf("distinct seeds produced identical administration order: %v", orderA)
	}
}

// The administered-vs-transport discriminator in isolation: a completed re-run
// grades as-is; an errored re-run that WAS administered the ablation
// (servedCompletions>0) settles dependent (empty response, ok=true); an errored
// re-run that served NOTHING (servedCompletions==0) stays pending (ok=false).
func TestSettleAblatedRunDiscriminator(t *testing.T) {
	// runErr == nil: the response is returned verbatim and settled.
	if resp, ok := settleAblatedRun(protocol.RunResponse{Answer: "x"}, nil, 0); !ok || resp.Answer != "x" {
		t.Fatalf("completed re-run must settle as-is: resp=%+v ok=%v", resp, ok)
	}
	// Administered then failed to answer: settle ablated-incorrect (empty) -> dependent.
	if resp, ok := settleAblatedRun(protocol.RunResponse{Answer: "stale"}, errModeledDrain, 3); !ok || resp.Answer != "" {
		t.Fatalf("administered-drain must settle empty/ok: resp=%+v ok=%v", resp, ok)
	}
	// Nothing administered: a genuine transport gap stays pending (fail open).
	if resp, ok := settleAblatedRun(protocol.RunResponse{}, errModeledTransportGap, 0); ok {
		t.Fatalf("transport gap must stay pending: resp=%+v ok=%v", resp, ok)
	}
}

// A retry-on-empty genuine agent: under full ablation the model returns nothing,
// so the harness retries every empty completion until its budget drains and the
// re-run errors -- but the ablation WAS administered (the broker served the empty
// completions). The administered-drain discriminator settles that case as
// ablated-incorrect -> dependent, so the run reaches full slice attribution and
// PASSES instead of being false-gated to 0. This is the red-dragon regression.
func TestV12CounterfactualAdministeredDrainSettlesDependent(t *testing.T) {
	const n = 10
	perCase, transcripts, specs := modelReachedRun(t, n, true)
	// case-04 is the retry-on-empty drain: served the ablation, then errors empty.
	summary := populateV12Counterfactual(context.Background(), "run-drain", 4242, protocol.BenchVersionV12, perCase, transcripts, specs,
		&modeledCounterfactualRunner{genuine: true, drainCase: "case-04"})
	if summary.Administered != n || summary.Dependent != n || summary.Independent != 0 || summary.Excluded != 0 {
		t.Fatalf("drain summary = %+v, want administered=dependent=%d independent=excluded=0", summary, n)
	}
	// The drained case must be administered with a dependent verdict, not pending.
	var drain transcriptCase
	for i := range transcripts {
		if transcripts[i].CaseID == "case-04" {
			drain = transcripts[i]
		}
	}
	if !drain.Execution.ModelCounterfactualObserved || drain.Execution.ModelCounterfactualDependent == nil || !*drain.Execution.ModelCounterfactualDependent {
		t.Fatalf("drained case not settled dependent: %+v", drain.Execution)
	}
	dep := v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)
	if len(dep) != 1 || !dep[0].SliceAttributionComplete || dep[0].DependentCases != n || dep[0].EligibleCases != n {
		t.Fatalf("drain dependence telemetry = %+v", dep)
	}
	details, effective := assembleFromTranscripts(t, perCase, transcripts)
	gdep := details.ScoreGates.ModelDependence
	if gdep.Result != string(scoregates.ResultPassed) || gdep.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("drain genuine agent did not pass: %+v", gdep)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("drain composite not preserved: %v", effective.Composite)
	}
}

// A genuine relay/transport gap (the broker never served an ablated completion
// for the case) must NOT be turned into a dependence verdict: it stays pending,
// so slice attribution is incomplete and the gate fails OPEN -- an honest agent
// is never penalized for a stack/relay gap.
func TestV12CounterfactualTransportGapStaysPending(t *testing.T) {
	const n = 8
	perCase, transcripts, specs := modelReachedRun(t, n, true)
	summary := populateV12Counterfactual(context.Background(), "run-gap", 99, protocol.BenchVersionV12, perCase, transcripts, specs,
		&modeledCounterfactualRunner{genuine: true, transportGapCase: "case-03"})
	if summary.Administered != n-1 {
		t.Fatalf("transport gap must leave one case unsettled: administered=%d", summary.Administered)
	}
	for i := range transcripts {
		if transcripts[i].CaseID == "case-03" && transcripts[i].Execution.ModelCounterfactualObserved {
			t.Fatalf("transport-gap case must not be administered: %+v", transcripts[i].Execution)
		}
	}
	dep := v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)
	if len(dep) != 1 || dep[0].SliceAttributionComplete {
		t.Fatalf("transport-gap case must leave slice attribution incomplete: %+v", dep)
	}
	gates, err := v9base.BuildGateEvidence(protocol.BenchVersionV12, perCase, passingModelTelemetry(n), true, dep...)
	if err != nil {
		t.Fatalf("BuildGateEvidence error = %v", err)
	}
	if gates.ModelDependence.Result != scoregates.ResultInsufficientEvidence || gates.ModelDependence.FactorBPS != 0 {
		t.Fatalf("transport-gap run not fail-open-pending: %+v", gates.ModelDependence)
	}
}

// Pre-v12 runs must not administer the counterfactual at all.
func TestV12CounterfactualInertBeforeV12(t *testing.T) {
	perCase, transcripts, specs := modelReachedRun(t, 4, true)
	for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV10, protocol.BenchVersionV11} {
		runner := &modeledCounterfactualRunner{genuine: true}
		summary := populateV12Counterfactual(context.Background(), "run-pre", 1, version, perCase, transcripts, specs, runner)
		if summary != (counterfactualSummary{}) || len(runner.order) != 0 {
			t.Fatalf("v%d administered a counterfactual: summary=%+v order=%v", version, summary, runner.order)
		}
	}
	for i := range transcripts {
		if transcripts[i].Execution.ModelCounterfactualObserved || transcripts[i].Execution.ModelCounterfactualDependent != nil {
			t.Fatalf("pre-v12 run wrote counterfactual fields on case %d", i)
		}
	}
}
