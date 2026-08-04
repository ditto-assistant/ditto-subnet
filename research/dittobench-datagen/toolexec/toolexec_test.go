package toolexec

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func webCase(id string) protocol.ToolCase {
	return protocol.ToolCase{
		ID:            id,
		Category:      "web_search",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
		MaxToolCalls:  1,
	}
}

func TestServesAndObservable(t *testing.T) {
	if Serves("search_memories") || Serves("fetch_memories") {
		t.Fatal("memory tools must not be served")
	}
	if !Serves("search_web") || !Serves("get_agent_job_status") || !Serves("set_theme") {
		t.Fatal("external-world tools must be served")
	}
	if Serves("") {
		t.Fatal("empty tool name is not served")
	}

	if !Observable(webCase("w")) {
		t.Fatal("all-served case should be observable")
	}
	memCase := protocol.ToolCase{ExpectedTools: []protocol.ToolSpec{{Name: "search_memories"}}}
	if Observable(memCase) {
		t.Fatal("memory case is not observable")
	}
	mixed := protocol.ToolCase{ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}, {Name: "search_memories_in_subjects"}}}
	if Observable(mixed) {
		t.Fatal("mixed case with a memory tool is not observable")
	}
	if Observable(protocol.ToolCase{}) {
		t.Fatal("no-expected-tool case is not observable")
	}
}

func TestBuildFixtureDeterministic(t *testing.T) {
	c := webCase("web_search-42-0001")
	a := BuildFixture(42, c)
	b := BuildFixture(42, c)
	if a.NeedleText() != b.NeedleText() {
		t.Fatalf("same seed must give same needle: %q vs %q", a.NeedleText(), b.NeedleText())
	}
	if a.NeedleText() == "" || a.NeedleValue() == "" || a.Subject() == "" {
		t.Fatal("content case should carry a needle (subject + value + text)")
	}
	// Different seed → (almost surely) different needle.
	if BuildFixture(43, c).NeedleText() == a.NeedleText() {
		t.Fatal("different seed should change the needle")
	}
	// NeedleFor is the shared deriver both the fixture and prompt use, so the
	// question and served answer stay coherent.
	if NeedleFor(42, c.ID).Value != a.NeedleValue() {
		t.Fatal("NeedleFor must match the fixture's served needle")
	}
	// A settings case carries no needle (confirmation-only tool).
	settings := protocol.ToolCase{ID: "settings-1-0", ExpectedTools: []protocol.ToolSpec{{Name: "set_theme"}}}
	if BuildFixture(1, settings).NeedleText() != "" {
		t.Fatal("settings case should carry no needle")
	}
}

func TestV8WorldNeedlesKeepOneSeedLocalFactPerSubject(t *testing.T) {
	const seed int64 = 123456789
	seen := map[string]Needle{}
	for i := 0; i < 5_000; i++ {
		needle := NeedleForV8World(seed, fmt.Sprintf("case-%d", i))
		if prior, ok := seen[needle.Subject]; ok && (prior.Value != needle.Value || prior.Unit != needle.Unit) {
			t.Fatalf("subject %q changed fact within one seed: %+v then %+v", needle.Subject, prior, needle)
		}
		seen[needle.Subject] = needle
	}
	for subject, needle := range seen {
		noun := subject[strings.LastIndex(subject, " ")+1:]
		compatible := false
		for _, unit := range unitsForNoun[noun] {
			compatible = compatible || unit == needle.Unit
		}
		if !compatible {
			t.Fatalf("subject %q has incompatible unit %q", subject, needle.Unit)
		}
	}
}

func TestResultDeterministicAndTyped(t *testing.T) {
	f := BuildFixture(7, webCase("web_search-7-0"))
	r1, ok := f.Result("search_web", nil)
	if !ok || r1 == "" {
		t.Fatal("search_web should return content")
	}
	if r2, _ := f.Result("search_web", nil); r2 != r1 {
		t.Fatal("same call must be deterministic")
	}
	// The needle value (the distinctive number) is present in a content result.
	nv := f.NeedleValue()
	if nv == "" || !strings.Contains(r1, nv) {
		t.Fatalf("needle value %q should appear in web result %q", nv, r1)
	}
	// A non-served (memory) tool is not answered.
	if _, ok := f.Result("search_memories", nil); ok {
		t.Fatal("memory tool must not be served")
	}
}

func TestResultDistinctByArgs(t *testing.T) {
	f := BuildFixture(9, protocol.ToolCase{ID: "multi_web_read-9-0",
		ExpectedTools: []protocol.ToolSpec{{Name: "read_links"}}})
	a, _ := f.Result("read_links", json.RawMessage(`{"url":"https://a.example"}`))
	b, _ := f.Result("read_links", json.RawMessage(`{"url":"https://b.example"}`))
	if a == b {
		t.Fatal("read_links on different URLs should vary")
	}
	// ...but stable per arg set (and order-independent).
	b2, _ := f.Result("read_links", json.RawMessage(`{"url":"https://b.example"}`))
	if b != b2 {
		t.Fatal("same args must be stable")
	}
}

func TestServerObservesAndServes(t *testing.T) {
	s := NewServer()
	c := webCase("web_search-1-0")
	s.Register(c.ID, BuildFixture(1, c))
	ts := httptest.NewServer(s)
	defer ts.Close()

	call := func(name string, hop int, args string) protocol.ToolExecResponse {
		body, _ := json.Marshal(protocol.ToolExecRequest{
			CaseID: c.ID, Name: name, Hop: hop, Args: json.RawMessage(args),
		})
		resp, err := http.Post(ts.URL, "application/json", bytes.NewReader(body))
		if err != nil {
			t.Fatalf("post: %v", err)
		}
		defer resp.Body.Close()
		var out protocol.ToolExecResponse
		if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
			t.Fatalf("decode: %v", err)
		}
		return out
	}

	if r := call("search_web", 0, `{"query":"x"}`); r.Result == "" || r.Error != "" {
		t.Fatalf("served tool should return a result, got %+v", r)
	}
	// A memory tool routed here is recorded but errored.
	if r := call("search_memories", 1, `{"query":"y"}`); r.Error == "" {
		t.Fatalf("memory tool should error, got %+v", r)
	}

	obs := s.Observed(c.ID)
	if len(obs) != 2 {
		t.Fatalf("expected 2 observed calls, got %d", len(obs))
	}
	if obs[0].Name != "search_web" || obs[1].Name != "search_memories" {
		t.Fatalf("observed order wrong: %+v", obs)
	}
	if s.Observed("no-such-case") != nil {
		t.Fatal("unknown case should observe nothing")
	}
}

func TestServerUnknownCase(t *testing.T) {
	s := NewServer()
	ts := httptest.NewServer(s)
	defer ts.Close()
	body, _ := json.Marshal(protocol.ToolExecRequest{CaseID: "ghost", Name: "search_web"})
	resp, err := http.Post(ts.URL, "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("unknown case should 404, got %d", resp.StatusCode)
	}
}

// TestJobChainDependency verifies the dependent-arg gate: get_agent_job_status
// yields the needle ONLY when queried with the job id execute_agent_job served.
func TestJobChainDependency(t *testing.T) {
	c := protocol.ToolCase{
		ID:       "cabc123",
		Category: "job_chain_result_usage",
		ExpectedTools: []protocol.ToolSpec{
			{Name: "execute_agent_job"}, {Name: "get_agent_job_status"},
		},
	}
	f := BuildFixture(4242, c)
	if !f.dependent {
		t.Fatal("job_chain category should build a dependent fixture")
	}
	if f.NeedleValue() == "" {
		t.Fatal("job_chain case should carry a needle")
	}

	// Step 1: dispatch returns the stable job id.
	disp, _ := f.Result("execute_agent_job", json.RawMessage(`{"task":"compute the Veltrix index"}`))
	if !strings.Contains(disp, f.jobID) {
		t.Fatalf("dispatch %q should contain the served job id %q", disp, f.jobID)
	}

	// Step 2a: correct id → needle revealed.
	ok, _ := f.Result("get_agent_job_status", json.RawMessage(`{"job_id":"`+f.jobID+`"}`))
	if !strings.Contains(ok, f.NeedleValue()) {
		t.Fatalf("status with the correct id should reveal the needle %q, got %q", f.NeedleValue(), ok)
	}

	// Step 2b: wrong id → no needle (cannot answer without chaining).
	bad, _ := f.Result("get_agent_job_status", json.RawMessage(`{"job_id":"job-00000"}`))
	if strings.Contains(bad, f.NeedleValue()) {
		t.Fatalf("status with a wrong id must NOT reveal the needle, got %q", bad)
	}
}

// TestLinkChainDependency verifies the v7 dependent link chain: search_web
// serves a stable page URL, and read_links reveals the needle ONLY when called
// with that URL. The bearer is read_links (the LAST content tool), so the
// search snippet carries the scored decoy, not the answer.
func TestLinkChainDependency(t *testing.T) {
	c := protocol.ToolCase{
		ID:       "clink01",
		Category: "link_chain_result_usage",
		ExpectedTools: []protocol.ToolSpec{
			{Name: "search_web"}, {Name: "read_links"},
		},
	}
	f := BuildFixture(7777, c)
	if !f.linkDep {
		t.Fatal("link_chain category should build a link-dependent fixture")
	}
	if f.Bearer() != "read_links" {
		t.Fatalf("bearer should be read_links, got %q", f.Bearer())
	}

	// Step 1: search returns the stable page URL and the DECOY (not the needle).
	search, _ := f.Result("search_web", json.RawMessage(`{"queries":["the Veltrix index"]}`))
	if !strings.Contains(search, f.pageURL) {
		t.Fatalf("search %q should contain the served page URL %q", search, f.pageURL)
	}
	if strings.Contains(search, f.NeedleValue()) {
		t.Fatalf("search snippet must NOT carry the needle %q, got %q", f.NeedleValue(), search)
	}

	// Step 2a: read the served URL → needle revealed.
	ok, _ := f.Result("read_links", json.RawMessage(`{"urls":["`+f.pageURL+`"]}`))
	if !strings.Contains(ok, f.NeedleValue()) {
		t.Fatalf("read of the served URL should reveal the needle %q, got %q", f.NeedleValue(), ok)
	}

	// Step 2b: read a different URL → no needle (cannot answer without threading
	// the served URL from the search result).
	bad, _ := f.Result("read_links", json.RawMessage(`{"urls":["https://elsewhere.example/other"]}`))
	if strings.Contains(bad, f.NeedleValue()) {
		t.Fatalf("read of a wrong URL must NOT reveal the needle, got %q", bad)
	}
}

// TestJobChainRecoveryComposition verifies the v7 composed hard case: the
// dependent job-id chain AND transient-error recovery apply at once.
func TestJobChainRecoveryComposition(t *testing.T) {
	c := protocol.ToolCase{
		ID:       "cjcr01",
		Category: "job_chain_recovery_result_usage",
		ExpectedTools: []protocol.ToolSpec{
			{Name: "execute_agent_job"}, {Name: "get_agent_job_status"},
		},
	}
	f := BuildFixture(31337, c)
	if !f.dependent || !f.recovery {
		t.Fatalf("category should be both dependent and recovery: dependent=%v recovery=%v", f.dependent, f.recovery)
	}
	s := NewServer()
	s.Register(c.ID, BuildFixture(31337, c))
	ts := httptest.NewServer(s)
	defer ts.Close()

	post := func(name, args string) protocol.ToolExecResponse {
		body, _ := json.Marshal(protocol.ToolExecRequest{CaseID: c.ID, Name: name, Args: json.RawMessage(args)})
		resp, err := http.Post(ts.URL, "application/json", bytes.NewReader(body))
		if err != nil {
			t.Fatalf("post: %v", err)
		}
		defer resp.Body.Close()
		var out protocol.ToolExecResponse
		if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
			t.Fatalf("decode: %v", err)
		}
		return out
	}
	disp := post("execute_agent_job", `{"task":"compute it"}`)
	if disp.Result == "" || !strings.Contains(disp.Result, f.jobID) {
		t.Fatalf("dispatch should return the job id, got %+v", disp)
	}
	// First status call with the correct id STILL flakes (recovery gate).
	first := post("get_agent_job_status", `{"job_id":"`+f.jobID+`"}`)
	if first.Error == "" {
		t.Fatalf("first status call should flake, got %+v", first)
	}
	// Retry with the correct id → needle.
	second := post("get_agent_job_status", `{"job_id":"`+f.jobID+`"}`)
	if !strings.Contains(second.Result, f.NeedleValue()) {
		t.Fatalf("retry with the correct id should serve the needle %q, got %q", f.NeedleValue(), second.Result)
	}
}

// TestErrorRecoveryGate verifies the first content-tool call flakes and the
// needle is served only on the retry.
func TestErrorRecoveryGate(t *testing.T) {
	c := protocol.ToolCase{
		ID:            "crec001",
		Category:      "web_recovery_result_usage",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
	}
	s := NewServer()
	s.Register(c.ID, BuildFixture(99, c))
	ts := httptest.NewServer(s)
	defer ts.Close()

	call := func() protocol.ToolExecResponse {
		body, _ := json.Marshal(protocol.ToolExecRequest{CaseID: c.ID, Name: "search_web", Args: json.RawMessage(`{"queries":["x"]}`)})
		resp, err := http.Post(ts.URL, "application/json", bytes.NewReader(body))
		if err != nil {
			t.Fatalf("post: %v", err)
		}
		defer resp.Body.Close()
		var out protocol.ToolExecResponse
		if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
			t.Fatalf("decode: %v", err)
		}
		return out
	}
	first := call()
	if first.Error == "" || first.Result != "" {
		t.Fatalf("first content-tool call should return a transient error, got %+v", first)
	}
	second := call()
	nv := BuildFixture(99, c).NeedleValue()
	if !strings.Contains(second.Result, nv) {
		t.Fatalf("retry should serve the needle %q, got %q", nv, second.Result)
	}
}

// TestNeedleGatedToBearer proves the AV-1 fix: the answer needle is served ONLY
// by the case's needle-bearing tool (the last expected content tool); other
// content tools serve a decoy number, never the needle.
func TestNeedleGatedToBearer(t *testing.T) {
	// A search-then-read case: read_links is the bearer, search_web must NOT leak.
	c := protocol.ToolCase{
		ID:            "mw",
		Category:      "multi_web_result_usage",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}, {Name: "read_links"}},
	}
	f := BuildFixture(7, c)
	if f.Bearer() != "read_links" {
		t.Fatalf("bearer should be the last content tool read_links, got %q", f.Bearer())
	}
	nv, dv := f.NeedleValue(), f.DecoyValue()
	if nv == "" || dv == "" || nv == dv {
		t.Fatalf("needle/decoy must be non-empty and distinct: needle=%q decoy=%q", nv, dv)
	}
	// The bearer serves the needle value.
	read, _ := f.Result("read_links", json.RawMessage(`{"url":"https://x.example"}`))
	if !strings.Contains(read, nv) {
		t.Fatalf("bearer read_links must serve the needle value %q: %q", nv, read)
	}
	// A non-bearer content tool serves the decoy, NOT the needle.
	web, _ := f.Result("search_web", json.RawMessage(`{"query":"anything"}`))
	if strings.Contains(web, nv) {
		t.Fatalf("non-bearer search_web must NOT serve the needle value %q: %q", nv, web)
	}
	if !strings.Contains(web, dv) {
		t.Fatalf("non-bearer search_web should serve the decoy value %q: %q", dv, web)
	}
}

// TestNeedleTemplateVariesBySeed proves the served needle is not a single fixed
// public template a lone regex can anchor on. The subject/value/unit and decoy
// clause fillers vary per seed regardless of template, so they are normalized to
// placeholders first: the diversity assertion is about the TEMPLATE pool itself,
// and a regression to one fixed sentence template fails even though the coined
// fillers still differ.
func TestNeedleTemplateVariesBySeed(t *testing.T) {
	c := protocol.ToolCase{ID: "w", Category: "web_result_usage", ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}}}
	seen := map[string]bool{}
	for s := int64(0); s < 40; s++ {
		f := BuildFixture(s, c)
		tmpl := f.NeedleText()
		for _, sub := range [][2]string{
			{f.Subject(), "<subject>"},
			{f.decoySubject(), "<decoy-subject>"},
			{f.NeedleValue(), "<value>"},
			{f.DecoyValue(), "<decoy>"},
			{f.needle.Unit, "<unit>"},
		} {
			tmpl = strings.ReplaceAll(tmpl, sub[0], sub[1])
		}
		if strings.ContainsAny(tmpl, "0123456789") {
			t.Fatalf("seed %d: normalization left a dynamic value in %q", s, tmpl)
		}
		seen[tmpl] = true
	}
	// The pool holds len(needleTemplates) (6) shapes; 40 seeds hash across all of
	// them, so fewer than 5 distinct normalized shapes means the pool shrank or
	// selection collapsed.
	if len(seen) < 5 {
		t.Fatalf("needle template should vary across seeds, only %d distinct: %v", len(seen), seen)
	}
}

func TestDecoySubjectNeverCollidesWithNeedle(t *testing.T) {
	collisions := 0
	for s := int64(1); s <= 100000; s++ {
		id := "c" + fmt.Sprintf("%016x", uint64(s)*0x9e3779b97f4a7c15)
		f := Fixture{seed: caseSeed(s, id), needle: NeedleFor(s, id), has: true}
		if f.decoySubject() == f.needle.Subject {
			collisions++
			if collisions <= 3 {
				t.Errorf("seed %d: decoy subject collides with needle subject %q", s, f.needle.Subject)
			}
		}
	}
	if collisions > 0 {
		t.Fatalf("%d/100000 fixtures had a decoy subject equal to the needle subject", collisions)
	}
}
