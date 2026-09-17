package main

import (
	"reflect"
	"slices"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestApplyV13CatalogGateLeavesV12FrozenAndRecordsV13(t *testing.T) {
	full := catalog.CatalogForVersion(protocol.BenchVersionV12)
	c := protocol.ToolCase{ID: "chat", Category: "no_tool", Prompt: "Good morning!"}
	base := protocol.CaseScore{CaseID: "chat", Kind: protocol.KindTool, ToolScore: 1}
	zero := 0
	execution := runner.CaseExecution{Catalog: &protocol.CatalogEvidence{
		CompletionsTotal: &zero, Complete: true,
	}}
	if got := applyV13CatalogGate(protocol.BenchVersionV12, scorer.ScopeScored, scorer.CatalogGateEnforce, base, c, full, nil, execution); !reflect.DeepEqual(got, base) {
		t.Fatalf("v12 changed: %+v", got)
	}
	shadow := applyV13CatalogGate(protocol.BenchVersionV13, scorer.ScopeScored, scorer.CatalogGateShadow, base, c, full, nil, execution)
	if shadow.ToolScore != 1 || shadow.Catalog == nil || !slices.Contains(shadow.Catalog.Findings, scorer.CatalogFindingRestraintWithoutOffer) {
		t.Fatalf("shadow=%+v", shadow)
	}
	enforce := applyV13CatalogGate(protocol.BenchVersionV13, scorer.ScopeScored, scorer.CatalogGateEnforce, base, c, full, nil, execution)
	if enforce.ToolScore != 0 {
		t.Fatalf("enforce=%+v", enforce)
	}
}

func TestSummarizeV13CatalogGatePublishesSuppressionRateAndCoverage(t *testing.T) {
	if got := summarizeV13CatalogGate(protocol.BenchVersionV12, scorer.CatalogGateShadow, nil, nil); got != nil {
		t.Fatalf("v12 summary=%+v", got)
	}
	one, zero := 1, 0
	perCase := []protocol.CaseScore{
		{Kind: protocol.KindMemory, Catalog: &protocol.CatalogEvidence{CompletionsTotal: &one, Complete: true}},
		{Kind: protocol.KindTool, Catalog: &protocol.CatalogEvidence{CompletionsTotal: &one, Complete: true, CatalogPresent: true,
			Findings: []string{scorer.CatalogFindingSafeHarbor}}},
		{Kind: protocol.KindTool, Catalog: &protocol.CatalogEvidence{CompletionsTotal: &one, Complete: true,
			Findings: []string{scorer.CatalogFindingCatalogAbsent, scorer.CatalogFindingRestraintWithoutOffer, scorer.CatalogFindingZeroed}}},
		{Kind: protocol.KindTool, Catalog: &protocol.CatalogEvidence{CompletionsTotal: &zero, Complete: true,
			Findings: []string{scorer.CatalogFindingNoModelCompletion, scorer.CatalogFindingExpectedToolNotOffered}}},
		{Kind: protocol.KindTool, Catalog: &protocol.CatalogEvidence{Complete: false, Findings: []string{scorer.CatalogFindingEvidenceIncomplete}}},
		// Unattributed, but every candidate completion offered a catalog.
		{Kind: protocol.KindTool, Catalog: &protocol.CatalogEvidence{Complete: false, OverlapCompletions: 2, OverlapCompletionsWithCatalog: 2,
			CatalogPresentLowerBound: true, Findings: []string{scorer.CatalogFindingEvidenceIncomplete, scorer.CatalogFindingCatalogPresentLowerBound, scorer.CatalogFindingSafeHarbor}}},
		// Attributed (completions_total known) but the capture was not settled: an
		// unparseable body. Counted as coverage, excluded from the finding counts.
		{Kind: protocol.KindTool, Catalog: &protocol.CatalogEvidence{CompletionsTotal: &one, Complete: false,
			Findings: []string{catalogFindingUnparseableRequest, scorer.CatalogFindingEvidenceIncomplete}}},
		// Settled zero withheld under enforce: attribution rests on a claim.
		{Kind: protocol.KindTool, Catalog: &protocol.CatalogEvidence{CompletionsTotal: &one, Complete: true, ClaimAttributedCompletions: 1,
			Findings: []string{scorer.CatalogFindingCatalogAbsent, scorer.CatalogFindingRestraintWithoutOffer, scorer.CatalogFindingClaimUncorroborated}}},
		{Kind: protocol.KindTool},
	}
	totals := sessionCatalogTotals{Completions: 7, CompletionsWithCatalog: 4, Unattributed: 2}
	got := summarizeV13CatalogGate(protocol.BenchVersionV13, scorer.CatalogGateEnforce, perCase, &totals)
	want := &protocol.CatalogGateSummary{
		Posture: "enforce", ToolCases: 8, AttributedCases: 5, IncompleteCaptureCases: 1, LowerBoundCases: 1,
		NoCompletionCases: 1, CatalogAbsentCases: 2, CatalogSuppressionRate: 0.666667, SafeHarborCases: 1,
		RestraintWithoutOffer: 2, ExpectedToolNotOffered: 1, ZeroedCases: 1,
		ClaimUncorroboratedCases: 1, ClaimAttributedCompletions: 1,
		CompletionsTotal: 7, CompletionsUnattributed: 2, AttributionCoverageBPS: 6250,
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("summary=%+v want %+v", got, want)
	}
}
