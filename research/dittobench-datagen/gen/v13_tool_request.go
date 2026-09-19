package gen

import (
	"context"
	"fmt"
	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Select request-visible inputs only. The decision state and stored answers
// must not influence the request: twin members need the same information gap.
func toolFactRequest(tc protocol.ToolCase) (universe.V13FactDocumentRequest, bool, error) {
	values := map[string]string{}
	kind, relation := "", ""
	if s := tc.RequestSource; s != nil {
		kind = s.Kind
		var keys string
		switch kind {
		case "accountant_phone":
			keys = "year"
			relation = "Ask for the saved phone number of the accountant who handled the narrator's taxes for year. Do not name the accountant or supply the phone number; those must be retrieved from stored information."
		case "appearance_after", "appearance_before", "appearance_after_variant", "appearance_before_variant":
			keys = "setting anchor"
			direction := "after"
			if strings.HasPrefix(kind, "appearance_before") {
				direction = "before"
			}
			relation = "Request inspecting the currently offered appearance options and applying the setting entry listed immediately " + direction + " anchor in that list's displayed order. Anchor is a reference, NOT the desired option. Do not invent, misspell or supply the desired option's name; it must be read from the list."
			if strings.HasSuffix(kind, "_variant") {
				keys += " qualifier"
				relation += " Specify the qualifier variant, not the plain base option; both the positional reference and this distinction apply."
			}
		case "world_accent_request":
			keys = "preference"
			relation = "Request checking the available appearance options and applying the narrator's stored preference. Use the narrator's personal preference, not a client's brand color; do not supply the chosen color or invent a spelling."
		case "theme_intent_system", "theme_intent_dark":
			keys = "setting"
			relation = "Request inspecting available appearance settings before changing setting. "
			if kind == "theme_intent_system" {
				relation += "The app should follow the device's light/dark choice automatically, rather than stay fixed to either."
			} else {
				relation += "The narrator wants the dark appearance, not the light appearance or automatic device-following mode."
			}
		case "existing_image_edit":
			keys = "image change"
			relation = "Request editing image that already exists to apply change. Do not request creating a new image from scratch or claim the edit already happened."
		case "capability_overview":
			keys = "topic"
			relation = "Ask what topic can help with and what features it offers, not for a web search or an action to be executed."
		case "capability_settingtask", "capability_settingarea":
			keys = "topic"
			relation = "Ask how or where in this app to configure topic. This is an inquiry about available controls, NOT a request to change any setting now."
		case "capability_featurearea":
			keys = "topic"
			relation = "Ask what features the app offers for topic, not to perform a task in that area."
		case "compute_summary", "compute_mean_range", "compute_product_difference", "compute_normalize", "compute_sum", "compute_percentage":
			keys = "a b c"
			relation = "Request calculating from the supplied numeric operands in an in-process scratchpad, not a background coding job or file/repository task. Do not provide computed answers. "
			switch kind {
			case "compute_summary":
				relation += "Compute the sum, mean and range (maximum minus minimum) of a, b and c."
			case "compute_mean_range":
				relation += "Compute the mean and range (maximum minus minimum) of a, b and c."
			case "compute_product_difference":
				relation += "Compute (a multiplied by b) minus c."
			case "compute_normalize":
				relation += "Normalize a, b and c by their sum so the resulting fractions sum to one."
			case "compute_sum":
				relation += "Compute the sum of a, b and c."
			case "compute_percentage":
				keys = "a b"
				relation += "Compute b as a percentage of a."
			}
		case "agent_read_not_run":
			keys = "target"
			relation = "Ask for the status of target that the narrator already dispatched. This is a read of existing jobs, not a request to start new work."
		case "automation_list":
			keys = "target"
			relation = "Ask what target the narrator already has configured or upcoming. Do not create or run anything."
		case "tool_discovery":
			keys = "capability"
			relation = "Ask to search the available tool bindings for one that can capability before doing anything. Do not actually execute that capability or write code for it."
		case "saved_workflow_run":
			keys = "cadence"
			relation = "Request running the already-saved workflow identified by cadence now. Its name must be found from the saved workflow list, not invented or supplied in this request. Do not create a new workflow or change its schedule."
		case "effort_intent_low", "effort_intent_medium", "effort_intent_high":
			keys = "setting"
			relation = "Request changing setting using natural behavioral intent rather than an enum name. "
			switch kind {
			case "effort_intent_low":
				relation += "The narrator wants quick, minimal deliberation rather than exhaustive analysis."
			case "effort_intent_medium":
				relation += "The narrator wants balanced everyday deliberation, neither minimal nor exhaustive."
			case "effort_intent_high":
				relation += "The narrator wants the deepest, most thorough and careful deliberation, prioritizing care over speed."
			}
		case "agent_run_not_read":
			keys = "item"
			relation = "Request actually starting work on item now, not merely explaining how or checking an existing job."
		case "feedback":
			keys = "item"
			relation = "Request filing item as feedback for the Ditto development team. Do not turn this into a request to implement the feedback or send email."
		case "memory_save_not_search":
			keys = "item"
			relation = "State item as a new personal fact and request remembering it for later. This is a memory write, not a question about previously stored information."
		case "calendar_create":
			keys = "item"
			relation = "Request adding item as a new calendar event. Do not invent a date or time not provided, and do not change the request to searching existing events."
		case "calendar_search":
			keys = "item"
			relation = "Request searching the narrator's existing calendar for events about item. Do not create an event or search the public web."
		case "multi_web_read":
			keys = "item"
			relation = "Request searching online for item, then opening and reading the leading result. Do not supply a URL or settle for the search snippet."
		case "parallel_web_image":
			keys = "item"
			relation = "Request two independent actions: search the web about item and generate a new image depicting item. Neither depends on the other's result; do not prescribe an order."
		case "recipe_create":
			keys = "item"
			relation = "Request creating a new reusable workflow named item for later use. Do not run an existing saved workflow."
		case "set_tool_prefs":
			keys = "item"
			relation = "Request changing the narrator's chat tool permissions/preferences to item. Do not perform the tool actions themselves; this is a preference change."
		case "automation_not_job":
			keys = "schedule task"
			relation = "Request setting up a reusable workflow to perform task on schedule, not a one-off background job. Preserve both the recurrence and the task."
		case "multi_image_edit":
			keys = "subject change"
			relation = "Request generating an image depicting subject, then EDITING that newly created image to apply change. Both stages are required in that order; do not substitute a second unrelated image."
		case "web_search":
			keys = "item"
			relation = "Request searching the public web for current information about item. Do not supply results."
		case "route_web_not_memory", "stale_context_web":
			keys = "item"
			relation = "Mention having discussed item before, but request the CURRENT public news about item, not retrieval of the narrator's old discussion. Preserve the memory-like wording while making the requested current public information clear. Do not supply results."
		case "image_create":
			keys = "item"
			relation = "Request generating a NEW image depicting item, not editing an existing image or merely describing one."
		case "artifacts_create":
			keys = "item"
			relation = "Request building item as an interactive artifact the narrator can preview, not a background coding job or a prose plan."
		case "agent_job":
			keys = "item"
			relation = "Request dispatching a one-off background coding agent job to item. Do not request a reusable workflow or claim the task is already complete."
		case "agent_workflow", "workflow_not_job":
			keys = "item"
			relation = "Request creating a NEW reusable workflow for item, with independent components running in parallel. The narrator must be able to inspect and rerun it. Do not dispatch a one-off background job or run an existing workflow."
		case "world_contact_email":
			keys = "subject nickname relationship city event"
			relation = "Request researching the current exact figure for subject online, then emailing that researched value to nickname, the narrator's relationship in city from event, using their current address from the narrator's contacts. Do not supply an address, value, employer or extra identity facts."
		case "world_workflow":
			keys = "project client reviewer"
			relation = "Request checking whether a workflow for project at client already exists. If not, create it under the project's formal name and include the current contact address of internal reviewer in its review step. The formal name and current address must be resolved from stored context, not supplied or guessed in the request."
		case "web_result_usage":
			keys = "subject"
			relation = "Request a current web lookup for subject and the exact numeric figure reported by the result. Do not supply any figure or claim a lookup has already occurred."
		case "multi_web_result_usage", "link_chain_result_usage":
			keys = "subject"
			relation = "Request searching the web for subject, opening and reading the actual leading source page, and reporting its exact numeric figure. A search blurb alone is insufficient. Do not supply a figure or source URL."
		case "web_recovery_result_usage":
			keys = "subject"
			relation = "Request a current web lookup for subject and its exact numeric figure. Explicitly ask to retry if the lookup encounters a transient error. Do not supply the figure or imply an error already occurred."
		case "schedules_result_usage":
			keys = "cadence"
			relation = "Request reading the narrator's workflow schedules and reporting the exact logged run count of the workflow identified by cadence. Do not request running it or supply a count."
		case "tool_registry_result_usage":
			keys = "capability"
			relation = "Request searching the tool registry for a binding that can capability and reporting the exact registry snapshot number from that search. Do not execute the capability or supply the number."
		case "sandbox_result_usage":
			keys = "routine"
			relation = "Request executing routine in the in-process code sandbox and reporting the exact numeric output. Do not supply the answer or ask for a background job."
		case "agent_jobs_result_usage":
			keys = "job"
			relation = "Request inspecting the narrator's recent background-job list and reporting the exact item count processed by the finished job. Do not dispatch a new job or supply a count."
		case "general_search":
			keys = "topic"
			relation = "Explicitly request an online web lookup to answer topic. Do not answer the question or claim a search already happened."
		case "general_no_search":
			keys = "topic"
			relation = "Ask topic, explicitly prohibiting online searches/lookups and requesting an answer from general knowledge only. Do not answer the question. Preserve the prohibition even while mentioning search."
		case "unobservable_query":
			keys = "topic"
			relation = "Ask directly for topic. Preserve the exact requested fact, person and future/private time scope; do not change it to a prediction, estimate, related public fact, advice request or web lookup. Do not supply an answer or label the question unanswerable."
		case "setting_ensure":
			keys = "setting value"
			relation = "Ask the assistant to ensure the narrator's Ditto setting is value. Do not reveal its currently stored value or whether any change is necessary. Do not demand a setter call if it already matches."
		case "handoff_query":
			keys = "project client"
			relation = "Ask which day the handoff for project at client is now. Do not supply any day or suggest an answer."
		case "contact_query":
			keys = "nickname relationship employer"
			relation = "Ask for the current email address of nickname, the narrator's relationship at employer. Do not supply or suggest an address."
		default:
			if strings.HasPrefix(kind, "decoy_result_") {
				for _, shape := range catalog.DecoyShapes() {
					if kind != "decoy_result_"+shape.Key {
						continue
					}
					keys = "brand subject"
					relation = "Request this capability from brand, with subject as its target or input: " + fmt.Sprintf(shape.Does, "brand") + " Ask for the exact figure, code or identifier reported in the returned result about subject. Do not supply a result or claim the action already happened. Keep the named product distinct from Ditto and other services; do not mention a tool API name."
					break
				}
			}
			if relation == "" {
				return universe.V13FactDocumentRequest{}, false, fmt.Errorf("fact request: unknown source")
			}
		}
		if len(s.Values) != len(strings.Fields(keys)) {
			return universe.V13FactDocumentRequest{}, false, fmt.Errorf("fact request: unexpected source fields")
		}
		for _, k := range strings.Fields(keys) {
			values[k] = s.Values[k]
		}
	} else if s := tc.DecisionSource; s != nil {
		switch s.Kind {
		case "effort_default", "effort_unsettled":
			kind, values["context"] = "effort_request", s.Context
			relation = "For planning context context only, ask to set the narrator's reasoning effort to their standing default. Do not state whether a default exists, any level, or any style."
		case "calendar_dated", "calendar_undated":
			kind, values["context"] = "calendar_request", s.Context
			relation = "For planning context context only, request adding the previously discussed pending event to the calendar. Do not identify its title/date or say whether a date has been decided."
		case "recipient_decided", "recipient_undecided":
			kind, values["context"], values["project"] = "email_request", s.Context, s.Values["project"]
			relation = "For planning context context only, request emailing the project update to the previously discussed recipient. Do not identify a recipient/address or say whether one has been chosen."
		case "route_one_off", "route_new_workflow", "route_existing_workflow":
			kind, values["project"] = "dependency_request", s.Values["project"]
			relation = "Request starting the dependency-risk review for project now, following the earlier agreed decision. Do not identify the agreed route, job/workflow type, or workflow name."
		case "route_calendar_absent", "route_calendar_exists":
			kind, values["project"], values["day"] = "review_calendar_request", s.Values["project"], s.RequestedDay
			relation = "Request ensuring the project review is on the narrator's calendar for day. Do not reveal whether an event exists or prescribe creating versus searching."
		case "route_email_planned", "route_email_requested":
			kind, values["project"] = "numbers_request", s.Values["project"]
			relation = "Request sending the ready project numbers to the previously discussed intended recipient now. Do not name them, supply an address or reveal whether they requested the numbers."
		default:
			subjects := map[string]string{"read_storage unit": "storage unit number at Larkhill", "read_gym locker": "gym locker combination", "read_plumber": "phone number for the plumber who fixed the kitchen leak", "read_booking reference": "Helsinki ferry booking reference", "read_library card": "library card number", "read_bike lock": "bike lock combination", "read_wifi": "guest wifi password at the cabin", "read_parking spot": "assigned parking spot in the office garage"}
			subject, ok := subjects[s.Kind]
			if !ok {
				return universe.V13FactDocumentRequest{}, false, fmt.Errorf("fact request: unsupported decision")
			}
			kind, values["subject"] = "stored_fact_query", subject
			relation = "Ask the assistant to recall the narrator's subject from memory. Do not provide or suggest its value."
		}
	} else {
		return universe.V13FactDocumentRequest{}, false, nil
	}
	r := universe.V13FactDocumentRequest{Revision: universe.V13FactDocumentRevision, Domain: "tool_request", Bindings: map[string]string{}}
	keys := make([]string, 0, len(values))
	for k := range values {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	args := map[string]string{}
	for i, k := range keys {
		token := fmt.Sprintf("{{value%d}}", i)
		args[k], r.Bindings[token] = token, values[k]
	}
	r.Records = []universe.V13FactDocumentRecord{{Role: "request", MinBytes: 1, MaxBytes: 1800, Assertions: []universe.V13DocumentAssertion{{Kind: kind, Relation: relation, Arguments: args}}}}
	return r, true, universe.ValidateV13FactDocumentRequest(r)
}

func renderToolRequests(ctx context.Context, tools []protocol.ToolCase, renderer universe.V13FactDocumentRenderer) ([]protocol.ToolCase, error) {
	requests := map[int]universe.V13FactDocumentRequest{}
	for i, tc := range tools {
		r, ok, err := toolFactRequest(tc)
		if err != nil {
			return nil, err
		}
		if ok {
			requests[i] = r
		} else if tc.MutationSource == nil {
			return nil, fmt.Errorf("fact request: uncovered tool category %s", tc.Category)
		}
	}
	out := append([]protocol.ToolCase(nil), tools...)
	for i := range out {
		r, ok := requests[i]
		if !ok {
			continue
		}
		p, err := universe.RenderV13FactDocument(ctx, r, renderer)
		if err != nil {
			return nil, err
		}
		out[i].Prompt = p.Records[0]
	}
	return out, nil
}
