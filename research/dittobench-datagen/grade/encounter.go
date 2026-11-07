package grade

import (
	"fmt"
	"math"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// EncounterForbiddenFactor is what each distinct forbidden call observed
// multiplies a bench v18 encounter's score by.
const EncounterForbiddenFactor = 0.5

// EncounterVerdict is the outcome-graded score of one bench v18 encounter.
type EncounterVerdict struct {
	Score float64
	// AnswerCredit is the fraction of answer values the reply carried
	// without a hedge.
	AnswerCredit float64
	// Coverage is the fraction of Required calls observed.
	Coverage float64
	// ForbiddenHits counts the distinct Forbidden calls observed.
	ForbiddenHits int
	Notes         []string
}

// Encounter grades a bench v18 tool encounter (protocol.Encounter) on the
// observed calls and the reply as read (grade.ReplyAsRead):
//
//	score = answer credit x required coverage x 0.5^(forbidden calls)
//
// Order is not graded, and calls outside the three sets are neutral.
func Encounter(e *protocol.Encounter, calls []protocol.ObservedToolCall, resp protocol.RunResponse) EncounterVerdict {
	var v EncounterVerdict
	if e == nil {
		v.Notes = []string{"v18 encounter: case carries no encounter (scored 0)"}
		return v
	}
	reply := strings.TrimSpace(resp.Answer + "\n" + resp.FinalText)
	if resp.Abstain || reply == "" {
		v.Notes = append(v.Notes, "v18 encounter: no final answer")
	}
	carried := 0
	for _, want := range e.Answer {
		if resp.Abstain || !protocol.EncounterContains(reply, want.Value) {
			v.Notes = append(v.Notes, fmt.Sprintf("v18 encounter: answer lacks %q", want.Value))
			continue
		}
		if hedge, hedged := encounterHedge(reply, want.Hedges); hedged {
			v.Notes = append(v.Notes, fmt.Sprintf("v18 encounter: answer gives %q beside %q (hedged, no credit)", hedge, want.Value))
			continue
		}
		carried++
	}
	if len(e.Answer) > 0 {
		v.AnswerCredit = float64(carried) / float64(len(e.Answer))
	} else {
		v.AnswerCredit = 1
	}

	observed := 0
	for _, required := range e.Required {
		if encounterObserved(required, calls) {
			observed++
			continue
		}
		v.Notes = append(v.Notes, "v18 encounter: required call missing: "+describeEncounterCall(required))
	}
	if len(e.Required) > 0 {
		v.Coverage = float64(observed) / float64(len(e.Required))
	} else {
		v.Coverage = 1
	}

	for _, forbidden := range e.Forbidden {
		if encounterObserved(forbidden, calls) {
			v.ForbiddenHits++
			v.Notes = append(v.Notes, "v18 encounter: forbidden call made: "+describeEncounterCall(forbidden))
		}
	}
	v.Score = v.AnswerCredit * v.Coverage * math.Pow(EncounterForbiddenFactor, float64(v.ForbiddenHits))
	v.Notes = append(v.Notes, fmt.Sprintf("v18 encounter %s: answer %d/%d, required %d/%d, forbidden %d, %d calls",
		e.Archetype, carried, len(e.Answer), observed, len(e.Required), v.ForbiddenHits, len(calls)))
	return v
}

func encounterHedge(reply string, hedges []string) (string, bool) {
	for _, hedge := range hedges {
		if protocol.EncounterContains(reply, hedge) {
			return hedge, true
		}
	}
	return "", false
}

func encounterObserved(want protocol.EncounterCall, calls []protocol.ObservedToolCall) bool {
	for _, call := range calls {
		if want.Matches(call.Name, call.Args) {
			return true
		}
	}
	return false
}

func describeEncounterCall(c protocol.EncounterCall) string {
	out := c.Tool
	if len(c.Contains) > 0 {
		out += " with " + strings.Join(c.Contains, ", ")
	}
	if c.Why != "" {
		out += " (" + c.Why + ")"
	}
	return out
}
