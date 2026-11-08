package toolexec

import (
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func encounterTestCase(seed int64) protocol.ToolCase {
	plan := catalog.V16ConnectorPlanForSeed(seed)
	linked := plan.Linked[0]
	return protocol.ToolCase{
		ID:            "enc-1",
		Category:      "encounter_job_followthrough",
		ExpectedTools: []protocol.ToolSpec{{Name: "get_agent_job_status"}},
		Encounter: &protocol.Encounter{
			Rules: []protocol.EncounterRule{
				{Tool: "run_workflow", Contains: []string{"wf-9000"}, Error: "Workflow wf-9000 is archived and cannot run."},
				{Tool: "run_workflow", Result: "Started as job job-12345."},
				{Tool: "get_agent_job_status", Contains: []string{"job-12345"}, MinPrior: 1, Result: "Job job-12345 completed: 4,821 new signups."},
				{Tool: "get_agent_job_status", Contains: []string{"job-12345"}, Result: "Job job-12345 is still running."},
				{Tool: "get_agent_job_status", Error: "Job not found."},
				{Tool: linked.WriteTool, Result: "accepted"},
			},
			Fallback: "Nothing matched.",
		},
	}
}

// An encounter fixture serves its argument-keyed world: a job is running on
// the first poll and finished on the second, an id that was never served is
// not found, an unmatched served tool gets the fallback, and the run's
// refusals still apply (an unlinked app stays "not connected").
func TestV18EncounterFixtureServesTheMockWorld(t *testing.T) {
	const seed = 7
	tc := encounterTestCase(seed)
	srv := NewServer()
	srv.Register(tc.ID, BuildFixtureForVersion(seed, tc, protocol.BenchVersionV18))
	ts := httptest.NewServer(srv)
	defer ts.Close()

	if got := v13Post(t, ts, tc.ID, "run_workflow", `{"recipe_id":"wf-9000"}`); !strings.Contains(got.Error, "archived") {
		t.Fatalf("archived run: %+v", got)
	}
	if got := v13Post(t, ts, tc.ID, "run_workflow", `{"recipe_id":"wf-1000"}`); got.Result != "Started as job job-12345." {
		t.Fatalf("run: %+v", got)
	}
	if got := v13Post(t, ts, tc.ID, "get_agent_job_status", `{"job_id":"job-1234"}`); got.Error != "Job not found." {
		t.Fatalf("a prefix of the id matched: %+v", got)
	}
	if got := v13Post(t, ts, tc.ID, "get_agent_job_status", `{"job_id":"job-12345"}`); !strings.Contains(got.Result, "completed") {
		// The not-found poll above is the first call of this tool, so the
		// matching poll is the second and the job has finished.
		t.Fatalf("second poll: %+v", got)
	}
	if got := v13Post(t, ts, tc.ID, "search_web", `{"queries":["anything"]}`); got.Result != "Nothing matched." {
		t.Fatalf("fallback: %+v", got)
	}
	unlinked := catalog.V16ConnectorPlanForSeed(seed).Named[0]
	if got := v13Post(t, ts, tc.ID, unlinked.ReadTool, `{}`); !strings.Contains(got.Error, "not connected") {
		t.Fatalf("unlinked connector served inside an encounter: %+v", got)
	}
	if got := v13Post(t, ts, tc.ID, "search_memories", `{}`); got.Error == "" {
		t.Fatalf("memory tool served: %+v", got)
	}
	if n := len(srv.Observed(tc.ID)); n != 7 {
		t.Fatalf("observed %d calls, want 7", n)
	}
}

// The first poll of a fresh case is still running.
func TestV18EncounterJobIsRunningOnFirstPoll(t *testing.T) {
	const seed = 7
	tc := encounterTestCase(seed)
	srv := NewServer()
	srv.Register(tc.ID, BuildFixtureForVersion(seed, tc, protocol.BenchVersionV18))
	ts := httptest.NewServer(srv)
	defer ts.Close()
	if got := v13Post(t, ts, tc.ID, "get_agent_job_status", `{"job_id":"job-12345"}`); !strings.Contains(got.Result, "still running") {
		t.Fatalf("first poll: %+v", got)
	}
	if got := v13Post(t, ts, tc.ID, "get_agent_job_status", `{"job_id":"job-12345"}`); !strings.Contains(got.Result, "4,821") {
		t.Fatalf("second poll: %+v", got)
	}
}

// Below v18 the encounter is ignored and the case serves the ordinary
// fixture.
func TestV18EncounterIsIgnoredBelowV18(t *testing.T) {
	tc := encounterTestCase(7)
	if f := BuildFixtureForVersion(7, tc, protocol.BenchVersionV17); f.Encounter() != nil {
		t.Fatal("v17 fixture carries the encounter")
	}
}

func TestEncounterMatchingIsBoundedAndSeparatorBlind(t *testing.T) {
	cases := []struct {
		text, value string
		want        bool
	}{
		{"Balance $18,420 due", "18420", true},
		{"Balance 18420 due", "18,420", true},
		{"Balance 184,200 due", "18420", false},
		{"job-123456", "job-12345", false},
		{"see JOB-12345.", "job-12345", true},
		{"vel‑4821", "VEL-4821", true},
		{"dana.ruiz@velmora.com", "dana.ruiz@velmora.com", true},
		// PR #3430 review cases.
		{"VEL\u20154821", "VEL-4821", true},        // horizontal bar
		{"VEL-48\u00ad21", "VEL-4821", true},       // soft hyphen
		{"US$18,420", "18420", true},               // country-prefixed currency
		{"18\u2009420", "18420", true},             // thin-space thousands
		{"\u201cVEL-4821\u201d", "VEL-4821", true}, // curly quotes are boundaries
		{"https://x.com/pricing/notice-7", "https://x.com/pricing", false},
		{"see https://x.com/pricing/ now", "https://x.com/pricing", true},
		{"https://x.com/pricing.", "https://x.com/pricing", true},
		{"https://x.com/pricing?plan=pro", "https://x.com/pricing", false},
	}
	for _, c := range cases {
		if got := protocol.EncounterContains(c.text, c.value); got != c.want {
			t.Errorf("EncounterContains(%q, %q) = %v, want %v", c.text, c.value, got, c.want)
		}
	}
	if !protocol.EncounterArgsMatch([]byte(`{"urls":["https://a.com/x","https://b.com/y"]}`), []string{"https://b.com/y"}) {
		t.Error("array argument not matched")
	}
	if !protocol.EncounterArgsMatch([]byte(`{"n":18420}`), []string{"18,420"}) {
		t.Error("numeric argument not matched")
	}
}

func TestEncounterArgTextKeepsFalseAndNull(t *testing.T) {
	if !protocol.EncounterArgsMatch([]byte(`{"archived":false,"note":null}`), []string{"false", "null"}) {
		t.Fatal("false/null arguments not matched")
	}
}

// The first matching rule decides; a matching Page rule's Error fails the
// call; several Page rules of one call are joined.
func TestEncounterRespondOrdering(t *testing.T) {
	e := &protocol.Encounter{Rules: []protocol.EncounterRule{
		{Tool: "read_links", Contains: []string{"https://x.com/gone"}, Error: "page not found"},
		{Tool: "read_links", Page: true, Contains: []string{"https://x.com/a"}, Result: "A"},
		{Tool: "read_links", Page: true, Contains: []string{"https://x.com/b"}, Result: "B"},
		{Tool: "read_links", Page: true, Contains: []string{"https://x.com/broken"}, Error: "broken page"},
	}}
	if _, errText, _ := e.Respond("read_links", []byte(`{"urls":["https://x.com/gone","https://x.com/a"]}`), 0); errText != "page not found" {
		t.Fatalf("an earlier non-page rule was bypassed: %q", errText)
	}
	if got, _, _ := e.Respond("read_links", []byte(`{"urls":["https://x.com/a","https://x.com/b"]}`), 0); !strings.Contains(got, "A") || !strings.Contains(got, "B") {
		t.Fatalf("two pages not joined: %q", got)
	}
	if _, errText, _ := e.Respond("read_links", []byte(`{"urls":["https://x.com/a","https://x.com/broken"]}`), 0); errText != "broken page" {
		t.Fatalf("a page error was discarded: %q", errText)
	}
}
