package datagen

import (
	"math/rand"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

func genV13(t *testing.T, seed int64, n int) []protocol.ToolCase {
	t.Helper()
	rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, n, protocol.BenchVersionV13)
	return cases
}

// bakedPoolValues are the v8..v12 setter pools a phrase table could bake
// (#1580): model slugs and chat fonts may appear in no v13 prompt at all; the
// five-colour accent pool may appear in no prompt that sets an accent (a font
// family such as "Crimson Text" legitimately contains a colour word).
var bakedPoolValues, bakedAccentValues = func() ([]*regexp.Regexp, []*regexp.Regexp) {
	compile := func(pools ...[]string) []*regexp.Regexp {
		var out []*regexp.Regexp
		for _, pool := range pools {
			for _, v := range pool {
				out = append(out, regexp.MustCompile(`(?i)\b`+regexp.QuoteMeta(v)+`\b`))
			}
		}
		return out
	}
	return compile(models, chatFonts), compile(accentColors)
}()

func setsAccent(tc protocol.ToolCase) bool {
	for _, spec := range tc.ExpectedTools {
		if spec.Name == "set_accent_color" {
			return true
		}
	}
	return false
}

// carriesNumber mirrors the scorer's number-token containment: the value must
// not be attached to another digit, separator, or sign on either side.
func carriesNumber(text, num string) bool {
	for i := 0; ; {
		j := strings.Index(text[i:], num)
		if j < 0 {
			return false
		}
		j += i
		attached := func(b byte) bool { return (b >= '0' && b <= '9') || b == '.' || b == ',' || b == '-' || b == '+' }
		before := j == 0 || !attached(text[j-1])
		after := j+len(num) >= len(text) || !attached(text[j+len(num)])
		if before && after {
			return true
		}
		i = j + 1
	}
}

// TestV13ToolBenchContractAcrossFortySeeds pins the v13 tool-bench contract on
// the full profile (issues #1843, #1842, #1580, #1840).
func TestV13ToolBenchContractAcrossFortySeeds(t *testing.T) {
	legacyRegistry := map[string]bool{}
	for _, v := range []string{"teal", "indigo", "amber", "emerald", "crimson", "violet", "cobalt", "coral", "inter", "jetbrains mono", "georgia", "system default"} {
		legacyRegistry[v] = true
	}
	setEffortRuns := 0
	discoveryTotal, discoveryNearMiss, discoveryLegacy := 0, 0, 0
	histograms := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		cases := genV13(t, seed, 100)
		if len(cases) != 100 {
			t.Fatalf("seed %d emitted %d cases", seed, len(cases))
		}
		inv := catalog.InventoryForSeed(seed)
		decoys := map[string]catalog.Decoy{}
		for _, d := range catalog.DecoysForSeed(seed) {
			decoys[d.Name] = d
		}
		coined := toolexec.CoinedForSeed(seed)
		counts := map[string]int{}
		decoyCorrect, unexpected := 0, map[string]int{}
		for _, tc := range cases {
			counts[tc.Category]++
			if v13DroppedFamilies[tc.Category] {
				t.Fatalf("seed %d emitted dropped family %q", seed, tc.Category)
			}
			for _, spec := range tc.ExpectedTools {
				if spec.Name == "set_main_model" {
					t.Fatalf("seed %d case %s expects retired set_main_model", seed, tc.ID)
				}
				if _, isDecoy := decoys[spec.Name]; isDecoy && !IsDecoyCorrect(tc.Category) {
					t.Fatalf("seed %d non-decoy case %s expects decoy %s", seed, tc.Category, spec.Name)
				}
			}
			for _, re := range bakedPoolValues {
				if re.MatchString(tc.Prompt) {
					t.Fatalf("seed %d %s prompt carries a baked pool value (%s): %q", seed, tc.Category, re, tc.Prompt)
				}
			}
			if setsAccent(tc) {
				for _, re := range bakedAccentValues {
					if re.MatchString(tc.Prompt) {
						t.Fatalf("seed %d %s prompt carries a baked accent value (%s): %q", seed, tc.Category, re, tc.Prompt)
					}
				}
			}
			switch {
			case IsDecoyCorrect(tc.Category):
				decoyCorrect++
				if !IsResultUsage(tc.Category) || len(tc.ExpectedTools) != 1 {
					t.Fatalf("seed %d decoy-correct case malformed: %+v", seed, tc)
				}
				d, ok := decoys[tc.ExpectedTools[0].Name]
				if !ok {
					t.Fatalf("seed %d decoy-correct case expects non-decoy %q", seed, tc.ExpectedTools[0].Name)
				}
				if strings.Contains(tc.Prompt, d.Name) || !strings.Contains(tc.Prompt, d.Brand) {
					t.Fatalf("seed %d decoy prompt must name the brand, never the tool: %q", seed, tc.Prompt)
				}
				if !strings.Contains(tc.Prompt, toolexec.NeedleFor(seed, tc.ID).Subject) {
					t.Fatalf("seed %d decoy prompt lost its needle subject: %q", seed, tc.Prompt)
				}
			case v13IsUnexpectedFamily(tc.Category):
				unexpected[tc.Category]++
				if !IsResultUsage(tc.Category) || len(tc.ExpectedTools) != 1 {
					t.Fatalf("seed %d unexpected-tool case malformed: %+v", seed, tc)
				}
			case v13DiscoveryFamily(tc.Category):
				discoveryTotal++
				if len(tc.ExpectedTools) != 2 || tc.ExpectedTools[0].Name != "discover_capabilities" || !tc.FuzzyTrajectory {
					t.Fatalf("seed %d discovery case malformed: %+v", seed, tc)
				}
				setter := tc.ExpectedTools[1]
				var canonical string
				var options []string
				var pair catalog.NearMiss
				if setter.Name == "set_accent_color" {
					canonical, options, pair = setter.RequiredArgs["color"], inv.Accents, inv.AccentNearMiss
				} else if setter.Name == "set_chat_font" {
					canonical, options, pair = setter.RequiredArgs["font"], inv.Fonts, inv.FontNearMiss
				} else {
					t.Fatalf("seed %d discovery case sets %q", seed, setter.Name)
				}
				if _, listed := catalog.InventoryForSeed(seed).MatchAccent(canonical); !listed {
					if _, listed := catalog.InventoryForSeed(seed).MatchFont(canonical); !listed {
						t.Fatalf("seed %d discovery target %q is not listed by discover_capabilities", seed, canonical)
					}
				}
				if strings.Contains(strings.ToLower(tc.Prompt), strings.ToLower(canonical)) {
					t.Fatalf("seed %d discovery prompt carries the canonical spelling %q: %q", seed, canonical, tc.Prompt)
				}
				if legacyRegistry[strings.ToLower(canonical)] {
					discoveryLegacy++
				}
				nearMiss := strings.EqualFold(canonical, pair.Partner)
				if nearMiss {
					discoveryNearMiss++
					if !strings.Contains(strings.ToLower(tc.Prompt), pair.Qualifier) {
						t.Fatalf("seed %d near-miss prompt lacks qualifier %q: %q", seed, pair.Qualifier, tc.Prompt)
					}
				}
				// Margin property on the emitted alias (WritingProtected[0], which the
				// prompt carries verbatim): its unique nearest listed option is the
				// base (the partner's base on near-miss cases) with a margin of at
				// least one edit, within floor(len/3) edits.
				wantBase := canonical
				if nearMiss {
					wantBase = pair.Base
				}
				if len(tc.WritingProtected) == 0 || !strings.Contains(tc.Prompt, tc.WritingProtected[0]) {
					t.Fatalf("seed %d discovery case does not carry its alias verbatim: %+v", seed, tc)
				}
				alias := tc.WritingProtected[0]
				d := catalog.OSADistance(alias, wantBase)
				if d < 1 || d > catalog.MaxAliasEdits(wantBase) {
					t.Fatalf("seed %d discovery alias %q distance %d to %q outside [1,%d]", seed, alias, d, wantBase, catalog.MaxAliasEdits(wantBase))
				}
				for _, other := range options {
					if strings.EqualFold(other, wantBase) {
						continue
					}
					if od := catalog.OSADistance(alias, other); od-d < 1 {
						t.Fatalf("seed %d discovery alias %q for %q is within margin of %q (%d vs %d)", seed, alias, wantBase, other, od, d)
					}
				}
			case tc.Category == "settings":
				if len(tc.ExpectedTools) != 1 || tc.ExpectedTools[0].Name != "set_theme" {
					t.Fatalf("seed %d settings case must be a plain schema-enum set_theme case: %+v", seed, tc.ExpectedTools)
				}
				theme := tc.ExpectedTools[0].RequiredArgs["theme"]
				found := false
				for _, v := range catalog.ThemeEnum() {
					found = found || v == theme
				}
				if !found {
					t.Fatalf("seed %d settings value %q not in the wire enum", seed, theme)
				}
			case tc.Category == "set_effort":
				effort := tc.ExpectedTools[0].RequiredArgs["effort"]
				found := false
				for _, v := range catalog.EffortEnum() {
					found = found || v == effort
				}
				if !found {
					t.Fatalf("seed %d set_effort value %q not in the wire enum", seed, effort)
				}
			case tc.Category == "recipe_apply":
				if len(tc.ExpectedTools) != 2 || tc.ExpectedTools[1].Name != "run_workflow" {
					t.Fatalf("seed %d recipe_apply malformed: %+v", seed, tc.ExpectedTools)
				}
				name := tc.ExpectedTools[1].RequiredArgs["name"]
				known := false
				for _, w := range coined.Workflows {
					known = known || w.Name == name
				}
				if !known || strings.Contains(strings.ToLower(tc.Prompt), strings.ToLower(name)) {
					t.Fatalf("seed %d recipe_apply must pin a coined workflow absent from the prompt: %q -> %q", seed, tc.Prompt, name)
				}
			}
		}
		if decoyCorrect < 10 {
			t.Errorf("seed %d decoy-correct cases=%d, want >= 10%% of 100", seed, decoyCorrect)
		}
		for _, family := range v13UnexpectedFamilies {
			if unexpected[family] != 1 {
				t.Errorf("seed %d %s=%d, want 1", seed, family, unexpected[family])
			}
		}
		if counts["world_theme_discover_set"] > v13WorldThemeCap {
			t.Errorf("seed %d world_theme_discover_set=%d, want <= %d", seed, counts["world_theme_discover_set"], v13WorldThemeCap)
		}
		if got := counts["discovery_accent_set"] + counts["discovery_font_set"]; got != 6 {
			t.Errorf("seed %d discovery-grounded cases=%d, want 6", seed, got)
		}
		if counts["set_effort"] > 0 {
			setEffortRuns++
		}
		for _, family := range []string{"stale_context_web", "memory_fetch", "v10_state_dependent_routing", "world_contact_research_email_result_usage", "settings", "recipe_apply", "capability_discovery", "automation_list", "agent_read_not_run", "tool_discovery"} {
			if counts[family] < 1 {
				t.Errorf("seed %d omitted family %q", seed, family)
			}
		}
		histograms[histogramKey(counts)] = true
	}
	if share := float64(discoveryNearMiss) / float64(discoveryTotal); share < 0.30 {
		t.Errorf("near-miss share %.3f < 0.30 (%d/%d)", share, discoveryNearMiss, discoveryTotal)
	}
	if share := float64(discoveryLegacy) / float64(discoveryTotal); share >= 0.20 {
		t.Errorf("baked 8-colour/4-font registry would pass %.3f of the discovery slice, want < 0.20", share)
	}
	if presence := float64(setEffortRuns) / 40; presence < 0.40 || presence > 0.80 {
		t.Errorf("set_effort present in %.2f of full runs, want ~0.60 (not mandatory)", presence)
	}
	if len(histograms) < 35 {
		t.Errorf("only %d distinct histograms across 40 seeds", len(histograms))
	}
}

// TestV13PublicProfileQuotas pins the small/medium quotas and the
// world_theme_discover_set cap at every public run size (the cap must hold
// even when the discovery quota is already spent).
func TestV13PublicProfileQuotas(t *testing.T) {
	for _, profile := range []struct {
		n                               int
		discovery, decoyMin, unexpected int
	}{{6, 1, 1, 0}, {48, 3, 5, 2}} {
		// Quota asserts hold on the twenty qualification seeds; the theme cap is
		// checked across forty (the surplus path is seed-rare).
		for seed := int64(1); seed <= 40; seed++ {
			cases := genV13(t, seed, profile.n)
			discovery, decoy, unexpected, theme := 0, 0, 0, 0
			for _, tc := range cases {
				switch {
				case v13DiscoveryFamily(tc.Category):
					discovery++
				case IsDecoyCorrect(tc.Category):
					decoy++
				case v13IsUnexpectedFamily(tc.Category):
					unexpected++
				case tc.Category == "world_theme_discover_set":
					theme++
				}
			}
			if seed <= 20 && (discovery != profile.discovery || decoy < profile.decoyMin || unexpected != profile.unexpected) {
				t.Errorf("n=%d seed %d: discovery=%d decoy=%d unexpected=%d, want %d/>=%d/%d", profile.n, seed, discovery, decoy, unexpected, profile.discovery, profile.decoyMin, profile.unexpected)
			}
			if theme > v13WorldThemeCap {
				t.Errorf("n=%d seed %d: world_theme_discover_set=%d, want <= %d", profile.n, seed, theme, v13WorldThemeCap)
			}
		}
	}
}

// TestV13ThemeCapHoldsWhenQuotaIsSpent drives the surplus path directly: a
// run whose legacy theme family exceeds cap + discovery quota must still end at
// the cap, with the surplus converted rather than left standing.
func TestV13ThemeCapHoldsWhenQuotaIsSpent(t *testing.T) {
	const n = 48
	seed := int64(7)
	cases := genV13(t, seed, n)
	quota := v13QuotaFor(n)
	// Force well over cap + quota theme cases onto ordinary convertible slots.
	forced := 0
	for i := range cases {
		if cases[i].Category == "world_theme_discover_set" || len(cases[i].ExpectedTools) == 0 || IsResultUsage(cases[i].Category) {
			continue
		}
		if forced >= v13WorldThemeCap+quota.Discovery+quota.Decoy+quota.Unexpected+2 {
			break
		}
		cases[i].Category = "world_theme_discover_set"
		cases[i].PrerequisitePairs = nil
		forced++
	}
	applyV13ToolBench(seed, cases)
	theme := 0
	for _, tc := range cases {
		if tc.Category == "world_theme_discover_set" {
			theme++
		}
	}
	if theme > v13WorldThemeCap {
		t.Fatalf("world_theme_discover_set=%d after forcing %d, want <= %d", theme, forced, v13WorldThemeCap)
	}
}

// TestV13CategoriesAreInPublicGlossary guards the hand-written Platform glossary
// mirror (apps/platform/ditto/api_models/bench_glossary.py): every category the
// v13 pass introduces — one per decoy shape, the two discovery-grounded
// families, and the four coined-fixture families — must have a public entry, or
// the /public/bench/glossary endpoint cannot explain a v13 run. Skips when the
// mirror is absent (standalone module use).
func TestV13CategoriesAreInPublicGlossary(t *testing.T) {
	rel := filepath.Join("..", "..", "..", "apps", "platform", "ditto", "api_models", "bench_glossary.py")
	raw, err := os.ReadFile(rel)
	if err != nil {
		t.Skipf("glossary mirror %s not present: %v", rel, err)
	}
	src := string(raw)
	block := regexp.MustCompile(`(?s)_V13_DECOY_SHAPES: dict\[str, tuple\[str, str\]\] = \{(.*?)\n\}`).FindStringSubmatch(src)
	if block == nil {
		t.Fatal("_V13_DECOY_SHAPES block not found in bench_glossary.py")
	}
	shapes := map[string]bool{}
	for _, m := range regexp.MustCompile(`(?m)^\s*"([a-z_]+)": \(`).FindAllStringSubmatch(block[1], -1) {
		shapes[m[1]] = true
	}
	for _, key := range catalog.DecoyShapeKeys() {
		if !shapes[key] {
			t.Errorf("decoy shape %q has no _V13_DECOY_SHAPES glossary row", key)
		}
		delete(shapes, key)
	}
	for extra := range shapes {
		t.Errorf("_V13_DECOY_SHAPES names %q, which is not a decoy shape", extra)
	}
	literal := []string{"discovery_accent_set", "discovery_font_set"}
	literal = append(literal, v13UnexpectedFamilies...)
	for _, category := range literal {
		if !strings.Contains(src, "\""+category+"\": (") {
			t.Errorf("category %q has no CATEGORY_GLOSSARY row", category)
		}
	}
	// And every v13-introduced category an actual run emits maps onto one of
	// those rows.
	for seed := int64(1); seed <= 20; seed++ {
		for _, tc := range genV13(t, seed, 100) {
			switch {
			case IsDecoyCorrect(tc.Category):
				key := strings.TrimSuffix(strings.TrimPrefix(tc.Category, "decoy_"), "_result_usage")
				if !strings.Contains(block[1], "\""+key+"\": (") {
					t.Errorf("seed %d emitted %q with no glossary shape row", seed, tc.Category)
				}
			case v13DiscoveryFamily(tc.Category), v13IsUnexpectedFamily(tc.Category):
				if !strings.Contains(src, "\""+tc.Category+"\": (") {
					t.Errorf("seed %d emitted %q with no glossary row", seed, tc.Category)
				}
			}
		}
	}
}

// TestV13CatalogCoverageWithDecoyExemption: every v13 production tool is a
// correct answer somewhere across the qualification seeds, and every expected
// name outside the production surface is one of that seed's decoys inside a
// decoy-correct case.
func TestV13CatalogCoverageWithDecoyExemption(t *testing.T) {
	production := map[string]bool{}
	for _, tool := range catalog.CatalogForVersion(protocol.BenchVersionV13) {
		production[tool.Name] = true
	}
	reachable := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		decoys := map[string]bool{}
		for _, d := range catalog.DecoysForSeed(seed) {
			decoys[d.Name] = true
		}
		for _, tc := range genV13(t, seed, 100) {
			for _, spec := range tc.ExpectedTools {
				reachable[spec.Name] = true
				if !production[spec.Name] {
					if !decoys[spec.Name] || !IsDecoyCorrect(tc.Category) {
						t.Fatalf("seed %d case %s expects %q, which is neither production nor this seed's decoy", seed, tc.Category, spec.Name)
					}
				}
			}
		}
	}
	for name := range production {
		if !reachable[name] {
			t.Errorf("v13 catalog tool %q is never a correct answer", name)
		}
	}
	if reachable["set_main_model"] {
		t.Error("v13 still grades set_main_model")
	}
}

// TestV13NeedleIsAbsentFromOtherFixturesAndRecords (#1840): a result-usage
// case's needle value exists only in its own bearer result — not in any other
// case's prompt, seeded record, or coined list/discover/sandbox content (the
// non-bearer coined fixtures carry six-digit fillers only, so this holds by
// construction rather than by luck).
func TestV13NeedleIsAbsentFromOtherFixturesAndRecords(t *testing.T) {
	listTools := []string{"list_workflows", "list_schedules", "list_agent_jobs", "search_tools", "run_code", "discover_capabilities"}
	for seed := int64(1); seed <= 40; seed++ {
		cases := genV13(t, seed, 100)
		fixtures := make([]toolexec.Fixture, len(cases))
		served := make([]string, len(cases))
		for i, tc := range cases {
			fixtures[i] = toolexec.BuildFixtureForVersion(seed, tc, protocol.BenchVersionV13)
			var sb strings.Builder
			for _, tool := range listTools {
				out, _ := fixtures[i].Result(tool, nil)
				sb.WriteString(out)
				sb.WriteByte('\n')
			}
			served[i] = sb.String()
		}
		for i, tc := range cases {
			if !IsResultUsage(tc.Category) || fixtures[i].NeedleValue() == "" {
				continue
			}
			needle := fixtures[i].NeedleValue()
			for j, other := range cases {
				if j == i {
					continue
				}
				if carriesNumber(other.Prompt, needle) {
					t.Fatalf("seed %d needle %s of %s appears in prompt of %s", seed, needle, tc.ID, other.ID)
				}
				for _, pair := range other.PrerequisitePairs {
					if carriesNumber(pair.Prompt, needle) || carriesNumber(pair.Response, needle) {
						t.Fatalf("seed %d needle %s of %s appears in record %s", seed, needle, tc.ID, pair.PairID)
					}
				}
				if carriesNumber(served[j], needle) {
					t.Fatalf("seed %d needle %s of %s appears in served content of %s (%s)", seed, needle, tc.ID, other.ID, other.Category)
				}
			}
		}
	}
}
