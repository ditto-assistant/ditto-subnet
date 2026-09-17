package parserprobe

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// addV13WireCatalog binds coined names to the catalog visible on /run. It
// receives no artifact seed, cases, grading rules or fixture needles.
func (tp *toolParser) addV13WireCatalog(definitions []protocol.ToolDefinition) {
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
