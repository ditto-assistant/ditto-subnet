package toolexec

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// numberTokens extracts comma-formatted number tokens ("775,992", "4,022",
// "234") that are not attached to another digit run.
var numberTokens = regexp.MustCompile(`(?:^|[^\d,.+-])(\d{1,3}(?:,\d{3})*)(?:[^\d,.+-]|$)`)

// carriesFiveDigitFigure reports whether text holds a needle-shaped figure
// (100..99,999) as a standalone number token.
func carriesFiveDigitFigure(text string) bool {
	for _, m := range numberTokens.FindAllStringSubmatch(text, -1) {
		digits := strings.ReplaceAll(m[1], ",", "")
		if len(digits) >= 3 && len(digits) <= 5 {
			return true
		}
	}
	return false
}

func v13Post(t *testing.T, ts *httptest.Server, caseID, name, args string) protocol.ToolExecResponse {
	t.Helper()
	body, _ := json.Marshal(protocol.ToolExecRequest{CaseID: caseID, Name: name, Args: json.RawMessage(args)})
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

// TestBuildFixtureForVersionIsFrozenBelowV13: the versioned constructor is
// byte-for-byte BuildFixture for every earlier contract, including the served
// bytes for a coined name the legacy default branch answers with "Done.".
func TestBuildFixtureForVersionIsFrozenBelowV13(t *testing.T) {
	c := webCase("web_search-42-0001")
	for _, version := range []int{protocol.BenchVersionV2, protocol.BenchVersionV8, protocol.BenchVersionV12} {
		legacy := BuildFixture(42, c)
		versioned := BuildFixtureForVersion(42, c, version)
		if legacy.NeedleText() != versioned.NeedleText() || legacy.Bearer() != versioned.Bearer() {
			t.Fatalf("v%d fixture drifted from BuildFixture", version)
		}
		decoy := catalog.DecoysForSeed(42)[0]
		got, ok := versioned.Result(decoy.Name, nil)
		if !ok || got != "Done." {
			t.Fatalf("v%d served %q for an unknown tool, want the frozen \"Done.\"", version, got)
		}
		if msg, refused := versioned.unavailable(decoy.Name, nil); refused {
			t.Fatalf("v%d refused a call pre-v13: %q", version, msg)
		}
		got, _ = versioned.Result("list_workflows", nil)
		if got != "Saved workflows: weekly standup digest; invoice review; launch checklist." {
			t.Fatalf("v%d list_workflows changed: %q", version, got)
		}
	}
}

// TestV13DecoysNotConfiguredUnlessExpected: a decoy call on an ordinary case is
// recorded and answered with a "not configured" error; on the decoy-correct
// case the same decoy is the bearer and serves the needle.
func TestV13DecoysNotConfiguredUnlessExpected(t *testing.T) {
	const seed = int64(7)
	decoys := catalog.DecoysForSeed(seed)
	web := webCase("cweb")
	webFixture := BuildFixtureForVersion(seed, web, protocol.BenchVersionV13)
	decoyCase := protocol.ToolCase{
		ID: "cdecoy", Category: "decoy_" + decoys[0].Shape.Key + "_result_usage",
		ExpectedTools: []protocol.ToolSpec{{Name: decoys[0].Name}}, MaxToolCalls: 1,
	}
	decoyFixture := BuildFixtureForVersion(seed, decoyCase, protocol.BenchVersionV13)
	if !decoyFixture.has || decoyFixture.Bearer() != decoys[0].Name {
		t.Fatalf("decoy-correct case must carry a needle borne by the decoy: has=%v bearer=%q", decoyFixture.has, decoyFixture.Bearer())
	}
	if !Observable(decoyCase) {
		t.Fatal("decoy-correct case must be observable")
	}

	s := NewServer()
	s.Register(web.ID, webFixture)
	s.Register(decoyCase.ID, decoyFixture)
	ts := httptest.NewServer(s)
	defer ts.Close()

	for _, d := range decoys {
		r := v13Post(t, ts, web.ID, d.Name, `{"query":"x"}`)
		if r.Error == "" || !strings.Contains(r.Error, "not configured") || !strings.Contains(r.Error, d.Brand) {
			t.Fatalf("decoy %s on an ordinary case: %+v", d.Name, r)
		}
		if r.Result != "" {
			t.Fatalf("decoy must not serve content on an ordinary case: %+v", r)
		}
	}
	if got := s.Observed(web.ID); len(got) != len(decoys) {
		t.Fatalf("decoy calls must be recorded as ordinary calls: %d", len(got))
	}
	r := v13Post(t, ts, decoyCase.ID, decoys[0].Name, `{"query":"x"}`)
	if r.Error != "" || !strings.Contains(r.Result, decoyFixture.NeedleValue()) || !strings.Contains(r.Result, decoys[0].Brand) {
		t.Fatalf("decoy-correct case must serve the branded needle result: %+v (needle %s)", r, decoyFixture.NeedleValue())
	}
	// A second decoy on the decoy-correct case is still "not configured".
	if len(decoys) > 1 {
		if r := v13Post(t, ts, decoyCase.ID, decoys[1].Name, `{}`); !strings.Contains(r.Error, "not configured") {
			t.Fatalf("non-bearer decoy on a decoy-correct case: %+v", r)
		}
	}
}

// TestV13SettersValidateAgainstInventoryWithoutEchoingCanonical: an unlisted
// accent/font is refused with an error that carries the submitted value only;
// a listed value (any case) is applied and echoed canonically; theme/effort
// enforce the wire enum.
func TestV13SettersValidateAgainstInventoryWithoutEchoingCanonical(t *testing.T) {
	const seed = int64(11)
	inv := catalog.InventoryForSeed(seed)
	c := protocol.ToolCase{ID: "cset", Category: "discovery_accent_set", ExpectedTools: []protocol.ToolSpec{{Name: "discover_capabilities"}, {Name: "set_accent_color", RequiredArgs: map[string]string{"color": inv.AccentTargets()[0]}}}}
	f := BuildFixtureForVersion(seed, c, protocol.BenchVersionV13)
	s := NewServer()
	s.Register(c.ID, f)
	ts := httptest.NewServer(s)
	defer ts.Close()

	target := inv.AccentTargets()[0]
	alias, ok := catalog.AliasFor(target, inv.Accents, seed, "t")
	if !ok {
		t.Fatal("no alias")
	}
	r := v13Post(t, ts, c.ID, "set_accent_color", fmt.Sprintf(`{"color":%q}`, alias.Text))
	if r.Error == "" || !strings.Contains(r.Error, alias.Text) {
		t.Fatalf("misspelled accent must be refused naming the submitted value: %+v", r)
	}
	for _, option := range inv.Accents {
		if strings.EqualFold(option, alias.Text) {
			continue
		}
		if strings.Contains(strings.ToLower(r.Error), strings.ToLower(option)) {
			t.Fatalf("error text leaks canonical option %q: %q", option, r.Error)
		}
	}
	r = v13Post(t, ts, c.ID, "set_accent_color", fmt.Sprintf(`{"color":%q}`, strings.ToUpper(target)))
	if r.Error != "" || !strings.Contains(r.Result, target) {
		t.Fatalf("listed accent (any case) must be applied canonically: %+v", r)
	}
	font := inv.FontTargets()[0]
	if r := v13Post(t, ts, c.ID, "set_chat_font", fmt.Sprintf(`{"font":%q}`, strings.ToLower(font))); r.Error != "" || !strings.Contains(r.Result, font) {
		t.Fatalf("listed font must be applied: %+v", r)
	}
	if r := v13Post(t, ts, c.ID, "set_chat_font", `{"font":"Comic Sans"}`); r.Error == "" {
		t.Fatalf("unlisted font must be refused: %+v", r)
	}
	if r := v13Post(t, ts, c.ID, "set_theme", `{"theme":"neon"}`); r.Error == "" {
		t.Fatalf("out-of-enum theme must be refused: %+v", r)
	}
	if r := v13Post(t, ts, c.ID, "set_theme", `{"theme":"Midnight"}`); r.Error != "" {
		t.Fatalf("enum theme must be applied: %+v", r)
	}
	if r := v13Post(t, ts, c.ID, "set_reasoning_effort", `{"effort":"xhigh"}`); r.Error == "" {
		t.Fatalf("out-of-enum effort must be refused: %+v", r)
	}
	// discover_capabilities lists every canonical option.
	r = v13Post(t, ts, c.ID, "discover_capabilities", `{"query":"appearance"}`)
	for _, option := range append(append([]string{}, inv.Accents...), inv.Fonts...) {
		if !strings.Contains(r.Result, option) {
			t.Fatalf("discover_capabilities omits %q: %q", option, r.Result)
		}
	}
}

// TestV13CoinedFixturesCarryTheNeedleOnlyOnTheBearer: the coined list tools
// embed the needle when they are the case's bearer, embed the scored decoy (or
// no needle) otherwise, and differ per seed.
func TestV13CoinedFixturesCarryTheNeedleOnlyOnTheBearer(t *testing.T) {
	families := map[string]string{
		"schedules_result_usage":     "list_schedules",
		"tool_registry_result_usage": "search_tools",
		"sandbox_result_usage":       "run_code",
		"agent_jobs_result_usage":    "list_agent_jobs",
	}
	contentBySeed := map[string]map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		for family, tool := range families {
			c := protocol.ToolCase{ID: fmt.Sprintf("c%s", family), Category: family, ExpectedTools: []protocol.ToolSpec{{Name: tool}}, MaxToolCalls: 1}
			f := BuildFixtureForVersion(seed, c, protocol.BenchVersionV13)
			if !f.has || f.Bearer() != tool {
				t.Fatalf("seed %d %s: bearer=%q has=%v", seed, family, f.Bearer(), f.has)
			}
			served, ok := f.Result(tool, nil)
			if !ok || !strings.Contains(served, f.NeedleValue()) {
				t.Fatalf("seed %d %s: served %q lacks needle %s", seed, family, served, f.NeedleValue())
			}
			if strings.Contains(served, f.DecoyValue()) {
				t.Fatalf("seed %d %s: bearer result carries the decoy value: %q", seed, family, served)
			}
			// The same tool on an ordinary web case carries neither that case's
			// needle nor any five-digit figure: non-bearer coined content is
			// six-digit filler only, so it can never collide with a needle.
			web := BuildFixtureForVersion(seed, webCase("cweb"), protocol.BenchVersionV13)
			other, _ := web.Result(tool, nil)
			if strings.Contains(other, web.NeedleValue()) || strings.Contains(other, f.NeedleValue()) {
				t.Fatalf("seed %d %s on a web case must not carry a needle: %q", seed, tool, other)
			}
			if carriesFiveDigitFigure(other) {
				t.Fatalf("seed %d %s on a non-bearer case carries a five-digit figure: %q", seed, tool, other)
			}
			if contentBySeed[tool] == nil {
				contentBySeed[tool] = map[string]bool{}
			}
			contentBySeed[tool][strings.ReplaceAll(served, f.NeedleValue(), "#")] = true
		}
		// The coined content itself is distinct per seed.
		co := CoinedForSeed(seed)
		if co.Workflows[0].Name == CoinedForSeed(seed + 1).Workflows[0].Name {
			t.Fatalf("seed %d coined workflow repeats on the next seed", seed)
		}
	}
	for tool, set := range contentBySeed {
		if len(set) < 35 {
			t.Errorf("%s coined content only has %d distinct forms across 40 seeds (baked fixture would pay)", tool, len(set))
		}
	}
	// A plain routing case that merely expects a list tool carries no needle.
	plain := BuildFixtureForVersion(3, protocol.ToolCase{ID: "cplain", Category: "world_business_workflow", ExpectedTools: []protocol.ToolSpec{{Name: "list_workflows"}, {Name: "create_workflow"}}}, protocol.BenchVersionV13)
	if plain.has {
		t.Fatal("non-result-usage list case must not carry a needle")
	}
}
