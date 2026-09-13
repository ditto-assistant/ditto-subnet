package catalog

import (
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func toolNames(tools []protocol.ToolDefinition) []string {
	out := make([]string, 0, len(tools))
	for _, t := range tools {
		out = append(out, t.Name)
	}
	return out
}

// TestPreV13CatalogsAreFrozen pins the v8..v12 surface: 31 tools, set_main_model
// still advertised, no enum anywhere, and CatalogForSeed is seed-independent.
func TestPreV13CatalogsAreFrozen(t *testing.T) {
	for _, version := range []int{protocol.BenchVersionV8, protocol.BenchVersionV9, protocol.BenchVersionV10, protocol.BenchVersionV11, protocol.BenchVersionV12} {
		tools := CatalogForVersion(version)
		if len(tools) != 31 {
			t.Fatalf("v%d catalog has %d tools, want 31", version, len(tools))
		}
		names := toolNames(tools)
		if !slices(names, "set_main_model") {
			t.Fatalf("v%d catalog dropped set_main_model", version)
		}
		for _, tool := range tools {
			if strings.Contains(string(tool.Parameters), `"enum"`) {
				t.Fatalf("v%d tool %s carries an enum", version, tool.Name)
			}
		}
		for _, seed := range []int64{0, 1, 42, 123456789} {
			if !reflect.DeepEqual(CatalogForSeed(version, seed), tools) {
				t.Fatalf("v%d CatalogForSeed(%d) differs from CatalogForVersion", version, seed)
			}
		}
	}
	legacy := Catalog()
	if len(legacy) != 33 {
		t.Fatalf("legacy catalog has %d tools, want 33", len(legacy))
	}
}

func slices(values []string, want string) bool {
	for _, v := range values {
		if v == want {
			return true
		}
	}
	return false
}

// TestV13ProductionSurface: set_main_model retired, every other v12 tool kept,
// enums on set_theme/set_reasoning_effort only, runtime-described setters.
func TestV13ProductionSurface(t *testing.T) {
	v12 := toolNames(CatalogForVersion(protocol.BenchVersionV12))
	v13 := CatalogForVersion(protocol.BenchVersionV13)
	names := toolNames(v13)
	if slices(names, "set_main_model") {
		t.Fatal("v13 still advertises set_main_model (#1580)")
	}
	for _, name := range v12 {
		if name != "set_main_model" && !slices(names, name) {
			t.Errorf("v13 dropped production tool %q", name)
		}
	}
	if len(v13) != 30 {
		t.Fatalf("v13 production surface has %d tools, want 30", len(v13))
	}
	enums := map[string][]string{}
	for _, tool := range v13 {
		var params struct {
			Properties map[string]struct {
				Enum        []string `json:"enum"`
				Description string   `json:"description"`
			} `json:"properties"`
		}
		if err := json.Unmarshal(tool.Parameters, &params); err != nil {
			t.Fatalf("%s: %v", tool.Name, err)
		}
		for key, p := range params.Properties {
			if len(p.Enum) > 0 {
				enums[tool.Name+"."+key] = p.Enum
			}
		}
	}
	if !reflect.DeepEqual(enums["set_theme.theme"], ThemeEnum()) {
		t.Errorf("set_theme enum = %v", enums["set_theme.theme"])
	}
	if !reflect.DeepEqual(enums["set_reasoning_effort.effort"], EffortEnum()) {
		t.Errorf("set_reasoning_effort enum = %v", enums["set_reasoning_effort.effort"])
	}
	if len(enums) != 2 {
		t.Errorf("unexpected enum set: %v", enums)
	}
	for _, name := range []string{"set_accent_color", "set_chat_font"} {
		for _, tool := range v13 {
			if tool.Name == name && !strings.Contains(string(tool.Parameters), "discover_capabilities") {
				t.Errorf("%s schema does not describe its runtime option list", name)
			}
		}
	}
}

// TestV13DescriptionBanks: every production tool has >= 6 distinct paraphrases,
// entry 0 is what CatalogForVersion(13) advertises, and no bank names a retired
// tool.
func TestV13DescriptionBanks(t *testing.T) {
	production := CatalogForVersion(protocol.BenchVersionV13)
	for _, tool := range production {
		bank, ok := v13DescriptionBanks[tool.Name]
		if !ok {
			t.Errorf("%s has no description bank", tool.Name)
			continue
		}
		if len(bank) < MinDescriptionParaphrases {
			t.Errorf("%s bank has %d paraphrases, want >= %d", tool.Name, len(bank), MinDescriptionParaphrases)
		}
		seen := map[string]bool{}
		for _, d := range bank {
			if seen[d] {
				t.Errorf("%s bank repeats %q", tool.Name, d)
			}
			seen[d] = true
			if strings.Contains(d, "set_main_model") {
				t.Errorf("%s bank names retired set_main_model", tool.Name)
			}
		}
		if tool.Description != bank[0] {
			t.Errorf("%s canonical description is not bank[0]", tool.Name)
		}
	}
	for name := range v13DescriptionBanks {
		if !slices(toolNames(production), name) {
			t.Errorf("bank for %q has no production tool", name)
		}
	}
}

// TestV13SeededCatalogVariesDescriptionsAndKeepsNames: names never change per
// seed, descriptions do, and the production tools keep their relative order.
func TestV13SeededCatalogVariesDescriptionsAndKeepsNames(t *testing.T) {
	production := CatalogForVersion(protocol.BenchVersionV13)
	distinct := map[string]map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		seeded := CatalogForSeed(protocol.BenchVersionV13, seed)
		decoys := DecoysForSeed(seed)
		if len(seeded) != len(production)+len(decoys) {
			t.Fatalf("seed %d catalog has %d tools, want %d", seed, len(seeded), len(production)+len(decoys))
		}
		productionIdx := 0
		for _, tool := range seeded {
			if _, isDecoy := DecoyByName(seed, tool.Name); isDecoy {
				continue
			}
			want := production[productionIdx]
			productionIdx++
			if tool.Name != want.Name {
				t.Fatalf("seed %d production order changed: %s vs %s", seed, tool.Name, want.Name)
			}
			if string(tool.Parameters) != string(want.Parameters) {
				t.Fatalf("seed %d %s schema changed per seed", seed, tool.Name)
			}
			if distinct[tool.Name] == nil {
				distinct[tool.Name] = map[string]bool{}
			}
			distinct[tool.Name][tool.Description] = true
		}
		if !reflect.DeepEqual(seeded, CatalogForSeed(protocol.BenchVersionV13, seed)) {
			t.Fatalf("seed %d catalog not deterministic", seed)
		}
	}
	for name, set := range distinct {
		if len(set) < 3 {
			t.Errorf("%s: only %d distinct descriptions across 40 seeds", name, len(set))
		}
	}
}

// TestV13Decoys pins the decoy contract: 3..5 per seed over a >= 20-shape pool,
// coined names disjoint from every production tool (at any version), a brand in
// the description, and a "not" clause naming what the decoy is not.
func TestV13Decoys(t *testing.T) {
	if DecoyShapeCount() < 20 {
		t.Fatalf("decoy shape pool has %d shapes, want >= 20", DecoyShapeCount())
	}
	production := map[string]bool{}
	for _, tool := range Catalog() {
		production[tool.Name] = true
	}
	for _, tool := range CatalogForVersion(protocol.BenchVersionV13) {
		production[tool.Name] = true
	}
	counts := map[int]bool{}
	shapesSeen := map[string]bool{}
	namesAcrossSeeds := map[string]int64{}
	for seed := int64(1); seed <= 200; seed++ {
		decoys := DecoysForSeed(seed)
		if len(decoys) < MinDecoys || len(decoys) > MaxDecoys {
			t.Fatalf("seed %d has %d decoys", seed, len(decoys))
		}
		counts[len(decoys)] = true
		seen := map[string]bool{}
		for _, d := range decoys {
			shapesSeen[d.Shape.Key] = true
			if production[d.Name] {
				t.Fatalf("seed %d decoy %q collides with a production tool", seed, d.Name)
			}
			if seen[d.Name] {
				t.Fatalf("seed %d repeats decoy %q", seed, d.Name)
			}
			seen[d.Name] = true
			if !strings.HasPrefix(d.Name, strings.ToLower(d.Brand)+"_") {
				t.Fatalf("seed %d decoy %q does not carry its brand %q", seed, d.Name, d.Brand)
			}
			if !strings.Contains(d.Description, d.Brand) || !strings.Contains(d.Description, "not") {
				t.Fatalf("seed %d decoy %q description lacks brand or NOT clause: %q", seed, d.Name, d.Description)
			}
			if strings.Contains(d.Description, "%") {
				t.Fatalf("seed %d decoy %q description leaked a format verb: %q", seed, d.Name, d.Description)
			}
			if prior, ok := namesAcrossSeeds[d.Name]; ok && prior != seed {
				t.Fatalf("decoy name %q repeats across seeds %d and %d", d.Name, prior, seed)
			}
			namesAcrossSeeds[d.Name] = seed
			if len(d.Shape.Prompts) < 3 {
				t.Fatalf("shape %s has %d prompts", d.Shape.Key, len(d.Shape.Prompts))
			}
			prompt := d.Prompt(1, "the Veltrix index")
			if !strings.Contains(prompt, d.Brand) || strings.Contains(prompt, d.Name) || strings.Contains(prompt, "%") {
				t.Fatalf("decoy prompt must name the brand, never the tool: %q", prompt)
			}
			if !strings.Contains(d.ServedResult("FACT"), "FACT") {
				t.Fatalf("decoy result does not embed the served fact")
			}
		}
	}
	for n := MinDecoys; n <= MaxDecoys; n++ {
		if !counts[n] {
			t.Errorf("no seed drew %d decoys", n)
		}
	}
	if len(shapesSeen) < DecoyShapeCount() {
		t.Errorf("only %d/%d shapes drawn across 200 seeds", len(shapesSeen), DecoyShapeCount())
	}
	keys := make([]string, 0, len(decoyShapes))
	for _, s := range decoyShapes {
		keys = append(keys, s.Key)
	}
	sort.Strings(keys)
	for i := 1; i < len(keys); i++ {
		if keys[i] == keys[i-1] {
			t.Fatalf("duplicate decoy shape key %q", keys[i])
		}
	}
}

func TestProductionToolNamesMatchCatalog(t *testing.T) {
	names := ProductionToolNames(protocol.BenchVersionV13)
	if len(names) != 30 || slices(names, "set_main_model") {
		t.Fatalf("v13 production names = %v", names)
	}
}
