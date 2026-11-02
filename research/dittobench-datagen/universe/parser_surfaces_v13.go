package universe

import "github.com/ditto-assistant/dittobench-datagen/persona"

// V13StoryProbeGrammars exposes only public renderer grammars. It takes no
// seed or world and cannot expose an instance's answers, labels, or state.
// Return copies so adversarial probes cannot mutate generation contracts.
func V13StoryProbeGrammars() (map[string]persona.Grammar, map[string]persona.Grammar, persona.Grammar) {
	clone := func(g persona.Grammar) persona.Grammar {
		out := persona.Grammar{}
		for k, v := range g {
			out[k] = append([]string(nil), v...)
		}
		return out
	}
	events := map[string]persona.Grammar{}
	for k, g := range storyEventGrammars {
		events[string(k)] = clone(g)
	}
	events["personal-root"] = clone(personalRootGrammar)
	for _, g := range events {
		for _, status := range storyStatusOrder {
			g["status"] = append(g["status"], storyStatusVocabulary[status]...)
		}
		for _, action := range storyNextActions {
			g["what"] = append(g["what"], action.what)
		}
		for _, channel := range storyChannels {
			g["channel"] = append(g["channel"], channel.channel)
		}
	}
	tasks := map[string]persona.Grammar{}
	for k, g := range storyV13TaskGrammars {
		tasks["world-"+k] = clone(g)
	}
	return events, tasks, clone(storyFactGrammars["lesson"])
}
