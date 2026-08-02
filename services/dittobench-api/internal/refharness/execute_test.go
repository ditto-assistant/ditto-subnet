package refharness_test

import (
	"context"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/refharness"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// End-to-end of observed tool execution across three packages: the reference
// harness routes a prompt, EXECUTES the routed tools through the validator's mock
// endpoint, the server OBSERVES the trajectory, and the scorer grades the
// observation (not the self-report). Also checks the served needle flows back so
// the harness's answer can incorporate it (result-usage substrate).
func TestObservedExecutionRoundTrip(t *testing.T) {
	c := protocol.ToolCase{
		ID:            "web_search-7-0",
		Category:      "web_search",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
		MaxToolCalls:  1,
	}
	fixture := toolexec.BuildFixture(7, c)

	srv := toolexec.NewServer()
	srv.Register(c.ID, fixture)
	mux := http.NewServeMux()
	mux.Handle("POST /tool", srv)
	ts := httptest.NewServer(mux)
	defer ts.Close()
	endpoint := ts.URL + "/tool"

	// The harness routes the prompt to a tool, then executes it through the endpoint.
	prompt := "Search the web for the Veltrix index."
	calls := refharness.Route(prompt, catalog.Catalog())
	if len(calls) != 1 || calls[0].Name != "search_web" {
		t.Fatalf("expected route to search_web, got %v", calls)
	}
	result, err := refharness.Execute(context.Background(), endpoint, c.ID, "", calls)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if nv := fixture.NeedleValue(); nv == "" || !strings.Contains(result, nv) {
		t.Fatalf("served result %q should carry needle value %q", result, nv)
	}

	// The validator scores what it OBSERVED, and it saw the real call.
	observed := srv.Observed(c.ID)
	if len(observed) != 1 || observed[0].Name != "search_web" {
		t.Fatalf("expected 1 observed search_web, got %v", observed)
	}
	resp := protocol.RunResponse{FinalText: result, ToolCalls: calls}
	cs := scorer.ScoreToolCaseObserved(c, resp, true, observed)
	if cs.ToolScore == 0 {
		t.Fatalf("observed correct trajectory should score > 0, got %v", cs.ToolScore)
	}
}

// Full result-usage flow across datagen → toolexec → refharness → scorer: a
// generated result-usage case whose answer requires the fabricated needle value.
// The reference harness executes the tool, gets the needle in the result,
// incorporates it into its answer, and the scorer credits result-usage.
func TestResultUsageEndToEnd(t *testing.T) {
	// Pick a single-hop result-usage case the reference (token-overlap) router
	// actually routes to search_web — some result-usage templates omit the
	// "search"/"web" keywords the baseline keys on, and this test exercises the
	// execute→use→score path, which requires the harness to make the call.
	// Template draws are seed-dependent, so scan a few seeds for a routable one.
	var seed int64
	var ruc protocol.ToolCase
	var calls []protocol.ObservedToolCall
	for s := int64(20260707); s < 20260707+6 && ruc.ID == ""; s++ {
		r := rand.New(rand.NewSource(s))
		for _, c := range datagen.GenerateCases(r, s, 200) {
			if !datagen.IsResultUsage(c.Category) || len(c.ExpectedTools) != 1 { // single-hop web_result_usage
				continue
			}
			got := refharness.Route(c.Prompt, catalog.Catalog())
			if len(got) == 1 && got[0].Name == "search_web" {
				seed, ruc, calls = s, c, got
				break
			}
		}
	}
	if ruc.ID == "" {
		t.Fatal("no routable single-hop result-usage case generated across seeds")
	}
	fixture := toolexec.BuildFixture(seed, ruc)

	srv := toolexec.NewServer()
	srv.Register(ruc.ID, fixture)
	mux := http.NewServeMux()
	mux.Handle("POST /tool", srv)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	answer, err := refharness.Execute(context.Background(), ts.URL+"/tool", ruc.ID, "", calls)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	nv := fixture.NeedleValue()
	if nv == "" || !strings.Contains(answer, nv) {
		t.Fatalf("harness answer %q should carry needle value %q", answer, nv)
	}

	observed := srv.Observed(ruc.ID)
	cs := scorer.ScoreToolCaseObserved(ruc, protocol.RunResponse{FinalText: answer, ToolCalls: calls}, true, observed)
	cs = scorer.ComposeResultUsage(cs, answer, nv)
	if cs.ResultUsage != 1.0 {
		t.Fatalf("executed + used result should score usage 1.0, got %v (answer=%q)", cs.ResultUsage, answer)
	}

	// A harness that never executes cannot produce the fabricated value.
	noExec := scorer.ComposeResultUsage(
		scorer.ScoreToolCaseObserved(ruc, protocol.RunResponse{FinalText: "I think it's 5,000."}, true, nil),
		"I think it's 5,000.", nv)
	if noExec.ResultUsage != 0 {
		t.Fatalf("unexecuted harness should score usage 0, got %v", noExec.ResultUsage)
	}
}

// A harness that ignores the endpoint (endpoint unreachable / old harness) makes
// no observed calls, so an observable case is capped — even with a perfect
// self-reported trajectory and answer.
func TestUnobservedObservableCaseCapped(t *testing.T) {
	c := protocol.ToolCase{
		ID:            "web_search-7-1",
		Category:      "web_search",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
		MaxToolCalls:  1,
	}
	if !toolexec.Observable(c) {
		t.Fatal("web_search case should be observable")
	}
	// Perfect self-report, but nothing observed.
	self := protocol.RunResponse{FinalText: "ok", ToolCalls: []protocol.ObservedToolCall{{Name: "search_web"}}}
	cs := scorer.ScoreToolCaseObserved(c, self, true, nil)
	cs = scorer.FinishTool(cs)
	cs = scorer.CapUnobserved(cs)
	if cs.Score > scorer.UnobservedCeiling {
		t.Fatalf("unobserved observable case must be capped at %v, got %v", scorer.UnobservedCeiling, cs.Score)
	}
}
