package toolexec

import (
	"encoding/json"
	"fmt"
	"math/rand"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v16 linked connectors (docs/dueling-bench.md section 8.1). Reached
// only from a fixture built at bench_version >= 16; v15 keeps refusing every
// connector.
//
//   - A connector of one of the seed's linked apps (catalog.V16ConnectorPlan)
//     executes like a product tool: its read action serves the case needle
//     when the case is a linked-connector result-usage case and that tool is
//     the bearer, and a decoy figure otherwise; its write action confirms the
//     payload it was given; any other action of a linked app completes.
//   - Every other connector still answers "not connected" and performs
//     nothing, offered on this seed or not. On a case that names that app the
//     refusal is the correct runtime answer to a probe.

// linkedConnector reports whether name is a tool of one of the fixture's
// linked apps (v16 only).
func (f Fixture) linkedConnector(name string) (catalog.ConnectorApp, bool) {
	if f.connectors == nil {
		return catalog.ConnectorApp{}, false
	}
	return f.connectors.LinkedApp(name)
}

// isLinkedReadBearer reports whether name is a linked app's scripted read
// action on a result-usage case: the tool that carries that case's needle.
func (f Fixture) isLinkedReadBearer(category, name string) bool {
	app, linked := f.linkedConnector(name)
	return linked && name == app.ReadTool && isResultUsageCategory(category)
}

// connectorRefusedV16 is the v16 connector branch of unavailable: a linked
// app's tool is served, every other connector is refused.
func (f Fixture) connectorRefusedV16(name string) (string, bool) {
	// From v18 the bulk toolkits are connectors too (never linked).
	app, isConnector := catalog.IsConnectorToolForVersion(name, f.benchVersion)
	if !isConnector {
		return "", false
	}
	if _, linked := f.linkedConnector(name); linked {
		return "", false
	}
	return fmt.Sprintf("%s is not connected for this workspace. Link %s in Settings before retrying; the request was not performed.", name, app), true
}

// resultV16Connector serves a linked app's tool. handled=false falls through.
func (f Fixture) resultV16Connector(name string, args json.RawMessage, r *rand.Rand, serveNeedle bool) (string, bool) {
	app, linked := f.linkedConnector(name)
	if !linked {
		return "", false
	}
	switch name {
	case app.ReadTool:
		body := fmt.Sprintf("No %s items matched.", app.Name)
		switch {
		case serveNeedle:
			body = f.needleSentence()
		case f.has:
			body = f.decoySentence(r)
		}
		return fmt.Sprintf(app.Script.ReadResult, app.Name, body), true
	case app.WriteTool:
		value := stringArg(args, app.WriteParam)
		if value == "" {
			return fmt.Sprintf("%s rejected the request: %s is required.", app.Name, app.WriteParam), true
		}
		return fmt.Sprintf("%s accepted the request (%s %q, reference %s).", app.Name, app.WriteParam, value, jobID(r)), true
	default:
		if f.has {
			return fmt.Sprintf("%s: %s", app.Name, f.decoySentence(r)), true
		}
		return fmt.Sprintf("%s completed the request.", app.Name), true
	}
}

// V16ConnectorPlan exposes the fixture's connector plan (nil below v16).
func (f Fixture) V16ConnectorPlan() *catalog.V16ConnectorPlan { return f.connectors }

// buildV16Connectors attaches the seed's connector plan to a v16 fixture.
func (f *Fixture) buildV16Connectors(masterSeed int64) {
	if f.benchVersion < protocol.BenchVersionV16 {
		return
	}
	plan := catalog.V16ConnectorPlanForSeed(masterSeed)
	f.connectors = &plan
}
