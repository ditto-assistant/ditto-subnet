package datagen

import (
	"math/rand"
	"slices"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestToolSurfacesMirrorTheCategoryTable proves the exported prompt banks are a
// faithful projection of the category table the generator samples from, so an
// inverse parser assembled from them cannot drift from generation.
func TestToolSurfacesMirrorTheCategoryTable(t *testing.T) {
	for _, version := range []int{protocol.BenchVersionV8, protocol.BenchVersionV12} {
		surfaces := ToolSurfacesForVersion(version)
		cats := categoriesForVersion(version)
		if len(surfaces) != len(cats) {
			t.Fatalf("v%d: %d surfaces for %d categories", version, len(surfaces), len(cats))
		}
		byName := map[string]ToolSurface{}
		for _, s := range surfaces {
			byName[s.Category] = s
		}
		for _, cat := range cats {
			s, ok := byName[cat.name]
			if !ok {
				t.Fatalf("v%d: category %q missing from surfaces", version, cat.name)
			}
			if len(s.Templates) == 0 && s.Grammar == nil {
				t.Errorf("v%d: %q exports neither templates nor a grammar", version, cat.name)
			}
			if (cat.grammar != nil) != (s.Grammar != nil) {
				t.Errorf("v%d: %q grammar export mismatch", version, cat.name)
			}
			wantTools := len(cat.tools)
			if wantTools == 0 && cat.tool != "" {
				wantTools = 1
			}
			if len(s.Tools) != wantTools || s.ArgKey != cat.argKey || s.Wrap != cat.wrap {
				t.Errorf("v%d: %q exported tools/argKey/wrap = %d/%q/%v, want %d/%q/%v", version, cat.name, len(s.Tools), s.ArgKey, s.Wrap, wantTools, cat.argKey, cat.wrap)
			}
		}
	}
	if len(ToolPromptLeadIns()) != len(promptLeadIns) || len(ToolPromptTrailers()) != len(promptTrailers) {
		t.Fatal("wrap pools not mirrored")
	}
}

// TestWorldToolSurfacesCoverEveryWorldFamily proves every world-conversion
// family the v12 generator emits has an exported frame bank, and that the
// exported setting pools are the generator's own.
func TestWorldToolSurfacesCoverEveryWorldFamily(t *testing.T) {
	r := rand.New(rand.NewSource(protocol.RotateSeed(123456789)))
	cases, _ := GenerateCasesWithFillersForVersion(r, 123456789, 100, protocol.BenchVersionV12)
	families := map[string]bool{}
	for _, w := range WorldToolSurfaces(protocol.BenchVersionV12) {
		if len(w.Frames) == 0 {
			t.Errorf("%s exports no frames", w.Category)
		}
		families[w.Category] = true
	}
	source := map[string]bool{}
	for _, s := range ToolSurfacesForVersion(protocol.BenchVersionV12) {
		source[s.Category] = true
	}
	for _, tc := range cases {
		if !families[tc.Category] && !source[tc.Category] {
			t.Errorf("emitted category %q has no exported surface", tc.Category)
		}
	}
	themesOut, modelsOut, fontsOut := SettingValuePools()
	if len(themesOut) != len(themes) || len(modelsOut) != len(models) || len(fontsOut) != len(chatFonts) {
		t.Fatal("setting pools not mirrored")
	}
}

// TestWorldToolSurfacesAreTheGeneratorFrames proves the exported world frames
// and v12 routing banks are the generator's own package-level banks, frame for
// frame, so a wording change in datagen.go cannot leave the export behind.
func TestWorldToolSurfacesAreTheGeneratorFrames(t *testing.T) {
	want := map[string][]string{
		"world_contact_research_email_result_usage": worldContactEmailFrames,
		"world_memory_delete":                       {worldMemoryDeleteFrame},
		"world_memory_update":                       {worldMemoryUpdateFrame},
		"world_theme_discover_set":                  {worldThemeDiscoverSetFrame},
		"world_business_workflow":                   {worldBusinessWorkflowFrame},
		"world_link_chain_result_usage":             {worldLinkChainFrame},
		"v10_state_dependent_routing":               {v10StateDependentRoutingFrame},
	}
	surfaces := WorldToolSurfaces(protocol.BenchVersionV12)
	if len(surfaces) != len(want) {
		t.Fatalf("%d world surfaces, want %d", len(surfaces), len(want))
	}
	for _, w := range surfaces {
		frames, ok := want[w.Category]
		if !ok {
			t.Errorf("unexpected world surface %q", w.Category)
			continue
		}
		if !slices.Equal(w.Frames, frames) {
			t.Errorf("%s: exported frames differ from the generator bank\n got %q\nwant %q", w.Category, w.Frames, frames)
		}
	}
	if len(WorldToolSurfaces(protocol.BenchVersionV8)) != len(want)-1 {
		t.Error("v8 surfaces should omit the v10 routing family")
	}
	banks := V12RoutingCueBanks()
	for name, pair := range map[string][2][]string{
		"ask leads": {banks.AskLeads, v12RoutingAskLeads}, "ask tails": {banks.AskTails, v12RoutingAskTails},
		"plan leads": {banks.PlanLeads, v12RoutingPlanLeads}, "plan mids": {banks.PlanMids, v12RoutingPlanMids},
		"plan tails": {banks.PlanTails, v12RoutingPlanTails}, "route leads": {banks.RouteLeads, v12RoutingRouteLeads},
		"route seps": {banks.RouteSeps, v12RoutingRouteSeps},
	} {
		if !slices.Equal(pair[0], pair[1]) {
			t.Errorf("v12 routing %s: exported bank differs from the generator bank", name)
		}
	}
}
