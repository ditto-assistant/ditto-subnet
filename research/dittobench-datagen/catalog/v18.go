package catalog

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"sync"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v18 production catalog.
//
// A real Ditto workspace offers its first-party tools plus every tool of every
// toolkit the user enables. V18 builds each seed's menu from a PINNED snapshot
// of real toolkits (the production Composio mirror) and of Ditto's
// first-party tool catalog. The seed picks whole toolkits at random until the
// seed's menu lands inside CatalogSizeForVersion, so the skew
// stays realistic: a few huge toolkits, many small ones. A harness that hands
// the whole menu to the model on every turn cannot keep that up; one that
// defers or searches tools can, which is what Ditto's own harness does.
//
// Contract rules:
//
//   - The snapshot is never read live. It is embedded in the module and its
//     SHA-256 must equal V18CatalogSnapshotSHA256 (generation fails closed
//     otherwise), so every validator generates identical datasets. Changing
//     the snapshot is a new bench version.
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

// CatalogSize bounds one seed's whole menu (inclusive).
type CatalogSize struct{ Min, Max int }

// catalogSizes is the per-contract menu size, keyed by the bench version that
// introduced it; a later version inherits the newest entry at or below it.
// The production range is 1,400 to 5,000 tools; the bench grows toward it one
// version at a time, so a later contract changes ONE number here plus the
// pinned snapshot. V18 starts at the low end: weak, expensive harnesses that
// forward the whole menu are already culled there.
var catalogSizes = []struct {
	BenchVersion int
	Size         CatalogSize
}{
	{protocol.BenchVersionV18, CatalogSize{Min: 1400, Max: 2000}},
}

// CatalogSizeForVersion returns the menu size a contract samples, or false
// below v18 (no snapshot menu).
func CatalogSizeForVersion(benchVersion int) (CatalogSize, bool) {
	var out CatalogSize
	ok := false
	for _, entry := range catalogSizes {
		if benchVersion >= entry.BenchVersion {
			out, ok = entry.Size, true
		}
	}
	return out, ok
}

// MaxCatalogTools is the largest menu any supported contract samples.
func MaxCatalogTools() int {
	out := 0
	for _, entry := range catalogSizes {
		out = max(out, entry.Size.Max)
	}
	return out
}

// V18CatalogSnapshotSHA256 pins the snapshot every v18 dataset is built
// from. Empty until the production snapshot is pinned: v18 generation fails
// closed until then (V18SnapshotReady).
const V18CatalogSnapshotSHA256 = "2f38d1bf046ed60b53d11d54a0b8ab69c042d17eed3bd27d1e161e385fa06756"

// v18SnapshotGzip is the pinned snapshot, embedded so every binary built from
// this module generates from the same bytes: the production Composio mirror
// (1,717 enabled toolkits, 77,948 enabled non-deprecated tools) and Ditto's
// 128 first-party tools, read-only on 2026-11-07.
//
//go:embed v18snapshot/catalog-2026-11-07.json.gz
var v18SnapshotGzip []byte

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
	v18SnapshotErr    = errors.New("v18 catalog snapshot not loaded")
	v18SnapshotOnce   sync.Once
	v18Index          map[string]string
	v18FirstParty     map[string]bool
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
	// A snapshot name that is also a product tool or a v15 pool connector is
	// the simulated tool itself (Ditto's first-party catalog shares names
	// with the bench's product tools): it must keep its simulated behaviour,
	// so it never enters the refusal index.
	reserved := map[string]bool{}
	for _, def := range v13ProductionCatalog() {
		reserved[def.Name] = true
	}
	for _, t := range connectorUniverse() {
		reserved[t.name()] = true
	}
	index := map[string]string{}
	for _, kit := range snap.Toolkits {
		for _, tool := range kit.Tools {
			if !reserved[tool.Slug] {
				index[tool.Slug] = kit.Name
			}
		}
	}
	firstParty := map[string]bool{}
	for _, tool := range snap.FirstParty {
		if !reserved[tool.Name] {
			index[tool.Name] = "Ditto"
			firstParty[tool.Name] = true
		}
	}
	v18Index = index
	v18FirstParty = firstParty
}

// V18SnapshotReady verifies and decodes the embedded snapshot once and
// reports whether v18 generation can run. Nothing below v18 calls it.
func V18SnapshotReady() error {
	v18SnapshotOnce.Do(func() {
		snap, err := LoadV18Snapshot(v18SnapshotGzip, V18CatalogSnapshotSHA256)
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

// v18LinkedToolkitAliases maps a v16 linkable app to the snapshot toolkits
// that are the same product, so a seed that links the app never shows those
// toolkits' "not connected" tools. Read from the 2026-11-07 snapshot; an app
// not listed maps to its own slug and "<slug>_mcp".
var v18LinkedToolkitAliases = map[string][]string{
	"slack":       {"slack", "slackbot"},
	"github":      {"github", "github_mcp"},
	"teams":       {"microsoft_teams"},
	"discord":     {"discord", "discordbot"},
	"wordpress":   {"wordpress", "wordpress_com"},
	"salesforce":  {"salesforce", "salesforce_data_360", "salesforce_service_cloud"},
	"googledrive": {"googledrive", "google_drive"},
	"statuspage":  {"statuspage", "atlassian_statuspage"},
}

// v18LinkedToolkitSlugs returns the snapshot toolkit slugs one v16 app covers.
func v18LinkedToolkitSlugs(app string) []string {
	app = strings.ToLower(app)
	if aliases, ok := v18LinkedToolkitAliases[app]; ok {
		return aliases
	}
	return []string{app, app + "_mcp"}
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
// served names the tools already on the menu; only a toolkit's new names
// count toward the target, exactly the tools V18ProductionToolsForSeed emits.
func v18SampledToolkits(seed int64, benchVersion int, snap *V18Snapshot, served map[string]bool) ([]V18SnapshotToolkit, int) {
	size, _ := CatalogSizeForVersion(benchVersion)
	linked := map[string]bool{}
	for _, app := range V16ConnectorPlanForSeed(seed).Linked {
		for _, slug := range v18LinkedToolkitSlugs(app.Slug) {
			linked[slug] = true
		}
	}
	var eligible []V18SnapshotToolkit
	for _, kit := range snap.Toolkits {
		if v18ToolkitEligible(kit) && !linked[strings.ToLower(kit.Slug)] {
			eligible = append(eligible, kit)
		}
	}
	target := size.Min + v13Pick(seed, "v18-catalog-target", size.Max-size.Min+1)
	seen := make(map[string]bool, len(served))
	for name := range served {
		seen[name] = true
	}
	total := len(seen)
	var out []V18SnapshotToolkit
	for _, index := range v13Perm(seed, "v18-toolkit-order", len(eligible)) {
		if total >= target {
			break
		}
		kit := eligible[index]
		fresh := 0
		for _, tool := range kit.Tools {
			if !seen[tool.Slug] {
				fresh++
			}
		}
		if fresh == 0 || total+fresh > size.Max {
			continue
		}
		for _, tool := range kit.Tools {
			seen[tool.Slug] = true
		}
		out = append(out, kit)
		total += fresh
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

// isWordByte counts hyphens as word bytes, so a swap never rewrites part of a
// hyphenated compound ("pre-existing" stays "pre-existing").
func isWordByte(c byte) bool {
	return c == '_' || c == '\'' || c == '-' || c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
}

// v18ToolkitSchema is the input schema every snapshot toolkit tool renders.
var v18ToolkitSchema = schema(nil, prop{name: "input", typ: "string", desc: "optional request for the tool"})

// V18ProductionToolsForSeed returns the snapshot tools one seed offers:
// first-party tools that the v16 surface does not already carry, then the
// sampled toolkits' tools, each with its seeded description.
func V18ProductionToolsForSeed(seed int64, benchVersion int) []protocol.ToolDefinition {
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
	kits, _ := v18SampledToolkits(seed, benchVersion, snap, surface)
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
				// A copy per definition: a caller that edits one schema in
				// place must not change every other toolkit tool's.
				Parameters: append(json.RawMessage(nil), v18ToolkitSchema...),
			})
		}
	}
	return out
}

// CatalogForSeedChecked is CatalogForSeed for callers that must fail cleanly
// instead of panicking when a v18 snapshot is missing.
// It also fails, rather than serving a menu outside the contract, when the
// seed's menu misses CatalogSizeForVersion.
func CatalogForSeedChecked(benchVersion int, seed int64) ([]protocol.ToolDefinition, error) {
	if benchVersion >= protocol.BenchVersionV18 {
		if err := V18SnapshotReady(); err != nil {
			return nil, err
		}
	}
	tools := CatalogForSeed(benchVersion, seed)
	if size, ok := CatalogSizeForVersion(benchVersion); ok && (len(tools) < size.Min || len(tools) > size.Max) {
		return nil, fmt.Errorf("bench_version %d seed %d: catalog of %d tools outside the contract's %d..%d", benchVersion, seed, len(tools), size.Min, size.Max)
	}
	return tools, nil
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

// IsV18FirstPartyTool reports whether name is a refused first-party snapshot
// tool. Ditto is not a linkable app, so the mock refuses these as unavailable
// in the benchmark workspace rather than with the connector template.
func IsV18FirstPartyTool(name string) bool {
	if V18SnapshotReady() != nil {
		return false
	}
	v18SnapshotMu.RLock()
	defer v18SnapshotMu.RUnlock()
	return v18FirstParty[name]
}

// IsConnectorToolForVersionChecked is IsConnectorToolForVersion that reports
// an unavailable v18 snapshot instead of answering "not a connector" for a
// name it cannot look up. Callers that enforce the refusal use it.
func IsConnectorToolForVersionChecked(name string, benchVersion int) (appName string, ok bool, err error) {
	if appName, ok := IsConnectorTool(name); ok {
		return appName, true, nil
	}
	if benchVersion >= protocol.BenchVersionV18 {
		if err := V18SnapshotReady(); err != nil {
			return "", false, err
		}
		appName, ok := IsV18ProductionTool(name)
		return appName, ok, nil
	}
	return "", false, nil
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
func v18SeededCatalog(seed int64, benchVersion int) []protocol.ToolDefinition {
	base := v16SeededCatalog(seed)
	extra := V18ProductionToolsForSeed(seed, benchVersion)
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
