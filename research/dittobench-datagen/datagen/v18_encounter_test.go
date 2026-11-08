package datagen

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func v18Encounters(t *testing.T, seed int64, n int) []protocol.ToolCase {
	t.Helper()
	ds, err := GenerateForVersion(seed, n, protocol.BenchVersionV18)
	if err != nil {
		t.Fatalf("seed %d: %v", seed, err)
	}
	var out []protocol.ToolCase
	for _, tc := range ds.ToolCases {
		if IsV18Encounter(tc.Category) != (tc.Encounter != nil) {
			t.Fatalf("seed %d case %s: category %s and encounter disagree", seed, tc.ID, tc.Category)
		}
		if tc.Encounter != nil {
			out = append(out, tc)
		}
	}
	return out
}

// The full v18 half carries the full encounter quota (two of each
// archetype) on almost every seed; a seed short of free slots trims it, but
// keeps every archetype. 2026-11-07 sweep of seeds 1..100: 97 full, two at
// 9, one at 7.
func TestV18EncounterQuotaHoldsAcrossSeeds(t *testing.T) {
	seeds := 100
	if testing.Short() {
		seeds = 8
	}
	want := V18EncounterQuotaFor(V18FullToolHalf)
	full, low := 0, want
	for seed := int64(1); seed <= int64(seeds); seed++ {
		encounters := v18Encounters(t, seed, V18FullToolHalf)
		per := map[string]int{}
		for _, tc := range encounters {
			per[tc.Encounter.Archetype]++
		}
		for _, archetype := range V18EncounterArchetypes() {
			if per[archetype] < 1 || per[archetype] > 2 {
				t.Fatalf("seed %d: archetype %s appears %d times (%v)", seed, archetype, per[archetype], per)
			}
		}
		if len(encounters) == want {
			full++
		}
		low = min(low, len(encounters))
	}
	if low < 7 || !testing.Short() && full < 95 {
		t.Fatalf("encounters: %d of %d seeds full, minimum %d", full, seeds, low)
	}
	t.Logf("encounters: %d of %d seeds full, minimum %d", full, seeds, low)
}

func TestV18EncountersAreAbsentBelowV18(t *testing.T) {
	for seed := int64(1); seed <= 4; seed++ {
		ds, err := GenerateForVersion(seed, V18FullToolHalf, protocol.BenchVersionV17)
		if err != nil {
			t.Fatal(err)
		}
		for _, tc := range ds.ToolCases {
			if tc.Encounter != nil || IsV18Encounter(tc.Category) {
				t.Fatalf("seed %d: v17 carries encounter %s", seed, tc.ID)
			}
		}
	}
}

// encounterOracle is an honest solution of an encounter: every required call
// with the values it must carry, plus the reads that reveal those values.
func encounterOracle(tc protocol.ToolCase) []protocol.ObservedToolCall {
	var calls []protocol.ObservedToolCall
	for _, required := range tc.Encounter.Required {
		values := append([]string(nil), required.Contains...)
		if len(required.AnyContains) > 0 {
			values = append(values, required.AnyContains[0])
		}
		args, _ := json.Marshal(map[string]string{"q": strings.Join(values, " ")})
		calls = append(calls, protocol.ObservedToolCall{Name: required.Tool, Args: args})
	}
	return calls
}

func encounterReply(values ...string) protocol.RunResponse {
	return protocol.RunResponse{FinalText: "Done. " + strings.Join(values, "; ") + "."}
}

// Each encounter is solvable, needs its calls, and cannot be won by hedging:
//
//   - every required call targets a tool on the seed's menu that the mock
//     endpoint serves (a linked app, never a refused one);
//   - every value a required call must carry, and every answer value, is
//     reachable: it appears in the prompt, a served result or the case's
//     memory, never only in the grader;
//   - the oracle trajectory and reply score 1, dropping a required call
//     lowers the score, a hedge zeroes that value, and a forbidden call
//     halves the score.
func TestV18EncountersAreSolvableAndDiscriminate(t *testing.T) {
	seeds := 24
	if testing.Short() {
		seeds = 6
	}
	checked := 0
	for seed := int64(1); seed <= int64(seeds); seed++ {
		menu := map[string]bool{}
		for _, def := range catalog.CatalogForSeed(protocol.BenchVersionV18, seed) {
			menu[def.Name] = true
		}
		for _, tc := range v18Encounters(t, seed, V18FullToolHalf) {
			checked++
			enc := tc.Encounter
			fixture := toolexec.BuildFixtureForVersion(seed, tc, protocol.BenchVersionV18)
			if fixture.Encounter() == nil {
				t.Fatalf("seed %d %s: fixture lost the encounter", seed, tc.ID)
			}
			world := tc.Prompt
			for _, pair := range tc.PrerequisitePairs {
				world += "\n" + pair.Prompt + "\n" + pair.Response
			}
			for _, rule := range enc.Rules {
				world += "\n" + rule.Result + "\n" + rule.Error
			}
			for _, required := range enc.Required {
				if !menu[required.Tool] {
					t.Fatalf("seed %d %s: required %s is not on the menu", seed, tc.ID, required.Tool)
				}
				for _, value := range append(append([]string(nil), required.Contains...), required.AnyContains...) {
					if !protocol.EncounterContains(world, value) {
						t.Fatalf("seed %d %s: required value %q is not discoverable", seed, tc.ID, value)
					}
				}
			}
			for _, call := range encounterOracle(tc) {
				srv := toolexec.NewServer()
				srv.Register(tc.ID, fixture)
				res := serveOnce(t, srv, tc.ID, call)
				if res.Error != "" && !strings.Contains(res.Error, "archived") {
					t.Fatalf("seed %d %s: required %s refused: %s", seed, tc.ID, call.Name, res.Error)
				}
			}
			var values []string
			for _, want := range enc.Answer {
				if want.Derivation == "" && !protocol.EncounterContains(world, want.Value) {
					t.Fatalf("seed %d %s: answer %q is not discoverable", seed, tc.ID, want.Value)
				}
				for _, hedge := range want.Hedges {
					if protocol.EncounterContains(want.Value, hedge) || protocol.EncounterContains(hedge, want.Value) {
						t.Fatalf("seed %d %s: hedge %q overlaps answer %q", seed, tc.ID, hedge, want.Value)
					}
				}
				values = append(values, want.Value)
			}
			calls := encounterOracle(tc)
			if v := grade.Encounter(enc, calls, encounterReply(values...)); v.Score != 1 {
				t.Fatalf("seed %d %s: oracle scored %.3f: %v", seed, tc.ID, v.Score, v.Notes)
			}
			if v := grade.Encounter(enc, calls[1:], encounterReply(values...)); v.Score >= 1 {
				t.Fatalf("seed %d %s: dropping %s still scored %.3f", seed, tc.ID, calls[0].Name, v.Score)
			}
			if v := grade.Encounter(enc, calls, encounterReply()); v.Score != 0 {
				t.Fatalf("seed %d %s: no answer scored %.3f", seed, tc.ID, v.Score)
			}
			for _, want := range enc.Answer {
				for _, hedge := range want.Hedges {
					if v := grade.Encounter(enc, calls, encounterReply(append(values, hedge)...)); v.Score >= 1 {
						t.Fatalf("seed %d %s: hedge %q still scored %.3f", seed, tc.ID, hedge, v.Score)
					}
				}
			}
			for _, forbidden := range enc.Forbidden {
				args, _ := json.Marshal(map[string]string{"q": strings.Join(forbidden.Contains, " ")})
				withForbidden := append(append([]protocol.ObservedToolCall(nil), calls...), protocol.ObservedToolCall{Name: forbidden.Tool, Args: args})
				if v := grade.Encounter(enc, withForbidden, encounterReply(values...)); v.Score > 0.5 {
					t.Fatalf("seed %d %s: forbidden %s scored %.3f", seed, tc.ID, forbidden.Tool, v.Score)
				}
			}
		}
	}
	if checked == 0 {
		t.Fatal("no encounter inspected")
	}
	t.Logf("encounters inspected: %d", checked)
}

// A required call carrying a stale or invented value does not count: the
// dependency must be followed.
func TestV18EncounterRequiredCallsNeedTheDiscoveredValue(t *testing.T) {
	checked := 0
	for _, tc := range v18Encounters(t, 3, V18FullToolHalf) {
		enc := tc.Encounter
		withValues := 0
		var calls []protocol.ObservedToolCall
		for _, required := range enc.Required {
			if len(required.Contains) > 0 {
				withValues++
			}
			calls = append(calls, protocol.ObservedToolCall{Name: required.Tool, Args: json.RawMessage(`{"q":"something else entirely"}`)})
		}
		if withValues == 0 {
			continue
		}
		checked++
		var values []string
		for _, want := range enc.Answer {
			values = append(values, want.Value)
		}
		if v := grade.Encounter(enc, calls, encounterReply(values...)); v.Score >= 1 {
			t.Fatalf("%s: calls without the discovered values scored %.3f", tc.ID, v.Score)
		}
	}
	if checked == 0 {
		t.Fatal("no encounter with a value-carrying required call")
	}
}

func serveOnce(t *testing.T, srv *toolexec.Server, caseID string, call protocol.ObservedToolCall) protocol.ToolExecResponse {
	t.Helper()
	body, _ := json.Marshal(protocol.ToolExecRequest{CaseID: caseID, Name: call.Name, Args: call.Args})
	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/tool", bytes.NewReader(body)))
	var out protocol.ToolExecResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode tool response: %v", err)
	}
	return out
}

// v18EncounterVector pins the encounters of seeds 1..5. The public v18
// vector (gen.TestV18KnownVector) hashes only what reaches the artifact, and
// an encounter's mock world and grading sets are grader-only, so they get
// their own pin. Changing either is a new bench version.
const v18EncounterVector = "5a3d94c6d856a4e91fe1b011ebe96b2a0481da0988b80f8550435c7ddecdffba"

func TestV18EncounterVector(t *testing.T) {
	type pinned struct {
		ID, Category, Prompt string
		Pairs                []protocol.MemoryPair
		Encounter            *protocol.Encounter
	}
	var all []pinned
	for seed := int64(1); seed <= 5; seed++ {
		for _, tc := range v18Encounters(t, seed, V18FullToolHalf) {
			all = append(all, pinned{tc.ID, tc.Category, tc.Prompt, tc.PrerequisitePairs, tc.Encounter})
		}
	}
	raw, err := json.Marshal(all)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if got := hex.EncodeToString(sum[:]); got != v18EncounterVector {
		t.Fatalf("v18 encounter vector: got %s, want %s (%d encounters)", got, v18EncounterVector, len(all))
	}
}

// One observed call that matches several forbidden entries is charged once.
func TestV18EncounterForbiddenCallChargedOnce(t *testing.T) {
	e := &protocol.Encounter{
		Forbidden: []protocol.EncounterCall{{Tool: "gmail_send"}, {Tool: "gmail_send", Contains: []string{"old@x.com"}}},
		Answer:    []protocol.EncounterAnswer{{Value: "42"}},
	}
	one := []protocol.ObservedToolCall{{Name: "gmail_send", Args: json.RawMessage(`{"to":"old@x.com"}`)}}
	if v := grade.Encounter(e, one, encounterReply("42")); v.ForbiddenHits != 1 || v.Score != 0.5 {
		t.Fatalf("one call charged %d times (score %.3f)", v.ForbiddenHits, v.Score)
	}
}

// A forbidden filing rule with Excludes charges filing the wrong incident,
// not a filing that names the right one and quotes the other as context.
func TestV18EncounterForbiddenExcludesContext(t *testing.T) {
	e := &protocol.Encounter{
		Forbidden: []protocol.EncounterCall{{Tool: "linear_create_issue", Contains: []string{"VEL-7100"}, Excludes: []string{"VEL-4821"}}},
		Answer:    []protocol.EncounterAnswer{{Value: "VEL-4821"}},
	}
	right := []protocol.ObservedToolCall{{Name: "linear_create_issue", Args: json.RawMessage(`{"title":"VEL-4821 outage (not the resolved VEL-7100)"}`)}}
	wrong := []protocol.ObservedToolCall{{Name: "linear_create_issue", Args: json.RawMessage(`{"title":"VEL-7100 outage"}`)}}
	if v := grade.Encounter(e, right, encounterReply("VEL-4821")); v.ForbiddenHits != 0 {
		t.Fatalf("quoting context was charged: %+v", v)
	}
	if v := grade.Encounter(e, wrong, encounterReply("VEL-4821")); v.ForbiddenHits != 1 {
		t.Fatalf("filing the wrong incident was not charged: %+v", v)
	}
}

// The encounter pass never offers the world carrier as a slot, even when the
// carrier looks like an ordinary prerequisite-bearing case (the last slot
// tier). Across seeds 1..100 the generator never puts the carrier there, so
// the slot function is tested directly with a carrier dressed as one.
func TestV18EncountersKeepTheWorldCarrier(t *testing.T) {
	checked := 0
	for seed := int64(1); seed <= 20; seed++ {
		ds, err := GenerateForVersion(seed, V18FullToolHalf, protocol.BenchVersionV18)
		if err != nil {
			t.Fatal(err)
		}
		cases := append([]protocol.ToolCase(nil), ds.ToolCases...)
		world := universe.GenerateForVersion(seed, ToolWorldScale(len(cases), protocol.BenchVersionV18), protocol.BenchVersionV18)
		carrier := v13WorldCarrier(world, cases)
		if carrier < 0 {
			continue
		}
		checked++
		pairs := cases[carrier].PrerequisitePairs
		cases[carrier] = protocol.ToolCase{ID: cases[carrier].ID, Category: "stale_context_web",
			ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}}, PrerequisitePairs: pairs}
		for _, slot := range v18EncounterSlots(seed, protocol.BenchVersionV18, cases) {
			if slot == carrier {
				t.Fatalf("seed %d: the world carrier is offered as an encounter slot", seed)
			}
		}
	}
	if checked == 0 {
		t.Fatal("no seed had a world carrier to check")
	}
}
