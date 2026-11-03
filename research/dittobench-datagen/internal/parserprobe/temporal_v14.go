package parserprobe

import (
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Inverse grammar for the v14 temporal business families (SN-355, SN-358).
// Like the v13 frames it reads only the public prose: dated records, relative
// dates resolved against their named anchor, and the question's position or
// date. A family it cannot invert stays unparsed rather than being credited as
// resistant.

var temporalV14EventFrames = []businessFrame{
	// Relative dates first: the absolute "is on record for %s" frame would
	// otherwise capture "N days after the X" as its date.
	businessPattern("%s logged a %s: %s, %s days %s the %s.", "subject", "event_label", "event", "days", "dir", "anchor"),
	businessPattern("%s's %s entry reads %s, held %s days %s the %s.", "subject", "event_label", "event", "days", "dir", "anchor"),
	businessPattern("A %s for %s — %s — is on record for %s days %s the %s.", "event_label", "subject", "event", "days", "dir", "anchor"),
	businessPattern("On %s, %s logged a %s: %s.", "date", "subject", "event_label", "event"),
	businessPattern("%s's %s entry dated %s reads %s.", "subject", "event_label", "date", "event"),
	businessPattern("A %s for %s — %s — is on record for %s.", "event_label", "subject", "event", "date"),
}

var temporalV14ChannelFrames = []businessFrame{
	businessPattern("%s's %s was set up as %s on %s.", "subject", "label", "channel", "date"),
	businessPattern("On %s, %s opened its %s over %s.", "date", "subject", "label", "channel"),
	businessPattern("The %s first agreed for %s, on %s, was %s.", "label", "subject", "date", "channel"),
	businessPattern("From %s, %s's %s moved to %s.", "date", "subject", "label", "channel"),
	businessPattern("On %s the %s for %s switched to %s.", "date", "label", "subject", "channel"),
	businessPattern("%s changed its %s to %s with effect from %s.", "subject", "label", "channel", "date"),
}

// Question bodies, matched fuzzily: the surface pass composes typo noise into
// question words. Roles: "ord" is a position word, "named" the event a
// before/after question names.
var temporalV14Questions = temporalV14QuestionFrames(

	businessPattern("Counting from the earliest, which %s was the %s one on %s?", "label", "ord", "subject"),
	businessPattern("In date order, what was the %s %s recorded for %s?", "ord", "label", "subject"),
	businessPattern("Putting every dated %s on %s in order from the earliest, which comes %s?", "label", "subject", "ord"),
	businessPattern("Which %s on %s came immediately after the %s?", "label", "subject", "after"),
	businessPattern("In date order, which %s on %s directly followed the %s?", "label", "subject", "after"),
	businessPattern("For %s, what was the next %s after the %s?", "subject", "label", "after"),
	businessPattern("Which %s on %s came immediately before the %s?", "label", "subject", "before"),
	businessPattern("In date order, which %s on %s directly preceded the %s?", "label", "subject", "before"),
	businessPattern("For %s, what was the last %s before the %s?", "subject", "label", "before"),
)

// temporalV14QuestionFrames widens the literal budget: question words carry
// heavier typo noise than record rows.
func temporalV14QuestionFrames(frames ...businessFrame) []businessFrame {
	for i := range frames {
		frames[i].frame.literalBudget = 3
	}
	return frames
}

var (
	temporalV14Ordinals = []string{"first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth"}
	temporalV14Date     = regexp.MustCompile(`\d{4}-\d{2}-\d{2}|[A-Z][a-z]+ \d{1,2}, \d{4}|\d{1,2} [A-Z][a-z]+ \d{4}`)
)

// temporalV14Body returns the question sentence: the clause ending in "?",
// without the case-file reference, opener or closer around it.
func temporalV14Body(question string) string {
	end := strings.Index(question, "?")
	if end < 0 {
		return ""
	}
	body := question[:end+1]
	if i := strings.LastIndex(body, ". "); i >= 0 {
		body = body[i+2:]
	}
	return strings.TrimSpace(body)
}

// temporalV14Event resolves a captured (possibly noised) event name to the
// recorded event it denotes.
func temporalV14Event(raw string, events []string) (int, bool) {
	raw = strings.ToLower(strings.TrimSpace(raw))
	for i, e := range events {
		if strings.EqualFold(e, raw) {
			return i, true
		}
	}
	for i, e := range events {
		ew, rw := strings.Fields(strings.ToLower(e)), strings.Fields(raw)
		if len(ew) != len(rw) {
			continue
		}
		same := true
		for k := range ew {
			if !fuzzyWordBudget(ew[k], rw[k], 2) {
				same = false
			}
		}
		if same {
			return i, true
		}
	}
	return -1, false
}

func temporalV14Parse(frames []businessFrame, row string) (map[string]string, bool) {
	for _, p := range frames {
		slots, ok := p.frame.match(row)
		if !ok {
			continue
		}
		parsed := map[string]string{}
		for i, role := range p.roles {
			parsed[role] = slots[i]
		}
		return parsed, true
	}
	return nil, false
}

// answerTemporalV14 inverts the v14 event-sequence and channel-as-of
// families. ok is false when the rows carry neither shape.
func answerTemporalV14(rows []string, question string) (derived, bool) {
	if d, ok := answerEventSequenceV14(rows, question); ok {
		return d, true
	}
	return answerChannelAsOfV14(rows, question)
}

func answerEventSequenceV14(rows []string, question string) (derived, bool) {
	type relative struct {
		event, anchor string
		offset        int
	}
	dates := map[string]time.Time{}
	var rels []relative
	for _, row := range rows {
		parsed, ok := temporalV14Parse(temporalV14EventFrames, row)
		if !ok {
			continue
		}
		if parsed["anchor"] != "" {
			days, err := strconv.Atoi(parsed["days"])
			if err != nil {
				continue
			}
			if parsed["dir"] == "before" {
				days = -days
			}
			rels = append(rels, relative{parsed["event"], parsed["anchor"], days})
			continue
		}
		if at, ok := publicEventDate(parsed["date"]); ok {
			dates[parsed["event"]] = at
		}
	}
	// Only the v14 sequence carries relative dates; v13's latest-event has
	// three absolute dates and stays with the v13 grammar.
	if len(rels) == 0 {
		return derived{}, false
	}
	for _, r := range rels {
		anchor, ok := dates[r.anchor]
		if !ok {
			return derived{}, false
		}
		dates[r.event] = anchor.AddDate(0, 0, r.offset)
	}
	order := make([]string, 0, len(dates))
	for event := range dates {
		order = append(order, event)
	}
	sort.Slice(order, func(i, j int) bool { return dates[order[i]].Before(dates[order[j]]) })
	result := derived{family: "v13-open-program", kind: protocol.AnswerValue}
	parsed, ok := temporalV14Parse(temporalV14Questions, temporalV14Body(question))
	if !ok {
		return result, true
	}
	switch {
	case parsed["after"] != "":
		if i, ok := temporalV14Event(parsed["after"], order); ok && i+1 < len(order) {
			result.value, result.ok = order[i+1], true
		}
	case parsed["before"] != "":
		if i, ok := temporalV14Event(parsed["before"], order); ok && i > 0 {
			result.value, result.ok = order[i-1], true
		}
	default:
		if k, ok := temporalV14Event(parsed["ord"], temporalV14Ordinals); ok && k < len(order) {
			result.value, result.ok = order[k], true
		}
	}
	return result, true
}

func answerChannelAsOfV14(rows []string, question string) (derived, bool) {
	type change struct {
		at      time.Time
		channel string
	}
	var chain []change
	for _, row := range rows {
		parsed, ok := temporalV14Parse(temporalV14ChannelFrames, row)
		if !ok {
			continue
		}
		if at, ok := publicEventDate(parsed["date"]); ok {
			chain = append(chain, change{at, parsed["channel"]})
		}
	}
	if len(chain) == 0 {
		return derived{}, false
	}
	query, ok := publicEventDate(temporalV14Date.FindString(temporalV14Body(question)))
	result := derived{family: "v13-open-program", kind: protocol.AnswerValue}
	if !ok {
		return result, true
	}
	sort.Slice(chain, func(i, j int) bool { return chain[i].at.Before(chain[j].at) })
	for _, c := range chain {
		if c.at.Before(query) {
			result.value, result.ok = c.channel, true
		}
	}
	return result, true
}
