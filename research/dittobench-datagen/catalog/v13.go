package catalog

import (
	"encoding/json"
	"fmt"
	"hash/fnv"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 tool catalog (issues #1843, #1842, #1580). Everything in this file
// is reachable only from bench_version >= 13; the v8..v12 surface is frozen in
// catalogV8ToV12.
//
// Three levers make a fixed-name phrase table lose weight without renaming any
// production tool:
//
//   - Every production tool's description is drawn per seed from a bank of at
//     least MinDescriptionParaphrases paraphrases, so a harness that matches
//     description bytes (rather than reading them) sees a different surface on
//     every run. Bank entry 0 is the canonical production description.
//   - set_theme and set_reasoning_effort close their value space with a
//     JSON-schema enum on the wire — the production ChatV2 pattern — so a
//     schema-reading agent passes those cases without a discovery call. The
//     discovery-grounded setters (set_accent_color, set_chat_font) instead say
//     that their options are configured at runtime and listed only by
//     discover_capabilities (see inventory.go).
//   - Three to five DECOY tools with coined names and descriptions are spliced
//     into the catalog per seed. A decoy is a near-miss capability whose
//     description states what it is NOT; the mock endpoint answers it with a
//     "not configured" error, so calling one is an ordinary extra call. On the
//     decoy-correct cases the coined decoy IS the right tool and serves the
//     result-usage needle, so a blacklist of "unknown" names forfeits weight.
//
// set_main_model is retired from the advertised surface (#1580): production
// offers no model catalog to search and the family graded baked slugs.

const (
	// MinDecoys / MaxDecoys bound the coined decoys spliced into one seed's
	// catalog.
	MinDecoys = 3
	MaxDecoys = 5
	// MinDescriptionParaphrases is the smallest description bank any v13
	// production tool may carry (TestV13DescriptionBanks).
	MinDescriptionParaphrases = 6
)

// ThemeEnum is the closed set of set_theme values on the v13 wire. It is the
// bench's theme value space (datagen themes) so every graded value is listed.
func ThemeEnum() []string { return []string{"system", "light", "dark", "midnight", "solarized"} }

// EffortEnum is the closed set of set_reasoning_effort values on the v13 wire;
// it equals the values the v9+ inference relay accepts.
func EffortEnum() []string { return []string{"low", "medium", "high"} }

// v13RetiredTools are v8..v12 catalog tools the v13 surface no longer offers.
var v13RetiredTools = map[string]bool{
	"set_main_model": true,
}

// v13ProductionCatalog is the seed-free v13 surface: canonical descriptions
// (bank entry 0), the v13 schemas, no decoys.
func v13ProductionCatalog() []protocol.ToolDefinition {
	base := catalogV8ToV12(protocol.BenchVersionV12)
	out := make([]protocol.ToolDefinition, 0, len(base))
	for _, tool := range base {
		if v13RetiredTools[tool.Name] {
			continue
		}
		bank, ok := v13DescriptionBanks[tool.Name]
		if !ok || len(bank) == 0 {
			// Every v13 production tool must carry a bank; a missing one is a
			// programming error caught by TestV13DescriptionBanks. Fall back to the
			// frozen description so the catalog still renders.
			bank = []string{tool.Description}
		}
		tool.Description = bank[0]
		if params, ok := v13Schemas[tool.Name]; ok {
			tool.Parameters = params()
		}
		out = append(out, tool)
	}
	return out
}

// v13SeededCatalog draws one paraphrase per production tool and splices the
// seed's decoys in at seeded positions.
func v13SeededCatalog(seed int64) []protocol.ToolDefinition {
	production := v13ProductionCatalog()
	for i := range production {
		bank := v13DescriptionBanks[production[i].Name]
		if len(bank) > 1 {
			production[i].Description = bank[v13Pick(seed, "desc:"+production[i].Name, len(bank))]
		}
	}
	decoys := DecoysForSeed(seed)
	out := make([]protocol.ToolDefinition, 0, len(production)+len(decoys))
	// Insertion slots: one seeded position per decoy over the production
	// surface, sorted so the splice is a single pass. Positions may collide;
	// colliding decoys are then adjacent, which is fine.
	slots := make([]int, len(decoys))
	for i := range decoys {
		slots[i] = v13Pick(seed, fmt.Sprintf("decoy-slot:%d", i), len(production)+1)
	}
	order := make([]int, len(decoys))
	for i := range order {
		order[i] = i
	}
	sort.SliceStable(order, func(a, b int) bool { return slots[order[a]] < slots[order[b]] })
	next := 0
	for pos := 0; pos <= len(production); pos++ {
		for next < len(order) && slots[order[next]] == pos {
			out = append(out, decoys[order[next]].Definition())
			next++
		}
		if pos < len(production) {
			out = append(out, production[pos])
		}
	}
	return out
}

// v13Pick is the seed-keyed selection primitive for this package: it hashes
// (seed, key) with FNV-1a and reduces modulo n, so a choice depends only on the
// seed and its label, never on call order.
func v13Pick(seed int64, key string, n int) int {
	if n <= 1 {
		return 0
	}
	return int(v13Hash(seed, key) % uint64(n))
}

func v13Hash(seed int64, key string) uint64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v13-catalog:%d:%s", seed, key)
	return h.Sum64()
}

// ── Schemas ──────────────────────────────────────────────────────────────────

// v13Schemas replaces the free-string settings schemas with the production
// pattern: closed enums where production closes the enum, runtime-described
// option lists where the options are configured per workspace.
var v13Schemas = map[string]func() json.RawMessage{
	"set_theme": func() json.RawMessage {
		return schema([]string{"theme"},
			prop{name: "theme", typ: "string", desc: "Color mode to apply.", enum: ThemeEnum()},
		)
	},
	"set_reasoning_effort": func() json.RawMessage {
		return schema([]string{"effort"},
			prop{name: "effort", typ: "string", desc: "How much reasoning to spend per response.", enum: EffortEnum()},
		)
	},
	"set_accent_color": func() json.RawMessage {
		return schema([]string{"color"},
			prop{name: "color", typ: "string", desc: "Exactly one of the accent color options discover_capabilities lists for this workspace. The option list is configured at runtime and is not enumerated here; pass the listed spelling."},
		)
	},
	"set_chat_font": func() json.RawMessage {
		return schema([]string{"font"},
			prop{name: "font", typ: "string", desc: "Exactly one of the chat font options discover_capabilities lists for this workspace. The option list is configured at runtime and is not enumerated here; pass the listed spelling."},
		)
	},
	"discover_capabilities": func() json.RawMessage {
		return schema(nil,
			prop{name: "query", typ: "string", desc: "What the user is trying to do (leave empty for an overview). Ask about appearance to get this workspace's configured accent colors, chat fonts, and color modes."},
		)
	},
}

// ── Description banks ────────────────────────────────────────────────────────

// v13DescriptionBanks holds every production tool's paraphrase bank. Entry 0 is
// the canonical (production) description; later entries preserve the routing
// guidance (which sibling tool to prefer, what the tool must not be used for)
// in different words. A paraphrase must never change the tool's meaning.
var v13DescriptionBanks = map[string][]string{
	"create_image": {
		"Generate a new image from a text prompt.",
		"Create a brand-new picture from a written description.",
		"Render an original image described in words.",
		"Produce a fresh image from scratch based on a prompt.",
		"Make a new illustration or photo-like image from text.",
		"Turn a text description into a newly generated image.",
	},
	"edit_image": {
		"Edit an existing image given an instruction.",
		"Modify an image that already exists according to an instruction.",
		"Apply a requested change to an existing picture.",
		"Adjust, retouch, or alter an already-generated image.",
		"Change an existing image as instructed instead of creating a new one.",
		"Rework a given image following the user's edit request.",
	},
	"read_links": {
		"Read one or more URLs and return markdown text content.",
		"Open the given web pages and return their text as markdown.",
		"Fetch the content behind one or more links as readable markdown.",
		"Load URLs and return what the pages actually say, in markdown.",
		"Retrieve the text of one or more web pages from their URLs.",
		"Follow links and return the page content as markdown text.",
	},
	"search_web": {
		"Search live sources for one or more queries.",
		"Run one or more live web searches and return the top results.",
		"Query the public web for current information on one or more topics.",
		"Look up live, public information for the given search queries.",
		"Search current online sources for each query and return snippets.",
		"Find up-to-date public information on the web for one or more queries.",
	},
	"search_memories": {
		"Search past conversations and return compact memory summaries. Use fetch_memories for selected IDs that need full text. For named entities/topics, prefer search_subjects -> search_memories_in_subjects.",
		"Look through the user's past conversations and return short memory summaries. Call fetch_memories on selected IDs when the full text is needed; for named entities or topics prefer search_subjects then search_memories_in_subjects.",
		"Find relevant remembered conversations and return them as compact summaries. Selected IDs can be expanded with fetch_memories; for a specific person or topic, search_subjects followed by search_memories_in_subjects works better.",
		"Retrieve summaries of earlier conversations matching the queries. Use fetch_memories for full text on chosen IDs and prefer search_subjects -> search_memories_in_subjects for named entities and topics.",
		"Search the user's conversation history and return brief memory summaries; expand chosen IDs with fetch_memories. For named entities or topics, go through search_subjects and search_memories_in_subjects instead.",
		"Query long-term memory for past exchanges and get compact summaries back. Full text comes from fetch_memories on selected IDs; entity- or topic-scoped questions should prefer search_subjects -> search_memories_in_subjects.",
	},
	"search_subjects": {
		"Search the user's subject graph and return subject objects with id, name, description, and similarity.",
		"Find subjects (people, topics, projects) in the user's memory graph, returning id, name, description, and similarity.",
		"Look up matching subjects in the user's knowledge graph; each result carries an id, name, description, and similarity score.",
		"Search the graph of remembered subjects and return the matching subject objects with ids, names, descriptions, and similarity.",
		"Locate subjects the user has notes on, returning subject objects (id, name, description, similarity).",
		"Query the user's subject index and return matching subjects with their id, name, description, and similarity.",
	},
	"fetch_memories": {
		"Fetch full conversation text for selected memory pair IDs.",
		"Return the complete conversation text behind the given memory pair IDs.",
		"Load the full text of specific remembered exchanges by their pair IDs.",
		"Expand selected memory pair IDs into their complete conversation text.",
		"Retrieve the entire stored exchange for each requested memory pair ID.",
		"Get the full conversation content for chosen memory pair IDs.",
	},
	"search_memories_in_subjects": {
		"Semantic memory search inside one subject. Use focused queries; fetch selected IDs for full text.",
		"Search memories scoped to a single subject. Keep queries focused and fetch selected IDs for the full text.",
		"Run a semantic search over the memories linked to one subject; use narrow queries and fetch chosen IDs afterwards.",
		"Find memories within one subject by meaning. Use focused queries, then fetch the selected IDs for complete text.",
		"Query the memories attached to a specific subject semantically; focused queries work best and selected IDs can be fetched in full.",
		"Search inside one subject's linked memories. Use targeted queries and fetch the IDs you select for full text.",
	},
	"artifacts": {
		"Create an interactive, previewable artifact (web app, doc, game).",
		"Build a previewable interactive artifact such as a web app, document, or game.",
		"Produce an interactive artifact the user can preview (app, page, doc, game).",
		"Create something the user can open and interact with: a web app, a document, a game.",
		"Generate a live, previewable artifact like a small web app, document, or game.",
		"Make an interactive, previewable deliverable (web app, doc, game) from a spec.",
	},
	"execute_agent_job": {
		"Dispatch a one-off background agent job to Ditto Code — a sandboxed coding harness that can write and run code, install packages, run shell commands, and edit files in a workspace. Use for real coding/automation work that needs the file system, the network, or a repo. For a pure in-context calculation with no side effects, use run_code instead.",
		"Send a one-off background job to Ditto Code, a sandboxed coding harness that writes and runs code, installs packages, runs shell commands, and edits workspace files. Use it for real coding or automation that needs a file system, the network, or a repository; for a side-effect-free in-context calculation use run_code.",
		"Kick off a background Ditto Code job: a sandboxed coding agent with a file system, shell, package installs, and file editing. Choose it for genuine coding/automation work touching files, the network, or a repo. A pure calculation with no side effects belongs in run_code instead.",
		"Start a one-off agent job in Ditto Code (sandboxed: can write and execute code, install packages, run shell commands, edit files). Right for real coding and automation needing fs/network/repo access; wrong for a simple in-context computation, which run_code handles.",
		"Dispatch real coding or automation work to Ditto Code, a sandboxed harness that can edit files, run shell commands, install packages, and execute code in a workspace. Use run_code instead when the request is only an in-context calculation without side effects.",
		"Launch a background job on Ditto Code — the sandboxed coding harness with file, shell, network, and package access — for work that needs a workspace or repo. For a one-off calculation with no side effects, prefer run_code.",
	},
	"run_code": {
		"Execute JavaScript in a fast in-process sandbox to compute a result or chain tool calls (Code Mode). Use for a one-off calculation, data transformation, or orchestrating several tools over their results — it has NO file system, network, or package installs. For real coding work (writing a repo, running shell, editing files) use execute_agent_job instead.",
		"Run JavaScript in a quick in-process sandbox to compute something or chain tool calls (Code Mode). Good for one-off calculations, data transformations, or orchestrating tools over their results; it has no file system, network, or package installs. Real coding work (repos, shell, file edits) goes to execute_agent_job.",
		"Evaluate JavaScript in an in-process sandbox for a calculation, a data transformation, or a multi-tool orchestration (Code Mode). There is no file system, no network, and no package install here; for actual coding work such as editing files or running shell, use execute_agent_job.",
		"Compute a result or orchestrate several tool calls by executing JavaScript in a fast sandbox (Code Mode). It cannot touch files, the network, or install packages. When the task is real coding — writing a repo, running shell commands, editing files — dispatch execute_agent_job instead.",
		"Execute a snippet of JavaScript in-process to crunch numbers, transform data, or chain tool results (Code Mode). No file system, network, or packages are available; genuine coding tasks (repo, shell, file edits) belong to execute_agent_job.",
		"Use Code Mode: run JavaScript in a lightweight sandbox for a one-off computation or to combine tool outputs. It has no fs, network, or package access — for real coding work use execute_agent_job.",
	},
	"search_tools": {
		"Search the available tools by keyword and return the matching tool signatures. Use inside Code Mode to discover which tool binding to call before writing run_code.",
		"Look up available tool bindings by keyword and return their signatures. Use it in Code Mode to find the right binding before writing run_code.",
		"Find tools by keyword and get back the matching signatures; the Code Mode way to discover which binding to call before run_code.",
		"Query the tool registry by keyword and return matching tool signatures, so a run_code snippet can call the right binding.",
		"Discover which tool binding fits a need by searching the registry with keywords; returns the matching signatures for Code Mode.",
		"Search the tool registry for a capability and return the matching bindings' signatures before writing run_code.",
	},
	"list_agent_jobs": {
		"List the user's recent agent jobs.",
		"Show the user's recent background agent jobs and their status.",
		"Return the recent Ditto Code jobs the user has dispatched.",
		"Enumerate the user's latest agent jobs (pending, running, finished).",
		"Report on the background jobs the user recently kicked off.",
		"Fetch the user's recent agent job records.",
	},
	"file_feedback_for_team": {
		"Send feedback or a bug report to the Ditto team.",
		"File a bug report or product feedback with the Ditto team.",
		"Forward the user's feedback or a bug report to the people who build Ditto.",
		"Submit feedback about Ditto, or a bug, to the Ditto team.",
		"Report a problem or suggestion about the app to the Ditto team.",
		"Pass a bug report or feedback message along to the Ditto team.",
	},
	"set_theme": {
		"Change the app's color mode. The theme value must be one of the listed enum options.",
		"Switch the app's color theme to one of the enumerated modes.",
		"Set Ditto's color mode; the accepted values are the enum listed in the schema.",
		"Apply one of the listed color modes to the app.",
		"Change how the app looks by choosing one of the enumerated theme values.",
		"Pick the app's color theme from the closed list of modes in this schema.",
	},
	"set_reasoning_effort": {
		"Set the reasoning effort level for responses. The effort value must be one of the listed enum options.",
		"Choose how much reasoning each response spends, from the enumerated effort levels.",
		"Adjust the assistant's thinking depth using one of the listed effort values.",
		"Set the per-response reasoning budget to one of the enumerated levels.",
		"Change the reasoning effort applied to answers; accepted values are the enum in this schema.",
		"Select a reasoning effort level from the closed list for future responses.",
	},
	"set_chat_tool_preferences": {
		"Enable or disable specific tools for chat.",
		"Turn individual chat tools on or off.",
		"Update which tools the assistant may use in chat.",
		"Toggle specific tool capabilities for chat on or off.",
		"Adjust the user's per-tool chat preferences (enable/disable).",
		"Change which chat tools are allowed or blocked.",
	},
	"discover_capabilities": {
		"Look up what Ditto can do and how to use a feature. Call whenever the user asks what you or Ditto can do, where a setting lives, or how to accomplish something in the app. Appearance queries return this workspace's configured accent colors, chat fonts, and color modes.",
		"Find out what Ditto can do and how a feature works. Use it when the user asks about capabilities, where a setting lives, or how to get something done in the app; asking about appearance returns the configured accent colors, chat fonts, and color modes for this workspace.",
		"Look up Ditto's own features and settings. Call it for 'what can you do', 'where is the setting for X', or 'how do I do Y here'; an appearance query lists the accent colors, chat fonts, and color modes configured for this workspace.",
		"Check what the app is capable of and how to use it. Right for questions about Ditto itself rather than the user's data or the web; appearance queries return this workspace's configured accent colors, chat fonts, and color modes.",
		"Answer questions about Ditto's capabilities and where things live in the app. When asked about appearance it returns the accent colors, chat fonts, and color modes configured for this workspace.",
		"Consult Ditto's feature guide: what it can do, how to accomplish a task in the app, and which appearance options (accent colors, chat fonts, color modes) this workspace has configured.",
	},
	"save_memory": {
		"Save a new fact the user states about themselves to long-term memory. Use when the user TELLS you something to remember, not when they ask a question.",
		"Store a new fact the user shares about themselves in long-term memory. Use it when the user tells you something to remember, never when they are asking a question.",
		"Remember a new personal fact the user has just stated. For statements to keep, not for questions.",
		"Write a fact the user tells you about themselves into long-term memory; questions are not saves.",
		"Persist a newly stated user fact to memory. Trigger on the user telling you something, not on a lookup request.",
		"Record something the user wants remembered about themselves. Use for declarations, not for questions.",
	},
	"update_memory": {
		"Update an existing remembered fact to a new value.",
		"Change the value of a fact that is already in memory.",
		"Revise an existing memory entry with new content.",
		"Overwrite a stored fact with its corrected or updated value.",
		"Edit a remembered fact in place rather than saving a new one.",
		"Modify an existing memory pair to hold a new value.",
	},
	"delete_memory": {
		"Delete a remembered fact at the user's request.",
		"Remove a stored memory entry when the user asks for it to be forgotten.",
		"Erase a specific remembered fact from long-term memory.",
		"Forget a memory pair the user no longer wants kept.",
		"Permanently remove one remembered fact at the user's request.",
		"Drop a stored memory entry from the user's history.",
	},
	"calendar_create_event": {
		"Create an event on the user's Google Calendar.",
		"Add a new event to the user's Google Calendar.",
		"Schedule an event on the user's Google Calendar.",
		"Put a new entry on the user's Google Calendar.",
		"Create a Google Calendar event for the user.",
		"Book an event into the user's Google Calendar.",
	},
	"calendar_search_events": {
		"Search the user's Google Calendar for events.",
		"Find events on the user's Google Calendar.",
		"Look up matching entries in the user's Google Calendar.",
		"Query the user's Google Calendar for events that match.",
		"Search for existing events in the user's Google Calendar.",
		"Retrieve Google Calendar events matching a query.",
	},
	"gmail_send": {
		"Send an email from the user's Gmail account.",
		"Send a message from the user's Gmail account.",
		"Compose and send an email via the user's Gmail.",
		"Email someone from the user's Gmail account.",
		"Deliver an email through the user's Gmail account.",
		"Send mail on the user's behalf from Gmail.",
	},
	"set_accent_color": {
		"Change the app's accent color. Pass exactly one of the accent options that discover_capabilities lists for this workspace; the options are configured at runtime and are not enumerated in the schema.",
		"Set the app accent color to one of the options this workspace has configured. discover_capabilities lists them at runtime; the schema does not enumerate them.",
		"Apply a new accent color to Ditto. The accepted colors are configured per workspace and returned by discover_capabilities, so pass one of those listed spellings.",
		"Switch the app's accent to one of the runtime-configured options. Use discover_capabilities to see the current list; it is not fixed in the schema.",
		"Change the accent color using one of the workspace's configured options, which discover_capabilities enumerates at runtime.",
		"Update the app accent. Valid colors are whatever this workspace has configured — discover_capabilities lists them; the schema leaves them open.",
	},
	"set_chat_font": {
		"Change the font used in the chat view. Pass exactly one of the font options that discover_capabilities lists for this workspace; the options are configured at runtime and are not enumerated in the schema.",
		"Set the chat font to one of the options this workspace has configured. discover_capabilities lists them at runtime; the schema does not enumerate them.",
		"Apply a new chat typeface. The accepted fonts are configured per workspace and returned by discover_capabilities, so pass one of those listed spellings.",
		"Switch the chat view's font to one of the runtime-configured options. Use discover_capabilities to see the current list; it is not fixed in the schema.",
		"Change the chat font using one of the workspace's configured options, which discover_capabilities enumerates at runtime.",
		"Update the chat typeface. Valid fonts are whatever this workspace has configured — discover_capabilities lists them; the schema leaves them open.",
	},
	"create_workflow": {
		"Propose a reusable workflow made of one or more inspectable steps. A schedule, when requested, is part of this same proposal.",
		"Create a reusable, multi-step workflow the user can inspect and run again; include the schedule in the same proposal when one is requested.",
		"Define a new reusable workflow from one or more steps. If the user wants it on a schedule, the schedule is part of this proposal.",
		"Set up a saved workflow of inspectable steps that can run later or on a schedule; the schedule, if any, is proposed here too.",
		"Propose a new saved workflow (ordered steps, optional schedule) the user can review and reuse.",
		"Build a reusable workflow of one or more steps, including any requested schedule, as a single proposal.",
	},
	"list_workflows": {
		"List saved workflows with their schedules.",
		"Show the user's saved workflows and any schedules attached to them.",
		"Return the workflows the user has saved, including their schedules.",
		"Enumerate existing saved workflows along with their schedules.",
		"Look up which workflows are saved and how they are scheduled.",
		"Report the user's saved workflows and their schedules.",
	},
	"list_schedules": {
		"List schedules attached to the user's workflows.",
		"Show the schedules that drive the user's workflows.",
		"Return every schedule attached to a saved workflow.",
		"Enumerate the timers and cadences configured on the user's workflows.",
		"Look up when the user's workflows are set to run.",
		"Report the schedules configured across the user's workflows.",
	},
	"run_workflow": {
		"Propose running one saved workflow now.",
		"Trigger an existing saved workflow immediately.",
		"Propose an immediate run of one of the user's saved workflows.",
		"Start a saved workflow now instead of waiting for its schedule.",
		"Execute a previously saved workflow on demand.",
		"Kick off one saved workflow right away.",
	},
}

// ── Coined decoys ────────────────────────────────────────────────────────────

// DecoyShape is one near-miss capability family. A seed coins a brand for the
// shape and derives the decoy's name, description, and (on decoy-correct cases)
// its served result from these templates. %[1]s is the coined Brand.
type DecoyShape struct {
	// Key names the shape in category names ("decoy_docs_search_result_usage").
	Key string
	// Suffix is the snake_case tail of the coined tool name.
	Suffix string
	// Does states the capability; Not states what the decoy is NOT (the sibling
	// production tool a keyword router would confuse it with).
	Does string
	Not  string
	// Param is the single string argument the decoy takes.
	Param     string
	ParamDesc string
	// Prompts are the decoy-correct case surfaces: %[1]s is the coined Brand and
	// %[2]s the needle subject the served result answers about.
	Prompts []string
	// Result renders the decoy's served content on a decoy-correct case: %[1]s
	// is the Brand and %[2]s the needle sentence.
	Result string
}

// decoyShapes is the frozen pool of near-miss capability families. Enlarging
// it changes v13 bytes, so a later change needs a new bench version.
var decoyShapes = []DecoyShape{
	{Key: "docs_search", Suffix: "docs_search", Does: "Search the %[1]s product documentation portal for how-to articles and reference pages.", Not: "This is not a live web search and it does not read the user's own memories.", Param: "query", ParamDesc: "documentation search terms",
		Prompts: []string{"Look up %[2]s in the %[1]s documentation portal and tell me the exact figure the article lists.", "Check the %[1]s docs for %[2]s — I need the precise number their reference page gives.", "Search %[1]s's documentation for %[2]s and report the exact value it states."},
		Result:  "%[1]s docs — top article: %[2]s."},
	{Key: "chat_message", Suffix: "chat_post", Does: "Post a message to a %[1]s team chat channel.", Not: "This is not email: it never sends to a Gmail address and it does not file feedback with the Ditto team.", Param: "message", ParamDesc: "the chat message to post",
		Prompts: []string{"Drop a note in our %[1]s team channel asking about %[2]s and tell me the exact figure the channel bot replies with.", "Post to %[1]s chat about %[2]s and give me the precise number the room's bot returns.", "Ping the %[1]s channel about %[2]s; report the exact value it answers with."},
		Result:  "Posted to %[1]s channel. Channel bot replied: %[2]s."},
	{Key: "sidebar_color", Suffix: "sidebar_palette", Does: "Change the %[1]s sidebar panel palette inside the %[1]s add-on.", Not: "This is not the Ditto app accent color or theme; it only recolors the add-on's own sidebar.", Param: "palette", ParamDesc: "sidebar palette name",
		Prompts: []string{"Recolor my %[1]s add-on sidebar to match %[2]s and tell me the exact palette code it reports.", "Set the %[1]s sidebar palette for %[2]s; give me the precise code it confirms.", "Adjust the %[1]s panel colors around %[2]s and report the exact palette value."},
		Result:  "%[1]s sidebar palette updated. Status line: %[2]s."},
	{Key: "reminders_list", Suffix: "reminders", Does: "List the reminders kept in the %[1]s reminders add-on.", Not: "These are not saved workflows or workflow schedules and not the user's calendar.", Param: "filter", ParamDesc: "optional reminder filter",
		Prompts: []string{"Check my %[1]s reminders for the one about %[2]s and tell me the exact figure it notes.", "Pull up %[1]s reminders mentioning %[2]s — what precise number does the reminder carry?", "Look through my %[1]s reminder list for %[2]s and report the exact value written there."},
		Result:  "%[1]s reminders (1 match): %[2]s."},
	{Key: "note_create", Suffix: "notebook_add", Does: "Create a page in the %[1]s notebook add-on.", Not: "This is not long-term memory: it does not save, update, or delete a remembered fact.", Param: "content", ParamDesc: "note text",
		Prompts: []string{"Start a %[1]s notebook page about %[2]s and tell me the exact figure the notebook's summary line shows.", "Create a %[1]s note on %[2]s; give me the precise number in its auto-summary.", "Add %[2]s to my %[1]s notebook and report the exact value the page header states."},
		Result:  "%[1]s notebook page created. Auto-summary: %[2]s."},
	{Key: "ticket_lookup", Suffix: "helpdesk_ticket", Does: "Look up a support ticket in the %[1]s helpdesk.", Not: "This is not feedback for the Ditto team and it does not open a new ticket.", Param: "ticket", ParamDesc: "ticket reference or search terms",
		Prompts: []string{"Find the %[1]s helpdesk ticket about %[2]s and tell me the exact figure in its latest update.", "Check %[1]s support for the ticket on %[2]s — what precise number does the last reply give?", "Look up %[2]s in %[1]s helpdesk and report the exact value the ticket records."},
		Result:  "%[1]s helpdesk ticket found. Latest update: %[2]s."},
	{Key: "price_quote", Suffix: "pricing_quote", Does: "Fetch a price quote from the %[1]s pricing service.", Not: "This is not a web search and it does not read calendar or memory data.", Param: "item", ParamDesc: "what to price",
		Prompts: []string{"Get a %[1]s pricing quote for %[2]s and tell me the exact figure quoted.", "Ask %[1]s pricing about %[2]s; give me the precise number it returns.", "Pull the %[1]s quote for %[2]s and report the exact value."},
		Result:  "%[1]s pricing quote: %[2]s."},
	{Key: "translate_text", Suffix: "translate", Does: "Translate text with the %[1]s translation service.", Not: "This is not a model, theme, or reasoning setting and it does not search anything.", Param: "text", ParamDesc: "text to translate",
		Prompts: []string{"Run the note on %[2]s through %[1]s translate and tell me the exact figure the translation memory attaches.", "Translate the phrase about %[2]s with %[1]s; report the precise number its glossary note shows.", "Use %[1]s translation on %[2]s and give me the exact value in the returned footnote."},
		Result:  "%[1]s translation complete. Glossary note: %[2]s."},
	{Key: "weather_lookup", Suffix: "weather", Does: "Get current conditions from the %[1]s weather feed.", Not: "This is not a general web search and it does not read the user's memories or calendar.", Param: "location", ParamDesc: "place to look up",
		Prompts: []string{"Check the %[1]s weather feed for %[2]s and tell me the exact reading it reports.", "What does %[1]s weather say for %[2]s? I need the precise figure.", "Pull %[1]s conditions for %[2]s and report the exact value."},
		Result:  "%[1]s weather feed: %[2]s."},
	{Key: "contact_lookup", Suffix: "address_book", Does: "Look up a contact card in the %[1]s address book.", Not: "This is not the user's memory graph or subject search and it does not send email.", Param: "name", ParamDesc: "contact name",
		Prompts: []string{"Find the %[1]s address-book card for %[2]s and tell me the exact figure in its notes field.", "Look up %[2]s in %[1]s contacts; give me the precise number on the card.", "Open my %[1]s address book entry for %[2]s and report the exact value it holds."},
		Result:  "%[1]s contact card found. Notes: %[2]s."},
	{Key: "file_convert", Suffix: "file_converter", Does: "Convert a document between file formats with the %[1]s converter.", Not: "This is not an artifact builder and it does not run code or agent jobs.", Param: "file", ParamDesc: "file to convert",
		Prompts: []string{"Convert my export on %[2]s with %[1]s and tell me the exact figure its conversion report shows.", "Run the %[2]s export through the %[1]s converter; give me the precise number in the result summary.", "Use %[1]s to convert the file on %[2]s and report the exact value the log states."},
		Result:  "%[1]s conversion finished. Report: %[2]s."},
	{Key: "stock_quote", Suffix: "markets_quote", Does: "Quote a ticker from the %[1]s markets feed.", Not: "This is not a web search and it does not read the user's memories.", Param: "ticker", ParamDesc: "symbol to quote",
		Prompts: []string{"Quote %[2]s on the %[1]s markets feed and tell me the exact figure.", "Check %[1]s markets for %[2]s — what precise number does it show?", "Pull the %[1]s quote for %[2]s and report the exact value."},
		Result:  "%[1]s markets feed: %[2]s."},
	{Key: "timer_start", Suffix: "focus_timer", Does: "Start a countdown timer in the %[1]s focus add-on.", Not: "This is not a calendar event and not a workflow schedule.", Param: "duration", ParamDesc: "timer length",
		Prompts: []string{"Start a %[1]s focus timer for my work on %[2]s and tell me the exact session id number it returns.", "Kick off a %[1]s timer labeled %[2]s; give me the precise number it confirms.", "Begin a %[1]s focus session for %[2]s and report the exact value in the confirmation."},
		Result:  "%[1]s focus timer started. Confirmation: %[2]s."},
	{Key: "map_route", Suffix: "route_planner", Does: "Compute a travel route with the %[1]s maps service.", Not: "This is not a web search and it does not create calendar events.", Param: "destination", ParamDesc: "where to route to",
		Prompts: []string{"Plan a %[1]s route to %[2]s and tell me the exact figure it reports for the trip.", "Ask %[1]s maps for directions to %[2]s; give me the precise number in the summary.", "Route me to %[2]s with %[1]s and report the exact value it returns."},
		Result:  "%[1]s route computed. Summary: %[2]s."},
	{Key: "spreadsheet_read", Suffix: "sheet_rows", Does: "Read rows from a %[1]s spreadsheet.", Not: "This is not memory search and it does not read web pages.", Param: "sheet", ParamDesc: "sheet or range to read",
		Prompts: []string{"Read the %[1]s sheet row for %[2]s and tell me the exact figure in it.", "Pull %[2]s from my %[1]s spreadsheet; give me the precise number.", "Open the %[1]s sheet, find %[2]s, and report the exact value."},
		Result:  "%[1]s sheet rows (1 match): %[2]s."},
	{Key: "issue_create", Suffix: "tracker_issue", Does: "Open an issue in the %[1]s issue tracker.", Not: "This is not a Ditto Code agent job and not feedback for the Ditto team.", Param: "title", ParamDesc: "issue title",
		Prompts: []string{"Open a %[1]s tracker issue about %[2]s and tell me the exact issue number it assigns.", "File %[2]s in %[1]s tracker; give me the precise number of the new issue.", "Create a %[1]s issue for %[2]s and report the exact id it returns."},
		Result:  "%[1]s issue opened. Tracker reply: %[2]s."},
	{Key: "video_summarize", Suffix: "video_digest", Does: "Summarize a video with the %[1]s video digest service.", Not: "This is not read_links: it does not open ordinary web pages or articles.", Param: "video", ParamDesc: "video reference",
		Prompts: []string{"Digest the %[1]s video on %[2]s and tell me the exact figure it highlights.", "Run the talk about %[2]s through %[1]s video digest; give me the precise number in the summary.", "Summarize the %[1]s recording about %[2]s and report the exact value it calls out."},
		Result:  "%[1]s video digest: %[2]s."},
	{Key: "inbox_triage", Suffix: "inbox_digest", Does: "Produce a digest of the %[1]s inbox.", Not: "This is not gmail_send: it never sends mail, and it does not search the web.", Param: "folder", ParamDesc: "inbox folder",
		Prompts: []string{"Triage my %[1]s inbox for anything on %[2]s and tell me the exact figure the digest surfaces.", "Digest %[1]s mail about %[2]s; give me the precise number it pulls out.", "Run the %[1]s inbox digest for %[2]s and report the exact value."},
		Result:  "%[1]s inbox digest: %[2]s."},
	{Key: "crm_lookup", Suffix: "crm_account", Does: "Look up an account record in the %[1]s CRM.", Not: "This is not the user's subject graph and it does not send email.", Param: "account", ParamDesc: "account name",
		Prompts: []string{"Look up %[2]s in the %[1]s CRM and tell me the exact figure on the account record.", "Check %[1]s CRM for %[2]s — what precise number is on file?", "Open the %[1]s account for %[2]s and report the exact value it shows."},
		Result:  "%[1]s CRM record: %[2]s."},
	{Key: "thesaurus_lookup", Suffix: "thesaurus", Does: "Look up synonyms and usage notes in the %[1]s thesaurus.", Not: "This is not a web search and not a memory lookup.", Param: "word", ParamDesc: "word to look up",
		Prompts: []string{"Look up %[2]s in the %[1]s thesaurus and tell me the exact figure its usage note cites.", "Check %[1]s thesaurus for %[2]s; give me the precise number in the entry.", "Open the %[1]s entry for %[2]s and report the exact value in its note."},
		Result:  "%[1]s thesaurus entry. Usage note: %[2]s."},
	{Key: "unit_convert", Suffix: "unit_converter", Does: "Convert measurements with the %[1]s unit converter.", Not: "This is not run_code: it does not execute JavaScript or general calculations.", Param: "expression", ParamDesc: "quantity and target unit",
		Prompts: []string{"Convert the reading for %[2]s with %[1]s units and tell me the exact figure it returns.", "Run the figure for %[2]s through the %[1]s converter; give me the precise converted number.", "Use %[1]s to convert %[2]s and report the exact value."},
		Result:  "%[1]s unit conversion: %[2]s."},
	{Key: "poll_create", Suffix: "poll", Does: "Create a poll in the %[1]s team space.", Not: "This is not a workflow, not a calendar event, and not an email.", Param: "question", ParamDesc: "poll question",
		Prompts: []string{"Open a %[1]s poll about %[2]s and tell me the exact poll number it creates.", "Start a %[1]s poll on %[2]s; give me the precise id it returns.", "Create a %[1]s poll for %[2]s and report the exact number it confirms."},
		Result:  "%[1]s poll created. Confirmation: %[2]s."},
	{Key: "wiki_search", Suffix: "wiki", Does: "Search the %[1]s internal wiki.", Not: "This is not the public web and not the user's personal memories.", Param: "query", ParamDesc: "wiki search terms",
		Prompts: []string{"Search the %[1]s wiki for %[2]s and tell me the exact figure the page lists.", "Check our %[1]s wiki on %[2]s — what precise number does it give?", "Look up %[2]s in %[1]s wiki and report the exact value stated there."},
		Result:  "%[1]s wiki — top page: %[2]s."},
}

// DecoyShapeCount is the size of the frozen shape pool (>= 20 by contract).
func DecoyShapeCount() int { return len(decoyShapes) }

// DecoyShapeKeys returns every shape key in pool order. Category names are
// "decoy_<key>_result_usage"; the public glossary mirror is checked against it.
func DecoyShapeKeys() []string {
	keys := make([]string, len(decoyShapes))
	for i, shape := range decoyShapes {
		keys[i] = shape.Key
	}
	return keys
}

// Decoy is one seed's coined near-miss tool.
type Decoy struct {
	Name        string
	Brand       string
	Description string
	Shape       DecoyShape
}

// Definition renders the decoy as a catalog entry.
func (d Decoy) Definition() protocol.ToolDefinition {
	return protocol.ToolDefinition{
		Name:        d.Name,
		Description: d.Description,
		Parameters: schema([]string{d.Shape.Param},
			prop{name: d.Shape.Param, typ: "string", desc: d.Shape.ParamDesc},
		),
	}
}

// Prompt renders a decoy-correct case surface: variant selects the template,
// subject is the needle subject the served result answers about.
func (d Decoy) Prompt(variant int, subject string) string {
	tmpl := d.Shape.Prompts[((variant%len(d.Shape.Prompts))+len(d.Shape.Prompts))%len(d.Shape.Prompts)]
	return fmt.Sprintf(tmpl, d.Brand, subject)
}

// ServedResult renders the decoy's content on a decoy-correct case.
func (d Decoy) ServedResult(needleSentence string) string {
	return fmt.Sprintf(d.Shape.Result, d.Brand, needleSentence)
}

// DecoysForSeed coins the seed's decoy tools: a seeded count in
// [MinDecoys, MaxDecoys], shapes drawn without replacement from the frozen pool,
// each with a coined brand that names the tool ("<brand>_<suffix>") and appears
// in its description. Pure in seed. Decoy names never collide with a production
// tool: every suffix is disjoint from the production name set and the brand is
// a coined stem.
func DecoysForSeed(seed int64) []Decoy {
	count := MinDecoys + v13Pick(seed, "decoy-count", MaxDecoys-MinDecoys+1)
	perm := v13Perm(seed, "decoy-shapes", len(decoyShapes))
	out := make([]Decoy, 0, count)
	used := map[string]bool{}
	for _, idx := range perm {
		if len(out) == count {
			break
		}
		shape := decoyShapes[idx]
		stem := CoinedStem(seed, "decoy-brand:"+shape.Key)
		for attempt := 1; used[stem]; attempt++ {
			stem = CoinedStem(seed, fmt.Sprintf("decoy-brand:%s:%d", shape.Key, attempt))
		}
		used[stem] = true
		brand := strings.ToUpper(stem[:1]) + stem[1:]
		out = append(out, Decoy{
			Name:        stem + "_" + shape.Suffix,
			Brand:       brand,
			Description: strings.ReplaceAll(shape.Does+" "+shape.Not, "%[1]s", brand),
			Shape:       shape,
		})
	}
	return out
}

// DecoyByName resolves a tool name to the seed's decoy, if it is one.
func DecoyByName(seed int64, name string) (Decoy, bool) {
	for _, d := range DecoysForSeed(seed) {
		if d.Name == name {
			return d, true
		}
	}
	return Decoy{}, false
}

// v13Perm is a seeded Fisher-Yates permutation of [0, n) keyed by (seed, key).
func v13Perm(seed int64, key string, n int) []int {
	perm := make([]int, n)
	for i := range perm {
		perm[i] = i
	}
	for i := n - 1; i > 0; i-- {
		j := int(v13Hash(seed, fmt.Sprintf("%s:%d", key, i)) % uint64(i+1))
		perm[i], perm[j] = perm[j], perm[i]
	}
	return perm
}

// CoinedStem coins a pronounceable lowercase stem (two or three
// consonant-vowel syllables, e.g. "veltra") for (seed, salt). It follows the
// persona.CoinShaped construction — splitmix64 steps over an FNV base so distinct
// salts give unrelated streams — but always renders the lowercase-letters shape
// a snake_case tool name needs. The stem alone is not guaranteed to avoid an
// English word (a CV·CV·CV draw can spell one); uniqueness against the
// production surface comes from the seeded stream plus the coined
// `<brand>_<shape-suffix>` composition, which TestV13Decoys pins disjoint from
// every production tool name.
func CoinedStem(seed int64, salt string) string {
	h := v13Hash(seed, "stem:"+salt)
	next := func(alpha string) byte {
		h += 0x9e3779b97f4a7c15
		z := h
		z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9
		z = (z ^ (z >> 27)) * 0x94d049bb133111eb
		z ^= z >> 31
		return alpha[z%uint64(len(alpha))]
	}
	const cons = "bdfgklmnprstvz"
	const vow = "aeiou"
	syllables := 2 + int(next("01")-'0')
	var b strings.Builder
	for i := 0; i < syllables; i++ {
		b.WriteByte(next(cons))
		b.WriteByte(next(vow))
	}
	if next("01") == '1' {
		b.WriteByte(next("lnrst"))
	}
	return b.String()
}

// ProductionToolNames returns the sorted names of the v13 production surface
// (no decoys). Mirrors (starter kit, screener oracle, OpenClaw plugin) are
// regenerated from it.
func ProductionToolNames(benchVersion int) []string {
	tools := CatalogForVersion(benchVersion)
	names := make([]string, 0, len(tools))
	for _, tool := range tools {
		names = append(names, tool.Name)
	}
	return names
}
