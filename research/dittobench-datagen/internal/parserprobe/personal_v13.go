package parserprobe

import (
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// These are inverses of the public household note grammar, not values from
// the generator's private grading fields. The case reference scopes evidence.
var personalV13Frames = []businessFrame{
	businessPattern("Change of plan: %s and %s swapped, so %s has %s from now on.", "old", "current", "value", "subject"),
	businessPattern("%s took over %s from %s — that swap is permanent.", "value", "subject", "old"),
	businessPattern("Rota update: %s moves from %s to %s going forward.", "subject", "old", "value"),
	businessPattern("The clinic moved %s's %s to %s; the old slot is gone.", "person", "subject", "value"),
	businessPattern("Rescheduled: %s's %s is now %s instead.", "person", "subject", "value"),
	businessPattern("Update on the %s for %s — new date %s, replacing the earlier booking.", "subject", "person", "value"),
	businessPattern("%s's %s list from school: %s.", "person", "subject", "list"),
	businessPattern("For %s's %s the school wants: %s.", "person", "subject", "list"),
	businessPattern("Packing list for %s's %s — %s.", "person", "subject", "list"),
	businessPattern("The school changed the %s list: they dropped the %s and added a %s.", "subject", "drop", "add"),
	businessPattern("Update for the %s: no %s after all, but they do now want a %s.", "subject", "drop", "add"),
	businessPattern("Revised %s list — %s is off it, %s is on it.", "subject", "drop", "add"),
	businessPattern("Heard from %s: the %s is now %s — that replaces what I said before.", "person", "subject", "value"),
	businessPattern("Update — %s's %s has gone from what it was to %s.", "person", "subject", "value"),
	businessPattern("%s messaged: the %s plan is %s as of today.", "person", "subject", "value"),
	businessPattern("Changed my mind on %s: the plan now is to %s instead.", "subject", "value"),
	businessPattern("Scrap the earlier plan for %s — we'll %s.", "subject", "value"),
	businessPattern("Final call on %s: %s. Ignore what I said before.", "subject", "value"),
	businessPattern("Rebooked the %s for %s onto the %s departure; the earlier one is cancelled on our booking.", "leg", "subject", "value"),
	businessPattern("%s change: we're now on the %s %s, not the earlier one.", "subject", "value", "leg"),
	businessPattern("New plan for %s — the %s at %s. Forget the original time.", "subject", "leg", "value"),
	businessPattern("%s has taken over organising %s from %s.", "value", "subject", "old"),
	businessPattern("%s changed hands: %s runs it now, not %s.", "subject", "value", "old"),
	businessPattern("Since last month %s is the organiser of %s; %s stepped back.", "value", "subject", "old"),
}

func answerPersonalV13(st *store, question string) derived {
	values := map[string]string{}
	for _, row := range scopedV13Rows(st, question) {
		for _, pattern := range personalV13Frames {
			slots, ok := pattern.frame.match(row)
			if !ok {
				continue
			}
			for i, role := range pattern.roles {
				values[role] = slots[i]
			}
			break
		}
	}
	d := derived{family: "v13-personal-program", kind: protocol.AnswerValue}
	if values["list"] != "" && values["drop"] != "" && values["add"] != "" {
		d.kind = protocol.AnswerList
		dropped := false
		for _, item := range strings.Split(values["list"], ",") {
			item = strings.TrimSpace(item)
			if item == values["drop"] {
				dropped = true
				continue
			}
			d.items = append(d.items, item)
		}
		d.items = append(d.items, values["add"])
		d.ok = dropped
	} else if values["value"] != "" {
		d.value, d.ok = values["value"], true
	}
	return d
}

func answerV13(st *store, question string) derived {
	if d := answerStoryV13(st, question); d.ok {
		return d
	}
	if d := answerBusinessV13(st, question); d.ok {
		return d
	}
	if d := answerPersonalV13(st, question); d.ok {
		return d
	}
	if d := answerAsOfV13(st, question); d.ok {
		return d
	}
	if d := answerQuantityV13(st, question); d.ok {
		return d
	}
	if d := answerInjectionV13(st, question); d.ok {
		return d
	}
	return derived{}
}
