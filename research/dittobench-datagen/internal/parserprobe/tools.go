package parserprobe

import (
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// toolFrame is one compiled tool-prompt frame with the outcome it implies.
type toolFrame struct {
	category string
	frame    frame
	tools    []string
	argKey   string
	// intentValue is the resolved argument value of an intent-phrased prompt.
	intentValue string
	world       bool
}

// toolParser is the template inverse of the generator's tool banks for one
// bench version: every category template, every grammar expansion (bounded),
// every intent prompt, the v8 capability-resolution prompts, and the
// coherent-world frames, plus the wrap lead-in/trailer pools to strip.
type toolParser struct {
	frames   []toolFrame
	leadIns  []string
	trailers []string
	routing  []frame // v12 composed routing-cue asks
}

// grammarExpansionCap bounds the enumeration of one grammar; the audited
// categories expand to a few hundred forms, well below it.
const grammarExpansionCap = 4096

func newToolParser(benchVersion int) *toolParser {
	tp := &toolParser{leadIns: datagen.ToolPromptLeadIns(), trailers: datagen.ToolPromptTrailers()}
	for _, s := range datagen.ToolSurfacesForVersion(benchVersion) {
		add := func(template, intentValue string) {
			tp.frames = append(tp.frames, toolFrame{category: s.Category, frame: compileFrame(template), tools: s.Tools, argKey: s.ArgKey, intentValue: intentValue})
		}
		for _, t := range s.Templates {
			add(t, "")
		}
		if s.Grammar != nil {
			for _, t := range enumerateGrammar(s.Grammar) {
				add(t, "")
			}
		}
		for _, it := range s.Intents {
			add(it.Prompt, it.Value)
		}
	}
	// v8 capability-resolution prompts (applyV8CapabilityResolution) replace the
	// set_* prompts with a fixed frame around a misspelled value; the tools
	// become discover_capabilities then the setter.
	if benchVersion >= protocol.BenchVersionV8 {
		res := []struct{ cat, template, setter, key string }{
			{"set_model", "Use %s for my main chats. I don't know its exact model id, so resolve the current available option instead of making me type a slug.", "set_main_model", "model"},
			{"settings", "Match Ditto's color mode to my device. Check the available appearance settings if you need the canonical option.", "set_theme", "theme"},
			{"settings", "Switch Ditto to %s-ish mode; check the available appearance options rather than guessing the setting name.", "set_theme", "theme"},
			{"set_accent", "Make the app accent %s-ish. I may have mangled the spelling, so inspect the available appearance options first.", "set_accent_color", "color"},
			{"set_font", "Use %s in chat. Treat that case-insensitively and check the available font options if the name is slightly off.", "set_chat_font", "font"},
		}
		for _, r := range res {
			tp.frames = append(tp.frames, toolFrame{category: r.cat, frame: compileFrame(r.template), tools: []string{"discover_capabilities", r.setter}, argKey: r.key})
		}
		for _, w := range datagen.WorldToolSurfaces(benchVersion) {
			for _, f := range w.Frames {
				tp.frames = append(tp.frames, toolFrame{category: w.Category, frame: compileFrame(f), tools: w.Tools, world: true})
			}
		}
		if benchVersion >= protocol.BenchVersionV12 {
			banks := datagen.V12RoutingCueBanks()
			for _, lead := range banks.AskLeads {
				for _, tail := range banks.AskTails {
					tp.routing = append(tp.routing, compileFrame(lead+" the dependency-risk review for %q "+tail))
				}
			}
		}
	}
	// Longer frames first so a specific frame wins over a short generic one.
	sort.SliceStable(tp.frames, func(i, j int) bool { return len(tp.frames[i].frame.tokens) > len(tp.frames[j].frame.tokens) })
	return tp
}

// enumerateGrammar lists every expansion of g's root (deterministic: the
// grammar is walked, not sampled), capped at grammarExpansionCap.
func enumerateGrammar(g persona.Grammar) []string {
	var expand func(symbol string, depth int) []string
	expand = func(symbol string, depth int) []string {
		if depth > 8 {
			return []string{""}
		}
		alts, ok := g[symbol]
		if !ok || len(alts) == 0 {
			return []string{"#" + symbol + "#"}
		}
		var out []string
		for _, alt := range alts {
			parts := []string{""}
			rest := alt
			for {
				i := strings.Index(rest, "#")
				if i < 0 {
					parts = appendAll(parts, []string{rest})
					break
				}
				j := strings.Index(rest[i+1:], "#")
				if j < 0 {
					parts = appendAll(parts, []string{rest})
					break
				}
				parts = appendAll(parts, []string{rest[:i]})
				parts = appendAll(parts, expand(rest[i+1:i+1+j], depth+1))
				rest = rest[i+1+j+1:]
				if len(parts) > grammarExpansionCap {
					parts = parts[:grammarExpansionCap]
				}
			}
			out = append(out, parts...)
			if len(out) > grammarExpansionCap {
				return out[:grammarExpansionCap]
			}
		}
		return out
	}
	return expand("root", 0)
}

func appendAll(prefixes, suffixes []string) []string {
	out := make([]string, 0, len(prefixes)*len(suffixes))
	for _, p := range prefixes {
		for _, s := range suffixes {
			out = append(out, p+s)
		}
	}
	return out
}

// toolPrediction is the parser's outcome signature for one tool prompt.
type toolPrediction struct {
	category string
	tools    []protocol.ToolSpec
	ok       bool
}

// classifyTool strips wrap lead-ins/trailers, matches the frame banks, and
// resolves seed-bound arguments from the store.
func (tp *toolParser) classifyTool(prompt string, st *store) toolPrediction {
	candidates := tp.unwrap(strings.TrimSpace(prompt))
	// Specific frames first across every unwrapped variant, then the composed
	// routing cues, and only then the bare "%s" catch-all categories (no_tool,
	// abstention), which match any message.
	for pass := 0; pass < 2; pass++ {
		for _, text := range candidates {
			for _, tf := range tp.frames {
				catchAll := tf.frame.slots == 1 && len(tf.frame.tokens) == 1
				if catchAll != (pass == 1) {
					continue
				}
				slots, ok := tf.frame.match(text)
				if !ok {
					continue
				}
				if tf.world {
					return tp.worldPrediction(tf, slots, st)
				}
				return tp.templatePrediction(tf, slots, st)
			}
			if pass == 0 {
				if _, slots, ok := matchAny(tp.routing, text); ok {
					return tp.routePrediction(slots[0], st)
				}
			}
		}
	}
	return toolPrediction{}
}

// templatePrediction builds the outcome of a source-table category: the tool
// sequence plus the pinned argument, resolved to its canonical pool value when
// the category is a closed product setting.
func (tp *toolParser) templatePrediction(tf toolFrame, slots []string, st *store) toolPrediction {
	pred := toolPrediction{category: tf.category, ok: true}
	for i, name := range tf.tools {
		spec := protocol.ToolSpec{Name: name}
		if i == len(tf.tools)-1 && tf.argKey != "" {
			value := tf.intentValue
			if value == "" && len(slots) > 0 {
				value = slots[len(slots)-1]
			}
			if value == "" && tf.argKey == "theme" && containsPhraseFuzzy(tf.frame.raw, "to my device") {
				value = "system"
			}
			if value != "" {
				value = resolveSettingValue(tf.category, tf.argKey, value)
				spec.RequiredArgs = map[string]string{tf.argKey: value}
			}
		}
		pred.tools = append(pred.tools, spec)
	}
	return pred
}

// resolveSettingValue maps an approximate or misspelled closed-setting value
// ("twal", "dak", "JetBarins Mono", "Claude") onto the canonical pool entry a
// product-quality harness would discover, using the public pools.
func resolveSettingValue(category, key, value string) string {
	themes, modelIDs, fonts := datagen.SettingValuePools()
	lower := strings.ToLower(strings.TrimSpace(value))
	switch key {
	case "theme":
		return nearestPoolValue(lower, themes)
	case "color":
		return nearestPoolValue(lower, universe.AccentColors())
	case "font":
		return nearestPoolValue(lower, fonts)
	case "model":
		for _, id := range modelIDs {
			if strings.HasPrefix(strings.ToLower(id), lower) {
				return id
			}
		}
		return nearestPoolValue(lower, modelIDs)
	}
	return value
}

// nearestPoolValue returns the pool entry whose words all fuzzy-match value's
// words (in order), else value unchanged.
func nearestPoolValue(value string, pool []string) string {
	for _, candidate := range pool {
		if strings.EqualFold(candidate, value) {
			return candidate
		}
	}
	for _, candidate := range pool {
		if fuzzyName(candidate, value) {
			return candidate
		}
	}
	return value
}

// unwrap returns the prompt and every lead-in/trailer-stripped variant.
func (tp *toolParser) unwrap(prompt string) []string {
	out := []string{prompt}
	for _, lead := range tp.leadIns {
		if lead == "" {
			continue
		}
		if fuzzyPrefix(prompt, lead) {
			stripped := strings.TrimSpace(prompt[len(lead):])
			// The wrap lowercases the first letter of the statement; restore it.
			out = append(out, stripped, capitalizeFirst(stripped))
			for _, tr := range tp.trailers {
				if tr != "" && fuzzySuffix(stripped, tr) {
					s2 := strings.TrimSpace(stripped[:len(stripped)-len(tr)])
					out = append(out, s2, capitalizeFirst(s2))
				}
			}
		}
	}
	for _, tr := range tp.trailers {
		if tr != "" && fuzzySuffix(prompt, tr) {
			out = append(out, strings.TrimSpace(prompt[:len(prompt)-len(tr)]))
		}
	}
	return out
}

func fuzzySuffix(text, trailer string) bool {
	tw := strings.Fields(trailer)
	ww := strings.Fields(text)
	if len(ww) < len(tw) {
		return false
	}
	for i := range tw {
		if !fuzzyWord(strings.ToLower(tw[i]), strings.ToLower(ww[len(ww)-len(tw)+i])) {
			return false
		}
	}
	return true
}

func capitalizeFirst(s string) string {
	if s == "" {
		return s
	}
	return strings.ToUpper(s[:1]) + s[1:]
}

// worldPrediction resolves the seed-bound arguments of a world family.
func (tp *toolParser) worldPrediction(tf toolFrame, slots []string, st *store) toolPrediction {
	pred := toolPrediction{category: tf.category, ok: true}
	switch tf.category {
	case "world_contact_research_email_result_usage":
		// slots: subject, nickname, relation, city, context
		pred.tools = []protocol.ToolSpec{{Name: "search_web"}, {Name: "gmail_send"}}
		if len(slots) >= 5 && st != nil {
			if p := st.personByNick(slots[1], slots[2]); p != nil && p.email != "" {
				pred.tools[1].RequiredArgs = map[string]string{"to": p.email}
			}
		}
	case "world_memory_delete":
		pred.tools = []protocol.ToolSpec{{Name: "delete_memory"}}
		if len(slots) >= 3 && st != nil {
			if p := st.personByNick(slots[0], slots[2]); p != nil && p.toolNotePairID != "" {
				pred.tools[0].RequiredArgs = map[string]string{"pair_id": p.toolNotePairID}
			}
		}
	case "world_memory_update":
		pred.tools = []protocol.ToolSpec{{Name: "update_memory"}}
		if len(slots) >= 1 && st != nil {
			if pr := st.projectByAlias(slots[0]); pr != nil && pr.toolNotePairID != "" {
				pred.tools[0].RequiredArgs = map[string]string{"pair_id": pr.toolNotePairID, "content": "handoff is Friday"}
			}
		}
	case "world_theme_discover_set":
		pred.tools = []protocol.ToolSpec{{Name: "discover_capabilities"}, {Name: "set_accent_color"}}
		if st != nil {
			if accent, ok := st.prefs["accent color"]; ok {
				pred.tools[1].RequiredArgs = map[string]string{"color": accent}
			}
		}
	case "world_business_workflow":
		pred.tools = []protocol.ToolSpec{{Name: "list_workflows"}, {Name: "create_workflow"}}
		if len(slots) >= 3 && st != nil {
			if pr := st.projectByAlias(slots[0]); pr != nil {
				args := map[string]string{"name": pr.name}
				if lead := st.personByNick(slots[2], ""); lead != nil && lead.email != "" {
					args["steps"] = lead.email
				}
				pred.tools[1].RequiredArgs = args
			}
		}
	case "world_link_chain_result_usage":
		pred.tools = []protocol.ToolSpec{{Name: "search_web"}, {Name: "read_links"}}
	case "v10_state_dependent_routing":
		return tp.routePrediction(slots[0], st)
	default:
		for _, name := range tf.tools {
			pred.tools = append(pred.tools, protocol.ToolSpec{Name: name})
		}
	}
	return pred
}

// routePrediction reads the planning note for the alias and emits the route.
func (tp *toolParser) routePrediction(alias string, st *store) toolPrediction {
	pred := toolPrediction{category: "v10_state_dependent_routing", ok: true}
	if st == nil {
		return pred
	}
	r, ok := st.routes[strings.ToLower(strings.Trim(alias, "\""))]
	if !ok {
		return pred
	}
	switch r.kind {
	case "job":
		pred.tools = []protocol.ToolSpec{{Name: "execute_agent_job"}}
	case "create":
		pred.tools = []protocol.ToolSpec{{Name: "create_workflow", RequiredArgs: map[string]string{"name": r.projectName}}}
	case "run":
		pred.tools = []protocol.ToolSpec{{Name: "list_workflows"}, {Name: "run_workflow", RequiredArgs: map[string]string{"name": r.projectName}}}
	}
	return pred
}

// toolSignature is the comparable outcome: tool names plus seed-bound required
// arguments. Result-usage needle values (served by the mock endpoint at run
// time) are excluded on both sides because a parser cannot know them without
// executing the tool — which any harness, honest or not, must do.
func toolSignature(specs []protocol.ToolSpec, fuzzy bool, excluded func(key, value string) bool) string {
	parts := make([]string, 0, len(specs))
	for _, spec := range specs {
		part := spec.Name
		keys := make([]string, 0, len(spec.RequiredArgs))
		for k, v := range spec.RequiredArgs {
			if excluded != nil && excluded(k, v) {
				continue
			}
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			part += "|" + k + "=" + spec.RequiredArgs[k]
		}
		parts = append(parts, part)
	}
	if fuzzy {
		sort.Strings(parts)
	}
	if len(parts) == 0 {
		return "<no-tool>"
	}
	return strings.Join(parts, " -> ")
}

// memoryFetchPrediction handles the v8 memory_fetch family whose pinned pair id
// is an accountant note in the store. Every memory_fetch case seeds its own
// note and asks the same question, so the k-th prompt binds the k-th note.
func memoryFetchPrediction(st *store, ordinal int) toolPrediction {
	pred := toolPrediction{category: "memory_fetch", ok: true, tools: []protocol.ToolSpec{{Name: "search_memories"}, {Name: "fetch_memories"}}}
	if st != nil && ordinal < len(st.accountants) {
		pred.tools[1].RequiredArgs = map[string]string{"pairIds": st.accountants[ordinal]}
	}
	return pred
}

var memoryFetchFrames = compileFrames(
	"What is the phone number of my accountant for 2024?",
	"Can you find the number for the accountant who handled my 2024 taxes?",
	"I need to call my 2024 accountant — what number do I have saved?",
)

// fixtureNeedles maps case id to the served needle text recorded in the
// artifact so result-usage argument values can be excluded from signatures.
func fixtureNeedles(a gen.DatasetArtifact) map[string]string {
	out := make(map[string]string, len(a.ToolFixtures))
	for _, f := range a.ToolFixtures {
		out[f.CaseID] = f.Needle
	}
	return out
}

// needleValue extracts the numeric value of a served needle sentence ("the
// Veltrix index reached 3,418 points") so the gmail_send body argument, which
// carries that value, is recognised.
func needleValue(needle string) string {
	best := ""
	for _, tok := range strings.Fields(needle) {
		t := strings.Trim(tok, ".,;")
		digits := 0
		for _, r := range t {
			if r >= '0' && r <= '9' {
				digits++
			}
		}
		if digits > 0 && digits >= len(best) {
			best = t
		}
	}
	return best
}
