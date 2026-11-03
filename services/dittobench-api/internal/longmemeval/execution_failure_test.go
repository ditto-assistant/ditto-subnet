package longmemeval

import (
	"bytes"
	"context"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

// TestExecutionFailureNamesAMeterOutageAfterRun is the case bundle 2ede5da4
// could only report as "dimension_execution": provider work completed and the
// meter read that follows it failed. It must now name both where and what.
func TestExecutionFailureNamesAMeterOutageAfterRun(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	zero, err := newRecordingMeter(profile).Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// Snapshots: 0 initial, 1 after seed, 2 after run.
	meter := &scriptedMeter{
		snapshots: [][]ProviderEvidence{zero, zero},
		errors:    map[int]error{2: errors.New("trusted meter unavailable")},
	}
	executor := Executor{
		Harness: newStarterHarness(nil), Judge: &exactJudge{}, Meter: meter,
		Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64},
	}

	_, err = executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)

	requireExecutionFailureClass(t, err, "longmem_eval_accounting_run_meter_unavailable")
	// The message is untouched: the class is added beside it, not instead of it.
	requireErrorContains(t, err, "LongMemEval provider accounting after run: trusted meter unavailable")
}

// TestExecutionFailureNamesAnAccountingRegressionAfterJudge proves the kind is
// carried through the monotonic check, and that the phase tracks the operation.
func TestExecutionFailureNamesAnAccountingRegressionAfterJudge(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	zeroValues, err := newRecordingMeter(profile).Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	zero := append([]ProviderEvidence(nil), zeroValues...)
	afterRun := append([]ProviderEvidence(nil), zeroValues...)
	for index := range afterRun {
		if afterRun[index].Lane == ReaderLane {
			afterRun[index].Requests, afterRun[index].Successes, afterRun[index].ReceiptedRequests = 1, 1, 1
			afterRun[index].PromptTokens, afterRun[index].CompletionTokens, afterRun[index].TotalTokens = 10, 2, 12
			afterRun[index].CostUSDmicros = 5
			afterRun[index].ReceiptSetSHA256 = receiptDigestA
		}
	}
	// Reader counters fall back to zero after the judge: a regression.
	afterJudge := append([]ProviderEvidence(nil), zeroValues...)
	for index := range afterJudge {
		if afterJudge[index].Lane == JudgeLane {
			afterJudge[index].Requests, afterJudge[index].Successes, afterJudge[index].ReceiptedRequests = 1, 1, 1
			afterJudge[index].PromptTokens, afterJudge[index].CompletionTokens, afterJudge[index].TotalTokens = 3, 2, 5
			afterJudge[index].CostUSDmicros = 7
			afterJudge[index].ReceiptSetSHA256 = receiptDigestB
		}
	}
	meter := &scriptedMeter{snapshots: [][]ProviderEvidence{zero, zero, afterRun, afterJudge}}
	executor := Executor{
		Harness: newStarterHarness(nil), Judge: &exactJudge{}, Meter: meter,
		Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64},
	}

	_, err = executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)

	requireExecutionFailureClass(t, err, "longmem_eval_accounting_judge_regressed")
}

// TestExecutionFailureClassNeverReadsTheWrappedErrorText pins the privacy rule:
// the class comes from literals in this package, so private words in a wrapped
// harness error cannot reach the wire through it.
func TestExecutionFailureClassNeverReadsTheWrappedErrorText(t *testing.T) {
	err := executionFailure("run_opaque", "", errors.New("private submitted credential-material"))

	class, ok := ExecutionFailureClass(err)

	if !ok || class != "longmem_eval_run_opaque" {
		t.Fatalf("class = %q, %v; want longmem_eval_run_opaque", class, ok)
	}
	for _, forbidden := range []string{"private", "submitted", "credential"} {
		if strings.Contains(class, forbidden) {
			t.Fatalf("class %q derived from wrapped error text %q", class, forbidden)
		}
	}
}

// TestExecutionFailureClassDegradesRatherThanInventing pins both fail-closed
// edges: an unknown kind falls back to its phase, an unknown phase to nothing.
func TestExecutionFailureClassDegradesRatherThanInventing(t *testing.T) {
	if class, ok := ExecutionFailureClass(executionFailure("accounting_run", "no_such_kind", errors.New("x"))); !ok ||
		class != "longmem_eval_accounting_run" {
		t.Fatalf("unknown kind = %q, %v; want the phase alone", class, ok)
	}
	if class, ok := ExecutionFailureClass(executionFailure("no_such_phase", "", errors.New("x"))); ok {
		t.Fatalf("unknown phase produced %q", class)
	}
	if class, ok := ExecutionFailureClass(errors.New("untyped")); ok {
		t.Fatalf("untyped error produced %q", class)
	}
}

// TestEveryExecutionFailureCallSiteIsInTheVocabulary parses this package for
// every phase and kind literal passed to the failure constructors. A new
// failure point with a new literal must be added to the vocabulary -- and so
// to the shared Python allowlist and Platform's constraint -- or Platform
// rejects the hand-back that reports it, at the moment it reports it.
func TestEveryExecutionFailureCallSiteIsInTheVocabulary(t *testing.T) {
	phases, kinds := failureLiteralsInPackage(t)
	if len(phases) == 0 || len(kinds) == 0 {
		t.Fatalf("parse found phases=%d kinds=%d; the parse is wrong, not the code", len(phases), len(kinds))
	}
	known := make(map[string]bool)
	for _, phase := range executionPhasesWithoutKind {
		known[phase] = true
	}
	knownKinds := map[string]bool{"mixed_zero_lanes": true}
	for phase, list := range accountingKinds {
		known[phase] = true
		for _, kind := range list {
			knownKinds[kind] = true
		}
	}
	for _, phase := range phases {
		if !known[phase] {
			t.Errorf("phase %q is used at a call site but is not in the vocabulary", phase)
		}
	}
	for _, kind := range kinds {
		if !knownKinds[kind] {
			t.Errorf("accounting kind %q is used at a call site but is not in the vocabulary", kind)
		}
	}
	t.Logf("phases inspected: %d, kinds inspected: %d, classes: %d",
		len(phases), len(kinds), len(ExecutionFailureClasses()))
}

func requireExecutionFailureClass(t *testing.T, err error, want string) {
	t.Helper()
	if err == nil {
		t.Fatalf("Execute succeeded; want failure class %s", want)
	}
	class, ok := ExecutionFailureClass(err)
	if !ok || class != want {
		t.Fatalf("failure class = %q, %v; want %q (error: %v)", class, ok, want, err)
	}
}

func failureLiteralsInPackage(t *testing.T) (phases, kinds []string) {
	t.Helper()
	phaseSet, kindSet := map[string]bool{}, map[string]bool{}
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(token.NewFileSet(), name, nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", name, err)
		}
		ast.Inspect(file, func(node ast.Node) bool {
			call, ok := node.(*ast.CallExpr)
			if !ok {
				return true
			}
			ident, ok := call.Fun.(*ast.Ident)
			if !ok {
				return true
			}
			literal := func(index int) (string, bool) {
				if index >= len(call.Args) {
					return "", false
				}
				lit, ok := call.Args[index].(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					return "", false
				}
				value, err := strconv.Unquote(lit.Value)
				return value, err == nil && value != ""
			}
			switch ident.Name {
			case "executionFailure":
				if phase, ok := literal(0); ok {
					phaseSet[phase] = true
				}
				if kind, ok := literal(1); ok {
					kindSet[kind] = true
				}
			case "accountingFailure":
				if phase, ok := literal(0); ok {
					phaseSet[phase] = true
				}
			case "newAccountingError", "wrapAccountingError":
				if kind, ok := literal(0); ok {
					kindSet[kind] = true
				}
			}
			return true
		})
	}
	for phase := range phaseSet {
		phases = append(phases, phase)
	}
	for kind := range kindSet {
		kinds = append(kinds, kind)
	}
	sort.Strings(phases)
	sort.Strings(kinds)
	return phases, kinds
}
