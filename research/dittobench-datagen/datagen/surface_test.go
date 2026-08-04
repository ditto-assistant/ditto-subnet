package datagen

import (
	"regexp"
	"strings"
	"testing"
)

// TestWrappedCategoriesHaveWideSurface pins the P1 surface-variety fix: the
// audited low-variety categories must present many distinct prompts across
// seeds (templates x lead-ins x trailers), not a memorizable handful.
func TestWrappedCategoriesHaveWideSurface(t *testing.T) {
	wrapped := map[string]bool{}
	for _, c := range categories {
		if c.wrap {
			wrapped[c.name] = true
		}
	}
	if len(wrapped) == 0 {
		t.Fatal("no wrap-flagged categories — the audited low-variety categories must be wrapped")
	}
	prompts := map[string]map[string]bool{}
	for seed := int64(1); seed <= 80; seed++ {
		for _, c := range Generate(seed, 60).ToolCases {
			if !wrapped[c.Category] {
				continue
			}
			if prompts[c.Category] == nil {
				prompts[c.Category] = map[string]bool{}
			}
			prompts[c.Category][c.Prompt] = true
		}
	}
	// Grammar-driven categories must clear a much higher floor than the plain
	// wrapped ones: the CFG expander exists to lift them from a memorizable
	// template list to hundreds of surfaces (audit follow-up item A).
	grammarDriven := map[string]bool{}
	for _, c := range categories {
		if c.grammar != nil {
			grammarDriven[c.name] = true
		}
	}
	if len(grammarDriven) < 5 {
		t.Fatalf("only %d grammar-driven categories — the five audited categories must use grammars", len(grammarDriven))
	}
	for cat, seen := range prompts {
		floor := 12
		if grammarDriven[cat] {
			floor = 60
		}
		if len(seen) < floor {
			t.Errorf("category %s: only %d distinct prompts across 80 seeds (floor %d) — template-memorizable", cat, len(seen), floor)
		}
	}
	for cat := range wrapped {
		if prompts[cat] == nil {
			t.Errorf("category %s never emitted in 80 seeds", cat)
		}
	}
}

// TestGrammarPromptsWellFormed sweeps every grammar-driven category's emitted
// prompts for leaked grammar syntax and unfilled placeholders.
func TestGrammarPromptsWellFormed(t *testing.T) {
	grammarDriven := map[string]bool{}
	for _, c := range categories {
		if c.grammar != nil {
			grammarDriven[c.name] = true
		}
	}
	for seed := int64(1); seed <= 60; seed++ {
		for _, c := range Generate(seed, 60).ToolCases {
			if !grammarDriven[c.Category] {
				continue
			}
			if strings.Contains(c.Prompt, "#") {
				t.Fatalf("seed %d %s: prompt leaked grammar syntax: %q", seed, c.Category, c.Prompt)
			}
			if strings.Contains(c.Prompt, "%s") {
				t.Fatalf("seed %d %s: prompt has unfilled placeholder: %q", seed, c.Category, c.Prompt)
			}
		}
	}
}

// routerLeakGuard is the single source of truth for the N4 anti-gaming check
// (docs/benchmark-antigaming-2026-addendum.md): every grammar-driven tool
// category declares the tokens its prompts must never contain. The leak class
// this closes is a grammar surface reintroducing a routable keyword, i.e. the
// EXPECTED tool's own name/description content words, which lets the no-model
// reference router solve the routing/restraint trap with no retrieval (found
// live for automation_list and memory_fetch, datagen commit 7c1676e; for
// capability_discovery in the addendum session).
//
// banned tokens must never appear (bounded word match). sharedOK lists the
// expected tool's name tokens that ARE permitted because they are generic
// family words that do not single the expected tool out to the router (e.g.
// "agent"/"jobs" are shared across the whole execute_agent_* / list_agent_jobs
// family; "image" is shared by create_image and edit_image). The completeness
// check below forces every expected-tool name token to be classified as one or
// the other, so a new grammar category (or a tool rename) cannot silently ship
// an unclassified, routable name token. Description content words that the
// router keys on (fetch_memories' "conversation"/"full", list_automations'
// "scheduled") are added to banned by hand; they are not auto-derived because
// natural phrasing legitimately overlaps generic description words like
// "setting"/"app" (capability_discovery), whose no-model floor is already
// verified safe by benchcal.
var routerLeakGuard = map[string]struct {
	banned   []string
	sharedOK []string
}{
	"agent_read_not_run":    {banned: []string{"list", "recent"}, sharedOK: []string{"agent", "jobs"}},
	"image_edit_not_create": {banned: []string{"edit"}, sharedOK: []string{"image"}},
	"memory_fetch":          {banned: []string{"fetch", "memories", "memory", "pair", "conversation", "full"}},
	"automation_list":       {banned: []string{"list", "automations", "scheduled", "existing"}},
	"capability_discovery":  {banned: []string{"discover", "capabilities"}},
}

// TestGrammarCategoriesNoRouterKeywordLeak generalizes the two hand-written
// per-category routability guards (automation_list, memory_fetch) to every
// grammar-driven category, and enforces coverage so a new one cannot skip the
// check. This is the N4 CI guard from the 2026 anti-gaming addendum.
func TestGrammarCategoriesNoRouterKeywordLeak(t *testing.T) {
	// Completeness: every grammar category has a guard entry, and every token of
	// its expected tool name is either banned or explicitly marked sharedOK.
	for _, c := range categories {
		if c.grammar == nil {
			continue
		}
		g, ok := routerLeakGuard[c.name]
		if !ok {
			t.Errorf("grammar category %q has no routerLeakGuard entry — a new grammar surface must classify its expected-tool name tokens", c.name)
			continue
		}
		banned := map[string]bool{}
		for _, b := range g.banned {
			banned[b] = true
		}
		shared := map[string]bool{}
		for _, s := range g.sharedOK {
			shared[s] = true
		}
		for _, tok := range strings.Split(c.tool, "_") {
			if !banned[tok] && !shared[tok] {
				t.Errorf("category %q: expected-tool name token %q is unclassified — add it to banned or sharedOK in routerLeakGuard", c.name, tok)
			}
		}
	}

	// Precompile a bounded-word matcher per banned token.
	res := map[string]*regexp.Regexp{}
	for _, g := range routerLeakGuard {
		for _, tok := range g.banned {
			if res[tok] == nil {
				res[tok] = regexp.MustCompile(`\b` + regexp.QuoteMeta(tok) + `\b`)
			}
		}
	}

	// Enforcement: no emitted prompt (post lead-in/trailer wrap) may contain a
	// banned token for its category.
	for seed := int64(1); seed <= 120; seed++ {
		for _, c := range Generate(seed, 60).ToolCases {
			g, ok := routerLeakGuard[c.Category]
			if !ok {
				continue
			}
			p := strings.ToLower(c.Prompt)
			for _, tok := range g.banned {
				if res[tok].MatchString(p) {
					t.Fatalf("seed %d: category %q prompt leaks router keyword %q: %q", seed, c.Category, tok, c.Prompt)
				}
			}
		}
	}
}

// TestMemoryFetchPromptNotVerbatim pins the P2 hardening: the memory_fetch
// prompt must not state the tool call's own keywords verbatim (the no-model
// keyword router solved the old surface at floor 1.0).
func TestMemoryFetchPromptNotVerbatim(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		for _, c := range Generate(seed, 60).ToolCases {
			if c.Category != "memory_fetch" {
				continue
			}
			p := strings.ToLower(c.Prompt)
			for _, kw := range []string{"fetch", "memory", "memories", "pair", "conversation"} {
				if strings.Contains(p, kw) {
					t.Fatalf("seed %d: memory_fetch prompt states tool keyword %q verbatim: %q", seed, kw, c.Prompt)
				}
			}
			// The pinned pair-ID argument must still be present in the prompt.
			if !strings.Contains(p, "mem-") {
				t.Fatalf("seed %d: memory_fetch prompt lost its pair ID: %q", seed, c.Prompt)
			}
		}
	}
}

// TestAutomationListPromptNotRoutable pins the leak fix for the automation_list
// grammar: the prompt must not state list_automations' own name/description
// content words ("list", "scheduled", "automations"). The no-model reference
// router (refharness) signals on those tokens, so reusing any of them lets it
// solve the read-vs-create trap with no retrieval (a grammar production once
// stacked all three and drove the benchcal floor to 1.0 on a seed).
func TestAutomationListPromptNotRoutable(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		for _, c := range Generate(seed, 60).ToolCases {
			if c.Category != "automation_list" {
				continue
			}
			p := strings.ToLower(c.Prompt)
			for _, kw := range []string{"list", "scheduled", "automations"} {
				if strings.Contains(p, kw) {
					t.Fatalf("seed %d: automation_list prompt states router keyword %q: %q", seed, kw, c.Prompt)
				}
			}
		}
	}
}
