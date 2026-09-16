package toolexec

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 mock-tool behavior (issues #1843, #1842, #1840). Everything here is
// reached only through a fixture built with BuildFixtureForVersion at
// bench_version >= 13; BuildFixture and every earlier contract keep the frozen
// Result/ServeHTTP behavior.
//
//   - The seed's coined decoy tools (catalog.DecoysForSeed) are served as a
//     "not configured" error unless the case expects the decoy, in which case
//     the decoy is the needle bearer and serves the result-usage fact.
//   - The list/discover tools (list_workflows, list_schedules, list_agent_jobs,
//     search_tools, discover_capabilities) and run_code serve per-seed COINED
//     content (CoinedForSeed) instead of fixed strings, so a baked fixture is
//     wrong on every other seed; on a result-usage case whose bearer is one of
//     them the needle number lives only inside that content.
//   - discover_capabilities lists the seed's appearance inventory
//     (catalog.InventoryForSeed) in canonical spelling; set_accent_color and
//     set_chat_font validate their argument against that inventory and answer
//     an unlisted value with an error that never echoes a canonical spelling.
//   - set_theme / set_reasoning_effort validate against the wire enum.

// v13ContentTools are the tools that carry a result-usage needle at v13 in
// addition to contentTools — only when the case is a result-usage case, so a
// plain routing case that merely expects list_workflows carries no needle.
var v13ContentTools = map[string]bool{
	"list_workflows":        true,
	"list_schedules":        true,
	"list_agent_jobs":       true,
	"search_tools":          true,
	"run_code":              true,
	"discover_capabilities": true,
}

// resultUsageSuffix mirrors datagen.IsResultUsage without importing datagen
// (datagen imports this package).
const resultUsageSuffix = "_result_usage"

func isResultUsageCategory(category string) bool {
	return strings.HasSuffix(category, resultUsageSuffix)
}

// BuildFixtureForVersion is BuildFixture under an explicit benchmark contract.
// Below bench_version 13 it is exactly BuildFixture; from v13 the fixture also
// knows the seed's decoys, appearance inventory, and coined list content.
func buildFixtureV13(masterSeed int64, benchVersion int, c protocol.ToolCase) Fixture {
	if benchVersion < protocol.BenchVersionV13 {
		return BuildFixture(masterSeed, c)
	}
	f := Fixture{seed: caseSeed(masterSeed, c.ID), benchVersion: benchVersion, category: c.Category}
	f.jobID = jobIDForSeed(f.seed)
	f.dependent = IsJobChain(c.Category)
	f.recovery = IsErrorRecovery(c.Category)
	if IsLinkChain(c.Category) {
		f.linkDep = true
		f.pageURL = pageURLForSeed(f.seed)
	}
	f.decoys = map[string]catalog.Decoy{}
	for _, d := range catalog.DecoysForSeed(masterSeed) {
		f.decoys[d.Name] = d
	}
	f.inventory = catalog.InventoryForSeed(masterSeed)
	f.coined = CoinedForSeed(masterSeed)
	if f.caseCarriesNeedleV13(c) {
		f.needle = NeedleForVersion(masterSeed, c.ID, benchVersion)
		if strings.HasPrefix(c.Category, "world_") {
			f.needle = NeedleForV8WorldVersion(masterSeed, c.ID, benchVersion)
		}
		f.has = true
		f.bearer = f.needleBearerV13(c)
		f.decoy = decoyValue(f.seed, f.needle.Value)
	}
	return f
}

// caseCarriesNeedleV13 extends caseCarriesNeedle: a coined list/discover tool or
// the sandbox carries the needle on a result-usage case, and an expected decoy
// always does (decoy-correct cases are result-usage by construction).
func (f Fixture) caseCarriesNeedleV13(c protocol.ToolCase) bool {
	for _, t := range c.ExpectedTools {
		if f.isContentToolV13(c.Category, t.Name) {
			return true
		}
	}
	return false
}

func (f Fixture) isContentToolV13(category, name string) bool {
	if contentTools[name] {
		return true
	}
	if _, isDecoy := f.decoys[name]; isDecoy {
		return true
	}
	return v13ContentTools[name] && isResultUsageCategory(category)
}

func (f Fixture) needleBearerV13(c protocol.ToolCase) string {
	bearer := ""
	for _, t := range c.ExpectedTools {
		if f.isContentToolV13(c.Category, t.Name) {
			bearer = t.Name
		}
	}
	return bearer
}

// IsDecoy reports whether name is one of this fixture's seed decoys (v13 only).
func (f Fixture) IsDecoy(name string) bool {
	_, ok := f.decoys[name]
	return ok
}

// Inventory returns the seed's appearance inventory (zero below v13).
func (f Fixture) Inventory() catalog.Inventory { return f.inventory }

// Coined returns the seed's coined list content (zero below v13).
func (f Fixture) Coined() Coined { return f.coined }

// unavailable reports the v13 tool errors that are not "tool not served":
// a decoy that is not this case's bearer, or a setter given an unlisted value.
// It never echoes a canonical option spelling. ok=false means "serve normally".
func (f Fixture) unavailable(name string, args json.RawMessage) (string, bool) {
	if f.benchVersion < protocol.BenchVersionV13 {
		return "", false
	}
	if d, isDecoy := f.decoys[name]; isDecoy && !(f.has && name == f.bearer) {
		return fmt.Sprintf("%s is not configured for this workspace. Connect %s in Settings before retrying; the request was not performed.", name, d.Brand), true
	}
	switch name {
	case "set_accent_color":
		value := stringArg(args, "color")
		if _, ok := f.inventory.MatchAccent(value); !ok {
			return fmt.Sprintf("Unknown accent color %q. Use exactly one of the accent options discover_capabilities lists for this workspace.", value), true
		}
	case "set_chat_font":
		value := stringArg(args, "font")
		if _, ok := f.inventory.MatchFont(value); !ok {
			return fmt.Sprintf("Unknown chat font %q. Use exactly one of the font options discover_capabilities lists for this workspace.", value), true
		}
	case "set_theme":
		if value := stringArg(args, "theme"); !inEnum(catalog.ThemeEnum(), value) {
			return fmt.Sprintf("Unknown theme %q. Allowed values: %s.", value, strings.Join(catalog.ThemeEnum(), ", ")), true
		}
	case "set_reasoning_effort":
		if value := stringArg(args, "effort"); !inEnum(catalog.EffortEnum(), value) {
			return fmt.Sprintf("Unknown reasoning effort %q. Allowed values: %s.", value, strings.Join(catalog.EffortEnum(), ", ")), true
		}
	}
	return "", false
}

func inEnum(values []string, value string) bool {
	want := strings.ToLower(strings.TrimSpace(value))
	for _, v := range values {
		if v == want {
			return true
		}
	}
	return false
}

func stringArg(raw json.RawMessage, key string) string {
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return ""
	}
	if v, ok := m[key].(string); ok {
		return strings.TrimSpace(v)
	}
	return ""
}

// resultV13 serves the v13-specific results. handled=false falls through to
// the frozen switch in Result.
func (f Fixture) resultV13(name string, args json.RawMessage, r *rand.Rand, serveNeedle bool) (string, bool) {
	if d, isDecoy := f.decoys[name]; isDecoy {
		// Only reachable for the bearer (unavailable handled the rest).
		return d.ServedResult(f.needleSentence()), true
	}
	// The figure a list entry carries. On the bearer the named entry carries
	// the needle and the other entries carry unrelated five-digit figures, so
	// the harness must match the named entry rather than "the number". On every
	// other case the coined content carries only SIX-digit fillers: a needle is
	// five digits, so no other case's served list can ever contain this case's
	// needle (TestV13NeedleIsAbsentFromOtherFixturesAndRecords, #1840).
	figure := func() string {
		if serveNeedle {
			return f.needle.Value
		}
		return f.filler6("figure")
	}
	other := func(salt string) string {
		if !serveNeedle {
			return f.filler6(salt)
		}
		or := rand.New(rand.NewSource(f.seed ^ int64(fnv1a("v13-other|"+salt))))
		for i := 0; i < 8; i++ {
			v := commaNumber(100 + or.Intn(99900))
			if v != f.needle.Value && v != f.decoy {
				return v
			}
		}
		return commaNumber(1)
	}
	co := f.coined
	switch name {
	case "list_workflows":
		lines := make([]string, len(co.Workflows))
		for i, w := range co.Workflows {
			runs := other(fmt.Sprintf("wf-%d", i))
			if i == co.Focus {
				runs = figure()
			}
			lines[i] = fmt.Sprintf("%s (id %s, %s, %s total runs)", w.Name, w.ID, w.Cadence, runs)
		}
		return "Saved workflows: " + strings.Join(lines, "; ") + ".", true
	case "list_schedules":
		lines := make([]string, len(co.Workflows))
		for i, w := range co.Workflows {
			logged := other(fmt.Sprintf("sched-%d", i))
			if i == co.Focus {
				logged = figure()
			}
			lines[i] = fmt.Sprintf("%s — %s, %s runs logged", w.Name, w.Cadence, logged)
		}
		return "Schedules: " + strings.Join(lines, "; ") + ".", true
	case "list_agent_jobs":
		return fmt.Sprintf("Recent jobs: %s (completed, %s items processed); %s (running, %s items so far).", co.Jobs[0], figure(), co.Jobs[1], other("job-1")), true
	case "search_tools":
		return fmt.Sprintf("Tool registry snapshot %s (%s namespace). Matching tools: run_code for calculations; artifacts for file conversion; search_web for live public information.", figure(), co.Registry), true
	case "run_code":
		return fmt.Sprintf("Sandbox run complete. %s => %s", co.Routine, figure()), true
	case "discover_capabilities":
		limit := figure()
		if !serveNeedle {
			limit = fmt.Sprintf("%d", 10+r.Intn(90))
		}
		return fmt.Sprintf("Ditto can generate and edit images, search the live web and read pages, keep long-term memory, build artifacts, run Ditto Code jobs, save and run workflows, and work with your calendar and Gmail. %s Workspace limits: up to %s saved workflows.", f.inventory.DiscoverText(), limit), true
	case "set_accent_color":
		canonical, _ := f.inventory.MatchAccent(stringArg(args, "color"))
		return fmt.Sprintf("Accent color set to %s.", canonical), true
	case "set_chat_font":
		canonical, _ := f.inventory.MatchFont(stringArg(args, "font"))
		return fmt.Sprintf("Chat font set to %s.", canonical), true
	case "set_theme":
		return fmt.Sprintf("Theme set to %s.", strings.ToLower(strings.TrimSpace(stringArg(args, "theme")))), true
	case "set_reasoning_effort":
		return fmt.Sprintf("Reasoning effort set to %s.", strings.ToLower(strings.TrimSpace(stringArg(args, "effort")))), true
	}
	return "", false
}

// filler6 is a seed-stable six-digit figure for non-bearer coined content.
// Needles and decoys are five digits, so a filler can never equal one.
func (f Fixture) filler6(salt string) string {
	r := rand.New(rand.NewSource(f.seed ^ int64(fnv1a("v13-filler|"+salt))))
	return commaNumber(100_000 + r.Intn(900_000))
}

// ── Coined list content (issue #1840) ────────────────────────────────────────

// CoinedWorkflow is one saved workflow the seed's list tools describe.
type CoinedWorkflow struct {
	Name    string
	ID      string
	Cadence string
}

// Coined is the seed's coined list/discover content. Every field is a pure
// function of the seed, distinct across seeds, and never a pool value a
// harness could bake: names carry a persona.CoinShaped-style stem and ids are
// persona.CoinShaped tokens.
type Coined struct {
	Workflows []CoinedWorkflow
	// Focus indexes the workflow whose statistic carries a result-usage needle.
	Focus int
	// Jobs are two coined background-job labels; Jobs[0] is the completed one.
	Jobs []string
	// Routine is the coined sandbox routine run_code reports on; Registry the
	// coined tool-registry namespace search_tools reports.
	Routine  string
	Registry string
}

var coinedCadences = []string{
	"every Monday at 08:00", "daily at 18:30", "each weekday at 07:15", "on the first of every month",
	"every Friday at noon", "hourly on weekdays", "every Sunday evening", "twice a day at 09:00 and 17:00",
}

var coinedWorkflowNouns = []string{"digest", "sweep", "review", "sync", "rollup", "audit", "handoff", "recap"}
var coinedJobNouns = []string{"export", "crawl", "backfill", "reindex", "scan", "rebuild"}

// CoinedForSeed derives the seed's coined list content.
func CoinedForSeed(seed int64) Coined {
	r := rand.New(rand.NewSource(seed ^ int64(fnv1a("v13-coined"))))
	cadences := r.Perm(len(coinedCadences))
	nouns := r.Perm(len(coinedWorkflowNouns))
	co := Coined{Focus: r.Intn(3)}
	for i := 0; i < 3; i++ {
		stem := catalog.CoinedStem(seed, fmt.Sprintf("workflow-%d", i))
		co.Workflows = append(co.Workflows, CoinedWorkflow{
			Name:    strings.ToUpper(stem[:1]) + stem[1:] + " " + coinedWorkflowNouns[nouns[i]],
			ID:      persona.CoinShaped(seed, fmt.Sprintf("v13-workflow-id-%d", i)),
			Cadence: coinedCadences[cadences[i]],
		})
	}
	jobNouns := r.Perm(len(coinedJobNouns))
	for i := 0; i < 2; i++ {
		stem := catalog.CoinedStem(seed, fmt.Sprintf("job-%d", i))
		co.Jobs = append(co.Jobs, strings.ToUpper(stem[:1])+stem[1:]+" "+coinedJobNouns[jobNouns[i]])
	}
	routine := catalog.CoinedStem(seed, "routine")
	co.Routine = strings.ToUpper(routine[:1]) + routine[1:] + " checksum"
	co.Registry = catalog.CoinedStem(seed, "registry")
	return co
}
