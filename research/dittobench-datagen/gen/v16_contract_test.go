package gen

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// v16KnownVectorWant is the dueling contract's datagen-half vector (seed
// 123456789, full profile, public surface salt 0). docs/bench-versions.md and
// the README publish the same string.
const v16KnownVectorWant = "30ab40c287fe7457859b4040e1334bcc5a0f8b50cc49f97e5bd98650753de34d"

// V16 is the dueling contract, STAGED: generatable with its own epoch and seed
// domain, not yet in the supported set. This module generates only the
// datagen half; Platform delivers the generator slice.
func TestV16KnownVector(t *testing.T) {
	prof, ok := ProfileForVersion("full", protocol.BenchVersionV16)
	if !ok {
		t.Fatal("v16 full profile missing")
	}
	a, err := GenerateDataset(123456789, prof, protocol.BenchVersionV16)
	if err != nil {
		t.Fatal(err)
	}
	digest, _, err := a.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	if digest != v16KnownVectorWant {
		t.Fatalf("v16 known vector: got %s, want %s", digest, v16KnownVectorWant)
	}
	if a.BenchVersion != 16 || a.GeneratedAt != "2027-08-01T00:00:00Z" {
		t.Fatalf("v16 envelope: version=%d epoch=%s", a.BenchVersion, a.GeneratedAt)
	}
	if protocol.SupportedBenchVersion(protocol.BenchVersionV16) || !protocol.StagedBenchVersion(protocol.BenchVersionV16) {
		t.Fatal("v16 must stay staged until the cross-layer PR raises the shared ceiling")
	}
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatal("preactivation default moved")
	}
	for _, doc := range []string{"../docs/bench-versions.md", "../README.md"} {
		if !strings.Contains(readFile(t, doc), v16KnownVectorWant) {
			t.Fatalf("%s does not publish the pinned v16 vector", doc)
		}
	}
}

// The v16 profile is the datagen half of the 350-case dueling run: full is 50
// tool + 125 memory = 175 cases, and the tool and memory phases draw one
// world scale (the tool passes key their world on the case count).
func TestV16ProfileIsTheDatagenHalf(t *testing.T) {
	want := map[string][2]int{"small": {6, 27}, "medium": {24, 63}, "full": {50, 125}}
	for runSize, counts := range want {
		prof, ok := ProfileForVersion(runSize, protocol.BenchVersionV16)
		if !ok || prof.Tools != counts[0] {
			t.Fatalf("%s: profile %+v (ok %v), want %d tool cases", runSize, prof, ok, counts[0])
		}
		env, ok := MemoryEnvelopeForRunSize(runSize, protocol.BenchVersionV16)
		if !ok || env.Total() != counts[1] {
			t.Fatalf("%s: memory table %+v (ok %v), want %d", runSize, env, ok, counts[1])
		}
		// One world per run, at the v15 scale of the same run size.
		toolScale := datagen.ToolWorldScale(prof.Tools, protocol.BenchVersionV16)
		memScale, _ := v8WorldProfile(prof.Mem)
		v15, _ := ProfileForVersion(runSize, protocol.BenchVersionV15)
		v15Scale, _ := v8WorldProfile(v15.Mem)
		if memScale != toolScale || memScale != v15Scale || datagen.ToolWorldScale(v15.Tools, protocol.BenchVersionV15) != v15Scale {
			t.Fatalf("%s: tool world scale %d, memory %d, v15 %d", runSize, toolScale, memScale, v15Scale)
		}
		if prof.IsoCases != v15.IsoCases || prof.Waves != v15.Waves {
			t.Fatalf("%s: isolation/waves %d/%d differ from v15 %d/%d", runSize, prof.IsoCases, prof.Waves, v15.IsoCases, v15.Waves)
		}
		for _, seed := range []int64{1, 7, 123456789} {
			a, err := GenerateDataset(seed, prof, protocol.BenchVersionV16)
			if err != nil {
				t.Fatalf("%s seed %d: %v", runSize, seed, err)
			}
			if len(a.ToolCases) != counts[0] || len(a.MemoryCases) != counts[1] {
				t.Fatalf("%s seed %d: %d tool + %d memory, want %v", runSize, seed, len(a.ToolCases), len(a.MemoryCases), counts)
			}
		}
	}
	if got := want["full"][0] + want["full"][1]; got != V16DatagenHalfCases {
		t.Fatalf("full datagen half %d, want %d", got, V16DatagenHalfCases)
	}
	if V16FullEnvelope.Total() != 125 || V13FullEnvelope.Total() != 250 {
		t.Fatal("published slot tables moved")
	}
	// The generator half (Platform's slice) is the same size as this half.
	if protocol.DuelingSliceColumns*protocol.DuelingSliceCasesPerColumn != V16DatagenHalfCases {
		t.Fatalf("slice %d x %d != datagen half %d", protocol.DuelingSliceColumns, protocol.DuelingSliceCasesPerColumn, V16DatagenHalfCases)
	}
	if _, ok := memoryEnvelopeForVersion(profilesV16["full"].Mem, protocol.NewestGeneratableBenchVersion()+1); ok {
		t.Fatal("an ungeneratable version inherited the v16 slot table")
	}
}

// The full v16 memory mix passes MixGateV16 (MixGateV13 unchanged, abstention
// target included) on every swept seed.
func TestV16MixAuditGate(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV16)
	for seed := int64(1); seed <= 12; seed++ {
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV16)
		if err != nil {
			t.Fatal(err)
		}
		audit, err := AuditMix(a)
		if err != nil {
			t.Fatal(err)
		}
		if v := MixGateForVersion(protocol.BenchVersionV16).Violations(audit); len(v) > 0 {
			t.Fatalf("seed %d: v16 mix gate violations %v", seed, v)
		}
	}
	if MixGateV16 != MixGateV13 || MixGateForVersion(protocol.BenchVersionV15).AbstentionTarget != 0.10 {
		t.Fatalf("abstention targets: v16 %.3f, v15 %.3f", MixGateV16.AbstentionTarget, MixGateForVersion(15).AbstentionTarget)
	}
}

// Section 8.1 (no marker): no v16 tool definition carries the v15 suffix or
// any link-state phrase, and the catalog still carries 200 connector tools.
func TestV16ToolDefinitionsCarryNoLinkState(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV16)
	for _, seed := range []int64{1, 7, 42, 123456789} {
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV16)
		if err != nil {
			t.Fatal(err)
		}
		connectors := 0
		for _, tool := range a.Catalog {
			if _, ok := catalog.IsConnectorTool(tool.Name); ok {
				connectors++
				if phrase, hit := catalog.HasLinkStatePhrase(tool.Description); hit {
					t.Fatalf("seed %d: %s description %q carries link-state phrase %q", seed, tool.Name, tool.Description, phrase)
				}
				raw, _ := json.Marshal(tool.Parameters)
				if phrase, hit := catalog.HasLinkStatePhrase(string(raw)); hit {
					t.Fatalf("seed %d: %s schema carries link-state phrase %q", seed, tool.Name, phrase)
				}
			}
			if strings.Contains(tool.Description, "Needs the") {
				t.Fatalf("seed %d: %s keeps the v15 connector suffix", seed, tool.Name)
			}
		}
		if connectors != catalog.ConnectorToolsPerSeed {
			t.Fatalf("seed %d: %d connector tools, want %d", seed, connectors, catalog.ConnectorToolsPerSeed)
		}
	}
}

// Section 8.1 (linked connectors and decoys): every seed links a seeded,
// non-empty set; linked cases need a linked connector that the mock endpoint
// serves (reads carry the needle); unlinked probes name a seed-named app the
// endpoint refuses; named-product cases name an app but expect a product tool.
func TestV16LinkedConnectorsAndWordingDecoys(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV16)
	linkedCounts := map[int]bool{}
	namedLinkedProduct := false
	for _, seed := range []int64{1, 2, 3, 7, 42, 123456789} {
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV16)
		if err != nil {
			t.Fatal(err)
		}
		plan := catalog.V16ConnectorPlanForSeed(seed)
		if len(plan.Linked) < catalog.V16LinkedMin || len(plan.Linked) > catalog.V16LinkedMax || len(plan.Named) != catalog.V16NamedUnlinked {
			t.Fatalf("seed %d: plan linked %d named %d", seed, len(plan.Linked), len(plan.Named))
		}
		linkedCounts[len(plan.Linked)] = true
		offered := map[string]bool{}
		for _, tool := range a.Catalog {
			offered[tool.Name] = true
		}
		linkedSlug, namedSlug := map[string]bool{}, map[string]bool{}
		for _, app := range plan.Linked {
			linkedSlug[app.Slug] = true
		}
		for _, app := range plan.Named {
			if linkedSlug[app.Slug] {
				t.Fatalf("seed %d: %s both linked and named-unlinked", seed, app.Slug)
			}
			namedSlug[app.Slug] = true
		}
		for _, app := range append(append([]catalog.ConnectorApp(nil), plan.Linked...), plan.Named...) {
			for _, tool := range app.Tools {
				if !offered[tool] {
					t.Fatalf("seed %d: %s tool %s missing from the catalog", seed, app.Slug, tool)
				}
			}
		}
		count := map[string]int{}
		for _, c := range a.ToolCases {
			if !datagen.IsV16ConnectorCase(c.Category) {
				for _, spec := range c.ExpectedTools {
					if _, isConnector := catalog.IsConnectorTool(spec.Name); isConnector {
						t.Fatalf("seed %d: non-connector case %s (%s) expects %s", seed, c.ID, c.Category, spec.Name)
					}
				}
				continue
			}
			count[c.Category]++
			if len(c.ExpectedTools) != 1 || c.MaxToolCalls != 1 || c.AllowExtraTools {
				t.Fatalf("seed %d: %s must expect exactly one call: %+v", seed, c.ID, c)
			}
			spec := c.ExpectedTools[0]
			f := toolexec.BuildFixtureForVersion(seed, c, protocol.BenchVersionV16)
			switch c.Category {
			case datagen.V16CategoryLinkedRead, datagen.V16CategoryLinkedAction:
				app, linked := plan.LinkedApp(spec.Name)
				if !linked {
					t.Fatalf("seed %d: %s expects %s, which is not linked", seed, c.ID, spec.Name)
				}
				if !strings.Contains(c.Prompt, app.Name) {
					t.Fatalf("seed %d: %s does not name %s: %q", seed, c.ID, app.Name, c.Prompt)
				}
				args, _ := json.Marshal(spec.RequiredArgs)
				out := toolResult(t, f, spec.Name, args)
				if c.Category == datagen.V16CategoryLinkedRead {
					if !datagen.IsResultUsage(c.Category) || spec.Name != app.ReadTool || f.Bearer() != spec.Name || f.NeedleValue() == "" || !strings.Contains(out, f.NeedleValue()) {
						t.Fatalf("seed %d: linked read %s does not serve its needle: bearer %q out %q", seed, c.ID, f.Bearer(), out)
					}
				} else {
					payload := spec.RequiredArgs[app.WriteParam]
					if spec.Name != app.WriteTool || payload == "" || !strings.Contains(c.Prompt, payload) || !strings.Contains(out, payload) {
						t.Fatalf("seed %d: linked action %s: spec %+v out %q", seed, c.ID, spec, out)
					}
				}
			case datagen.V16CategoryUnlinkedProbe:
				app, _ := catalog.IsConnectorTool(spec.Name)
				slug := strings.SplitN(spec.Name, "_", 2)[0]
				if !namedSlug[slug] || !strings.Contains(c.Prompt, app) || datagen.IsResultUsage(c.Category) {
					t.Fatalf("seed %d: unlinked probe %s names %s (%s)", seed, c.ID, spec.Name, c.Prompt)
				}
				args, _ := json.Marshal(spec.RequiredArgs)
				if out, refused := fixtureRefusal(f, spec.Name, args); !refused || !strings.Contains(out, "not connected") {
					t.Fatalf("seed %d: unlinked probe %s is not refused: %q", seed, c.ID, out)
				}
			case datagen.V16CategoryNamedProduct:
				if _, isConnector := catalog.IsConnectorTool(spec.Name); isConnector {
					t.Fatalf("seed %d: named-product %s expects connector %s", seed, c.ID, spec.Name)
				}
				named := false
				for _, app := range append(append([]catalog.ConnectorApp(nil), plan.Linked...), plan.Named...) {
					if strings.Contains(c.Prompt, app.Name) {
						named = true
						if linkedSlug[app.Slug] {
							namedLinkedProduct = true
						}
					}
				}
				if !named {
					t.Fatalf("seed %d: named-product %s names no planned app: %q", seed, c.ID, c.Prompt)
				}
			}
		}
		quota := datagen.V16ConnectorQuotaFor(prof.Tools)
		if count[datagen.V16CategoryLinkedRead] != quota.LinkedRead || count[datagen.V16CategoryLinkedAction] != quota.LinkedAction ||
			count[datagen.V16CategoryUnlinkedProbe] != quota.Unlinked || count[datagen.V16CategoryNamedProduct] != quota.NamedProduct {
			t.Fatalf("seed %d: connector case counts %v, want %+v", seed, count, quota)
		}
		if share := float64(quota.Total()) / float64(prof.Tools); share < 0.15 || share > 0.25 {
			t.Fatalf("connector share %.2f outside the documented 15-25%%", share)
		}
		// Every connector outside the linked set is refused, offered or not.
		probe := toolexec.BuildFixtureForVersion(seed, protocol.ToolCase{ID: "probe"}, protocol.BenchVersionV16)
		for _, tool := range a.Catalog {
			appName, isConnector := catalog.IsConnectorTool(tool.Name)
			if !isConnector {
				continue
			}
			_, linked := plan.LinkedApp(tool.Name)
			_, refused := fixtureRefusal(probe, tool.Name, []byte(`{}`))
			if refused == linked {
				t.Fatalf("seed %d: %s (%s) linked=%v refused=%v", seed, tool.Name, appName, linked, refused)
			}
		}
	}
	if !namedLinkedProduct {
		t.Fatal("no named-product case names a linked app; the sharpest decoy is missing")
	}
	if len(linkedCounts) < 2 {
		t.Fatalf("linked-set size never varies across seeds: %v", linkedCounts)
	}
}

// v15 keeps its bytes (TestV15KnownVector), its suffix and its refusal of
// every connector; no v15 case is a v16 connector case.
func TestV16LeavesV15Unchanged(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV15)
	if prof != profilesV13["full"] {
		t.Fatalf("v15 profile moved: %+v", prof)
	}
	a, err := GenerateDataset(7, prof, protocol.BenchVersionV15)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range a.ToolCases {
		if datagen.IsV16ConnectorCase(c.Category) {
			t.Fatalf("v15 case %s carries v16 category %s", c.ID, c.Category)
		}
	}
	suffixed := 0
	for _, tool := range a.Catalog {
		if _, ok := catalog.IsConnectorTool(tool.Name); ok && strings.HasSuffix(tool.Description, "connector to be linked in Settings.") {
			suffixed++
		}
	}
	if suffixed != catalog.ConnectorToolsPerSeed {
		t.Fatalf("v15 suffix on %d connectors, want %d", suffixed, catalog.ConnectorToolsPerSeed)
	}
	f := toolexec.BuildFixtureForVersion(7, protocol.ToolCase{ID: "probe"}, protocol.BenchVersionV15)
	for _, app := range catalog.V16ConnectorPlanForSeed(7).Linked {
		if _, refused := fixtureRefusal(f, app.ReadTool, []byte(`{}`)); !refused {
			t.Fatalf("v15 serves %s", app.ReadTool)
		}
	}
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

// toolResult calls the fixture through the mock endpoint and requires a result.
func toolResult(t *testing.T, f toolexec.Fixture, name string, args []byte) string {
	t.Helper()
	out, refused := fixtureRefusal(f, name, args)
	if refused {
		t.Fatalf("%s refused: %s", name, out)
	}
	return out
}

// fixtureRefusal runs one call through the mock tool endpoint (toolexec.Server)
// and reports whether it refused the call (an error result) and the text.
func fixtureRefusal(f toolexec.Fixture, name string, args []byte) (string, bool) {
	srv := toolexec.NewServer()
	srv.Register("case", f)
	body, _ := json.Marshal(protocol.ToolExecRequest{CaseID: "case", Name: name, Args: args})
	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/tool", bytes.NewReader(body)))
	var resp protocol.ToolExecResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		// A malformed endpoint response is a broken endpoint, never a refusal.
		panic(fmt.Sprintf("tool endpoint returned malformed JSON for %s: %v", name, err))
	}
	if resp.Error != "" {
		return resp.Error, true
	}
	return resp.Result, false
}
