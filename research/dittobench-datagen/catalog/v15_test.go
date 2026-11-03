package catalog

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV15ConnectorPoolIsUniqueAndDisjointFromProductTools(t *testing.T) {
	product := map[string]bool{}
	for _, name := range ProductionToolNames(protocol.BenchVersionV15) {
		product[name] = true
	}
	seen := map[string]bool{}
	for _, tool := range connectorUniverse() {
		name := tool.name()
		if seen[name] {
			t.Fatalf("duplicate connector tool %q", name)
		}
		seen[name] = true
		if product[name] {
			t.Fatalf("connector tool %q collides with a product tool", name)
		}
		if len(tool.action.Does) < 2 {
			t.Fatalf("connector tool %q needs at least two description variants", name)
		}
	}
	if ConnectorPoolSize() < ConnectorToolsPerSeed+50 {
		t.Fatalf("pool %d must exceed the per-seed draw %d by at least 50 so membership varies by seed",
			ConnectorPoolSize(), ConnectorToolsPerSeed)
	}
}

func TestV15SeededCatalogIsProductionScaleAndKeepsTheV14Surface(t *testing.T) {
	for _, seed := range []int64{1, 42, 123456789, 987654321} {
		v14 := CatalogForSeed(protocol.BenchVersionV14, seed)
		v15 := CatalogForSeed(protocol.BenchVersionV15, seed)
		if len(v15) != len(v14)+ConnectorToolsPerSeed {
			t.Fatalf("seed %d: v15 catalog has %d tools, want %d", seed, len(v15), len(v14)+ConnectorToolsPerSeed)
		}
		if len(v15) < 200 {
			t.Fatalf("seed %d: v15 catalog has %d tools, want a production-scale surface (>= 200)", seed, len(v15))
		}
		names := map[string]bool{}
		for _, tool := range v15 {
			if names[tool.Name] {
				t.Fatalf("seed %d: duplicate tool name %q", seed, tool.Name)
			}
			names[tool.Name] = true
		}
		// Every v14 tool is present, byte-identical, in the same relative order.
		next := 0
		for _, tool := range v15 {
			if next < len(v14) && tool.Name == v14[next].Name {
				a, _ := json.Marshal(tool)
				b, _ := json.Marshal(v14[next])
				if string(a) != string(b) {
					t.Fatalf("seed %d: v14 tool %q changed at v15", seed, tool.Name)
				}
				next++
				continue
			}
			if _, ok := IsConnectorTool(tool.Name); !ok {
				t.Fatalf("seed %d: unexpected non-connector tool %q out of v14 order", seed, tool.Name)
			}
			if !strings.Contains(tool.Description, "connector to be linked in Settings") {
				t.Fatalf("seed %d: connector %q does not say it needs linking", seed, tool.Name)
			}
		}
		if next != len(v14) {
			t.Fatalf("seed %d: only %d of %d v14 tools kept their order", seed, next, len(v14))
		}
	}
}

func TestV15ConnectorMembershipVariesBySeed(t *testing.T) {
	a := ConnectorsForSeed(1)
	b := ConnectorsForSeed(2)
	same := 0
	inA := map[string]bool{}
	for _, tool := range a {
		inA[tool.Name] = true
	}
	for _, tool := range b {
		if inA[tool.Name] {
			same++
		}
	}
	if same == len(a) {
		t.Fatal("connector membership did not change between seeds")
	}
}

func TestBelowV15CatalogsCarryNoConnectors(t *testing.T) {
	for _, version := range []int{protocol.BenchVersionV13, protocol.BenchVersionV14} {
		for _, tool := range CatalogForSeed(version, 42) {
			if _, ok := IsConnectorTool(tool.Name); ok {
				t.Fatalf("v%d catalog carries connector tool %q", version, tool.Name)
			}
		}
	}
}
