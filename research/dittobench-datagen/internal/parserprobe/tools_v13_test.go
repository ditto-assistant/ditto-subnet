package parserprobe

import (
	"encoding/json"
	"fmt"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

func TestV13DecoyControlUsesDeliveredCatalog(t *testing.T) {
	for _, shape := range catalog.DecoyShapes() {
		t.Run(shape.Key, func(t *testing.T) {
			decoy := catalog.Decoy{Name: "custom_" + shape.Suffix, Brand: "Custom", Shape: shape,
				Description: fmt.Sprintf(shape.Does, "Custom") + " " + shape.Not}
			definition := decoy.Definition()
			parser := newToolParser(13)
			parser.addV13WireCatalog([]protocol.ToolDefinition{definition})
			for i := range shape.Prompts {
				prediction := parser.classifyTool(decoy.Prompt(i, "Public subject"), nil)
				if !prediction.ok || len(prediction.tools) != 1 || prediction.tools[0].Name != decoy.Name {
					t.Fatalf("did not resolve delivered tool for prompt %d: %+v", i, prediction)
				}
			}
			definition.Description = "Different capability; not this task."
			parser = newToolParser(13)
			parser.addV13WireCatalog([]protocol.ToolDefinition{definition})
			prediction := parser.classifyTool(decoy.Prompt(0, "Public subject"), nil)
			for _, tool := range prediction.tools {
				if tool.Name == decoy.Name {
					t.Fatal("used name suffix without capability evidence")
				}
			}
		})
	}
}

func TestV13DecoyAndLinkControlGeneratedPublicCases(t *testing.T) {
	profile, _ := gen.ProfileForVersion("full", 13)
	seeds := []int64{4242}
	for seed := int64(1); seed <= 40; seed++ {
		seeds = append(seeds, seed)
	}
	for _, seed := range seeds {
		artifact, err := gen.GenerateDataset(seed, profile, 13)
		if err != nil {
			t.Fatal(err)
		}
		parser := newToolParser(13)
		parser.addV13WireCatalog(artifact.Catalog)
		covered, total := 0, 0
		for _, test := range artifact.ToolCases {
			if !datagenControlFamily(test.Category) {
				continue
			}
			total++
			prediction := parser.classifyTool(test.Prompt, nil)
			fixture := toolexec.BuildFixtureForVersion(seed, test, 13)
			if discovered, matched := parser.discoveryPrediction(test.Prompt, fixture.Result); matched {
				prediction = discovered
			}
			if prediction.ok && toolSignature(prediction.tools, test.FuzzyTrajectory, nil) == toolSignature(test.ExpectedTools, test.FuzzyTrajectory, nil) {
				covered++
			} else {
				t.Logf("seed %d category %s: prompt not covered: %s", seed, test.Category, test.Prompt)
			}
		}
		if total == 0 || covered != total {
			t.Errorf("seed %d coverage %d/%d", seed, covered, total)
		}
	}
}

func datagenControlFamily(category string) bool {
	return len(category) > 6 && category[:6] == "decoy_" || category == "world_link_chain_result_usage" || category == "schedules_result_usage" || category == "tool_registry_result_usage" || category == "sandbox_result_usage" || category == "agent_jobs_result_usage" || category == "discovery_accent_set" || category == "discovery_font_set"
}

func TestV13DiscoveryUsesOnlyServedOptions(t *testing.T) {
	parser := newToolParser(13)
	parser.addV13WireCatalog(nil)
	prompt := "Change the accent colour to seawvae. Spelling is approximate, so confirm it against the options this workspace lists."
	for _, valid := range []bool{true, false} {
		calls := 0
		prediction, matched := parser.discoveryPrediction(prompt, func(name string, args json.RawMessage) (string, bool) {
			calls++
			if name != "discover_capabilities" || string(args) != "{}" {
				t.Fatal("unexpected discovery call")
			}
			options := "amber, seaweed"
			if valid {
				options = "amber, seawave"
			}
			return "Appearance options for this workspace — accent colors: " + options + "; chat fonts: Inter; color modes: dark.", true
		})
		if !matched || calls != 1 {
			t.Fatal("did not use the served discovery response")
		}
		if valid && (!prediction.ok || prediction.tools[1].RequiredArgs["color"] != "seawave") {
			t.Fatal("did not resolve runtime-only option")
		}
		if !valid && prediction.ok {
			t.Fatal("invented an unlisted option")
		}
	}
	if p, _ := parser.discoveryPrediction(prompt, nil); p.ok {
		t.Fatal("discovery without a tool response accepted")
	}
}
