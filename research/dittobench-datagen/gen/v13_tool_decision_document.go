package gen

import (
	"context"
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func toolDecisionDocument(s protocol.ToolDecisionSource) (universe.V13FactDocumentRequest, error) {
	if s.Kind == "record_accountant" {
		if len(s.Values) != 3 {
			return universe.V13FactDocumentRequest{}, fmt.Errorf("fact accountant: unexpected values")
		}
		r := universe.V13FactDocumentRequest{Revision: universe.V13FactDocumentRevision, Domain: "personal", Bindings: map[string]string{"{{person0}}": s.Values["person"], "{{year0}}": s.Values["year"], "{{phone0}}": s.Values["phone"]}, Records: []universe.V13FactDocumentRecord{
			{MinBytes: 1, MaxBytes: 1800, Assertions: []universe.V13DocumentAssertion{{Kind: "accountant_identity", Relation: "The narrator asks to remember that person handled their taxes for year. Do not add contact information.", Arguments: map[string]string{"person": "{{person0}}", "year": "{{year0}}"}}}},
			{MinBytes: 1, MaxBytes: 1800, Assertions: []universe.V13DocumentAssertion{{Kind: "accountant_contact", Relation: "An assistant response states that person's office phone number is phone. Preserve the phone identifier exactly.", Arguments: map[string]string{"person": "{{person0}}", "phone": "{{phone0}}"}}}},
		}}
		return r, universe.ValidateV13FactDocumentRequest(r)
	}
	var relation, keys string
	switch s.Kind {
	case "record_stale_context":
		keys = "subject"
		relation = "The narrator saved notes about subject last year. These are historical background and may be out of date; they do not establish current facts or recent developments. Do not invent the contents of the notes or a current answer."
	case "read_storage unit", "read_gym locker", "read_plumber", "read_booking reference", "read_library card", "read_bike lock", "read_wifi", "read_parking spot":
		keys = "value"
		relations := map[string]string{
			"read_storage unit":      "The narrator's storage unit number at the Larkhill facility is value.",
			"read_gym locker":        "The narrator's gym locker combination is value.",
			"read_plumber":           "Reuben Achterberg is the plumber who fixed the narrator's kitchen leak; his phone number is value. Preserve the supplied identifier exactly, even if its format is unusual.",
			"read_booking reference": "The narrator's Helsinki ferry booking reference is value.",
			"read_library card":      "The narrator's library card number is value.",
			"read_bike lock":         "The narrator's bike lock combination is value.",
			"read_wifi":              "The guest wifi password at the narrator's cabin is value.",
			"read_parking spot":      "The narrator's assigned parking spot in the office garage is value.",
		}
		relation = relations[s.Kind]
	case "route_calendar_absent":
		keys = "project client title"
		relation = "For project at client, no calendar event for title exists yet. When requested, the narrator wants it created. Do not claim it is already booked."
	case "route_calendar_exists":
		keys = "project client title"
		relation = "For project at client, the calendar event already exists under title. When requested, locate the existing entry and report where it sits; do NOT double-book or create a duplicate."
	case "route_email_planned":
		keys = "project client person email"
		relation = "For project at client, nobody has requested the numbers yet. Once ready, the numbers should go in a new message to the planned recipient person at email."
	case "route_email_requested":
		keys = "project client person email"
		relation = "For project at client, person already emailed the narrator requesting the numbers. The reply must go back to that requester at email."
	case "route_one_off":
		keys = "project client"
		relation = "For dependency-risk work on project at client, the agreed decision is one one-off Ditto Code job, NOT creating or running a reusable workflow."
	case "route_new_workflow":
		keys = "project client workflow"
		relation = "For dependency-risk work on project at client, the agreed decision is to CREATE a new reusable workflow named workflow. NOT a one-off job and NOT running an existing workflow."
	case "route_existing_workflow":
		keys = "project client workflow"
		relation = "For dependency-risk work on project at client, the agreed decision is to RUN the already existing workflow named workflow. First list saved workflows. Do NOT create a replacement or dispatch a one-off job."
	case "effort_unsettled":
		keys = "first_style second_style"
		relation = "The narrator alternates between first_style and second_style for reasoning effort and has NOT chosen a default. Do not select either style or invent a level."
	case "effort_default":
		keys = "level"
		relation = "The narrator has chosen level as their standing reasoning-effort default for Ditto chats, to apply when requested."
	case "calendar_undated":
		keys = "title"
		relation = "The narrator intends to schedule the title event but has NOT decided any date or time. Do not invent one or imply the event is booked."
	case "calendar_dated":
		keys = "title when"
		relation = "The narrator's pending event is title at when. This identifies the event to add to the calendar when requested; it has not already been added."
	case "recipient_undecided":
		keys = "first_person second_person project"
		relation = "Both first_person and second_person requested the project update. The narrator has NOT chosen who receives it first. Neither person is the decided recipient; do not invent email addresses."
	case "recipient_decided":
		keys = "person email project"
		relation = "Person at email is the intended recipient waiting for the project update. When the narrator requests emailing that update, this is the intended recipient. It has not yet been sent."
	default:
		return universe.V13FactDocumentRequest{}, fmt.Errorf("fact tool decision: unknown kind")
	}
	roles := strings.Fields(keys)
	if len(roles) != len(s.Values) {
		return universe.V13FactDocumentRequest{}, fmt.Errorf("fact tool decision: unexpected values")
	}
	r := universe.V13FactDocumentRequest{Revision: universe.V13FactDocumentRevision, Domain: "planning", Bindings: map[string]string{}}
	args := map[string]string{}
	if !strings.HasPrefix(s.Kind, "route_") && !strings.HasPrefix(s.Kind, "read_") && !strings.HasPrefix(s.Kind, "record_") {
		r.Bindings["{{context0}}"] = s.Context
		args["context"] = "{{context0}}"
		relation = "This note applies ONLY to planning context context, not other contexts. " + relation
	}
	for i, role := range roles {
		token := fmt.Sprintf("{{value%d}}", i)
		r.Bindings[token] = s.Values[role]
		args[role] = token
	}
	r.Records = []universe.V13FactDocumentRecord{{MinBytes: 1, MaxBytes: 1800, Assertions: []universe.V13DocumentAssertion{{Kind: s.Kind, Relation: relation, Arguments: args}}}}
	return r, universe.ValidateV13FactDocumentRequest(r)
}

func renderToolDecisionFacts(ctx context.Context, tools []protocol.ToolCase, renderer universe.V13FactDocumentRenderer) ([]protocol.ToolCase, error) {
	type pending struct {
		tool, pair int
		kind       string
		request    universe.V13FactDocumentRequest
	}
	var requests []pending
	for i, tc := range tools {
		seen := map[*protocol.ToolDecisionSource]bool{}
		pairIDs := map[string]bool{}
		for source := tc.DecisionSource; source != nil; source = source.Previous {
			if seen[source] || pairIDs[source.PairID] {
				return nil, fmt.Errorf("fact tool decision: repeated source")
			}
			seen[source], pairIDs[source.PairID] = true, true
			index := -1
			for j, pair := range tc.PrerequisitePairs {
				if pair.PairID == source.PairID && pair.PairID != "" {
					if index != -1 {
						return nil, fmt.Errorf("fact tool decision: duplicate evidence")
					}
					index = j
				}
			}
			if index == -1 {
				return nil, fmt.Errorf("fact tool decision: missing evidence")
			}
			r, err := toolDecisionDocument(*source)
			if err != nil {
				return nil, err
			}
			requests = append(requests, pending{i, index, source.Kind, r})
		}
	}
	out := append([]protocol.ToolCase(nil), tools...)
	// Traverse original order, never map iteration: transcript replay is ordered.
	for i := range out {
		out[i].PrerequisitePairs = append([]protocol.MemoryPair(nil), tools[i].PrerequisitePairs...)
	}
	for _, item := range requests {
		p, err := universe.RenderV13FactDocument(ctx, item.request, renderer)
		if err != nil {
			return nil, err
		}
		pair := &out[item.tool].PrerequisitePairs[item.pair]
		pair.Prompt = p.Records[0]
		pair.Response = "Noted."
		if item.kind == "record_accountant" {
			// Keep the contact in the response: fetching the full memory, not
			// merely reading its searchable user-text snippet, is the task.
			pair.Response = p.Records[1]
		}
	}
	return out, nil
}
