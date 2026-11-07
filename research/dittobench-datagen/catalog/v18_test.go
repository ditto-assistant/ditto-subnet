package catalog

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func fixtureBytes(t *testing.T) ([]byte, string) {
	t.Helper()
	raw, err := json.Marshal(V18FixtureSnapshot())
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	if _, err := zw.Write(raw); err != nil {
		t.Fatal(err)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(buf.Bytes())
	return buf.Bytes(), hex.EncodeToString(sum[:])
}

// The snapshot is pinned by hash: a wrong or missing pin fails closed.
func TestV18SnapshotIsHashPinned(t *testing.T) {
	raw, sum := fixtureBytes(t)
	if _, err := LoadV18Snapshot(raw, sum); err != nil {
		t.Fatalf("pinned fixture rejected: %v", err)
	}
	if _, err := LoadV18Snapshot(raw, strings.Repeat("0", 64)); err == nil {
		t.Fatal("a snapshot whose hash differs from the pin was accepted")
	}
	if _, err := LoadV18Snapshot(raw, ""); err == nil {
		t.Fatal("an unpinned snapshot was accepted")
	}
	if V18CatalogSnapshotSHA256 == "" {
		t.Log("V18CatalogSnapshotSHA256 is not pinned yet: production v18 generation fails closed until it is")
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
		if first != len(snap.FirstParty) {
			t.Fatalf("seed %d: %d first-party tools, want %d", seed, first, len(snap.FirstParty))
		}
		linked := map[string]bool{}
		for _, app := range V16ConnectorPlanForSeed(seed).Linked {
			linked[app.Slug] = true
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
