package catalog

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Every description variant of every pool tool, rendered the v16 way, is free
// of link-state wording (not just the variant a few seeds happen to draw), and
// every rewrite still names a live v15 variant.
func TestV16ConnectorDescriptionsCarryNoLinkState(t *testing.T) {
	live := map[string]bool{}
	for _, tool := range connectorUniverse() {
		for _, does := range tool.action.Does {
			live[does] = true
			if rewrite, ok := v16DescriptionRewrites[does]; ok {
				does = rewrite
			}
			rendered := strings.ReplaceAll(does, "%[1]s", tool.app.Name)
			if phrase, hit := HasLinkStatePhrase(rendered); hit {
				t.Fatalf("%s variant %q carries %q at v16", tool.name(), rendered, phrase)
			}
		}
	}
	for from := range v16DescriptionRewrites {
		if !live[from] {
			t.Fatalf("rewrite for %q names no pool variant", from)
		}
	}
	if _, hit := HasLinkStatePhrase("Share a post on LinkedIn."); hit {
		t.Fatal("an app name must not read as a link state")
	}
	if _, hit := HasLinkStatePhrase("Post a message. Needs the Slack connector to be linked in Settings."); !hit {
		t.Fatal("the v15 suffix must read as a link state")
	}
}

func TestV16ScriptsResolveInPool(t *testing.T) {
	apps := V16ConnectorScripts()
	if len(apps) != len(v16ConnectorScripts) || len(apps) < V16LinkedMax+V16NamedUnlinked {
		t.Fatalf("%d scripts resolve", len(apps))
	}
	seen := map[string]bool{}
	for _, app := range apps {
		if seen[app.Slug] {
			t.Fatalf("duplicate script %s", app.Slug)
		}
		seen[app.Slug] = true
		if app.ReadTool == app.WriteTool || app.WriteParam == "" || len(app.Script.ReadPrompts) < 2 || len(app.Script.WritePrompts) < 2 {
			t.Fatalf("script %s incomplete: %+v", app.Slug, app)
		}
		for _, prompt := range append(append([]string(nil), app.Script.ReadPrompts...), app.Script.WritePrompts...) {
			if !strings.Contains(prompt, "%[2]s") {
				t.Fatalf("script %s prompt %q has no payload slot", app.Slug, prompt)
			}
		}
	}
}

func TestV16SeededCatalogKeepsTheV14SurfaceAndOffersEveryPlannedApp(t *testing.T) {
	counts := map[int]int{}
	for seed := int64(1); seed <= 64; seed++ {
		v14 := CatalogForSeed(protocol.BenchVersionV14, seed)
		v16 := CatalogForSeed(protocol.BenchVersionV16, seed)
		if len(v16) != len(v14)+ConnectorToolsPerSeed {
			t.Fatalf("seed %d: v16 catalog has %d tools, want %d", seed, len(v16), len(v14)+ConnectorToolsPerSeed)
		}
		names := map[string]bool{}
		next := 0
		for _, tool := range v16 {
			if names[tool.Name] {
				t.Fatalf("seed %d: duplicate %s", seed, tool.Name)
			}
			names[tool.Name] = true
			if next < len(v14) && tool.Name == v14[next].Name {
				a, _ := json.Marshal(tool)
				b, _ := json.Marshal(v14[next])
				if string(a) != string(b) {
					t.Fatalf("seed %d: v14 tool %s changed at v16", seed, tool.Name)
				}
				next++
			}
		}
		if next != len(v14) {
			t.Fatalf("seed %d: v14 surface not kept in order (%d of %d)", seed, next, len(v14))
		}
		plan := V16ConnectorPlanForSeed(seed)
		if !reflect.DeepEqual(plan, V16ConnectorPlanForSeed(seed)) {
			t.Fatal("plan is not a pure function of the seed")
		}
		counts[len(plan.Linked)]++
		for _, app := range append(append([]ConnectorApp(nil), plan.Linked...), plan.Named...) {
			for _, tool := range app.Tools {
				if !names[tool] {
					t.Fatalf("seed %d: planned tool %s not offered", seed, tool)
				}
			}
		}
	}
	for n := V16LinkedMin; n <= V16LinkedMax; n++ {
		if counts[n] == 0 {
			t.Fatalf("no seed in 1..64 links %d apps: %v", n, counts)
		}
	}
	// v15 is untouched: the suffix stays and its catalog differs from v16.
	v15 := CatalogForSeed(protocol.BenchVersionV15, 1)
	for _, tool := range v15 {
		if _, ok := IsConnectorTool(tool.Name); ok && !strings.HasSuffix(tool.Description, "connector to be linked in Settings.") {
			t.Fatalf("v15 connector %s lost its suffix", tool.Name)
		}
	}
}
