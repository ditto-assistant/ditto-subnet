package parserprobe

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// addV13WireCatalog binds coined names to the catalog visible on /run. It
// receives no artifact seed, cases, grading rules or fixture needles.
func (tp *toolParser) addV13WireCatalog(definitions []protocol.ToolDefinition) {
	// The V13 renderer can compose multiple edits in one word. Earlier
	// contracts retain their existing matcher budget.
	for i := range tp.frames {
		tp.frames[i].frame.literalBudget = 3
	}
	for i := range tp.routing {
		tp.routing[i].literalBudget = 3
	}
	for _, surface := range datagen.V13NamedToolGrammars() {
		for _, slot := range surface.Slots {
			surface.Grammar[slot] = []string{"SLOTZZ" + slot + "ZZ"}
		}
		for _, pattern := range enumerateGrammar(surface.Grammar) {
			var roles []string
			pattern = namedToolSlotV13.ReplaceAllStringFunc(pattern, func(marker string) string {
				roles = append(roles, strings.TrimSuffix(strings.TrimPrefix(marker, "SLOTZZ"), "ZZ"))
				return "%s"
			})
			f := compileFrame(pattern)
			f.literalBudget = 3
			tp.named = append(tp.named, namedToolFrameV13{f, surface.Category, roles, surface.Value})
		}
	}
	for _, surface := range datagen.V13DiscoverySurfaces() {
		for _, prompt := range surface.Templates {
			prompt = strings.NewReplacer("%[1]s", "%s", "%[2]s", "%s").Replace(prompt)
			frame := compileFrame(prompt)
			frame.literalBudget = 3
			tp.discovery = append(tp.discovery, discoveryFrameV13{surface: surface, frame: frame})
		}
	}
	for _, definition := range definitions {
		for _, shape := range catalog.DecoyShapes() {
			suffix := "_" + shape.Suffix
			if !strings.HasSuffix(definition.Name, suffix) {
				continue
			}
			stem := strings.TrimSuffix(definition.Name, suffix)
			if stem == "" {
				continue
			}
			brand := capitalizeFirst(stem)
			// The name alone isn't a capability definition. Bind the public
			// description too, so a coincidental suffix cannot select a tool.
			wantDescription := strings.ReplaceAll(shape.Does+" "+shape.Not, "%[1]s", brand)
			if definition.Description != wantDescription {
				continue
			}
			for _, prompt := range shape.Prompts {
				frame := compileFrame(fmt.Sprintf(prompt, brand, "%s"))
				frame.literalBudget = 3 // V13 applies composed writing-noise passes.
				tp.frames = append(tp.frames, toolFrame{category: "decoy_" + shape.Key + "_result_usage", frame: frame, tools: []string{definition.Name}})
			}
		}
	}
	grammar := datagen.V13LinkReadGrammar()
	grammar["subject"] = []string{"%s"}
	for _, prompt := range enumerateGrammar(grammar) {
		frame := compileFrame(prompt)
		frame.literalBudget = 3
		tp.frames = append(tp.frames, toolFrame{category: "world_link_chain_result_usage", frame: frame, tools: []string{"search_web", "read_links"}})
	}
	for _, surface := range datagen.V13UnexpectedSurfaces() {
		for _, prompt := range surface.Templates {
			frame := compileFrame(prompt)
			frame.literalBudget = 3
			tp.frames = append(tp.frames, toolFrame{category: surface.Category, frame: frame, tools: surface.Tools})
		}
	}
	sort.SliceStable(tp.frames, func(i, j int) bool { return len(tp.frames[i].frame.tokens) > len(tp.frames[j].frame.tokens) })
	// Match explicit trailers before a terminal value slot can swallow them.
	sort.SliceStable(tp.named, func(i, j int) bool { return len(tp.named[i].frame.tokens) > len(tp.named[j].frame.tokens) })
}

var namedToolSlotV13 = regexp.MustCompile("SLOTZZ[a-z]+ZZ")

type namedToolFrameV13 struct {
	frame    frame
	category string
	roles    []string
	value    string
}

func (tp *toolParser) namedPredictionV13(prompt string, st *store) (toolPrediction, bool) {
	if p, matched := restraintContextPredictionV13(prompt, st); matched {
		return p, true
	}
	for _, text := range tp.unwrap(strings.TrimSpace(prompt)) {
		for _, candidate := range tp.named {
			slots, ok := candidate.frame.match(text)
			if !ok {
				continue
			}
			values := map[string]string{}
			for i, role := range candidate.roles {
				values[role] = strings.Trim(slots[i], "\"“”")
			}
			if candidate.category == "set_effort" {
				return toolPrediction{category: candidate.category, ok: true, tools: []protocol.ToolSpec{{Name: "set_reasoning_effort", RequiredArgs: map[string]string{"effort": candidate.value}}}}, true
			}
			if candidate.category == "v13_restraint_abstention_web" {
				p := toolPrediction{category: candidate.category, ok: true}
				if candidate.value != "" {
					p.tools = []protocol.ToolSpec{{Name: candidate.value}}
				}
				return p, true
			}
			if candidate.category == "v13_restraint_declarative_preference" {
				p := toolPrediction{category: candidate.category}
				key, tool, arg := "accent color", "set_accent_color", "color"
				switch candidate.value {
				case "chat font":
					key, tool, arg = "interface font", "set_chat_font", "font"
				case "color mode":
					key, tool, arg = "color mode", "set_theme", "theme"
				}
				if st == nil || st.prefs[key] == "" {
					return p, true
				}
				p.ok = true
				if values["value"] != st.prefs[key] {
					p.tools = []protocol.ToolSpec{{Name: tool, RequiredArgs: map[string]string{arg: values["value"]}}}
				}
				return p, true
			}
			if candidate.category == datagen.V13StateDependentCalendarCategory || candidate.category == datagen.V13StateDependentEmailCategory {
				return stateRoutePredictionV13(candidate.category, values["alias"], st), true
			}
			var ordered []string
			switch candidate.category {
			case "world_contact_research_email_result_usage":
				ordered = []string{values["subject"], values["nickname"], values["relation"], values["city"], values["context"]}
			case "world_business_workflow":
				ordered = []string{values["alias"], values["client"], values["nickname"]}
			case "v10_state_dependent_routing":
				ordered = []string{values["alias"]}
			case "world_memory_delete":
				ordered = []string{values["nickname"], values["context"], values["relation"]}
			case "world_memory_update":
				p := tp.worldPrediction(toolFrame{category: candidate.category}, []string{values["alias"]}, st)
				if len(p.tools) == 1 && p.tools[0].RequiredArgs != nil {
					p.tools[0].RequiredArgs["content"] = "handoff is " + values["day"]
				}
				return p, true
			}
			return tp.worldPrediction(toolFrame{category: candidate.category}, ordered, st), true
		}
	}
	return toolPrediction{}, false
}

// Resolve a state-dependent route from the public alias and stored reply,
// never from a case index or the generator's hidden exists/absent draw.
func stateRoutePredictionV13(category, alias string, st *store) toolPrediction {
	p := toolPrediction{category: category}
	if st == nil {
		return p
	}
	var replies = map[string]bool{}
	for _, pair := range st.pairs {
		pattern := "Calendar state for %q at %s."
		if category == datagen.V13StateDependentEmailCategory {
			pattern = "Who the %q numbers go to (%s)."
		}
		f := compileFrame(pattern)
		f.literalBudget = 3
		if slots, ok := f.match(pair.Prompt); ok && slots[0] == alias {
			replies[pair.Response] = true
		}
	}
	if len(replies) != 1 {
		return p
	}
	for reply := range replies {
		for _, entry := range []struct {
			pattern, tool, arg string
			index              int
		}{
			{"The %s is already on the calendar as %q — I'd rather not double-book it, so when I ask, find the existing entry and tell me where it sits.", "calendar_search_events", "query", 1},
			{"Nothing for the %s is on the calendar yet — when I ask, create it.", "calendar_create_event", "title", -1},
			{"%s emailed me asking for the %q numbers — the reply goes back to them at %s.", "gmail_send", "to", 2},
			{"Nobody has asked for the %q numbers yet; when they're ready they go to %s at %s.", "gmail_send", "to", 2},
		} {
			if (entry.tool == "gmail_send") != (category == datagen.V13StateDependentEmailCategory) {
				continue
			}
			for _, pattern := range []string{strings.TrimSuffix(entry.pattern, ".") + ", %s.", entry.pattern} {
				f := compileFrame(pattern)
				f.literalBudget = 3
				if slots, ok := f.match(reply); ok {
					value := alias
					if entry.index >= 0 {
						value = slots[entry.index]
					}
					p.ok, p.tools = true, []protocol.ToolSpec{{Name: entry.tool, RequiredArgs: map[string]string{entry.arg: value}}}
					return p
				}
			}
		}
	}
	return p
}

// A scope token joins the visible request to exactly one graph record. It
// never determines the answer: changing the scoped record changes the action.
func restraintContextPredictionV13(prompt string, st *store) (toolPrediction, bool) {
	ref := caseFileRef.FindString(prompt)
	if ref == "" || st == nil {
		return toolPrediction{}, false
	}
	var note, reply string
	seen := map[string]bool{}
	for _, pair := range st.pairs {
		if !strings.Contains(pair.Prompt, ref) {
			continue
		}
		if seen[pair.PairID] {
			continue
		}
		seen[pair.PairID] = true
		note, reply = pair.Prompt, pair.Response
	}
	if len(seen) != 1 {
		return toolPrediction{}, false
	}
	// Strip only the scope preamble; matching starts at the semantic record.
	if _, tail, ok := strings.Cut(note, ". "); ok {
		note = tail
	}
	p := toolPrediction{ok: true}
	for _, candidate := range []struct{ pattern, category, tool, arg string }{
		{"For my Ditto chats, default the reasoning effort to %s — that's my standing preference.", "effort", "set_reasoning_effort", "effort"},
		{"Heads up: my %s is %s. When I ask you to get it on the calendar, that's the one.", "calendar", "calendar_create_event", "title"},
		{"%s (%s) is waiting on the %s update from me — if I say email them the update, that's who.", "email", "gmail_send", "to"},
	} {
		f := compileFrame(candidate.pattern)
		f.literalBudget = 3
		if slots, ok := f.match(note); ok {
			value := slots[0]
			if candidate.arg == "to" {
				value = slots[1]
			}
			p.category = "v13_restraint_" + candidate.category
			p.tools = []protocol.ToolSpec{{Name: candidate.tool, RequiredArgs: map[string]string{candidate.arg: value}}}
			return p, true
		}
	}
	for _, candidate := range []struct{ phrase, category string }{
		{"no default on file yet", "effort"},
		{"hold off on the calendar until you have a date", "calendar"},
		{"tell me who it goes to when you decide", "email"},
	} {
		if containsPhraseFuzzy(reply, candidate.phrase) {
			p.category = "v13_restraint_" + candidate.category
			return p, true
		}
	}
	return toolPrediction{}, false
}

type discoveryFrameV13 struct {
	surface datagen.DiscoverySurface
	frame   frame
}

// discoveryPrediction sees only the prompt and responses to calls it chooses.
// In particular, it has no seed, Inventory object, or expected argument value.
func (tp *toolParser) discoveryPrediction(prompt string, call func(string, json.RawMessage) (string, bool)) (toolPrediction, bool) {
	for _, candidate := range tp.discovery {
		slots, matched := candidate.frame.match(prompt)
		if !matched {
			continue
		}
		if call == nil {
			return toolPrediction{}, true
		}
		response, ok := call("discover_capabilities", json.RawMessage(`{}`))
		if !ok {
			return toolPrediction{}, true
		}
		key, end := "accent colors: ", "; chat fonts:"
		if candidate.surface.ArgKey == "font" {
			key, end = "chat fonts: ", "; color modes:"
		}
		_, tail, ok := strings.Cut(response, key)
		if !ok {
			return toolPrediction{}, true
		}
		list, _, ok := strings.Cut(tail, end)
		if !ok {
			return toolPrediction{}, true
		}
		options := strings.Split(list, ", ")
		alias := slots[len(slots)-1]
		value := closestListedOption(alias, options)
		if candidate.surface.NearMiss {
			qualifier := slots[0]
			if qualifier == "small-caps" {
				qualifier = "SC"
			}
			partner := value + " " + qualifier
			value = ""
			for _, option := range options {
				if strings.EqualFold(option, partner) {
					value = option
					break
				}
			}
		}
		if value == "" {
			return toolPrediction{}, true
		}
		return toolPrediction{category: candidate.surface.Category, ok: true, tools: []protocol.ToolSpec{{Name: "discover_capabilities"}, {Name: candidate.surface.Tool, RequiredArgs: map[string]string{candidate.surface.ArgKey: value}}}}, true
	}
	return toolPrediction{}, false
}

func closestListedOption(alias string, options []string) string {
	best, value, tied := int(^uint(0)>>1), "", false
	for _, option := range options {
		distance := catalog.OSADistance(strings.ToLower(alias), strings.ToLower(option))
		if distance < best {
			best, value, tied = distance, option, false
		} else if distance == best {
			tied = true
		}
	}
	if tied || value == "" || best > catalog.MaxAliasEdits(value) {
		return ""
	}
	return value
}
