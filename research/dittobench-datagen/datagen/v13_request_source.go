package datagen

import "github.com/ditto-assistant/dittobench-datagen/protocol"

// The private request identifies a position in the actually served inventory,
// not an intentionally corrupted spelling. The target itself is withheld.
func v13AppearanceRequestSource(options []string, target, setting, qualifier string) *protocol.ToolRequestSource {
	index := -1
	for i, option := range options {
		if option == target {
			index = i
			break
		}
	}
	if index < 0 || len(options) < 2 {
		panic("appearance source missing from inventory")
	}
	kind, anchor := "appearance_after", ""
	if index > 0 {
		anchor = options[index-1]
	} else {
		kind, anchor = "appearance_before", options[1]
	}
	values := map[string]string{"setting": setting, "anchor": anchor}
	if qualifier != "" {
		kind += "_variant"
		values["qualifier"] = qualifier
	}
	return &protocol.ToolRequestSource{Kind: kind, Values: values}
}

// These inputs are retained production choices, never the rendered prompt.
func v13GrammarRequestSource(category string, choices map[string][]string) *protocol.ToolRequestSource {
	first := func(k string) string {
		if len(choices[k]) == 0 {
			return ""
		}
		return choices[k][0]
	}
	s := &protocol.ToolRequestSource{Values: map[string]string{}}
	switch category {
	case "image_edit_not_create":
		s.Kind = "existing_image_edit"
		s.Values["image"] = first("imgref")
		for _, key := range []string{"change", "directchange", "directchange2"} {
			if first(key) != "" {
				s.Values["change"] = first(key)
				break
			}
		}
	case "capability_discovery":
		s.Kind, s.Values["topic"] = "capability_overview", "this assistant"
		for _, key := range []string{"settingtask", "settingarea", "featurearea"} {
			if first(key) != "" {
				s.Kind, s.Values["topic"] = "capability_"+key, first(key)
				break
			}
		}
	case "code_compute":
		s.Kind = "compute_summary"
		if first("root") == "#lead# my last three readings were #a#, #b#, and #c#. What is the mean, and how far apart are the high and low?" {
			s.Kind = "compute_mean_range"
		}
		for _, k := range []string{"a", "b", "c"} {
			s.Values[k] = first(k)
		}
	case "code_compute_not_agent_job":
		switch first("root") {
		case "No need to spin up a whole project or touch my files — just compute #a# times #b# minus #c# right now.":
			s.Kind = "compute_product_difference"
		case "This is a one-off calculation, not a coding job: normalize #a#, #b#, and #c# to sum to 1 and give me the fractions.":
			s.Kind = "compute_normalize"
		case "Don't build anything — just do the arithmetic: what's the compound total of #a#, #b#, and #c#?":
			s.Kind = "compute_sum"
		case "I don't need a script or a repo, just the answer in your head-scratchpad: what percent of #a# is #b#?":
			s.Kind = "compute_percentage"
		default:
			panic("unsupported v13 arithmetic source")
		}
		for _, k := range []string{"a", "b", "c"} {
			if first(k) != "" {
				s.Values[k] = first(k)
			}
		}
	default:
		return nil
	}
	return s
}
