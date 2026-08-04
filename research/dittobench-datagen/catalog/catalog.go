// Package catalog defines the Ditto tool catalog presented to harnesses. Each
// entry has a name, a short description, and a JSON-schema parameter
// definition. This is the tool menu a harness sees on every RunRequest.
//
// Schemas mirror the production Ditto backend's tool surface (array-valued
// queries/urls/pairIds, goal-based dynamic workflows), so a harness graded
// here faces the same shapes it would face in the real product.
package catalog

import (
	"encoding/json"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// prop is one JSON-schema property: name, type, and description. Type
// "string[]" renders an array of strings; anything else is used verbatim
// ("string", "integer", "boolean").
type prop struct {
	name string
	typ  string
	desc string
}

// schema builds a JSON-schema object from ordered property definitions.
func schema(required []string, props ...prop) json.RawMessage {
	properties := map[string]any{}
	for _, p := range props {
		if p.typ == "string[]" {
			properties[p.name] = map[string]any{
				"type":        "array",
				"items":       map[string]any{"type": "string"},
				"description": p.desc,
			}
			continue
		}
		properties[p.name] = map[string]any{
			"type":        p.typ,
			"description": p.desc,
		}
	}
	s := map[string]any{
		"type":       "object",
		"properties": properties,
	}
	if len(required) > 0 {
		s["required"] = required
	}
	b, _ := json.Marshal(s)
	return json.RawMessage(b)
}

// params keeps the string-only helper for simple tools.
func params(props map[string]string, required ...string) json.RawMessage {
	ps := make([]prop, 0, len(props))
	for name, desc := range props {
		ps = append(ps, prop{name: name, typ: "string", desc: desc})
	}
	return schema(required, ps...)
}

// Catalog returns the full Ditto tool catalog.
func Catalog() []protocol.ToolDefinition {
	return []protocol.ToolDefinition{
		{
			Name:        "create_image",
			Description: "Generate a new image from a text prompt.",
			Parameters:  params(map[string]string{"prompt": "what to draw"}, "prompt"),
		},
		{
			Name:        "edit_image",
			Description: "Edit an existing image given an instruction.",
			Parameters:  params(map[string]string{"image_url": "image to edit", "instruction": "how to change it"}, "image_url", "instruction"),
		},
		{
			Name:        "read_links",
			Description: "Read one or more URLs and return markdown text content.",
			Parameters: schema([]string{"urls"},
				prop{"urls", "string[]", "URLs to read"},
			),
		},
		{
			Name:        "search_web",
			Description: "Search live sources for one or more queries.",
			Parameters: schema([]string{"queries"},
				prop{"queries", "string[]", "Search queries"},
				prop{"num_results", "integer", "Number of results per query, default 10"},
				prop{"search_mode", "string", "Optional source filter, default all"},
			),
		},
		{
			Name:        "search_memories",
			Description: "Search past conversations and return compact memory summaries. Use fetch_memories for selected IDs that need full text. For named entities/topics, prefer search_subjects -> search_memories_in_subjects.",
			Parameters: schema([]string{"queries"},
				prop{"queries", "string[]", "Memory search queries"},
			),
		},
		{
			Name:        "search_subjects",
			Description: "Search the user's subject graph and return subject objects with id, name, description, and similarity.",
			Parameters: schema([]string{"queries"},
				prop{"queries", "string[]", "Subject search queries"},
			),
		},
		{
			Name:        "fetch_memories",
			Description: "Fetch full conversation text for selected memory pair IDs.",
			Parameters: schema([]string{"pairIds"},
				prop{"pairIds", "string[]", "Memory pair IDs to fetch"},
				prop{"stripImages", "boolean", "Exclude images, default true"},
			),
		},
		{
			Name:        "search_memories_in_subjects",
			Description: "Semantic memory search inside one subject. Use focused queries; fetch selected IDs for full text.",
			Parameters: schema([]string{"subject_id", "queries"},
				prop{"subject_id", "string", "Subject ID from search_subjects or the prompt"},
				prop{"queries", "string[]", "Focused queries within the subject"},
			),
		},
		{
			Name:        "artifacts",
			Description: "Create an interactive, previewable artifact (web app, doc, game).",
			Parameters:  params(map[string]string{"spec": "what to build"}, "spec"),
		},
		{
			Name:        "execute_agent_job",
			Description: "Dispatch a one-off background agent job to Ditto Code — a sandboxed coding harness that can write and run code, install packages, run shell commands, and edit files in a workspace. Use for real coding/automation work that needs the file system, the network, or a repo. For a pure in-context calculation with no side effects, use run_code instead.",
			Parameters:  params(map[string]string{"task": "the task to run"}, "task"),
		},
		{
			Name:        "run_code",
			Description: "Execute JavaScript in a fast in-process sandbox to compute a result or chain tool calls (Code Mode). Use for a one-off calculation, data transformation, or orchestrating several tools over their results — it has NO file system, network, or package installs. For real coding work (writing a repo, running shell, editing files) use execute_agent_job instead.",
			Parameters:  params(map[string]string{"code": "the JavaScript to execute"}, "code"),
		},
		{
			Name:        "search_tools",
			Description: "Search the available tools by keyword and return the matching tool signatures. Use inside Code Mode to discover which tool binding to call before writing run_code.",
			Parameters:  params(map[string]string{"query": "keywords describing the capability you need"}, "query"),
		},
		{
			Name:        "execute_agent_workflow",
			Description: "Plan a complex task as multiple parallel sub-agents. Use only when the task has clear independent parts (multi-angle research, audits across dimensions); for single one-shot tasks use execute_agent_job instead.",
			Parameters: schema([]string{"goal"},
				prop{"goal", "string", "High-level intent; the planner decomposes this into 2-6 parallel sub-tasks."},
				prop{"max_parallel", "integer", "Cap on concurrent workers (default 4, max 8)."},
			),
		},
		{
			Name:        "get_agent_job_status",
			Description: "Check the status of a running or finished agent job.",
			Parameters:  params(map[string]string{"job_id": "the job ID"}, "job_id"),
		},
		{
			Name:        "list_agent_jobs",
			Description: "List the user's recent agent jobs.",
			Parameters: schema(nil,
				prop{"status", "string", "Filter by status: pending, running, completed, failed, cancelled (optional)"},
				prop{"limit", "integer", "Max results to return (default 20, max 50)"},
			),
		},
		{
			Name:        "file_feedback_for_team",
			Description: "Send feedback or a bug report to the Ditto team.",
			Parameters:  params(map[string]string{"message": "the feedback message"}, "message"),
		},
		{
			Name:        "set_theme",
			Description: "Change the app's color theme.",
			Parameters:  params(map[string]string{"theme": "theme name, e.g. dark or light"}, "theme"),
		},
		{
			Name:        "set_main_model",
			Description: "Change the primary chat model.",
			Parameters:  params(map[string]string{"model": "model identifier"}, "model"),
		},
		{
			Name:        "set_reasoning_effort",
			Description: "Set the reasoning effort level for responses.",
			Parameters:  params(map[string]string{"effort": "low, medium, or high"}, "effort"),
		},
		{
			Name:        "set_chat_tool_preferences",
			Description: "Enable or disable specific tools for chat.",
			Parameters:  params(map[string]string{"preferences": "tool preference settings"}, "preferences"),
		},
		// --- scheduling / automations / recipes ---
		{
			Name:        "create_automation",
			Description: "Schedule a recurring or one-shot automation that runs on a cron/time trigger (e.g. every morning, every Monday). Use for anything the user wants to happen on a schedule, not right now.",
			Parameters:  params(map[string]string{"schedule": "when to run, e.g. 'every morning at 8am'", "task": "what the automation should do"}, "schedule", "task"),
		},
		{
			Name:        "list_automations",
			Description: "List the user's existing scheduled automations.",
			Parameters:  params(map[string]string{}),
		},
		{
			Name:        "create_recipe",
			Description: "Save a reusable multi-step recipe the user can run later on demand.",
			Parameters:  params(map[string]string{"name": "recipe name", "steps": "the ordered steps"}, "name", "steps"),
		},
		{
			Name:        "apply_recipe",
			Description: "Run a previously-saved recipe by name.",
			Parameters:  params(map[string]string{"name": "recipe name"}, "name"),
		},
		// --- capability discovery ---
		{
			Name:        "discover_capabilities",
			Description: "Look up what Ditto can do and how to use a feature. Call whenever the user asks what you or Ditto can do, where a setting lives, or how to accomplish something in the app.",
			Parameters:  params(map[string]string{"query": "what the user is trying to do (leave empty for an overview)"}),
		},
		// --- memory writes / graph management ---
		{
			Name:        "save_memory",
			Description: "Save a new fact the user states about themselves to long-term memory. Use when the user TELLS you something to remember, not when they ask a question.",
			Parameters:  params(map[string]string{"content": "the fact to remember"}, "content"),
		},
		{
			Name:        "update_memory",
			Description: "Update an existing remembered fact to a new value.",
			Parameters:  params(map[string]string{"pair_id": "memory to update", "content": "the new value"}, "pair_id", "content"),
		},
		{
			Name:        "delete_memory",
			Description: "Delete a remembered fact at the user's request.",
			Parameters:  params(map[string]string{"pair_id": "memory to delete"}, "pair_id"),
		},
		// --- Google Workspace integrations ---
		{
			Name:        "calendar_create_event",
			Description: "Create an event on the user's Google Calendar.",
			Parameters:  params(map[string]string{"title": "event title", "when": "date/time"}, "title", "when"),
		},
		{
			Name:        "calendar_search_events",
			Description: "Search the user's Google Calendar for events.",
			Parameters:  params(map[string]string{"query": "what to find"}, "query"),
		},
		{
			Name:        "gmail_send",
			Description: "Send an email from the user's Gmail account.",
			Parameters:  params(map[string]string{"to": "recipient", "subject": "subject", "body": "message body"}, "to", "body"),
		},
		// --- additional appearance settings ---
		{
			Name:        "set_accent_color",
			Description: "Change the app's accent color.",
			Parameters:  params(map[string]string{"color": "accent color"}, "color"),
		},
		{
			Name:        "set_chat_font",
			Description: "Change the font used in the chat view.",
			Parameters:  params(map[string]string{"font": "font name"}, "font"),
		},
	}
}

// CatalogForVersion returns the product tool surface frozen into a benchmark
// version. V7 retains the historical automation/recipe/workflow names. ChatV2
// consolidated those concepts for v8: workflows own reusable steps and their
// schedules, while the old names remain dispatch-only compatibility shims in
// the backend and must not be advertised to a new benchmark.
func CatalogForVersion(benchVersion int) []protocol.ToolDefinition {
	legacy := Catalog()
	if benchVersion < protocol.BenchVersionV8 {
		return legacy
	}
	retired := map[string]bool{
		"execute_agent_workflow": true,
		"get_agent_job_status":   true,
		"create_automation":      true,
		"list_automations":       true,
		"create_recipe":          true,
		"apply_recipe":           true,
	}
	out := make([]protocol.ToolDefinition, 0, len(legacy)+4)
	for _, tool := range legacy {
		if !retired[tool.Name] {
			out = append(out, tool)
		}
	}
	out = append(out,
		protocol.ToolDefinition{
			Name:        "create_workflow",
			Description: "Propose a reusable workflow made of one or more inspectable steps. A schedule, when requested, is part of this same proposal.",
			Parameters: schema([]string{"name", "steps"},
				prop{"name", "string", "Short workflow name"},
				prop{"steps", "array", "Workflow steps"},
				prop{"schedule", "string", "Optional JSON schedule for the workflow"},
			),
		},
		protocol.ToolDefinition{
			Name:        "list_workflows",
			Description: "List saved workflows with their schedules.",
			Parameters:  schema(nil),
		},
		protocol.ToolDefinition{
			Name:        "list_schedules",
			Description: "List schedules attached to the user's workflows.",
			Parameters:  schema(nil),
		},
		protocol.ToolDefinition{
			Name:        "run_workflow",
			Description: "Propose running one saved workflow now.",
			Parameters: schema(nil,
				prop{"recipe_id", "string", "Workflow id from list_workflows"},
				prop{"name", "string", "Workflow name when its id is unknown"},
			),
		},
	)
	return out
}
