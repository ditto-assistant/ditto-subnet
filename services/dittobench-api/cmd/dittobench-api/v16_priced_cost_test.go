package main

import (
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// fullCatalogRun is a v15 full-catalog harness's whole-run relay interval:
// $1.82 per 350 cases at the locked rates, factor 0.6737.
func fullCatalogRun() *protocol.TokenUsage {
	return &protocol.TokenUsage{
		AccountingVersion: 2, Status: "complete", Requests: 2043, Successes: 2043, UsageAvailable: 2043,
		PromptTokens: 22_318_268, CompletionTokens: 1_690_233, TotalTokens: 22_318_268 + 1_690_233,
	}
}

func TestPricedCostIsAbsentBelowBench16(t *testing.T) {
	s := &server{}
	for v := protocol.BenchVersionV9; v < protocol.PricedCostMinBenchVersion; v++ {
		if got := s.v16PricedCostFor(v, scoregates.PricedCostEnforce, fullCatalogRun(), "", 350); got != nil {
			t.Fatalf("bench %d priced its run: %+v", v, got)
		}
	}
}

func TestPricedCostPricesTheWholeRunRelayInterval(t *testing.T) {
	s := &server{}
	got := s.v16PricedCostFor(protocol.DuelingMinBenchVersion, scoregates.PricedCostEnforce, fullCatalogRun(), "", 350)
	if got == nil || got.Result != scoregates.PricedCostResultPriced || got.Source != scoregates.PricedCostSourceRelay ||
		got.Window != scoregates.PricedCostWindowWholeRun || got.FactorBPS != 6737 || got.AppliedFactorBPS != 6737 || !got.Applied {
		t.Fatalf("priced cost = %+v", got)
	}
	incomplete := fullCatalogRun()
	incomplete.UsageAvailable--
	incomplete.UsageUnavailable = 1
	incomplete.Status = "incomplete"
	if got := s.v16PricedCostFor(16, scoregates.PricedCostEnforce, incomplete, "", 350); got.Result != scoregates.PricedCostResultInsufficientEvidence || got.AppliedFactorBPS != scoregates.BasisPointScale {
		t.Fatalf("incomplete accounting priced: %+v", got)
	}
	// A practice run without a broker session meters nothing.
	if got := s.v16PricedCostFor(16, scoregates.PricedCostEnforce, nil, "", 350); got.Result != scoregates.PricedCostResultInsufficientEvidence || got.AppliedFactorBPS != scoregates.BasisPointScale || got.Source != "" {
		t.Fatalf("unmetered practice run priced: %+v", got)
	}
}

// The factor rides in the datagen gate, so the fold that recomputes the
// composite from details.dueling reproduces the reported composite, and the
// column statistics a generator is judged by are untouched.
func TestPricedCostRidesInTheDuelingGate(t *testing.T) {
	s := &server{}
	summary := s.v16PricedCostFor(16, scoregates.PricedCostEnforce, fullCatalogRun(), "", 350)
	half := scorer.DatagenHalf{Raw: 0.82, RawStderr: 0.021, Gate: 0.9}
	cols := duelingFixtureColumns()
	priced := applyPricedCostToHalf(half, summary)
	if priced.Raw != half.Raw || priced.Gate != half.Gate*scoregates.PricedCostMultiplier(summary) {
		t.Fatalf("priced half = %+v", priced)
	}
	composite, _, err := duelingOrdinaryScore(priced, cols, sliceTestOwner)
	if err != nil {
		t.Fatal(err)
	}
	unpriced, _, _ := duelingOrdinaryScore(half, cols, sliceTestOwner)
	if math.Abs(composite-unpriced*0.6737) > 2e-6 {
		t.Fatalf("composite %v is not the unpriced %v x 0.6737", composite, unpriced)
	}
	slice := &duelingSlice{dataset: challenge.SliceDataset{SeasonID: sliceTestSeason, SeasonIndex: 3}, sha256: strings.Repeat("c", 64), ownerKey: sliceTestOwner}
	details := duelingReportDetails(slice, priced, nil, &protocol.V9BaseDetails{AppliedGateFactorBPS: 10000}, cols)
	if details.DatagenGate != priced.Gate || details.DatagenGate > 1 || details.DatagenGate <= 0 {
		t.Fatalf("datagen_gate = %v", details.DatagenGate)
	}
	refold := challenge.DuelingComposite(details.DatagenRaw, details.DatagenGate, duelingAggregates(details.Columns), map[string]bool{details.OwnerKey: true})
	if math.Abs(refold-composite) > 1e-6 {
		t.Fatalf("fold recomputes %v, report carries %v", refold, composite)
	}
	for i := range cols {
		if details.Columns[i] != cols[i] {
			t.Fatalf("column %d statistics moved: %+v vs %+v", i, details.Columns[i], cols[i])
		}
	}
	// Shadow reports the factor and leaves the gate alone.
	shadow := s.v16PricedCostFor(16, scoregates.PricedCostShadow, fullCatalogRun(), "", 350)
	if got := applyPricedCostToHalf(half, shadow); got.Gate != half.Gate || shadow.FactorBPS != 6737 || shadow.Applied {
		t.Fatalf("shadow moved the gate: %+v / %+v", got, shadow)
	}
}

func TestPricedCostScalesANonDuelingBench16Report(t *testing.T) {
	summary := (&server{}).v16PricedCostFor(16, scoregates.PricedCostEnforce, fullCatalogRun(), "", 350)
	report := applyPricedCostToReport(protocol.ScoreReport{Composite: 0.9, CompositeStderr: 0.01}, summary)
	if report.Composite != 0.60633 || report.CompositeStderr != 0.006737 {
		t.Fatalf("report = %+v", report)
	}
	if same := applyPricedCostToReport(protocol.ScoreReport{Composite: 0.9}, nil); same.Composite != 0.9 {
		t.Fatalf("nil summary moved the composite: %+v", same)
	}
}

// Unlike the datagen boundary capture, the priced usage reads the session's
// whole-run totals: slice completions are charged too.
func TestPricedCostSessionUsageIncludesSliceCompletions(t *testing.T) {
	broker, sessionID, stop := newCatalogCaptureBroker(t, protocol.DuelingMinBenchVersion, emitSetThemeCompletion)
	defer stop()
	s := &server{broker: broker}
	postCatalogChat(t, broker, openAICatalogRequest) // datagen
	_ = s.captureDatagenSessionTotals(protocol.DuelingMinBenchVersion, sessionID)
	postCatalogChat(t, broker, openAICatalogRequest) // slice
	got, ok := broker.sessionPricedUsage(sessionID)
	if !ok || got.promptTokens != 6 || got.completionTokens != 8 || got.usageUnavailable != 0 {
		t.Fatalf("session priced usage = %+v ok=%v", got, ok)
	}
	summary := s.v16PricedCostFor(16, scoregates.PricedCostEnforce, nil, sessionID, 2)
	if summary.Source != scoregates.PricedCostSourceBrokerSession || summary.PromptTokens != 6 || summary.CompletionTokens != 8 ||
		summary.Result != scoregates.PricedCostResultPriced {
		t.Fatalf("practice priced cost = %+v", summary)
	}
}

func TestDecisionLaneUsageIsBookedForPricing(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"model":   "typesafe/jev-1.14",
			"answers": map[string]any{"tool": map[string]any{"label": "save_memory", "p": 0.91}},
			"usage":   map[string]int{"input_tokens": 120, "output_tokens": 3},
		})
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", llm.V9AggregateProfileRevision, llm.HarnessModelForVersion(protocol.DuelingMinBenchVersion))
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.72", protocol.DuelingMinBenchVersion)
	for range 2 {
		if response := callDecision(broker, "192.0.2.72", `{"state":{},"questions":{}}`); response.Code != http.StatusOK {
			t.Fatalf("decision status=%d body=%s", response.Code, response.Body.String())
		}
	}
	got, ok := broker.sessionPricedUsage(prepared["session_id"])
	if !ok || got.decisionCalls != 2 || got.decisionInputTokens != 240 || got.decisionOutputTokens != 6 {
		t.Fatalf("decision usage = %+v", got)
	}
	summary := (&server{broker: broker}).v16PricedCostFor(16, scoregates.PricedCostEnforce, fullCatalogRun(), prepared["session_id"], 350)
	if summary.DecisionCostNanoUSD != 240*scoregates.PricedCostNanoUSDPerDecisionInputToken || summary.DecisionCalls != 2 {
		t.Fatalf("decision lane not priced: %+v", summary)
	}
}

// A decision call the relay metered but answered with an error is still
// priced; a 2xx without usage makes the priced cost insufficient evidence.
func TestDecisionUsageCompleteness(t *testing.T) {
	session := &brokerSession{decisionPendingBytes: 4 * 100}
	recordDecisionUsage(session, http.StatusTooManyRequests, []byte(`{"usage":{"input_tokens":50,"output_tokens":0}}`), 100)
	recordDecisionUsage(session, http.StatusBadGateway, []byte(`{"error":"down"}`), 100)
	if session.decisionCompleted != 0 || session.decisionInputTokens != 50 || session.decisionUsageMissing != 0 || session.decisionPendingBytes != 200 {
		t.Fatalf("non-2xx booking = %+v", session)
	}
	// Absent usage fields are not zero-token usage.
	recordDecisionUsage(session, http.StatusOK, []byte(`{"usage":{}}`), 100)
	if session.decisionCompleted != 1 || session.decisionUsageMissing != 1 {
		t.Fatalf("2xx without usage = completed %d missing %d", session.decisionCompleted, session.decisionUsageMissing)
	}
	// One call is still in flight: the snapshot prices its 100-byte upper bound.
	usage, _ := (&inferenceBroker{sessions: map[string]*brokerSession{"s": session}}).sessionPricedUsage("s")
	if usage.decisionInputTokens != 150 {
		t.Fatalf("in-flight decision not priced at its upper bound: %+v", usage)
	}
	// A cancelled call keeps its bound; a transport failure is not charged.
	settleDecisionWithoutUsage(session, 60, true)
	settleDecisionWithoutUsage(session, 40, false)
	if session.decisionPendingBytes != 0 || session.decisionInputTokens != 110 {
		t.Fatalf("settled without usage = %+v", session)
	}
	b := newInferenceBroker(1)
	b.sessions = map[string]*brokerSession{"s": session}
	got := (&server{broker: b}).v16PricedCostFor(16, scoregates.PricedCostEnforce, fullCatalogRun(), "s", 350)
	if got.Result != scoregates.PricedCostResultInsufficientEvidence || got.AppliedFactorBPS != scoregates.BasisPointScale {
		t.Fatalf("missing decision usage still priced: %+v", got)
	}
}
