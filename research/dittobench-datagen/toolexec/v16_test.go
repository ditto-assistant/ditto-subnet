package toolexec

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func callV16(f Fixture, name string, args map[string]string) protocol.ToolExecResponse {
	raw, _ := json.Marshal(args)
	return fixtureToolResult(f, protocol.ToolExecRequest{Name: name, Args: raw}, 0)
}

// A linked app's read tool carries the needle only as the bearer of a
// linked-connector result-usage case; another linked read on that case serves
// the scored decoy, a write confirms its payload, and every unlinked
// connector is refused. v15 refuses every connector.
func TestV16LinkedConnectorServing(t *testing.T) {
	const seed = int64(123456789)
	plan := catalog.V16ConnectorPlanForSeed(seed)
	if len(plan.Linked) < 2 {
		t.Fatalf("seed links %d apps", len(plan.Linked))
	}
	bearerApp, otherApp := plan.Linked[0], plan.Linked[1]
	c := protocol.ToolCase{ID: "case-1", Category: "connector_linked_result_usage", ExpectedTools: []protocol.ToolSpec{{Name: bearerApp.ReadTool}}}
	f := BuildFixtureForVersion(seed, c, protocol.BenchVersionV16)
	if f.Bearer() != bearerApp.ReadTool || f.NeedleValue() == "" {
		t.Fatalf("bearer %q needle %q", f.Bearer(), f.NeedleValue())
	}
	if got := callV16(f, bearerApp.ReadTool, nil); got.Error != "" || !strings.Contains(got.Result, f.NeedleValue()) {
		t.Fatalf("bearer result %+v lacks the needle", got)
	}
	if got := callV16(f, otherApp.ReadTool, nil); got.Error != "" || strings.Contains(got.Result, f.NeedleValue()) || !strings.Contains(got.Result, f.DecoyValue()) {
		t.Fatalf("non-bearer linked read %+v must serve the decoy, not the needle", got)
	}
	if got := callV16(f, otherApp.WriteTool, map[string]string{otherApp.WriteParam: "Velmora launch notes"}); got.Error != "" || !strings.Contains(got.Result, "Velmora launch notes") {
		t.Fatalf("linked write %+v must confirm its payload", got)
	}
	if got := callV16(f, otherApp.WriteTool, nil); !strings.Contains(got.Result, "is required") {
		t.Fatalf("linked write without payload %+v must be rejected in the result", got)
	}
	for _, app := range plan.Named {
		if got := callV16(f, app.WriteTool, map[string]string{app.WriteParam: "x"}); !strings.Contains(got.Error, "not connected") {
			t.Fatalf("named-unlinked %s answered %+v", app.WriteTool, got)
		}
	}
	// The same case at v15 refuses the linked app too.
	f15 := BuildFixtureForVersion(seed, c, protocol.BenchVersionV15)
	if got := callV16(f15, bearerApp.ReadTool, nil); !strings.Contains(got.Error, "not connected") || f15.NeedleValue() != "" {
		t.Fatalf("v15 served %s: %+v", bearerApp.ReadTool, got)
	}
	if f15.V16ConnectorPlan() != nil || f.V16ConnectorPlan() == nil {
		t.Fatal("connector plan must exist only at v16")
	}
}

// A seed decoy whose coined name collides with a v18 snapshot slug keeps the
// decoy handling: as the bearer of its result-usage case it serves the
// needle instead of the snapshot's "not connected" refusal.
func TestV18SnapshotRefusalSkipsFixtureDecoys(t *testing.T) {
	const seed = int64(7)
	decoy := catalog.DecoysForSeed(seed)[0]
	c := protocol.ToolCase{
		ID: "cdecoy", Category: "decoy_" + decoy.Shape.Key + "_result_usage",
		ExpectedTools: []protocol.ToolSpec{{Name: decoy.Name}}, MaxToolCalls: 1,
	}
	f := BuildFixtureForVersion(seed, c, protocol.BenchVersionV18)
	if !f.has || f.Bearer() != decoy.Name {
		t.Fatalf("decoy-correct case must be borne by the decoy: has=%v bearer=%q", f.has, f.Bearer())
	}
	slug := ""
	for _, def := range catalog.V18ProductionToolsForSeed(seed, protocol.BenchVersionV18) {
		if _, ok := catalog.IsV18ProductionTool(def.Name); ok {
			slug = def.Name
			break
		}
	}
	if slug == "" {
		t.Fatal("seed offers no refusable v18 snapshot tool")
	}
	if got := callV16(f, slug, nil); !strings.Contains(got.Error, "not connected") {
		t.Fatalf("a plain snapshot tool must be refused: %+v", got)
	}
	// Re-key the bearer decoy under the snapshot slug to simulate a collision.
	collided := decoy
	collided.Name = slug
	f.decoys = map[string]catalog.Decoy{slug: collided}
	f.bearer = slug
	if got := callV16(f, slug, nil); got.Error != "" || !strings.Contains(got.Result, f.NeedleValue()) {
		t.Fatalf("colliding decoy bearer must serve the needle: %+v", got)
	}
}
