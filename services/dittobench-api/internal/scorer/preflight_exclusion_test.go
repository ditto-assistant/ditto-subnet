package scorer

import (
	"bytes"
	"encoding/json"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestProtocolPreflightsAreExcludedFromEveryScorePopulation(t *testing.T) {
	base := []protocol.CaseScore{
		{CaseID: "tool-1", Category: "web_search", Kind: protocol.KindTool, Score: 0.8, ToolScore: 0.8, LatencyMs: 10},
		{CaseID: "memory-1", Category: "single_hop", Kind: protocol.KindMemory, Score: 0.6, Correct: true, LatencyMs: 20},
	}
	preflights := []protocol.CaseScore{
		{
			CaseID: "preflight:tool-reachability", Category: "web_search",
			Kind: protocol.KindTool, Score: 1, ToolScore: 1, Observed: true,
			Called: []string{"search_web"}, Expected: []string{"search_web"},
		},
		{
			CaseID: ModelRoutePreflightCaseID, Category: "single_hop",
			Kind: protocol.KindMemory, Score: 0, Injection: true,
		},
	}

	want := AggregateForVersion("run", base, protocol.BenchVersionV8)
	got := AggregateForVersion(
		"run", append(append([]protocol.CaseScore{}, preflights...), base...),
		protocol.BenchVersionV8,
	)
	wantBytes, err := json.Marshal(want)
	if err != nil {
		t.Fatal(err)
	}
	gotBytes, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(gotBytes, wantBytes) {
		t.Fatalf("preflight changed v8 report bytes\nwant %s\ngot  %s", wantBytes, gotBytes)
	}
	if got.N != len(base) || len(got.PerCase) != len(base) {
		t.Fatalf("preflight entered report population: n=%d per_case=%d", got.N, len(got.PerCase))
	}
}

func TestPreflightRecognitionIsReservedAndNarrow(t *testing.T) {
	for _, id := range []string{"preflight:legacy", ModelRoutePreflightCaseID} {
		if !IsProtocolPreflightCaseID(id) {
			t.Fatalf("reserved preflight id %q was not recognized", id)
		}
	}
	for _, id := range []string{"preflighted-user-case", ModelRoutePreflightCaseID + "-real"} {
		if IsProtocolPreflightCaseID(id) {
			t.Fatalf("ordinary case id %q was excluded", id)
		}
	}
}
