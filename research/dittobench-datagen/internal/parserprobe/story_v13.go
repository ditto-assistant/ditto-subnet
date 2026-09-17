package parserprobe

import (
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

type storyFrameV13 struct {
	kind    string
	pattern frame
	roles   []string
	grammar persona.Grammar
}
type storyEventV13 struct {
	kind      string
	values    map[string]string
	timestamp string
	order     int
}

var storySlotV13 = regexp.MustCompile(`\{([a-z0-9]+)\}|#([a-z]+)#|%s`)

func namedStoryFrameV13(kind, template string, g persona.Grammar) storyFrameV13 {
	var roles []string
	template = storySlotV13.ReplaceAllStringFunc(template, func(slot string) string {
		name := strings.Trim(slot, "{}#")
		if slot == "%s" {
			name = "lesson"
		}
		roles = append(roles, name)
		return "%s"
	})
	f := compileFrame(template)
	f.literalBudget = 3
	f.slotLimit = 48
	f.validateSlot = map[int]func(string) bool{}
	for i, role := range roles {
		if bank := g[role]; len(bank) > 0 {
			var frames []frame
			for _, form := range bank {
				frames = append(frames, namedStoryFrameV13("", form, nil).pattern)
			}
			f.validateSlot[i] = func(value string) bool { _, _, ok := matchAny(frames, value); return ok }
		}
	}
	return storyFrameV13{kind: kind, pattern: f, roles: roles, grammar: g}
}

func storyFramesV13() ([]storyFrameV13, []storyFrameV13, []storyFrameV13) {
	events, tasks, lesson := universe.V13StoryProbeGrammars()
	var ef, qf, lf []storyFrameV13
	var keys []string
	for k := range events {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		for _, s := range events[k]["frame"] {
			ef = append(ef, namedStoryFrameV13(k, s, events[k]))
		}
	}
	keys = nil
	for k := range tasks {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		for _, s := range tasks[k]["task"] {
			qf = append(qf, namedStoryFrameV13(k, s, tasks[k]))
		}
	}
	for _, s := range lesson["r"] {
		for _, tail := range lesson["tail"] {
			lf = append(lf, namedStoryFrameV13("lesson", strings.ReplaceAll(s, "#tail#", tail), nil))
		}
	}
	return ef, qf, lf
}

var storyEventsV13, storyQuestionsV13, storyLessonsV13 = storyFramesV13()

func (p storyFrameV13) match(text string) (map[string]string, bool) {
	slots, ok := p.pattern.match(text)
	if !ok {
		return nil, false
	}
	values := map[string]string{}
	for i, role := range p.roles {
		value := slots[i]
		if old := values[role]; old != "" && !strings.EqualFold(old, value) {
			return nil, false
		}
		if bank := p.grammar[role]; len(bank) > 0 {
			valid := false
			for _, form := range bank {
				f := namedStoryFrameV13("", form, nil)
				if _, ok := f.pattern.match(value); ok {
					valid = true
					break
				}
			}
			if !valid {
				return nil, false
			}
		}
		values[role] = value
	}
	return values, true
}

func (s *store) parseStoriesV13() []storyEventV13 {
	if s.storyV13Ready {
		return s.storyV13
	}
	s.storyV13Ready = true
	seen := map[string]bool{}
	pairs := append([]protocol.MemoryPair(nil), s.pairs...)
	sort.SliceStable(pairs, func(i, j int) bool { return pairs[i].Timestamp < pairs[j].Timestamp })
	for _, p := range pairs {
		if len(p.Prompt) < 1500 || seen[p.PairID] {
			continue
		}
		seen[p.PairID] = true
		for _, para := range strings.Split(p.Prompt, "\n\n") {
			// Facts appended after events aren't part of the event grammar. Preserve
			// multi-sentence events but bound the prefix search at sentence ends.
			sentences := sentences(para)
			for n := 1; n <= min(len(sentences), 4); n++ {
				text := strings.Join(sentences[:n], " ")
				matched := false
				bestSpecificity := -1
				var bestEvent storyEventV13
				for _, f := range storyEventsV13 {
					values, ok := f.match(text)
					if !ok {
						continue
					}
					if who := values["who"]; who != "" && s.personByName(who, "") == nil {
						continue
					}
					if values["alias"] == "" {
						continue
					}
					for _, role := range []string{"status", "what", "channel"} {
						best, distance := values[role], 1000
						for _, form := range f.grammar[role] {
							if _, ok := namedStoryFrameV13("", form, nil).pattern.match(values[role]); ok {
								cost := damerauLevenshtein(strings.ToLower(form), strings.ToLower(values[role]), 20)
								if cost < distance {
									best, distance = form, cost
								}
							}
						}
						values[role] = best
					}
					if (f.kind == "vendor_swapped" || f.kind == "provider_swapped") && len(sentences) > n && containsPhraseFuzzy(sentences[n], "their version") {
						values["qty"] = strings.TrimSuffix(sentences[n], ".")
					}
					specificity := 0
					for _, tok := range f.pattern.tokens {
						if !tok.slot {
							specificity += len(tok.literal)
						}
					}
					if specificity > bestSpecificity {
						bestSpecificity = specificity
						bestEvent = storyEventV13{f.kind, values, p.Timestamp, len(s.storyV13)}
						matched = true
					}
				}
				if matched {
					s.storyV13 = append(s.storyV13, bestEvent)
					break
				}
			}
		}
	}
	return s.storyV13
}

func answerStoryV13(st *store, question string) derived {
	words := strings.Fields(question)
	family := ""
	unit := ""
	for _, f := range storyQuestionsV13 {
		for i := range words {
			if values, ok := f.match(strings.Join(words[i:], " ")); ok {
				family = f.kind
				unit = values["unit"]
				if fuzzyWordBudget("money", strings.ToLower(unit), 2) {
					unit = "money"
				}
				break
			}
		}
		if family != "" {
			break
		}
	}
	if family == "" {
		return derived{}
	}
	d := derived{family: family, kind: protocol.AnswerValue}
	var events []storyEventV13
	for _, e := range st.parseStoriesV13() {
		if containsPhraseFuzzy(question, e.values["alias"]) {
			events = append(events, e)
		}
	}
	if len(events) == 0 {
		return d
	}
	owner, status := "", ""
	var statuses []string
	var sequence, next []string
	var quantity string
	for _, e := range events {
		v := e.values
		switch e.kind {
		case "kickoff", "personal-root", "handoff_assigned":
			if v["who"] != "" {
				owner = v["who"]
			}
		}
		if v["status"] != "" {
			status = v["status"]
			if e.kind == "outcome" || e.kind == "outcome_disputed" {
				statuses = append(statuses, status)
			}
		}
		if (e.kind == "vendor_swapped" || e.kind == "provider_swapped") && v["from"] != "" && v["to"] != "" {
			sequence = []string{v["from"], v["to"]}
		}
		if v["who"] != "" && v["what"] != "" && v["channel"] != "" {
			next = []string{v["who"], v["what"], v["channel"]}
		}
		if phrase := v["capsentence"]; phrase != "" && unit == "money" {
			amounts := reMoney.FindAllString(phrase, -1)
			if len(amounts) == 2 {
				quantity = formatMoney(moneyCents(amounts[0]) - moneyCents(amounts[1]))
			}
		}
		if phrase := v["qty"]; phrase != "" && unit != "money" {
			f := businessPattern("the count is now %s %s, not the %s %s first written down")
			f.frame.validateSlot = map[int]func(string) bool{0: storyIntegerV13, 2: storyIntegerV13}
			if slots, ok := f.frame.match(phrase); ok {
				quantity = slots[0]
			}
			for _, op := range []struct {
				pattern string
				sign    int
			}{
				{"their version adds %s %s to the %s %s %s had quoted", 1},
				{"their version takes %s %s off the %s %s %s had quoted", -1},
			} {
				f := businessPattern(op.pattern).frame
				f.validateSlot = map[int]func(string) bool{0: storyIntegerV13, 2: storyIntegerV13}
				if slots, ok := f.match(phrase); ok {
					quantity = strconv.Itoa(atoi(slots[2]) + op.sign*atoi(slots[0]))
				}
			}
		}
	}
	switch family {
	case "world-story-owner-current":
		d.value = owner
	case "world-story-x-owner-email":
		if pe := st.personByName(owner, ""); pe != nil {
			d.value = pe.email
		}
	case "world-story-status-current":
		d.value = status
	case "world-story-status-disagree":
		if len(statuses) >= 2 {
			d.kind, d.items = protocol.AnswerList, append(statuses[len(statuses)-2:], "disagree")
		}
	case "world-story-order":
		d.kind, d.items = protocol.AnswerOrderedList, sequence
	case "world-story-next-action":
		d.kind, d.items = protocol.AnswerList, next
	case "world-story-quantity":
		d.value = quantity
	case "world-story-lesson-claims":
		for _, p := range st.pairs {
			if len(p.Prompt) < 1500 {
				continue
			}
			relevant := false
			for _, e := range events {
				if containsPhraseFuzzy(p.Prompt, e.values["alias"]) {
					relevant = true
					break
				}
			}
			if !relevant {
				continue
			}
			for _, sentence := range sentences(p.Prompt) {
				for _, f := range storyLessonsV13 {
					if v, ok := f.match(sentence); ok {
						d.value = v["lesson"]
					}
				}
			}
		}
	}
	d.ok = d.value != "" || len(d.items) > 0
	return d
}

func storyIntegerV13(value string) bool {
	_, err := strconv.Atoi(value)
	return err == nil
}
