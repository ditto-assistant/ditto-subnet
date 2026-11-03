package parserprobe

import (
	"regexp"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Inverse grammar for the v14 process (SN-356) and membership-fold (SN-357)
// families. Both fold state across records, so rows are read in filing-date
// order: from v14 the case-file timestamps follow the story (#3088). Only the
// public prose is read.

// scopedV14RowsInTime returns the scoped case-file rows ordered by their
// filing timestamps.
func scopedV14RowsInTime(st *store, question string) []string {
	scope := caseFileRef.FindString(question)
	if scope == "" {
		return nil
	}
	type row struct{ at, text string }
	seen := map[string]bool{}
	var rows []row
	for _, p := range st.pairs {
		marker := scope + ": "
		i := strings.Index(p.Prompt, marker)
		if i < 0 {
			continue
		}
		text := strings.TrimSpace(p.Prompt[i+len(marker):])
		text = strings.TrimSpace(strings.TrimRight(text, "|}\""))
		text = strings.ReplaceAll(text, ";", ",")
		if !seen[text] {
			seen[text] = true
			rows = append(rows, row{p.Timestamp, text})
		}
	}
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].at < rows[j].at })
	out := make([]string, len(rows))
	for i, r := range rows {
		out[i] = r.text
	}
	return out
}

// ── SN-356 process ──────────────────────────────────────────────────────────

var (
	processV14PlanStep = regexp.MustCompile(`\(\d\) `)
	processV14Done     = []businessFrame{
		businessPattern("Update on %s: %s, so that step is closed.", "subject", "act"),
		businessPattern("%s for %s, that step is complete.", "act", "subject"),
		businessPattern("On %s, %s has now %s.", "subject", "owner", "act"),
		businessPattern("Ahead of the plan, %s already %s for %s.", "owner", "act", "subject"),
		businessPattern("%s for %s early, out of turn, that step is complete.", "act", "subject"),
	}
	processV14Step = []businessFrame{
		businessPattern("%s has started to %s on %s but has not finished, that step is still open.", "owner", "open", "subject"),
		businessPattern("On %s the step to %s is under way with %s and not yet complete.", "subject", "open", "owner"),
		businessPattern("The step to %s on %s has been dropped: it is no longer needed and counts as closed.", "closed", "subject"),
		businessPattern("We are skipping the step to %s on %s altogether, treat it as closed.", "closed", "subject"),
		businessPattern("The step to %s on %s has been reopened: it has to be done again.", "reopened", "subject"),
		businessPattern("On %s the step to %s failed its check and is open again.", "subject", "reopened"),
		businessPattern("The step to %s on %s was checked again and stays closed.", "closed", "subject"),
		businessPattern("A second look at the step to %s on %s confirmed it is finished, it stays closed.", "closed", "subject"),
		businessPattern("Under this batch's %s, the %s for the step to %s on %s is now %s.", "correction", "label", "reassigned", "subject", "owner"),
		businessPattern("%s: %s takes over as %s for the step to %s on %s.", "correction", "owner", "label", "reassigned", "subject"),
	}
)

type processV14PlanEntry struct{ name, owner string }

// processV14Plan parses the ordered plan row: "(1) step, label owner, (2) ...".
func processV14Plan(rows []string) []processV14PlanEntry {
	for _, row := range rows {
		if !strings.Contains(row, "(1) ") {
			continue
		}
		var steps []processV14PlanEntry
		for _, piece := range processV14PlanStep.Split(row, -1)[1:] {
			piece = strings.TrimRight(strings.TrimSpace(piece), ",.")
			name, rest, ok := strings.Cut(piece, ", ")
			if !ok {
				return nil
			}
			fields := strings.Fields(rest)
			if len(fields) < 2 {
				return nil
			}
			steps = append(steps, processV14PlanEntry{name: name, owner: strings.Join(fields[1:], " ")})
		}
		return steps
	}
	return nil
}

// processV14Match resolves a captured step mention to a plan position: the
// whole step phrase, or (for a past-tense done clause) the object after the
// verb, compared word by word with the probe's typo budget.
func processV14Match(raw string, steps []processV14PlanEntry, object bool) int {
	words := strings.Fields(strings.ToLower(raw))
	best, bestLen := -1, 0
	for i, s := range steps {
		target := strings.Fields(strings.ToLower(s.name))
		if object {
			target = target[1:]
		}
		if len(target) == 0 || len(target) > len(words) {
			continue
		}
		tail := words[len(words)-len(target):]
		same := true
		for k := range target {
			if !fuzzyWordBudget(target[k], tail[k], 2) {
				same = false
			}
		}
		if same && len(target) > bestLen {
			best, bestLen = i, len(target)
		}
	}
	return best
}

func answerProcessV14(rows []string) (derived, bool) {
	steps := processV14Plan(rows)
	if len(steps) == 0 {
		return derived{}, false
	}
	closed := make([]bool, len(steps))
	for _, row := range rows {
		if parsed, ok := temporalV14Parse(processV14Step, row); ok {
			switch {
			case parsed["closed"] != "":
				if i := processV14Match(parsed["closed"], steps, false); i >= 0 {
					closed[i] = true
				}
			case parsed["reopened"] != "":
				if i := processV14Match(parsed["reopened"], steps, false); i >= 0 {
					closed[i] = false
				}
			case parsed["reassigned"] != "":
				if i := processV14Match(parsed["reassigned"], steps, false); i >= 0 {
					steps[i].owner = parsed["owner"]
				}
			}
			continue
		}
		if parsed, ok := temporalV14Parse(processV14Done, row); ok {
			if i := processV14Match(parsed["act"], steps, true); i >= 0 {
				closed[i] = true
			}
		}
	}
	d := derived{family: "v13-open-program", kind: protocol.AnswerList}
	for i, s := range steps {
		if !closed[i] {
			d.items, d.ok = []string{s.name, s.owner}, true
			break
		}
	}
	return d, true
}

// ── SN-357 membership fold ──────────────────────────────────────────────────

var membershipV14Frames = []businessFrame{
	businessPattern("The school says no %s for the %s, it was never on our list anyway, so nothing changes.", "noop", "subject"),
	businessPattern("They mentioned a %s is not needed for the %s, but we never had one on the list.", "noop", "subject"),
	businessPattern("The school took the %s off the %s list.", "remove", "subject"),
	businessPattern("Update for the %s: no %s needed any more.", "subject", "remove"),
	businessPattern("Scratch the earlier change for the %s: the %s is back on the list.", "subject", "add"),
	businessPattern("The school changed its mind again — the %s does need the %s after all.", "subject", "add"),
	businessPattern("For the %s, swap the %s for a %s.", "subject", "remove", "add"),
	businessPattern("The %s list now has a %s instead of the %s.", "subject", "add", "remove"),
	businessPattern("One more for the %s: they want a %s too.", "subject", "add"),
	businessPattern("The school added a %s to the %s list.", "add", "subject"),
}

func answerMembershipV14(st *store, question string) (derived, bool) {
	rows := scopedV14RowsInTime(st, question)
	var list []string
	events := 0
	present := map[string]bool{}
	for _, row := range rows {
		if list == nil {
			for _, pattern := range personalV13Frames[6:9] {
				if slots, ok := pattern.frame.match(row); ok {
					for _, item := range strings.Split(slots[2], ",") {
						item = strings.TrimSpace(item)
						list = append(list, item)
						present[item] = true
					}
				}
			}
			if list != nil {
				continue
			}
		}
		parsed, ok := temporalV14Parse(membershipV14Frames, row)
		if !ok || list == nil {
			continue
		}
		events++
		if item := parsed["remove"]; item != "" {
			present[item] = false
		}
		if item := parsed["add"]; item != "" {
			if _, known := present[item]; !known {
				list = append(list, item)
			}
			present[item] = true
		}
	}
	if events == 0 {
		return derived{}, false
	}
	d := derived{family: "v13-personal-program", kind: protocol.AnswerList}
	for _, item := range list {
		if present[item] {
			d.items = append(d.items, item)
		}
	}
	d.ok = len(d.items) > 0
	return d, true
}
