package catalog

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v18 production catalog.
//
// A real Ditto workspace offers its first-party tools plus every tool of every
// toolkit the user enables. V18 builds each seed's menu from a PINNED snapshot
// of real toolkits (the production Composio mirror) and of Ditto's
// first-party tool catalog. The seed picks whole toolkits at random until the
// seed's menu holds between V18MinTools and V18MaxTools tools, so the skew
// stays realistic: a few huge toolkits, many small ones. A harness that hands
// the whole menu to the model on every turn cannot keep that up; one that
// defers or searches tools can, which is what Ditto's own harness does.
//
// Contract rules:
//
//   - The snapshot is never read live. Generation loads a snapshot file whose
//     SHA-256 must equal V18CatalogSnapshotSHA256 and fails closed otherwise,
//     so every validator generates identical datasets. Changing the snapshot
//     is a new bench version.
//   - Graded cases stay on tools the datagen simulates (the v16 surface).
//     Snapshot tools are realistic bulk: every one is refused by the mock
//     endpoint and performs nothing, and no case expects one.
//   - Toolkits whose capability a product tool owns (email, calendar, web
//     search, maps, weather, translation, images, video) are never sampled, so
//     a snapshot tool is never a defensible answer to a generated request. A
//     toolkit for an app the seed links (V16ConnectorPlanForSeed) is skipped
//     on that seed, so a linked app never shows tools that say "not connected".
//   - Descriptions are paraphrased per seed (v18Paraphrase), so a published
//     catalog cannot simply be memorized.
//
// The snapshot carries no input schemas for toolkit tools (the production
// mirror stores none), so each renders one optional free-form argument.
// First-party tools keep their own schemas.

// V18MinTools and V18MaxTools bound one seed's whole menu.
const (
	V18MinTools = 1400
	V18MaxTools = 5000
)

// V18CatalogSnapshotSHA256 pins the snapshot every v18 dataset is built
// from. Empty until the production snapshot is pinned: v18 generation fails
// closed until then (V18SnapshotReady).
const V18CatalogSnapshotSHA256 = ""

// V18CatalogSnapshotEnv names the snapshot file a process loads.
const V18CatalogSnapshotEnv = "DITTOBENCH_V18_CATALOG_SNAPSHOT"

// V18Snapshot is the pinned catalog snapshot (gzip-compressed JSON).
type V18Snapshot struct {
	Source     string                 `json:"source"`
	CapturedAt string                 `json:"captured_at"`
	Toolkits   []V18SnapshotToolkit   `json:"toolkits"`
	FirstParty []V18SnapshotFirstTool `json:"first_party"`
}

// V18SnapshotToolkit is one real toolkit, whole.
type V18SnapshotToolkit struct {
	Slug  string            `json:"slug"`
	Name  string            `json:"name"`
	Tools []V18SnapshotTool `json:"tools"`
}

// V18SnapshotTool is one toolkit tool: its slug and description.
type V18SnapshotTool struct {
	Slug        string `json:"slug"`
	Description string `json:"description"`
}

// V18SnapshotFirstTool is one Ditto first-party tool with its schema.
type V18SnapshotFirstTool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Parameters  json.RawMessage `json:"parameters"`
}

var (
	v18SnapshotMu     sync.RWMutex
	v18SnapshotLoaded *V18Snapshot
	v18SnapshotErr    = errors.New("v18 catalog snapshot not loaded: set " + V18CatalogSnapshotEnv)
	v18SnapshotOnce   sync.Once
	v18Index          map[string]string
)

// LoadV18Snapshot verifies raw against want and decodes it.
func LoadV18Snapshot(raw []byte, want string) (*V18Snapshot, error) {
	sum := sha256.Sum256(raw)
	if got := hex.EncodeToString(sum[:]); want == "" || got != want {
		return nil, fmt.Errorf("v18 catalog snapshot sha256 %s does not match the pinned %q", got, want)
	}
	zr, err := gzip.NewReader(bytes.NewReader(raw))
	if err != nil {
		return nil, fmt.Errorf("v18 catalog snapshot: %w", err)
	}
	decoded, err := io.ReadAll(zr)
	if err != nil {
		return nil, fmt.Errorf("v18 catalog snapshot: %w", err)
	}
	var snap V18Snapshot
	if err := json.Unmarshal(decoded, &snap); err != nil {
		return nil, fmt.Errorf("v18 catalog snapshot: %w", err)
	}
	return &snap, nil
}

// useV18Snapshot installs a verified snapshot for the process.
func useV18Snapshot(snap *V18Snapshot) {
	v18SnapshotMu.Lock()
	defer v18SnapshotMu.Unlock()
	v18SnapshotLoaded, v18SnapshotErr = snap, nil
	index := map[string]string{}
	for _, kit := range snap.Toolkits {
		for _, tool := range kit.Tools {
			index[tool.Slug] = kit.Name
		}
	}
	for _, tool := range snap.FirstParty {
		index[tool.Name] = "Ditto"
	}
	v18Index = index
}

// V18SnapshotReady loads the pinned snapshot from V18CatalogSnapshotEnv once
// and reports whether v18 generation can run. Nothing below v18 calls it.
func V18SnapshotReady() error {
	v18SnapshotOnce.Do(func() {
		path := os.Getenv(V18CatalogSnapshotEnv)
		if path == "" {
			if testing.Testing() {
				// Test binaries without a snapshot use the deterministic
				// fixture, so suites stay hermetic. Production binaries never
				// reach this branch.
				useV18Snapshot(V18FixtureSnapshot())
			}
			return
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			v18SnapshotMu.Lock()
			v18SnapshotErr = fmt.Errorf("v18 catalog snapshot: %w", err)
			v18SnapshotMu.Unlock()
			return
		}
		snap, err := LoadV18Snapshot(raw, V18CatalogSnapshotSHA256)
		if err != nil {
			v18SnapshotMu.Lock()
			v18SnapshotErr = err
			v18SnapshotMu.Unlock()
			return
		}
		useV18Snapshot(snap)
	})
	v18SnapshotMu.RLock()
	defer v18SnapshotMu.RUnlock()
	return v18SnapshotErr
}

func v18Snapshot() *V18Snapshot {
	if err := V18SnapshotReady(); err != nil {
		// Generation entry points check V18SnapshotReady first; reaching here
		// means a caller skipped it, and silently serving the v16 menu would
		// be a different dataset.
		panic(err)
	}
	v18SnapshotMu.RLock()
	defer v18SnapshotMu.RUnlock()
	return v18SnapshotLoaded
}

// v18ExcludedCapabilities are toolkit slug fragments whose capability a
// product tool or coined decoy owns. Matching toolkits are never sampled.
var v18ExcludedCapabilities = []string{
	"gmail", "outlook", "mail", "calendar", "calendly", "cal_com",
	"search", "serp", "tavily", "exa", "perplexity", "brave", "bing", "duckduckgo",
	"maps", "weather", "translat", "deepl", "image", "dalle", "midjourney", "stability",
	"youtube", "vimeo", "video", "memory", "reminder",
}

// v18ToolkitEligible reports whether a toolkit may be sampled on any seed.
func v18ToolkitEligible(kit V18SnapshotToolkit) bool {
	if len(kit.Tools) == 0 {
		return false
	}
	slug := strings.ToLower(kit.Slug)
	for _, fragment := range v18ExcludedCapabilities {
		if strings.Contains(slug, fragment) {
			return false
		}
	}
	return true
}

// v18SampledToolkits returns the toolkits one seed offers, in sampled order,
// and the target menu size the seed drew. base is the size of the rest of the
// menu (the v16 surface plus first-party tools).
func v18SampledToolkits(seed int64, snap *V18Snapshot, base int) ([]V18SnapshotToolkit, int) {
	linked := map[string]bool{}
	for _, app := range V16ConnectorPlanForSeed(seed).Linked {
		linked[strings.ToLower(app.Slug)] = true
	}
	var eligible []V18SnapshotToolkit
	for _, kit := range snap.Toolkits {
		if v18ToolkitEligible(kit) && !linked[strings.ToLower(kit.Slug)] {
			eligible = append(eligible, kit)
		}
	}
	target := V18MinTools + v13Pick(seed, "v18-catalog-target", V18MaxTools-V18MinTools+1)
	total := base
	var out []V18SnapshotToolkit
	for _, index := range v13Perm(seed, "v18-toolkit-order", len(eligible)) {
		if total >= target {
			break
		}
		kit := eligible[index]
		if total+len(kit.Tools) > V18MaxTools {
			continue
		}
		out = append(out, kit)
		total += len(kit.Tools)
	}
	return out, target
}

// v18Paraphrase rewrites a snapshot description for one seed: a seeded subset
// of v18Synonyms and a seeded framing. The meaning is unchanged.
func v18Paraphrase(seed int64, key, toolkit, description string) string {
	text := strings.TrimSpace(description)
	for i, pair := range v18Synonyms {
		if v13Pick(seed, fmt.Sprintf("v18-syn:%s:%d", key, i), 2) == 1 {
			text = v18ReplaceWord(text, pair[0], pair[1])
		}
	}
	if text != "" && !strings.HasSuffix(text, ".") {
		text += "."
	}
	switch v13Pick(seed, "v18-frame:"+key, 3) {
	case 1:
		return toolkit + ": " + text
	case 2:
		return text + " (" + toolkit + ")"
	default:
		return text
	}
}

// v18Synonyms are meaning-preserving word swaps (word, replacement).
var v18Synonyms = [][2]string{
	{"Retrieves", "Fetches"}, {"retrieves", "fetches"}, {"Creates", "Adds"}, {"creates", "adds"},
	{"Updates", "Modifies"}, {"updates", "modifies"}, {"Deletes", "Removes"}, {"deletes", "removes"},
	{"Lists", "Returns"}, {"specified", "given"}, {"existing", "current"}, {"information", "details"},
	{"identifier", "ID"}, {"user's", "account's"}, {"using", "with"}, {"allows", "lets"},
}

// v18ReplaceWord replaces whole-word occurrences of from with to.
func v18ReplaceWord(text, from, to string) string {
	var b strings.Builder
	for i := 0; i < len(text); {
		j := strings.Index(text[i:], from)
		if j < 0 {
			b.WriteString(text[i:])
			break
		}
		j += i
		before := j == 0 || !isWordByte(text[j-1])
		after := j+len(from) >= len(text) || !isWordByte(text[j+len(from)])
		b.WriteString(text[i:j])
		if before && after {
			b.WriteString(to)
		} else {
			b.WriteString(from)
		}
		i = j + len(from)
	}
	return b.String()
}

func isWordByte(c byte) bool {
	return c == '_' || c == '\'' || c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
}

// v18ToolkitSchema is the input schema every snapshot toolkit tool renders.
var v18ToolkitSchema = schema(nil, prop{name: "input", typ: "string", desc: "optional request for the tool"})

// V18ProductionToolsForSeed returns the snapshot tools one seed offers:
// first-party tools that the v16 surface does not already carry, then the
// sampled toolkits' tools, each with its seeded description.
func V18ProductionToolsForSeed(seed int64) []protocol.ToolDefinition {
	snap := v18Snapshot()
	surface := map[string]bool{}
	for _, def := range v16SeededCatalog(seed) {
		surface[def.Name] = true
	}
	var out []protocol.ToolDefinition
	for _, tool := range snap.FirstParty {
		if surface[tool.Name] {
			continue
		}
		surface[tool.Name] = true
		out = append(out, protocol.ToolDefinition{
			Name:        tool.Name,
			Description: v18Paraphrase(seed, tool.Name, "Ditto", tool.Description),
			Parameters:  append(json.RawMessage(nil), tool.Parameters...),
		})
	}
	kits, _ := v18SampledToolkits(seed, snap, len(surface))
	for _, kit := range kits {
		tools := append([]V18SnapshotTool(nil), kit.Tools...)
		sort.Slice(tools, func(a, b int) bool { return tools[a].Slug < tools[b].Slug })
		for _, tool := range tools {
			if surface[tool.Slug] {
				continue
			}
			surface[tool.Slug] = true
			out = append(out, protocol.ToolDefinition{
				Name:        tool.Slug,
				Description: v18Paraphrase(seed, tool.Slug, kit.Name, tool.Description),
				Parameters:  v18ToolkitSchema,
			})
		}
	}
	return out
}

// CatalogForSeedChecked is CatalogForSeed for callers that must fail cleanly
// instead of panicking when a v18 snapshot is missing.
func CatalogForSeedChecked(benchVersion int, seed int64) ([]protocol.ToolDefinition, error) {
	if benchVersion >= protocol.BenchVersionV18 {
		if err := V18SnapshotReady(); err != nil {
			return nil, err
		}
	}
	return CatalogForSeed(benchVersion, seed), nil
}

// IsV18ProductionTool reports whether name is a snapshot tool (first-party or
// toolkit), with its toolkit's display name. It is seed-independent: the mock
// refuses every snapshot name, offered on this seed or not.
func IsV18ProductionTool(name string) (appName string, ok bool) {
	if V18SnapshotReady() != nil {
		return "", false
	}
	v18SnapshotMu.RLock()
	defer v18SnapshotMu.RUnlock()
	appName, ok = v18Index[name]
	return appName, ok
}

// IsConnectorToolForVersion is IsConnectorTool under an explicit contract:
// from v18 every snapshot tool is refused like an unlinked connector.
func IsConnectorToolForVersion(name string, benchVersion int) (appName string, ok bool) {
	if appName, ok := IsConnectorTool(name); ok {
		return appName, true
	}
	if benchVersion >= protocol.BenchVersionV18 {
		return IsV18ProductionTool(name)
	}
	return "", false
}

// v18SeededCatalog is the v16 surface with the seed's snapshot tools spliced
// in at seeded positions (their own slot keys, so the v16 order is kept).
func v18SeededCatalog(seed int64) []protocol.ToolDefinition {
	base := v16SeededCatalog(seed)
	extra := V18ProductionToolsForSeed(seed)
	byPos := make([][]protocol.ToolDefinition, len(base)+1)
	for i, def := range extra {
		pos := v13Pick(seed, fmt.Sprintf("v18-slot:%d", i), len(base)+1)
		byPos[pos] = append(byPos[pos], def)
	}
	out := make([]protocol.ToolDefinition, 0, len(base)+len(extra))
	for pos := 0; pos <= len(base); pos++ {
		out = append(out, byPos[pos]...)
		if pos < len(base) {
			out = append(out, base[pos])
		}
	}
	return out
}
