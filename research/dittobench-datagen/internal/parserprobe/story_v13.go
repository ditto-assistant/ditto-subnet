package parserprobe

import (
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Story v2 (bench_version >= 13) renders every story question as an anchor
// (person-first or subject-first, binding the nickname and the subject alias)
// followed by a per-oracle task sentence. The frame banks are compiled from
// the generator's own grammars (universe.StoryV13QuestionSurfaces), so the
// parser KNOWS the eight families; recovering the typed answers (owner after
// handoffs, status claim sets, event order, next action, quantities, lesson
// claims, the owner-email join) is the story-probe follow-up, so a known story
// v2 question that the parser cannot answer is an honest miss, not a silent
// unregistered family.
func storyV13Families() []memoryFamily {
	anchorGrammar, taskGrammars := universe.StoryV13QuestionSurfaces()
	anchors := enumerateGrammarFrom(anchorGrammar, "anchor")
	families := make([]string, 0, len(taskGrammars))
	for family := range taskGrammars {
		families = append(families, family)
	}
	sort.Strings(families)
	out := make([]memoryFamily, 0, len(families))
	for _, family := range families {
		var templates []string
		for _, task := range enumerateGrammarFrom(taskGrammars[family], "task") {
			for _, anchor := range anchors {
				templates = append(templates, storyV13Template(anchor)+" "+storyV13Template(task))
			}
		}
		out = append(out, memoryFamily{name: family, frames: compileFrames(templates...)})
	}
	return out
}

// storyV13Template turns a story v2 grammar expansion into a %s frame: the
// {nick} / {alias} slots become captures. Capitalisation is irrelevant to the
// lowercase token matcher, so storySentence's leading upper-case is not
// reproduced.
func storyV13Template(s string) string {
	s = strings.ReplaceAll(s, "{nick}", "%s")
	s = strings.ReplaceAll(s, "{alias}", "%s")
	return strings.TrimSpace(s)
}

// enumerateGrammarFrom is enumerateGrammar for a grammar whose root symbol is
// not "root".
func enumerateGrammarFrom(g persona.Grammar, root string) []string {
	if root == "root" {
		return enumerateGrammar(g)
	}
	rooted := persona.Grammar{"root": {"#" + root + "#"}}
	for symbol, alts := range g {
		rooted[symbol] = alts
	}
	return enumerateGrammar(rooted)
}

func init() {
	memoryFamilies = append(memoryFamilies, storyV13Families()...)
}
