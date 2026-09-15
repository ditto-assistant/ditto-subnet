package datagen

import (
	"regexp"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/internal/textnoise"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// TestV13EveryToolCategoryRendersFromAGrammar is the v13 surface contract for
// the tool suite: no category may render from a flat template list.
func TestV13EveryToolCategoryRendersFromAGrammar(t *testing.T) {
	for _, c := range categoriesForVersion(protocol.BenchVersionV13) {
		if c.grammar == nil || len(c.templates) != 0 {
			t.Errorf("v13 category %q still renders from templates", c.name)
		}
		if len(c.grammar["root"]) < 2 {
			t.Errorf("v13 category %q root has only %d alternatives", c.name, len(c.grammar["root"]))
		}
	}
}

// TestV13FillerGrammarsCarryExactlyOnePlaceholder keeps the RequiredArgs and
// needle contract: every root of a category whose v12 templates carried a %s
// filler carries exactly one, and no other root carries any.
func TestV13FillerGrammarsCarryExactlyOnePlaceholder(t *testing.T) {
	wantFiller := map[string]bool{}
	for _, c := range categoriesForVersion(protocol.BenchVersionV12) {
		if c.grammar != nil {
			continue
		}
		for _, tmpl := range c.templates {
			// A bare "%s" template (no_tool, abstention) means the filler IS the
			// message; the v13 grammar renders the whole message instead.
			if strings.Contains(tmpl, "%s") && tmpl != "%s" {
				wantFiller[c.name] = true
			}
		}
	}
	for _, c := range categoriesForVersion(protocol.BenchVersionV13) {
		grammar, ok := v13CategoryGrammars[c.name]
		if !ok {
			continue
		}
		for _, root := range grammar["root"] {
			got := strings.Count(root, "%s")
			if wantFiller[c.name] && got != 1 {
				t.Errorf("v13 %q root %q carries %d placeholders, want 1", c.name, root, got)
			}
			if !wantFiller[c.name] && got != 0 {
				t.Errorf("v13 %q root %q carries a placeholder but the category has no filler", c.name, root)
			}
		}
		for symbol, alts := range grammar {
			if symbol == "root" {
				continue
			}
			for _, alt := range alts {
				if strings.Contains(alt, "%s") {
					t.Errorf("v13 %q symbol %q carries a placeholder outside root", c.name, symbol)
				}
			}
		}
	}
}

// TestV13GrammarsAreWellFormed expands every v13 grammar many times and fails
// on an unresolved #symbol#, an empty prompt, or doubled spaces.
func TestV13GrammarsAreWellFormed(t *testing.T) {
	check := func(name string, grammar persona.Grammar, slots map[string]string) {
		r := persona.HashRand(1, "wellformed", name)
		for i := 0; i < 400; i++ {
			out := persona.ExpandSlots(r, persona.SeedBank(int64(i%7+1), name, grammar), "root", slots)
			if out == "" || strings.Contains(out, "#") || strings.Contains(out, "  ") {
				t.Fatalf("%s expansion malformed: %q", name, out)
			}
		}
	}
	for name, grammar := range v13CategoryGrammars {
		check(name, grammar, nil)
	}
	for name, intents := range v13ArgIntentGrammars {
		for i, intent := range intents {
			check(name+"-intent-"+string(rune('a'+i)), intent.grammar, nil)
		}
	}
	slots := map[string]string{
		"subject": "the Veltrix index", "nickname": "Scout", "relation": "accountant", "city": "Durham",
		"context": "Juniper client dinner", "employer": "Quillmere", "alias": "\"harbor line\"", "client": "Norrford Group",
		"purpose": "client migration", "accent": "tael", "project": "Project Harborview", "family": "Claude", "value": "Georgia",
	}
	for name, grammar := range map[string]persona.Grammar{
		"contact-email": v13WorldContactEmailGrammar, "memory-delete": v13WorldMemoryDeleteGrammar,
		"memory-update": v13WorldMemoryUpdateGrammar, "theme-discover": v13WorldThemeDiscoverGrammar,
		"business-workflow": v13WorldBusinessWorkflowGrammar, "link-read": v13WorldLinkReadGrammar,
		"agent-job": v13WorldAgentJobGrammar, "route-plan": v13RoutingPlanGrammar, "route-ask": v13RoutingAskGrammar,
		"route-prefix": v13RoutingRouteGrammar, "cap-model": v13CapabilityModelGrammar, "cap-system": v13CapabilitySystemModeGrammar,
		"cap-mode": v13CapabilityModeGrammar, "cap-accent": v13CapabilityAccentGrammar, "cap-font": v13CapabilityFontGrammar,
		"memory-fetch": v13MemoryFetchQuestionGrammar, "stale-note": v13StaleContextPairGrammar,
	} {
		check(name, grammar, slots)
	}
}

// TestV13RouterKeywordGuardHolds re-runs the N4 router-leak guard over the v13
// surface for the guarded categories.
func TestV13RouterKeywordGuardHolds(t *testing.T) {
	res := map[string]*regexp.Regexp{}
	for _, g := range routerLeakGuard {
		for _, tok := range g.banned {
			if res[tok] == nil {
				res[tok] = regexp.MustCompile(`\b` + regexp.QuoteMeta(tok) + `\b`)
			}
		}
	}
	for seed := int64(1); seed <= 60; seed++ {
		ds, err := GenerateForVersion(seed, 100, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range ds.ToolCases {
			g, ok := routerLeakGuard[c.Category]
			if !ok {
				continue
			}
			p := strings.ToLower(c.Prompt)
			for _, tok := range g.banned {
				if res[tok].MatchString(p) {
					t.Fatalf("seed %d: v13 category %q prompt leaks router keyword %q: %q", seed, c.Category, tok, c.Prompt)
				}
			}
		}
	}
}

// TestV13ToolSurfacesVaryAndKeepAnchors checks the world tool prompts and the
// settings families: many distinct frames across seeds, the seeded anchors
// (nickname, alias, needle subject) always present, and the v8 fixed frames
// never reproduced verbatim.
func TestV13ToolSurfacesVaryAndKeepAnchors(t *testing.T) {
	frames := map[string]map[string]bool{}
	note := func(category, prompt string) {
		if frames[category] == nil {
			frames[category] = map[string]bool{}
		}
		// Collapse digits and quoted values so the frame, not the slot, is counted.
		frame := regexp.MustCompile(`“[^”]*”|"[^"]*"|\b[A-Z][a-z]+\b`).ReplaceAllString(prompt, "_")
		frames[category][frame] = true
	}
	fixedV8 := []string{
		"See what ", " is at right now, and open the actual page rather than relying on the search blurb.",
		"You can bin that temporary note about fixing",
		"Add to the handoff note for",
		"Check whether I already have a workflow for",
		"Have Ditto Code inspect the",
	}
	fixedHits := 0
	for seed := int64(1); seed <= 30; seed++ {
		ds, err := GenerateForVersion(seed, 100, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		world := universe.GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		nicknames := map[string]bool{}
		for _, p := range world.People {
			nicknames[p.Nickname] = true
		}
		for _, tc := range ds.ToolCases {
			note(tc.Category, tc.Prompt)
			for _, fixed := range fixedV8[2:] {
				if strings.HasPrefix(tc.Prompt, fixed) {
					fixedHits++
				}
			}
			switch tc.Category {
			case "world_contact_research_email_result_usage", "world_memory_delete":
				found := false
				for nick := range nicknames {
					if strings.Contains(tc.Prompt, nick) {
						found = true
						break
					}
				}
				if !found {
					t.Fatalf("seed %d %s prompt lost its nickname anchor: %q", seed, tc.Category, tc.Prompt)
				}
			case "world_memory_update":
				if !strings.Contains(tc.Prompt, "Friday") || !strings.Contains(strings.ToLower(tc.Prompt), "handoff") {
					t.Fatalf("seed %d memory update prompt lost its content anchor: %q", seed, tc.Prompt)
				}
			case "world_theme_discover_set":
				if strings.Contains(tc.Prompt, world.Accent+"-ish") {
					// The alias must be misspelled: a verbatim accent is a baked lookup.
					t.Fatalf("seed %d theme prompt carries the canonical accent verbatim: %q", seed, tc.Prompt)
				}
			}
		}
	}
	for _, category := range []string{
		"world_contact_research_email_result_usage", "world_memory_delete", "world_memory_update",
		"world_theme_discover_set", "world_business_workflow", "world_link_chain_result_usage",
		"v10_state_dependent_routing", "set_model", "set_font", "set_accent",
	} {
		if len(frames[category]) < 3 {
			t.Errorf("v13 %s rendered only %d distinct frames across 30 seeds", category, len(frames[category]))
		}
	}
	if fixedHits != 0 {
		t.Fatalf("v13 reproduced a fixed v8 world prompt frame %d times", fixedHits)
	}
}

// TestV13ArgKeyCategoriesStillPinRequiredArgs keeps the exact-argument contract:
// a verbatim filler category still records RequiredArgs[argKey]=filler.
func TestV13ArgKeyCategoriesStillPinRequiredArgs(t *testing.T) {
	pinned := map[string]int{}
	for seed := int64(1); seed <= 30; seed++ {
		ds, err := GenerateForVersion(seed, 100, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		for _, tc := range ds.ToolCases {
			switch tc.Category {
			case "memory_fetch", "set_effort":
				for _, spec := range tc.ExpectedTools {
					if len(spec.RequiredArgs) > 0 {
						pinned[tc.Category]++
					}
				}
			}
		}
	}
	for _, category := range []string{"memory_fetch", "set_effort"} {
		if pinned[category] == 0 {
			t.Fatalf("v13 %s never pinned a required argument", category)
		}
	}
}

// TestV13ToolBytesBelowV13AreUnchanged pins that threading the version through
// the world converters left every earlier contract's tool bytes alone.
func TestV13ToolBytesBelowV13AreUnchanged(t *testing.T) {
	for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV12} {
		a, err := GenerateForVersion(31, 100, version)
		if err != nil {
			t.Fatal(err)
		}
		found := false
	scan:
		for _, tc := range a.ToolCases {
			for _, fixed := range []string{"Make Ditto use my usual ", "You can bin that temporary note", "Add to the handoff note for", "Check whether I already have a workflow for", "See what ", "Have Ditto Code inspect the"} {
				if strings.HasPrefix(tc.Prompt, fixed) {
					found = true // the frozen v8 frame is still rendered below v13
					break scan
				}
			}
		}
		if !found {
			t.Fatalf("v%d no longer renders any fixed v8 world frame", version)
		}
	}
}

// TestV13CapabilityAliasesNeverRenderTheCanonicalValue pins the #1828
// discover-then-set criterion for the settings families: the alias in the
// prompt is a bounded misspelling of the RequiredArgs value, never the value
// itself. A verbatim value (the proper-noun proxy used to skip capitalised
// multi-word fonts such as "JetBrains Mono") is a baked lookup.
func TestV13CapabilityAliasesNeverRenderTheCanonicalValue(t *testing.T) {
	checked := map[string]int{}
	for seed := int64(1); seed <= 30; seed++ {
		ds, err := GenerateForVersion(seed, 100, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		for _, tc := range ds.ToolCases {
			switch tc.Category {
			case "set_font", "set_accent", "settings":
			default:
				continue
			}
			if len(tc.ExpectedTools) != 2 || tc.ExpectedTools[0].Name != "discover_capabilities" {
				continue // not a discover-then-set case
			}
			for _, value := range tc.ExpectedTools[1].RequiredArgs {
				if value == "system" {
					continue // rendered by the value-free system-mode grammar
				}
				checked[tc.Category]++
				if strings.Contains(tc.Prompt, value) {
					t.Fatalf("seed %d %s prompt carries the canonical value %q verbatim: %q", seed, tc.Category, value, tc.Prompt)
				}
				if bound := textnoise.MaxEditsForToken(strings.Fields(value)[0]); bound > 3 {
					t.Fatalf("alias bound %d exceeds the published ceiling for %q", bound, value)
				}
			}
		}
	}
	for _, category := range []string{"set_font", "set_accent", "settings"} {
		if checked[category] == 0 {
			t.Fatalf("v13 never rendered a %s discover-then-set case across 30 seeds", category)
		}
	}
}
