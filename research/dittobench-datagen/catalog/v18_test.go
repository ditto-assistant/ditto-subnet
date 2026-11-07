package catalog

import (
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Production toolkit sizes measured read-only on 2026-11-07 (largest first).
var v18ProductionToolkitSizes = []int{328, 314, 127, 92, 70, 61, 57, 50, 47, 46, 26, 25, 16, 16, 7}

func TestV18BulkToolkitsMatchTheProductionSkew(t *testing.T) {
	if len(V18BulkToolkits) != len(v18ProductionToolkitSizes) {
		t.Fatalf("%d toolkits, want %d", len(V18BulkToolkits), len(v18ProductionToolkitSizes))
	}
	perKit := map[string]int{}
	for _, tool := range v18BulkUniverse() {
		perKit[tool.kit.Slug]++
	}
	total := 0
	for i, kit := range V18BulkToolkits {
		if kit.Size != v18ProductionToolkitSizes[i] || perKit[kit.Slug] != kit.Size {
			t.Fatalf("%s renders %d tools, size %d, want %d", kit.Slug, perKit[kit.Slug], kit.Size, v18ProductionToolkitSizes[i])
		}
		total += kit.Size
	}
	if total != 1282 || V18BulkToolCount() != 1282 {
		t.Fatalf("bulk tools %d (count %d), want 1282", total, V18BulkToolCount())
	}
}

// No bulk toolkit is an app a v16 seed can link or name, so a bulk tool can
// never contradict a linked app's state, and none shares a name with the
// pool, the product tools or another bulk tool.
func TestV18BulkToolkitsNeverOverlapTheLinkedAppsOrTheProduct(t *testing.T) {
	scripted := map[string]bool{}
	for _, s := range v16ConnectorScripts {
		scripted[s.Slug] = true
	}
	for _, kit := range V18BulkToolkits {
		if scripted[kit.Slug] {
			t.Fatalf("bulk toolkit %s is a v16 linkable app", kit.Slug)
		}
	}
	base := map[string]bool{}
	for _, def := range v16SeededCatalog(123456789) {
		base[def.Name] = true
	}
	seen := map[string]bool{}
	for _, def := range V18BulkConnectorsForSeed(123456789) {
		if base[def.Name] || seen[def.Name] {
			t.Fatalf("bulk tool %s collides", def.Name)
		}
		seen[def.Name] = true
		lower := strings.ToLower(def.Name + " " + def.Description)
		for _, owned := range []string{"email", "calendar", "reminder", "weather", "translat", "web search", "memory"} {
			if strings.Contains(lower, owned) {
				t.Fatalf("bulk tool %s mentions product capability %q: %s", def.Name, owned, def.Description)
			}
		}
	}
}

func TestV18BulkDescriptionsLookLikeProduction(t *testing.T) {
	for _, seed := range []int64{1, 2, 123456789} {
		for _, def := range V18BulkConnectorsForSeed(seed) {
			if n := len(def.Description); n < 140 || n > 280 {
				t.Fatalf("seed %d %s: description is %d characters, want 140..280: %q", seed, def.Name, n, def.Description)
			}
			if phrase, ok := HasLinkStatePhrase(def.Description); ok {
				t.Fatalf("seed %d %s carries link-state wording %q", seed, def.Name, phrase)
			}
			if app, ok := IsConnectorToolForVersion(def.Name, protocol.BenchVersionV18); !ok || app == "" {
				t.Fatalf("%s is not recognised as a v18 connector", def.Name)
			}
			if _, ok := IsConnectorToolForVersion(def.Name, protocol.BenchVersionV17); ok {
				t.Fatalf("%s is a connector below v18", def.Name)
			}
		}
	}
}

// The v18 surface is the v16 surface, in its order, with every bulk tool
// spliced in; below v18 the surface is unchanged.
func TestV18CatalogSplicesTheBulkToolkitsIntoTheV16Surface(t *testing.T) {
	for _, seed := range []int64{1, 7, 123456789} {
		v16 := CatalogForSeed(protocol.BenchVersionV17, seed)
		if !reflect.DeepEqual(v16, v16SeededCatalog(seed)) {
			t.Fatal("v17 surface moved")
		}
		v18 := CatalogForSeed(protocol.BenchVersionV18, seed)
		if len(v18) != len(v16)+V18BulkToolCount() {
			t.Fatalf("seed %d: %d tools, want %d", seed, len(v18), len(v16)+V18BulkToolCount())
		}
		var kept []protocol.ToolDefinition
		for _, def := range v18 {
			if _, bulk := IsV18BulkConnectorTool(def.Name); !bulk {
				kept = append(kept, def)
			}
		}
		if !reflect.DeepEqual(kept, v16) {
			t.Fatalf("seed %d: v18 reorders or changes the v16 surface", seed)
		}
	}
	a, b := CatalogForSeed(protocol.BenchVersionV18, 1), CatalogForSeed(protocol.BenchVersionV18, 2)
	if reflect.DeepEqual(a, b) {
		t.Fatal("v18 surface is not seeded")
	}
}
