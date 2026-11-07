package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func v18EncounterCase() protocol.ToolCase {
	return protocol.ToolCase{
		ID: "enc", Category: "encounter_status_triage",
		ExpectedTools:   []protocol.ToolSpec{{Name: "search_web"}, {Name: "linear_create_issue"}},
		FuzzyTrajectory: true, AllowExtraTools: true, Unordered: true,
		Encounter: &protocol.Encounter{
			Archetype: "status_triage",
			Required: []protocol.EncounterCall{
				{Tool: "search_web", Contains: []string{"velmora"}},
				{Tool: "linear_create_issue", Contains: []string{"VEL-4821"}},
			},
			Forbidden: []protocol.EncounterCall{{Tool: "slack_post_message"}},
			Answer:    []protocol.EncounterAnswer{{Value: "VEL-4821", Hedges: []string{"VEL-9100"}}},
		},
	}
}

// The v18 grader routes an encounter to grade.Encounter on the OBSERVED
// calls: extra calls in any order are neutral, a forbidden call halves the
// score, and a self-reported trajectory earns no coverage.
func TestV18EncounterScoresOnObservedOutcome(t *testing.T) {
	c := v18EncounterCase()
	reply := protocol.RunResponse{FinalText: "The current incident is VEL-4821; I filed it in Linear."}
	observed := []protocol.ObservedToolCall{
		call("read_links", `{"urls":["https://status.velmora.io"]}`),
		call("linear_create_issue", `{"title":"Velmora outage VEL-4821"}`),
		call("search_web", `{"queries":["velmora status"]}`),
	}
	if cs := ScoreToolCaseObservedForVersion(c, reply, true, observed, ScopeScored, protocol.BenchVersionV18); cs.Score != 1 {
		t.Fatalf("honest out-of-order encounter scored %.3f: %v", cs.Score, cs.Notes)
	}
	withForbidden := append(append([]protocol.ObservedToolCall(nil), observed...), call("slack_post_message", `{"text":"hi"}`))
	if cs := ScoreToolCaseObservedForVersion(c, reply, true, withForbidden, ScopeScored, protocol.BenchVersionV18); cs.Score != 0.5 {
		t.Fatalf("forbidden call scored %.3f, want 0.5", cs.Score)
	}
	selfReported := reply
	selfReported.ToolCalls = observed
	if cs := ScoreToolCaseObservedForVersion(c, selfReported, true, nil, ScopeScored, protocol.BenchVersionV18); cs.Score != 0 {
		t.Fatalf("self-reported encounter scored %.3f", cs.Score)
	}
	hedged := protocol.RunResponse{FinalText: "It is VEL-4821, or maybe VEL-9100."}
	if cs := ScoreToolCaseObservedForVersion(c, hedged, true, observed, ScopeScored, protocol.BenchVersionV18); cs.Score != 0 {
		t.Fatalf("hedged answer scored %.3f", cs.Score)
	}
	partial := observed[1:2]
	if cs := ScoreToolCaseObservedForVersion(c, reply, true, partial, ScopeScored, protocol.BenchVersionV18); cs.Score != 0.5 {
		t.Fatalf("half the required calls scored %.3f, want 0.5", cs.Score)
	}
}
