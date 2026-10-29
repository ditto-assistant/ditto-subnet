package main

import (
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/longmemeval"
)

// TestExecutorStageClassesCoverEveryWrappedStage parses this package and the
// runtime factory for every string literal handed to
// wrapConfirmationExecutionFailure, and requires each one to have a wire slug.
//
// The map is the kind of allowlist that rots silently: a new stage added at a
// new failure point still compiles, still logs, and still reports -- as
// "unclassified", which the handler suppresses. The symptom is therefore the
// exact blindness this diagnostic exists to remove, and it would surface only
// as another undiagnosable production bundle costing a ~$14 reservation. Read
// the stages out of the source rather than restating them, so the test cannot
// agree with a stale copy of the list.
func TestExecutorStageClassesCoverEveryWrappedStage(t *testing.T) {
	stages := wrappedStagesInPackage(t)
	if len(stages) == 0 {
		// A parse that finds nothing would make every assertion below vacuous.
		t.Fatal("found no wrapConfirmationExecutionFailure call sites; the parse is wrong, not the code")
	}
	// tool_endpoint is answered earlier in confirmationExecutionDiagnostic with
	// the more specific longmem_run_tool_endpoint, so it must NOT have a stage
	// class; TestToolEndpointKeepsItsHarnessClass pins that behaviour.
	classedElsewhere := map[string]bool{"tool_endpoint": true}
	var missing []string
	for _, stage := range stages {
		if classedElsewhere[stage] {
			if _, ok := confirmationExecutorStageClasses[stage]; ok {
				t.Errorf("stage %q has both a harness class and a stage class", stage)
			}
			continue
		}
		if _, ok := confirmationExecutorStageClasses[stage]; !ok {
			missing = append(missing, stage)
		}
	}
	sort.Strings(missing)
	if len(missing) != 0 {
		t.Fatalf(
			"executor stages have no bounded failure class: %v\n"+
				"add each to confirmationExecutorStageClasses as longmem_exec_<stage>, and add the\n"+
				"same slug to CONFIRMATION_FAILURE_CLASS_VALUES in\n"+
				"packages/ditto-screening-protocol/ditto_screening_protocol/confirmation_transport.py\n"+
				"plus the Platform CHECK constraint, or Platform rejects the hand-back that reports it.",
			missing,
		)
	}
	// "execution" is confirmationExecutionStage's fallback and never appears as
	// a literal, so assert it separately rather than letting it go unchecked.
	if _, ok := confirmationExecutorStageClasses["execution"]; !ok {
		t.Error(`confirmationExecutionStage falls back to "execution"; it needs a class too`)
	}
	t.Logf("stages inspected: %d, all classed", len(stages))
}

// TestExecutorStageClassSlugsAreWellFormed keeps the wire vocabulary mechanical.
func TestExecutorStageClassSlugsAreWellFormed(t *testing.T) {
	if len(confirmationExecutorStageClasses) == 0 {
		t.Fatal("stage class map is empty")
	}
	seen := make(map[string]string, len(confirmationExecutorStageClasses))
	for stage, slug := range confirmationExecutorStageClasses {
		if want := "longmem_exec_" + stage; slug != want {
			t.Errorf("stage %q maps to %q, want %q", stage, slug, want)
		}
		if previous, clash := seen[slug]; clash {
			t.Errorf("slug %q is produced by both %q and %q", slug, previous, stage)
		}
		seen[slug] = stage
	}
}

// TestStageFailureIsReportedWithZeroStatus is the behavioural assertion: an
// executor-stage failure must now name its stage instead of being suppressed.
func TestStageFailureIsReportedWithZeroStatus(t *testing.T) {
	err := wrapConfirmationExecutionFailure(
		"dimension_execution", errors.New("provider accounting disagreed"),
	)

	class, status := confirmationExecutionDiagnostic(err)

	if class != "longmem_exec_dimension_execution" {
		t.Errorf("failure_class = %q, want longmem_exec_dimension_execution", class)
	}
	// Not an HTTP failure, so there is no status to report. 0 is this
	// contract's value for "no status existed", and the validator only accepts
	// 0 for a class in its zero-status set.
	if status != 0 {
		t.Errorf("failure_status = %d, want 0", status)
	}
}

// TestUnknownStageStillDegradesToUnclassified pins the fail-closed edge: a
// stage outside the map must not be forwarded, because the validator and
// Platform share a signed allowlist and would reject an unknown slug at exactly
// the moment a failure is being reported.
func TestUnknownStageStillDegradesToUnclassified(t *testing.T) {
	err := wrapConfirmationExecutionFailure("a_stage_nobody_declared", errors.New("boom"))

	class, status := confirmationExecutionDiagnostic(err)

	if class != "unclassified" {
		t.Errorf("failure_class = %q, want unclassified", class)
	}
	if status != 0 {
		t.Errorf("failure_status = %d, want 0", status)
	}
}

// TestHarnessDiagnosticStillWinsOverStage guards the precedence that matters:
// a harness-boundary failure is strictly more specific than the stage that
// contains it, so wrapping must not downgrade it to longmem_exec_*.
func TestHarnessDiagnosticStillWinsOverStage(t *testing.T) {
	harness := &longmemeval.HarnessCaseFailure{Kind: "http_status", StatusCode: 502}
	err := wrapConfirmationExecutionFailure("dimension_execution", harness)

	class, status := confirmationExecutionDiagnostic(err)

	if class != "longmem_run_http_status" {
		t.Errorf("failure_class = %q, want longmem_run_http_status", class)
	}
	if status != 502 {
		t.Errorf("failure_status = %d, want 502", status)
	}
}

// TestToolEndpointKeepsItsHarnessClass pins the one stage that is deliberately
// not a stage class, so a future edit cannot quietly demote it.
func TestToolEndpointKeepsItsHarnessClass(t *testing.T) {
	err := wrapConfirmationExecutionFailure("tool_endpoint", errors.New("bind failed"))

	class, status := confirmationExecutionDiagnostic(err)

	if class != "longmem_run_tool_endpoint" {
		t.Errorf("failure_class = %q, want longmem_run_tool_endpoint", class)
	}
	if status != 0 {
		t.Errorf("failure_status = %d, want 0", status)
	}
}

func wrappedStagesInPackage(t *testing.T) []string {
	t.Helper()
	const marker = "wrapConfirmationExecutionFailure"
	unique := make(map[string]struct{})
	for _, dir := range []string{"."} {
		entries, err := os.ReadDir(dir)
		if err != nil {
			t.Fatalf("read %s: %v", dir, err)
		}
		for _, entry := range entries {
			name := entry.Name()
			if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
				continue
			}
			fset := token.NewFileSet()
			file, err := parser.ParseFile(fset, filepath.Join(dir, name), nil, 0)
			if err != nil {
				t.Fatalf("parse %s: %v", name, err)
			}
			ast.Inspect(file, func(node ast.Node) bool {
				call, ok := node.(*ast.CallExpr)
				if !ok || len(call.Args) == 0 {
					return true
				}
				ident, ok := call.Fun.(*ast.Ident)
				if !ok || ident.Name != marker {
					return true
				}
				literal, ok := call.Args[0].(*ast.BasicLit)
				if !ok || literal.Kind != token.STRING {
					return true
				}
				stage, err := strconv.Unquote(literal.Value)
				if err == nil && stage != "" {
					unique[stage] = struct{}{}
				}
				return true
			})
		}
	}
	stages := make([]string, 0, len(unique))
	for stage := range unique {
		stages = append(stages, stage)
	}
	sort.Strings(stages)
	return stages
}
