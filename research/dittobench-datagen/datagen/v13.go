package datagen

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Bench v13 tool bench (issues #1843, #1842, #1580, #1840). Every lever here is
// reached only from bench_version >= 13, so v12 and earlier regenerate
// byte-identically.
//
//   - Dropped families (#1580): set_model/set_main_model and set_font/set_chat_font
//     no longer exist as families, and set_accent's baked five-colour pool is
//     gone with them. Appearance values survive only in the schema-enum
//     `settings` family (set_theme's option list is on the wire, so no
//     discovery call is needed) and in the discovery-grounded families below.
//   - set_effort is no longer mandatory: it leaves the one-per-family floor and
//     is drawn with weight 2, which lands it in roughly 60% of full runs.
//   - Discovery-grounded appearance cases (#1842): v13DiscoveryQuota cases per
//     run resolve a bounded misspelling against the seed's inventory, whose
//     canonical spelling exists only in the served discover_capabilities result;
//     one near-miss share carries the partner's qualifier. The legacy
//     world_theme_discover_set family is capped at v13WorldThemeCap per run.
//   - Decoy-correct cases (#1843): at least 10% of the run expects one of the
//     seed's coined decoy tools, result-usage graded, so a blacklist of unknown
//     names forfeits real weight.
//   - Coined-fixture cases (#1840): four "unexpected tool" result-usage cases
//     (list_schedules, search_tools, run_code, list_agent_jobs) whose needle
//     lives only inside per-seed coined content, plus recipe_apply rewritten to
//     name its workflow by cadence so run_workflow's argument exists only in the
//     served list_workflows result.

const (
	// v13WorldThemeCap bounds the legacy 8-colour world_theme_discover_set
	// family per run (was ~5, up to 11).
	v13WorldThemeCap = 3
	// v13WorldFamilyFloor keeps every world family at or above this count when
	// the pass borrows slots from it.
	v13WorldFamilyFloor = 3
	// v13DecoyCorrectShareBps is the decoy-correct floor: 10% of tool cases.
	v13DecoyCorrectShareBps = 1000
)

// v13Quota is the per-run count of each v13 family the pass installs.
type v13Quota struct {
	Discovery  int
	Decoy      int
	Unexpected int
}

func v13QuotaFor(n int) v13Quota {
	q := v13Quota{Decoy: (n*v13DecoyCorrectShareBps + 9_999) / 10_000}
	switch {
	case n >= 80:
		q.Discovery, q.Unexpected = 6, 4
	case n >= 30:
		q.Discovery, q.Unexpected = 3, 2
	case n >= 6:
		q.Discovery, q.Unexpected = 1, 0
	default:
		q.Discovery, q.Unexpected = 0, 0
	}
	if q.Decoy < 1 && n >= 3 {
		q.Decoy = 1
	}
	if total := q.Discovery + q.Decoy + q.Unexpected; total > n {
		q.Discovery, q.Decoy, q.Unexpected = 0, min(1, n), 0
	}
	return q
}

// v13DroppedFamilies are source families the v13 catalog no longer emits.
var v13DroppedFamilies = map[string]bool{
	"set_model":  true,
	"set_font":   true,
	"set_accent": true,
}

// v13OptionalFamilies leave the one-per-family floor at v13 and appear only
// through the weighted residual draw.
var v13OptionalFamilies = map[string]bool{"set_effort": true}

// v13ArgIntents replaces the two-entry v8 banks for the surviving closed-enum
// setters with paraphrase banks that cover every wire enum value. Prompts are
// ordinary requests; a value may or may not appear literally.
var v13ArgIntents = map[string][]argIntent{
	"settings": {
		{"Please match Ditto's light or dark mode to my device.", "system"},
		{"Make the app dark mode.", "dark"},
		{"Follow whatever my operating system is set to for light versus dark.", "system"},
		{"Go with the bright look — I'm sitting outside today.", "light"},
		{"I want the deep-blue night palette, the midnight one.", "midnight"},
		{"Switch me over to the solarized color scheme.", "solarized"},
		{"Too bright in here; flip Ditto to the dark look.", "dark"},
		{"Use the light theme from now on.", "light"},
		{"Give me the solarized palette instead of plain dark.", "solarized"},
		{"Set the theme to midnight, please.", "midnight"},
	},
	"set_effort": {
		{"Reason as deeply and carefully as possible from now on.", "high"},
		{"Keep the reasoning balanced for everyday questions.", "medium"},
		{"Take your time and think hard before answering me.", "high"},
		{"Quick answers please — don't overthink anything.", "low"},
		{"Dial the thinking down to the lightest setting.", "low"},
		{"Middle-of-the-road reasoning is fine, nothing extreme.", "medium"},
		{"Max out how much you deliberate before you answer.", "high"},
		{"Snappy, minimal deliberation — keep it fast.", "low"},
	},
}

// toolCategoryWeightV13 keeps the v7 mix and gives the now-optional set_effort
// family weight 2 so it lands in roughly 60% of full runs.
func toolCategoryWeightV13(name string) int {
	if v13OptionalFamilies[name] {
		return 2
	}
	return toolCategoryWeightV7(name)
}

// sampledCategoryOrderV13 is sampledCategoryOrderV9 without a mandatory family
// and with optional families excluded from the one-per-family floor: they
// appear only through the weighted residual draw.
func sampledCategoryOrderV13(r *rand.Rand, n int, weights []int, optional []bool) []int {
	if n <= 0 || len(weights) == 0 {
		return nil
	}
	counts := make([]int, len(weights))
	available := make([]bool, len(weights))
	floorSlots := 0
	for i := range available {
		available[i] = !optional[i]
		if available[i] {
			floorSlots++
		}
	}
	remaining := n
	if floorSlots > remaining {
		floorSlots = remaining
	}
	for range floorSlots {
		total := 0
		for i, weight := range weights {
			if available[i] {
				total += positiveWeight(weight)
			}
		}
		draw := r.Intn(total)
		for i, weight := range weights {
			if !available[i] {
				continue
			}
			weight = positiveWeight(weight)
			if draw < weight {
				counts[i]++
				available[i] = false
				remaining--
				break
			}
			draw -= weight
		}
	}
	totalWeight := 0
	for _, weight := range weights {
		totalWeight += positiveWeight(weight)
	}
	for ; remaining > 0; remaining-- {
		draw := r.Intn(totalWeight)
		for i, weight := range weights {
			weight = positiveWeight(weight)
			if draw < weight {
				counts[i]++
				break
			}
			draw -= weight
		}
	}
	order := make([]int, 0, n)
	for i, count := range counts {
		for range count {
			order = append(order, i)
		}
	}
	r.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
	return order
}

// v13Categories applies the v13 family edits to the v8+ category list.
func v13Categories(out []category) []category {
	kept := make([]category, 0, len(out))
	for _, c := range out {
		if v13DroppedFamilies[c.name] {
			continue
		}
		switch c.name {
		case "arg_hallucination":
			// The retired model setter has no tool to ask about at v13.
			templates := make([]string, 0, len(c.templates))
			for _, t := range c.templates {
				if strings.Contains(strings.ToLower(t), "model") {
					continue
				}
				templates = append(templates, t)
			}
			c.templates = templates
		case "recipe_apply":
			// The workflow is named by its cadence; the canonical name exists only
			// in the served list_workflows result (applyV13ToolBench pins it).
			c.templates = []string{
				"Run the saved workflow that goes %s.",
				"Kick off my workflow that's set for %s — the one already saved.",
				"Start the existing workflow scheduled %s right now.",
			}
		}
		kept = append(kept, c)
	}
	return kept
}

// v13ToolPick is the seed-keyed surface chooser for the v13 pass. It hashes
// outside the shared generation RNG so the family mix and every earlier pass
// stay byte-identical whether or not a v13 surface is drawn.
func v13ToolPick(seed int64, index int, salt string, n int) int {
	if n <= 1 {
		return 0
	}
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v13-tool:%d:%d:%s", seed, index, salt)
	return int(h.Sum64() % uint64(n))
}

// IsDecoyCorrect reports whether a category is a v13 decoy-correct case.
func IsDecoyCorrect(category string) bool { return strings.HasPrefix(category, "decoy_") }

// v13DiscoveryFamily reports whether a category is a discovery-grounded
// appearance family (the world_theme_discover_set legacy family is separate).
func v13DiscoveryFamily(category string) bool {
	return category == "discovery_accent_set" || category == "discovery_font_set"
}

var v13UnexpectedFamilies = []string{"schedules_result_usage", "tool_registry_result_usage", "sandbox_result_usage", "agent_jobs_result_usage"}

// applyV13ToolBench installs the v13 families into an assembled run. It runs
// after the world and state-dependent passes and before writing noise, and it
// never touches a case that carries seeded evidence (the world carrier or a
// planted record).
func applyV13ToolBench(seed int64, cases []protocol.ToolCase) {
	if len(cases) == 0 {
		return
	}
	quota := v13QuotaFor(len(cases))
	inventory := catalog.InventoryForSeed(seed)
	decoys := catalog.DecoysForSeed(seed)
	coined := toolexec.CoinedForSeed(seed)

	// recipe_apply: pin the cadence-named workflow (every occurrence).
	for i := range cases {
		if cases[i].Category != "recipe_apply" || len(cases[i].ExpectedTools) != 2 {
			continue
		}
		w := coined.Workflows[v13ToolPick(seed, i, "recipe-workflow", len(coined.Workflows))]
		cases[i].Prompt = fmt.Sprintf(v13RecipeTemplates[v13ToolPick(seed, i, "recipe-tmpl", len(v13RecipeTemplates))], w.Cadence)
		cases[i].RequestSource = &protocol.ToolRequestSource{Kind: "saved_workflow_run", Values: map[string]string{"cadence": w.Cadence}}
		cases[i].ExpectedTools = []protocol.ToolSpec{{Name: "list_workflows"}, {Name: "run_workflow", RequiredArgs: map[string]string{"name": w.Name}}}
		cases[i].MaxToolCalls = 2
		cases[i].ExpectedBehavior = "list saved workflows, then run the one whose cadence the user named"
		cases[i].WritingProtected = append(cases[i].WritingProtected, strings.Fields(w.Cadence)...)
	}

	// Cap the legacy world_theme_discover_set family. Every case above the cap
	// is replaced: the first ones become discovery-grounded font cases (counting
	// toward the discovery quota); any surplus beyond that quota heads the
	// conversion order so the decoy and coined-fixture quotas consume it, and
	// whatever is still left becomes an extra decoy-correct case (that quota is
	// a floor). The cap therefore holds at every run size.
	installed := 0
	themeSeen := 0
	discoveryOrdinal := 0
	var surplusTheme []int
	for i := range cases {
		if cases[i].Category != "world_theme_discover_set" {
			continue
		}
		themeSeen++
		if themeSeen <= v13WorldThemeCap {
			continue
		}
		if installed < quota.Discovery {
			cases[i] = v13DiscoveryCase(seed, cases[i], inventory, discoveryOrdinal, "font")
			discoveryOrdinal++
			installed++
			continue
		}
		surplusTheme = append(surplusTheme, i)
	}

	eligible := append(surplusTheme, v13ConversionOrder(seed, cases)...)
	next := 0
	take := func() int {
		if next >= len(eligible) {
			return -1
		}
		i := eligible[next]
		next++
		return i
	}
	for installed < quota.Discovery {
		i := take()
		if i < 0 {
			break
		}
		kind := "accent"
		if discoveryOrdinal%2 == 1 {
			kind = "font"
		}
		cases[i] = v13DiscoveryCase(seed, cases[i], inventory, discoveryOrdinal, kind)
		discoveryOrdinal++
		installed++
	}
	d := 0
	for ; d < quota.Decoy; d++ {
		i := take()
		if i < 0 {
			break
		}
		cases[i] = v13DecoyCase(seed, cases[i], decoys[d%len(decoys)], i)
	}
	for u := 0; u < quota.Unexpected; u++ {
		i := take()
		if i < 0 {
			break
		}
		cases[i] = v13UnexpectedCase(seed, cases[i], coined, v13UnexpectedFamilies[u%len(v13UnexpectedFamilies)], i)
	}
	// Surplus theme cases the quotas did not consume: never leave the legacy
	// family above its cap.
	for next < len(surplusTheme) {
		i := take()
		cases[i] = v13DecoyCase(seed, cases[i], decoys[d%len(decoys)], i)
		d++
	}
	// Re-protect every replaced case's needle subject (the world pass added it
	// before the replacement) so writing noise cannot touch it.
	for i := range cases {
		if IsDecoyCorrect(cases[i].Category) || v13DiscoveryFamily(cases[i].Category) || v13IsUnexpectedFamily(cases[i].Category) {
			cases[i].WritingProtected = append(cases[i].WritingProtected, toolexec.NeedleForVersion(seed, cases[i].ID, protocol.BenchVersionV13).Subject)
		}
	}
}

// v13DecoyCase builds one decoy-correct result-usage case: the seed's coined
// decoy is the right tool and bears the case's needle.
func v13DecoyCase(seed int64, prior protocol.ToolCase, decoy catalog.Decoy, index int) protocol.ToolCase {
	needle := toolexec.NeedleForVersion(seed, prior.ID, protocol.BenchVersionV13)
	return protocol.ToolCase{
		ID:               prior.ID,
		Category:         "decoy_" + decoy.Shape.Key + "_result_usage",
		RequestSource:    &protocol.ToolRequestSource{Kind: "decoy_result_" + decoy.Shape.Key, Values: map[string]string{"brand": decoy.Brand, "subject": needle.Subject}},
		Prompt:           decoy.Prompt(v13ToolPick(seed, index, "decoy-prompt", len(decoy.Shape.Prompts)), needle.Subject),
		ExpectedTools:    []protocol.ToolSpec{{Name: decoy.Name}},
		MaxToolCalls:     1,
		ExpectedBehavior: fmt.Sprintf("call %s exactly once and report the figure it serves", decoy.Name),
		WritingProtected: []string{decoy.Brand, needle.Subject},
	}
}

var v13RecipeTemplates = []string{
	"Run the saved workflow that goes %s.",
	"Kick off my workflow that's set for %s — the one already saved.",
	"Start the existing workflow scheduled %s right now.",
}

func v13IsUnexpectedFamily(category string) bool {
	for _, f := range v13UnexpectedFamilies {
		if f == category {
			return true
		}
	}
	return false
}

// v13ConversionOrder ranks the cases the pass may replace: first ordinary
// duplicates (a family keeps at least one case), then world cases above the
// world floor from the most populous family down. A case carrying seeded
// evidence (the world carrier, a planted record, a state-dependent route) is
// never replaced, nor is a no-tool, result-usage, or already-v13 case.
func v13ConversionOrder(seed int64, cases []protocol.ToolCase) []int {
	scale := 1
	if len(cases) >= 80 {
		scale = 3
	} else if len(cases) >= 30 {
		scale = 2
	}
	worldPairs := map[string]bool{}
	for _, pair := range universe.GenerateForVersion(seed, scale, protocol.BenchVersionV13).Pairs {
		worldPairs[pair.PairID] = true
	}
	counts := map[string]int{}
	for _, tc := range cases {
		counts[tc.Category]++
	}
	convertible := func(tc protocol.ToolCase) bool {
		if len(tc.ExpectedTools) == 0 || IsResultUsage(tc.Category) && !v9WorldFamily(tc.Category) {
			return false
		}
		if IsDecoyCorrect(tc.Category) || v13DiscoveryFamily(tc.Category) || tc.Category == "recipe_apply" || tc.Category == "world_theme_discover_set" {
			return false
		}
		for _, pair := range tc.PrerequisitePairs {
			if worldPairs[pair.PairID] {
				return false
			}
		}
		return true
	}
	// Profile-scaled floors: the full profile keeps one case per ordinary family
	// and three per world family; smaller profiles never held a per-family floor
	// (their v9 draw is a partial weighted sample), so they keep one per world
	// family and may replace ordinary singletons after every duplicate.
	worldFloor, allowSingletons := v13WorldFamilyFloor, false
	if len(cases) < 80 {
		worldFloor, allowSingletons = 1, true
	}
	var ordinary, singletons, world, routed []int
	remaining := map[string]int{}
	for k, v := range counts {
		remaining[k] = v
	}
	for i, tc := range cases {
		if !convertible(tc) {
			continue
		}
		if tc.Category == "v10_state_dependent_routing" {
			// State-dependent routing is borrowed last: it is the strongest
			// remaining anti-shortcut family, so it only yields slots when every
			// world family is already at the floor.
			routed = append(routed, i)
			continue
		}
		if v9WorldFamily(tc.Category) {
			world = append(world, i)
			continue
		}
		if len(tc.PrerequisitePairs) != 0 {
			continue
		}
		if remaining[tc.Category] > 1 {
			ordinary = append(ordinary, i)
			remaining[tc.Category]--
		} else if allowSingletons {
			singletons = append(singletons, i)
		}
	}
	// World cases: round-robin from the most populous family so no family is
	// drained below the floor while another keeps a surplus.
	sort.SliceStable(world, func(a, b int) bool {
		ca, cb := counts[cases[world[a]].Category], counts[cases[world[b]].Category]
		if ca != cb {
			return ca > cb
		}
		return world[a] < world[b]
	})
	worldRemaining := map[string]int{}
	for k, v := range counts {
		worldRemaining[k] = v
	}
	var borrowed []int
	for len(world) > 0 {
		progressed := false
		kept := world[:0]
		for _, i := range world {
			cat := cases[i].Category
			if worldRemaining[cat] > worldFloor {
				borrowed = append(borrowed, i)
				worldRemaining[cat]--
				progressed = true
				continue
			}
			kept = append(kept, i)
		}
		world = kept
		if !progressed {
			break
		}
	}
	for _, i := range routed {
		if worldRemaining["v10_state_dependent_routing"] > worldFloor {
			borrowed = append(borrowed, i)
			worldRemaining["v10_state_dependent_routing"]--
		}
	}
	return append(append(ordinary, borrowed...), singletons...)
}

// v13DiscoveryCase builds one discovery-grounded appearance case: the prompt
// carries a bounded misspelling of a listed option (never the canonical
// spelling); on the near-miss share it misspells the pair's base and names the
// partner's qualifier so the intended option is the partner.
func v13DiscoveryCase(seed int64, prior protocol.ToolCase, inv catalog.Inventory, ordinal int, kind string) protocol.ToolCase {
	nearMiss := ordinal%3 == 0
	var options, targets []string
	var pair catalog.NearMiss
	tool, argKey := "set_accent_color", "color"
	if kind == "font" {
		options, targets, pair = inv.Fonts, inv.FontTargets(), inv.FontNearMiss
		tool, argKey = "set_chat_font", "font"
	} else {
		options, targets, pair = inv.Accents, inv.AccentTargets(), inv.AccentNearMiss
	}
	var canonical, base string
	if nearMiss {
		canonical, base = pair.Partner, pair.Base
	} else {
		base = targets[v13ToolPick(seed, ordinal, "discovery-target:"+kind, len(targets))]
		canonical = base
	}
	tryBase := func(b string) (catalog.Alias, bool) {
		for attempt := 0; attempt < 8; attempt++ {
			a, ok := catalog.AliasFor(b, options, seed, fmt.Sprintf("discovery:%s:%d:%d", kind, ordinal, attempt))
			if ok && (!nearMiss || !strings.Contains(strings.ToLower(a.Text), strings.ToLower(canonical))) {
				return a, true
			}
		}
		return catalog.Alias{}, false
	}
	var alias catalog.Alias
	ok := false
	if nearMiss {
		alias, ok = tryBase(base)
	} else {
		// Regenerate with the next target rather than shipping an unresolvable alias.
		start := v13ToolPick(seed, ordinal, "discovery-target:"+kind, len(targets))
		for k := 0; k < len(targets) && !ok; k++ {
			base = targets[(start+k)%len(targets)]
			canonical = base
			alias, ok = tryBase(base)
		}
	}
	if !ok {
		// Unreachable across the qualification seeds (TestV13DiscoveryAliasMargin,
		// TestV13ToolBenchContractAcrossFortySeeds). Fail loudly rather than emit
		// an alias that could carry the canonical spelling or break the margin
		// property (#1842): a silent fallback here would be a contract leak.
		panic(fmt.Sprintf("datagen v13: no bounded alias for %s inventory target %q (seed %d, ordinal %d, near-miss %t)", kind, base, seed, ordinal, nearMiss))
	}
	var prompt string
	if nearMiss {
		prompt = fmt.Sprintf(v13NearMissPrompts[kind][v13ToolPick(seed, ordinal, "discovery-nm-prompt", len(v13NearMissPrompts[kind]))], pair.Qualifier, alias.Text)
	} else {
		prompt = fmt.Sprintf(v13DiscoveryPrompts[kind][v13ToolPick(seed, ordinal, "discovery-prompt", len(v13DiscoveryPrompts[kind]))], alias.Text)
	}
	category := "discovery_accent_set"
	if kind == "font" {
		category = "discovery_font_set"
	}
	tc := fuzzyWorldTool(prior.ID, category, prompt, []protocol.ToolSpec{{Name: "discover_capabilities"}, {Name: tool, RequiredArgs: map[string]string{argKey: canonical}}}, "list the workspace's configured appearance options, resolve the user's approximate spelling against them, and apply the listed option")
	// WritingProtected[0] is the alias as emitted (tests read it back); the
	// qualifier follows. textnoise protects each word of a multiword entry.
	tc.WritingProtected = []string{alias.Text, pair.Qualifier}
	qualifier := ""
	if nearMiss {
		qualifier = pair.Qualifier
	}
	tc.RequestSource = v13AppearanceRequestSource(options, canonical, kind, qualifier)
	return tc
}

var v13DiscoveryPrompts = map[string][]string{
	"accent": {
		"Make the app accent %s-ish. I may have mangled the spelling, so check which accent options this workspace actually offers before applying it.",
		"Switch Ditto's accent color to something like %s — look up the configured appearance options first in case I misspelled it.",
		"Set my accent to %s (or whatever the closest configured option is called); check the appearance list before you apply anything.",
		"Change the accent colour to %s. Spelling is approximate, so confirm it against the options this workspace lists.",
	},
	"font": {
		"Use %s for the chat font. Treat my spelling loosely and pick the matching option from the workspace's font list first.",
		"Change the chat typeface to %s-ish — check which fonts this workspace has configured before setting it.",
		"Set the chat font to %s, or the configured option closest to that; look the list up first, I may have botched the name.",
		"Switch my chat view to the %s font. Verify the exact option name against the appearance settings before applying.",
	},
}

var v13NearMissPrompts = map[string][]string{
	"accent": {
		"Switch my accent to the %[1]s shade of %[2]s — check the configured appearance options first, I probably misspelled it.",
		"Set the app accent to the %[1]s variant of %[2]s. Look up the workspace's accent list before applying; my spelling is rough.",
		"Make Ditto's accent the %[1]s version of %[2]s, not the plain one. Confirm the exact option name from the appearance options first.",
	},
	"font": {
		"Set the chat font to the %[1]s cut of %[2]s; look up the available font options first, my spelling is rough.",
		"Switch the chat typeface to the %[1]s variant of %[2]s, not the regular one — check the configured font list before setting it.",
		"Use the %[1]s edition of %[2]s for chat. Verify the exact option name against the workspace's fonts before you apply it.",
	},
}

// v13UnexpectedCase builds one coined-fixture result-usage case: the prompt
// names an entry of the seed's coined list content and asks for the figure the
// served result attaches to it.
var v13UnexpectedPrompts = map[string][]string{
	"schedules_result_usage": {
		"Check the schedules on my workflows — how many runs has the one that goes %s logged so far? Exact number, please.",
		"Look at what's on a timer for me: the workflow scheduled %s — what's its exact logged run count?",
		"Pull up my workflow schedules and tell me precisely how many runs the %s one has logged.",
	},
	"tool_registry_result_usage": {
		"Search your tool registry for something that can %s and tell me the exact registry snapshot number the search reports.",
		"Before writing any code, look up which tool binding can %s — and give me the precise snapshot number the registry search returns.",
		"Find the right tool binding to %s and report the exact registry snapshot the lookup came back with.",
	},
	"sandbox_result_usage": {
		"Evaluate the %s routine in the code sandbox and report the exact value it prints — just the number.",
		"Run the %s routine through Code Mode and tell me precisely what it outputs.",
		"Execute %s in the in-process sandbox and give me the exact figure it returns.",
	},
	"agent_jobs_result_usage": {
		"Look at my recent background jobs — exactly how many items did the %s job process?",
		"Check the agent jobs I dispatched: what's the precise item count the finished %s job reports?",
		"How many items did my %s job get through? Read it off the job list and give me the exact number.",
	},
}

func v13UnexpectedCase(seed int64, prior protocol.ToolCase, co toolexec.Coined, family string, index int) protocol.ToolCase {
	focus := co.Workflows[co.Focus]
	var tool, prompt string
	var protect []string
	var source *protocol.ToolRequestSource
	switch family {
	case "schedules_result_usage":
		source = &protocol.ToolRequestSource{Kind: family, Values: map[string]string{"cadence": focus.Cadence}}
		tool = "list_schedules"
		prompts := v13UnexpectedPrompts[family]
		prompt = fmt.Sprintf(prompts[v13ToolPick(seed, index, "unexpected-prompt", len(prompts))], focus.Cadence)
		protect = strings.Fields(focus.Cadence)
	case "tool_registry_result_usage":
		tool = "search_tools"
		caps := []string{"convert a file between formats", "fetch live exchange rates", "resize a batch of images", "pull rows from a spreadsheet", "look up a package's latest version"}
		cap := caps[v13ToolPick(seed, index, "unexpected-cap", len(caps))]
		source = &protocol.ToolRequestSource{Kind: family, Values: map[string]string{"capability": cap}}
		prompts := v13UnexpectedPrompts[family]
		prompt = fmt.Sprintf(prompts[v13ToolPick(seed, index, "unexpected-prompt", len(prompts))], cap)
	case "sandbox_result_usage":
		source = &protocol.ToolRequestSource{Kind: family, Values: map[string]string{"routine": co.Routine}}
		tool = "run_code"
		prompts := v13UnexpectedPrompts[family]
		prompt = fmt.Sprintf(prompts[v13ToolPick(seed, index, "unexpected-prompt", len(prompts))], co.Routine)
		protect = strings.Fields(co.Routine)
	default: // agent_jobs_result_usage
		source = &protocol.ToolRequestSource{Kind: family, Values: map[string]string{"job": co.Jobs[0]}}
		tool = "list_agent_jobs"
		prompts := v13UnexpectedPrompts["agent_jobs_result_usage"]
		prompt = fmt.Sprintf(prompts[v13ToolPick(seed, index, "unexpected-prompt", len(prompts))], co.Jobs[0])
		protect = strings.Fields(co.Jobs[0])
	}
	return protocol.ToolCase{
		ID:               prior.ID,
		Category:         family,
		RequestSource:    source,
		Prompt:           prompt,
		ExpectedTools:    []protocol.ToolSpec{{Name: tool}},
		MaxToolCalls:     1,
		ExpectedBehavior: fmt.Sprintf("call %s exactly once and report the figure it serves", tool),
		WritingProtected: protect,
	}
}
