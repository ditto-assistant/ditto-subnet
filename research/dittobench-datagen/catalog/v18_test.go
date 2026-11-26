package catalog

import (
	"crypto/sha256"
	"encoding/hex"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The snapshot is pinned by hash: a wrong or missing pin fails closed.
func TestV18SnapshotIsHashPinned(t *testing.T) {
	sum := sha256.Sum256(v18SnapshotGzip)
	if got := hex.EncodeToString(sum[:]); got != V18CatalogSnapshotSHA256 {
		t.Fatalf("embedded snapshot sha256 %s, pinned %s", got, V18CatalogSnapshotSHA256)
	}
	if _, err := LoadV18Snapshot(v18SnapshotGzip, V18CatalogSnapshotSHA256); err != nil {
		t.Fatalf("pinned snapshot rejected: %v", err)
	}
	if _, err := LoadV18Snapshot(v18SnapshotGzip, strings.Repeat("0", 64)); err == nil {
		t.Fatal("a snapshot whose hash differs from the pin was accepted")
	}
	if _, err := LoadV18Snapshot(v18SnapshotGzip, ""); err == nil {
		t.Fatal("an unpinned snapshot was accepted")
	}
	snap := v18Snapshot()
	tools := 0
	for _, kit := range snap.Toolkits {
		tools += len(kit.Tools)
	}
	if len(snap.Toolkits) != 1717 || tools != 77948 || len(snap.FirstParty) != 128 {
		t.Fatalf("snapshot holds %d toolkits, %d tools, %d first-party tools", len(snap.Toolkits), tools, len(snap.FirstParty))
	}
}

// Every seed's whole menu stays within CatalogSizeForVersion, keeps the v16
// surface in order, carries the first-party tools, takes toolkits whole,
// never samples a capability-owned toolkit or a toolkit the seed links, and
// paraphrases per seed.
func TestV18CatalogPerSeed(t *testing.T) {
	if err := V18SnapshotReady(); err != nil {
		t.Fatal(err)
	}
	snap := v18Snapshot()
	kitOf := map[string]string{}
	kitSize := map[string]int{}
	for _, kit := range snap.Toolkits {
		kitSize[kit.Slug] = len(kit.Tools)
		for _, tool := range kit.Tools {
			kitOf[tool.Slug] = kit.Slug
		}
	}
	sizes := map[int]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		v16 := v16SeededCatalog(seed)
		v18 := CatalogForSeed(protocol.BenchVersionV18, seed)
		size, _ := CatalogSizeForVersion(protocol.BenchVersionV18)
		if n := len(v18); n < size.Min || n > size.Max {
			t.Fatalf("seed %d: %d tools, want %d..%d", seed, n, size.Min, size.Max)
		}
		sizes[len(v18)] = true
		var kept []protocol.ToolDefinition
		perKit := map[string]int{}
		first := 0
		for _, def := range v18 {
			if _, extra := IsV18ProductionTool(def.Name); !extra {
				kept = append(kept, def)
				continue
			}
			if kit, ok := kitOf[def.Name]; ok {
				perKit[kit]++
			} else {
				first++
			}
		}
		if !reflect.DeepEqual(kept, v16) {
			t.Fatalf("seed %d: v18 reorders or changes the v16 surface", seed)
		}
		// First-party tools the v16 surface already carries (the bench's own
		// simulated product tools) appear once, as the simulated tool.
		onSurface := map[string]bool{}
		for _, def := range v16 {
			onSurface[def.Name] = true
		}
		wantFirst := 0
		for _, tool := range snap.FirstParty {
			if !onSurface[tool.Name] {
				wantFirst++
			}
		}
		if first != wantFirst {
			t.Fatalf("seed %d: %d first-party tools, want %d", seed, first, wantFirst)
		}
		linked := map[string]bool{}
		for _, app := range V16ConnectorPlanForSeed(seed).Linked {
			for _, slug := range v18LinkedToolkitSlugs(app.Slug) {
				linked[slug] = true
			}
		}
		for kit, n := range perKit {
			if n != kitSize[kit] {
				t.Fatalf("seed %d: toolkit %s offers %d of its %d tools", seed, kit, n, kitSize[kit])
			}
			if !v18ToolkitEligible(V18SnapshotToolkit{Slug: kit, Tools: make([]V18SnapshotTool, 1)}) || linked[kit] {
				t.Fatalf("seed %d samples ineligible toolkit %s", seed, kit)
			}
		}
	}
	if len(sizes) < 10 {
		t.Fatalf("40 seeds produced only %d distinct menu sizes", len(sizes))
	}
	a, b := CatalogForSeed(protocol.BenchVersionV18, 1), CatalogForSeed(protocol.BenchVersionV18, 2)
	if reflect.DeepEqual(a, b) {
		t.Fatal("v18 surface is not seeded")
	}
}

func TestCatalogSizeIsOneVersionedNumber(t *testing.T) {
	if _, ok := CatalogSizeForVersion(protocol.BenchVersionV17); ok {
		t.Fatal("v17 samples no snapshot menu")
	}
	if size, ok := CatalogSizeForVersion(protocol.BenchVersionV18); !ok || size != (CatalogSize{Min: 1400, Max: 2000}) {
		t.Fatalf("v18 size %+v", size)
	}
	if MaxCatalogTools() != 2000 {
		t.Fatalf("max %d", MaxCatalogTools())
	}
}

func TestV18ParaphraseKeepsMeaningAndVariesBySeed(t *testing.T) {
	text := "Retrieves the specified record using its identifier"
	seen := map[string]bool{}
	for seed := int64(1); seed <= 20; seed++ {
		out := v18Paraphrase(seed, "K", "Kit", text)
		seen[out] = true
		if !strings.Contains(out, "record") {
			t.Fatalf("paraphrase lost the object: %q", out)
		}
	}
	if len(seen) < 4 {
		t.Fatalf("only %d paraphrases over 20 seeds", len(seen))
	}
	if got := v18ReplaceWord("pre-Retrieves Retrieves-x Retrieves", "Retrieves", "Fetches"); got != "pre-Retrieves Retrieves-x Fetches" {
		t.Fatalf("hyphenated compound rewritten: %q", got)
	}
	if got := v18ReplaceWord("Retrievesx Retrieves", "Retrieves", "Fetches"); got != "Retrievesx Fetches" {
		t.Fatalf("whole-word replace: %q", got)
	}
}

func TestV18ConnectorRecognitionIsVersionGated(t *testing.T) {
	if err := V18SnapshotReady(); err != nil {
		t.Fatal(err)
	}
	name := v18Snapshot().Toolkits[0].Tools[0].Slug
	if _, ok := IsConnectorToolForVersion(name, protocol.BenchVersionV18); !ok {
		t.Fatal("snapshot tool not refused at v18")
	}
	if _, ok := IsConnectorToolForVersion(name, protocol.BenchVersionV17); ok {
		t.Fatal("snapshot tool refused below v18")
	}
	if !reflect.DeepEqual(CatalogForSeed(protocol.BenchVersionV17, 9), v16SeededCatalog(9)) {
		t.Fatal("v17 surface moved")
	}
}

// A snapshot name that is also a simulated tool keeps its simulated
// behaviour: the refusal index never contains a product or pool tool.
func TestV18RefusalIndexNeverShadowsSimulatedTools(t *testing.T) {
	if err := V18SnapshotReady(); err != nil {
		t.Fatal(err)
	}
	for _, def := range v13ProductionCatalog() {
		if _, refused := IsConnectorToolForVersion(def.Name, protocol.BenchVersionV18); refused {
			t.Fatalf("product tool %s is refused at v18", def.Name)
		}
	}
}

// The checked lookup serves every seed's menu inside the contract (it errors
// rather than serving one outside), toolkit schemas are independent copies,
// and the checked connector classification agrees with the unchecked one.
func TestV18CheckedCatalogHoldsTheContract(t *testing.T) {
	size, _ := CatalogSizeForVersion(protocol.BenchVersionV18)
	seeds := 200
	if testing.Short() {
		seeds = 20
	}
	for seed := int64(1); seed <= int64(seeds); seed++ {
		tools, err := CatalogForSeedChecked(protocol.BenchVersionV18, seed)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if n := len(tools); n < size.Min || n > size.Max {
			t.Fatalf("seed %d: %d tools outside %d..%d", seed, n, size.Min, size.Max)
		}
	}
	extra := V18ProductionToolsForSeed(3, protocol.BenchVersionV18)
	var kitTools []int
	for i, def := range extra {
		if string(def.Parameters) == string(v18ToolkitSchema) {
			kitTools = append(kitTools, i)
		}
	}
	if len(kitTools) < 2 {
		t.Fatal("seed 3 offers fewer than two toolkit tools")
	}
	extra[kitTools[0]].Parameters[0] = 'X'
	if extra[kitTools[1]].Parameters[0] == 'X' || v18ToolkitSchema[0] == 'X' {
		t.Fatal("toolkit definitions share one mutable schema")
	}
	name := v18Snapshot().Toolkits[0].Tools[0].Slug
	for _, n := range []string{name, "search_web", "slack_post_message", "nonexistent_tool"} {
		app, ok := IsConnectorToolForVersion(n, protocol.BenchVersionV18)
		capp, cok, err := IsConnectorToolForVersionChecked(n, protocol.BenchVersionV18)
		if err != nil || app != capp || ok != cok {
			t.Fatalf("%s: checked (%q %v %v) disagrees with unchecked (%q %v)", n, capp, cok, err, app, ok)
		}
	}
}
