// Package datagen procedurally generates small, fresh, randomized DittoBench
// tool-calling datasets. Generation is deterministic per seed (so a given seed
// always yields the same dataset) but varies widely across seeds. The practice
// API rotates the seed on every request so no two evaluations are identical.
// This is the anti-overfit property of the off-chain practice loop.
package datagen

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/assistantvoice"
	"github.com/ditto-assistant/dittobench-datagen/internal/textnoise"
	"github.com/ditto-assistant/dittobench-datagen/internal/uservoice"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// category describes one kind of tool-calling case and how to render it.
type category struct {
	name string
	tool string // single expected tool; empty means "no tool" (unless tools is set)
	// tools, when non-empty, is a multi-hop expected sequence (overrides tool);
	// MaxToolCalls becomes len(tools) and order is scored.
	tools []string
	// unordered marks a tools set as INDEPENDENT parallel calls: name/arg set is
	// scored but relative call order is not (ToolCase.Unordered).
	unordered bool
	// allowExtra sets ToolCase.AllowExtraTools: an extra call to the expected tool
	// is not penalized. Used by error-recovery cases where a legitimate RETRY after
	// a transient tool error would otherwise read as an over-budget extra call.
	allowExtra bool
	// argKey, when set on a single-tool category whose filler is an exact token
	// (a URL, a theme), pins RequiredArgs[argKey]=filler so the argument value is
	// deterministically scored: right tool + wrong arg does not get full credit.
	argKey string
	// wrap applies the Tier-1a lead-in/trailer surface variation to the rendered
	// prompt (persona.Wrap with the prompt pools below). Set it on template-only
	// categories whose prompt pool would otherwise be a handful of memorizable
	// strings; leave it off for categories whose surface entropy comes from a
	// large filler pool, and for result-usage/intent prompts that must reach the
	// harness exactly as authored.
	wrap      bool
	templates []string
	// grammar, when set, replaces templates as the prompt source: the prompt is
	// persona.Expand(r, grammar, "root"), then the normal %s fill and wrap run on
	// the expansion. Used by the audited low-variety categories whose surface
	// must outgrow a memorizable template list.
	grammar persona.Grammar
	// intents are prompts that imply the arg VALUE (or name a near-miss) instead of
	// stating it verbatim, so a harness must parse intent rather than copy a token.
	// Used for a share of a set_* category's cases; requires argKey.
	intents []argIntent
}

// argIntent maps an intent-phrased prompt to the arg value it should resolve to.
// Prompt either implies Value ("think harder" → "high") or names a near-miss
// alongside it ("switch me off gpt-5 to gemini-3-pro" → "gemini-3-pro"), so a
// token-copying harness gets it wrong.
type argIntent struct {
	prompt string
	value  string
}

// v8ArgIntents keeps exact argument grading for values that the product itself
// resolves to a closed enum or identifier. Prompts are ordinary user requests;
// difficulty must not come from asking a model to assemble artificial token
// puzzles. Free-form fields are intentionally absent and are tool-scored only,
// because many paraphrases are equally valid.
var v8ArgIntents = map[string][]argIntent{
	"settings": {
		{"Please match Ditto's light or dark mode to my device.", "system"},
		{"Make the app dark mode.", "dark"},
	},
	"set_model": {
		{"Use GPT for my main chats; pick the current standard GPT option.", "gpt-5"},
		{"Switch my chats to Claude; use the available Sonnet option.", "claude-sonnet-5"},
	},
	"set_effort": {
		{"Reason as deeply and carefully as possible from now on.", "high"},
		{"Keep the reasoning balanced for everyday questions.", "medium"},
	},
	"set_accent": {
		{"Make my accent color teal-ish; check the option if I misspelled it.", "teal"},
		{"Switch the app accent to INDIGO, whatever capitalization the setting uses.", "indigo"},
	},
	"set_font": {
		{"Use jetbrians mono in chat; check the available font spelling.", "JetBrains Mono"},
		{"Change my chat font to GEORGIA, case-insensitively.", "Georgia"},
	},
}

// word pools used to vary entities/phrasings across seeds.
var (
	subjects = []string{
		"my dentist appointment", "the project deadline", "Sarah's birthday",
		"my car insurance", "the meeting notes", "my flight to Tokyo",
		"the grocery list", "my gym schedule", "the wifi password",
		"my passport number", "the book recommendation", "my doctor's name",
	}
	people = []string{
		"Alice", "Bob", "Carol", "David", "Erin", "Frank", "Grace", "Heidi",
	}
	topics = []string{
		"quantum computing", "the 2024 Olympics", "best espresso machines",
		"rust vs go", "climate policy", "the stock market today",
		"sourdough recipes", "electric vehicles", "the James Webb telescope",
	}
	urls = []string{
		"https://example.com/article", "https://news.site/story",
		"https://blog.dev/post", "https://docs.io/guide",
		"https://github.com/org/repo",
	}
	imagePrompts = []string{
		"a sunset over mountains", "a robot drinking coffee",
		"a futuristic city skyline", "a watercolor fox",
		"an astronaut on a beach", "a neon cyberpunk street",
	}
	artifactKinds = []string{
		"a landing page", "a todo app", "a snake game",
		"a markdown resume", "a pomodoro timer", "a budget tracker",
	}
	agentTasks = []string{
		"scrape the latest headlines", "summarize this PDF",
		"refactor the auth module", "generate unit tests",
		"build a CSV report", "deploy the staging branch",
	}
	themes = []string{"dark", "light", "system", "midnight", "solarized"}

	memoryIDs = []string{"mem-1042", "mem-2087", "mem-3310", "mem-7761", "mem-9024"}
	// workflowGoals are decomposable multi-part goals: the production
	// execute_agent_workflow takes a free-form goal a planner splits into
	// parallel sub-agents (there are no named, predefined workflows). The value
	// doubles as the pinned goal argument, so it must survive verbatim inside
	// the goal the harness passes.
	workflowGoals = []string{
		"a comparison of our top five competitors",
		"an audit of our three services for risks",
		"research on four laptop options with a recommendation",
		"a review of the codebase for security, performance, and correctness",
		"a market scan across several regions with a combined summary",
	}
	models    = []string{"gpt-5", "claude-sonnet-5", "gemini-3-pro", "llama-4-70b"}
	efforts   = []string{"low", "medium", "high"}
	toolPrefs = []string{"disable web search", "enable image tools", "turn off agent jobs", "allow only memory tools"}
	// Production-tool coverage pools: scheduling, discovery, workspace, memory
	// writes, appearance settings.
	schedules       = []string{"every morning at 8am", "every Monday", "each weekday evening", "the first of every month", "every Friday at noon"}
	automationTasks = []string{"email me a news digest", "summarize my unread messages", "post my standup notes", "back up my documents"}
	recipeNames     = []string{"weekly-review", "trip-planner", "invoice-run", "content-pipeline"}
	recipeSteps     = []string{"pull metrics, then draft a summary, then email it", "search flights, compare prices, book the cheapest", "gather receipts, total them, file the report"}
	savableFacts    = []string{"I'm allergic to shellfish", "my anniversary is June 3rd", "I prefer window seats", "my daughter's name is Mia", "I take my coffee black"}
	calendarTitles  = []string{"dentist appointment", "team offsite", "1:1 with Sam", "flight to Boston", "birthday dinner"}
	calendarWhens   = []string{"next Tuesday at 3pm", "Friday morning", "March 14th", "tomorrow at noon"}
	emailRecipients = []string{"sam@example.com", "the team", "my accountant", "jordan@example.com"}
	accentColors    = []string{"teal", "crimson", "indigo", "amber", "emerald"}
	chatFonts       = []string{"Inter", "JetBrains Mono", "Georgia", "system default"}
	feedback        = []string{
		"the image tool keeps timing out", "love the new memory search",
		"the export button is broken on mobile", "please add dark mode to artifacts",
	}

	chitchat = []string{
		"hey, how's it going?", "thanks, that was helpful!",
		"tell me a joke", "what's your favorite color?",
		"good morning!", "you're awesome", "lol nice",
		"haha that one made me laugh", "hope your day's going well",
		"just saying hi", "nice, that's exactly what I meant",
		"gm! ready when you are", "appreciate you", "ok cool cool cool",
	}
	abstentions = []string{
		"what's the meaning of life?", "should I quit my job?",
		"do you love me?", "what will the weather be like next year?",
		"who will win the next election?", "what am I thinking right now?",
		"will my startup succeed?", "is my sister secretly mad at me?",
		"what number am I thinking of?", "will it rain on my wedding day next June?",
		"how long am I going to live?", "did my neighbor take my package?",
	}
)

// categories enumerates every case type the generator can emit. Order is
// stable so seeding is reproducible.
var categories = []category{
	{
		name: "memory_lookup", tool: "search_memories",
		templates: []string{
			"What did I say about %s?",
			"Remind me about %s.",
			"Do you remember %s?",
			"Look up %s from my memories.",
		},
	},
	{
		name: "memory_subject", tool: "search_subjects",
		templates: []string{
			"What subjects do I have notes on related to %s?",
			"Find the topic that covers %s.",
			"Which of my subjects mention %s?",
		},
	},
	{
		name: "web_search", tool: "search_web",
		templates: []string{
			"Search the web for %s.",
			"What's the latest on %s?",
			"Find recent news about %s.",
			"Google %s for me.",
		},
	},
	{
		name: "link_read", tool: "read_links", argKey: "urls",
		templates: []string{
			"Read %s and summarize it.",
			"What does this page say: %s",
			"Open %s and tell me the main points.",
		},
	},
	{
		name: "image_create", tool: "create_image",
		templates: []string{
			"Generate an image of %s.",
			"Create a picture of %s.",
			"Draw me %s.",
		},
	},
	{
		name: "artifacts_create", tool: "artifacts",
		templates: []string{
			"Build me %s.",
			"Make %s I can preview.",
			"Create %s as an interactive artifact.",
		},
	},
	{
		name: "agent_job", tool: "execute_agent_job", argKey: "task",
		templates: []string{
			"Run a background job to %s.",
			"Kick off an agent to %s.",
			"Dispatch a task to %s.",
		},
	},
	{
		name: "settings", tool: "set_theme", argKey: "theme",
		templates: []string{
			"Switch to %s mode.",
			"Set my theme to %s.",
			"Change the app theme to %s.",
		},
		intents: []argIntent{
			{"I had it on light, but switch me to midnight instead.", "midnight"},
			{"Don't leave it on dark — set it to solarized.", "solarized"},
			{"Change it from system to dark.", "dark"},
			{"Not light — I want the solarized look.", "solarized"},
		},
	},
	{
		// Hard routing trap: phrased like a web search but the right action is a
		// memory lookup (the user is asking about THEIR past, not the public web).
		name: "route_memory_not_web", tool: "search_memories",
		templates: []string{
			"Search for what I told you about %s.",
			"Look up %s — I mentioned it before.",
			"Find that thing I saved about %s.",
		},
	},
	{
		// Hard routing trap: looks like a memory query but needs the live web
		// (current/real-time info the user can't have stored).
		name: "route_web_not_memory", tool: "search_web",
		templates: []string{
			"Remind me what the latest news on %s is.",
			"Do you recall the current price of %s?",
			"What's the up-to-date status of %s right now?",
		},
	},
	{
		// Run-vs-read trap: dispatch a NEW background job (not check existing).
		name: "agent_run_not_read", tool: "execute_agent_job",
		templates: []string{
			"Go ahead and %s for me now.",
			"Please actually %s, don't just tell me how.",
			"Start working on this: %s.",
		},
	},
	{
		// Run-vs-read trap: check status of EXISTING jobs (not start a new one).
		name: "agent_read_not_run", wrap: true, tool: "list_agent_jobs",
		grammar: agentReadGrammar,
	},
	{
		// Edit-vs-create trap: the user references an EXISTING image to modify, so
		// the right tool is edit_image, not create_image.
		name: "image_edit_not_create", wrap: true, tool: "edit_image",
		grammar: imageEditGrammar,
	},
	{
		// Job-vs-workflow trap: phrased like a one-off background job, but the goal
		// has clear independent parts, so the right tool is execute_agent_workflow
		// (production semantics: a planner decomposes a free-form goal into parallel
		// sub-agents; there are no named workflows).
		name: "workflow_not_job", tool: "execute_agent_workflow", argKey: "goal",
		templates: []string{
			"Kick off a background job to produce %s, working the independent parts in parallel.",
			"Dispatch an agent to put together %s, tackling each piece simultaneously.",
			"Start a one-off task for %s, splitting the separate angles across workers.",
		},
	},
	{
		name: "no_tool", tool: "", // chit-chat, answer directly
		templates: []string{
			"%s",
		},
	},
	{
		name: "abstention", tool: "", // should not call tools, answer/abstain
		templates: []string{
			"%s",
		},
	},
	{
		// Hallucinated-argument trap (BFCL): the request names an action that needs a
		// specific argument but never supplies its VALUE, and no prior context holds
		// it. The correct behavior is to ask for the missing detail, NOT to invoke the
		// tool with a fabricated argument; like abstention, calling any tool
		// scores 0 (a guessed value is the failure mode being probed).
		name: "arg_hallucination", wrap: true, tool: "", // required value missing: ask, don't invent
		templates: []string{
			"Change my theme.",
			"Switch my main model, please.",
			"Set my reasoning effort.",
			"Read that link and summarize it.",
			"File that as feedback for the team.",
			"Spin up a parallel agent workflow for me.",
			"Set my accent color.",
			"Change the chat font, please.",
			"Put it on my calendar.",
			"Email them the update.",
			"Delete that memory for me.",
			"Run my recipe.",
		},
	},
	// Full-catalog coverage: single-hop categories so every catalog tool is the
	// correct answer for some case.
	{
		// Deliberately avoids the tool's own keywords (fetch/full/text/memory/
		// pair): the old templates stated the call verbatim, so a no-model
		// keyword router solved this category at floor 1.0. The pair ID is still
		// the pinned argument; the intent must be read, not string-matched.
		name: "memory_fetch", wrap: true, tool: "fetch_memories", argKey: "pairIds",
		grammar: memoryFetchGrammar,
	},
	{
		name: "agent_workflow", tool: "execute_agent_workflow", argKey: "goal",
		templates: []string{
			"Plan %s as parallel sub-agents.",
			"Split %s into parallel subtasks and run them.",
			"Use a multi-agent workflow to produce %s.",
		},
	},
	{
		name: "feedback", tool: "file_feedback_for_team", argKey: "message",
		templates: []string{
			"Send this to the Ditto team: %s.",
			"File feedback for the team — %s.",
			"Report to the devs that %s.",
		},
	},
	{
		name: "set_model", tool: "set_main_model", argKey: "model",
		templates: []string{
			"Switch my chat model to %s.",
			"Use %s as my main model.",
			"Change my primary model to %s.",
		},
		intents: []argIntent{
			{"I'm on gemini-3-pro now — move me over to claude-sonnet-5.", "claude-sonnet-5"},
			{"Switch me off gpt-5 and onto gemini-3-pro.", "gemini-3-pro"},
			{"My main is llama-4-70b; change it to gpt-5.", "gpt-5"},
			{"Take me off claude-sonnet-5 and put me on llama-4-70b.", "llama-4-70b"},
		},
	},
	{
		intents: []argIntent{
			{"Really take your time and reason deeply on my questions from now on.", "high"},
			{"Make your answers as thorough and careful as you can.", "high"},
			{"Keep it quick and don't overthink my requests.", "low"},
			{"Snappy, minimal reasoning is fine — don't burn cycles.", "low"},
			{"Use a balanced amount of reasoning, nothing extreme.", "medium"},
		},
		name: "set_effort", tool: "set_reasoning_effort", argKey: "effort",
		templates: []string{
			"Set reasoning effort to %s.",
			"Make responses use %s effort.",
			"Change the thinking level to %s.",
		},
	},
	{
		name: "set_tool_prefs", tool: "set_chat_tool_preferences", argKey: "preferences",
		templates: []string{
			"Update my tool preferences: %s.",
			"Change my chat tools so you %s.",
			"Adjust which tools you use — %s.",
		},
	},
	// --- Production-tool coverage: routing/restraint traps ---
	{
		// Scheduling trap: a task phrased with a recurring TIME cue is an
		// automation, not a one-off agent job. The %s is the schedule (pinned).
		name: "automation_not_job", tool: "create_automation", argKey: "schedule",
		templates: []string{
			"Every morning, %s my news digest.",
			"On a schedule — %s — send me my summary.",
			"Set this to run %s: post my standup.",
		},
	},
	{
		name: "automation_list", wrap: true, tool: "list_automations",
		grammar: automationListGrammar,
	},
	{
		name: "recipe_create", tool: "create_recipe", argKey: "name",
		templates: []string{
			"Save a recipe called %s for later.",
			"Make a reusable recipe named %s.",
			"Create a %s recipe I can run again.",
		},
	},
	{
		name: "recipe_apply", tool: "apply_recipe", argKey: "name",
		templates: []string{
			"Run my %s recipe.",
			"Apply the %s recipe now.",
			"Execute my saved %s recipe.",
		},
	},
	{
		// Capability discovery: "what can you do / where is X" routes to
		// discover_capabilities, not a guess from memory or the web.
		name: "capability_discovery", wrap: true, tool: "discover_capabilities",
		grammar: capabilityGrammar,
	},
	{
		// Save-vs-search trap: a STATEMENT of a new fact is a memory WRITE, not a
		// search. The %s is the fact (freely rephrasable, no pinned arg token).
		name: "memory_save_not_search", tool: "save_memory",
		templates: []string{
			"Remember that %s.",
			"Note for later: %s.",
			"Please keep in mind that %s.",
		},
	},
	{
		name: "memory_update", tool: "update_memory", argKey: "pair_id",
		templates: []string{
			"Update memory %s with my new address.",
			"Change what you saved in memory %s.",
			"Correct memory %s to the new value.",
		},
	},
	{
		name: "memory_delete", tool: "delete_memory", argKey: "pair_id",
		templates: []string{
			"Delete memory %s.",
			"Forget what's in memory %s.",
			"Remove memory %s from my history.",
		},
	},
	{
		name: "calendar_create", tool: "calendar_create_event", argKey: "title",
		templates: []string{
			"Put %s on my calendar.",
			"Add %s to my calendar.",
			"Schedule %s on my calendar.",
		},
	},
	{
		name: "calendar_search", tool: "calendar_search_events", argKey: "query",
		templates: []string{
			"What's on my calendar about %s?",
			"Find the %s event on my calendar.",
			"Search my calendar for %s.",
		},
	},
	{
		name: "email_send", tool: "gmail_send", argKey: "to",
		templates: []string{
			"Email %s to let them know I'll be late.",
			"Send an email to %s with the update.",
			"Shoot %s a quick note about the meeting.",
		},
	},
	{
		name: "set_accent", tool: "set_accent_color", argKey: "color",
		templates: []string{
			"Set my accent color to %s.",
			"Make the accent color %s.",
			"Change the app accent to %s.",
		},
	},
	{
		name: "set_font", tool: "set_chat_font", argKey: "font",
		templates: []string{
			"Change my chat font to %s.",
			"Use %s as the chat font.",
			"Set the chat typeface to %s.",
		},
	},

	// Multi-hop trajectories: the correct answer is a tool SEQUENCE, scored
	// with order credit. These exercise the multi-call path.
	{
		name: "multi_web_read", tools: []string{"search_web", "read_links"},
		templates: []string{
			"Look up %s online and open the top result.",
			"Find a page about %s and read it for me.",
			"Search for %s and summarize the first source.",
		},
	},
	{
		// Parallel independent calls (BFCL "parallel"): two unrelated actions in one
		// turn, neither's input depending on the other's output, so any call order is
		// correct. Both are about the same entity so one filler renders both.
		name: "parallel_web_image", tools: []string{"search_web", "create_image"}, unordered: true,
		templates: []string{
			"Search the web for %s and also generate an image of it.",
			"Look up %s online, and separately make me a picture of it.",
			"Two things: find the latest on %s and create an image of it.",
		},
	},
	{
		name: "multi_subject_scope", tools: []string{"search_subjects", "search_memories_in_subjects"},
		templates: []string{
			"Find my notes related to %s and pull the details.",
			"Which subject covers %s — then recall the specifics.",
			"Look through my topics on %s and fetch what I saved.",
		},
	},
	{
		name: "multi_job_status", tools: []string{"execute_agent_job", "get_agent_job_status"},
		templates: []string{
			"Kick off a job to %s and tell me its status.",
			"Start %s in the background, then check how it's going.",
			"Run %s and report the job status.",
		},
	},
	{
		name: "multi_image_edit", tools: []string{"create_image", "edit_image"},
		templates: []string{
			"Make an image of %s, then brighten it.",
			"Generate %s and then add more detail.",
			"Create a picture of %s and tweak the colors.",
		},
	},
	// Result-usage: the answer requires a value
	// that exists ONLY in the tool's returned content (a fabricated per-seed
	// needle, toolexec), so the case cannot be answered by self-report or base-
	// model knowledge; the harness must actually execute the tool and USE the
	// result. The %s is the needle's Subject (filled below), keeping the question
	// and the served fact coherent. Scored deterministically (trajectory + needle-
	// in-answer).
	{
		name: "web_result_usage", tool: "search_web",
		templates: []string{
			"Search the web for the latest figure on %s and tell me the exact number.",
			"What number does the current top result report for %s?",
			"Look up %s online and give me the precise figure it cites.",
		},
	},
	{
		name: "multi_web_result_usage", tools: []string{"search_web", "read_links"},
		templates: []string{
			"Look up %s online, open the top result, and tell me the exact figure it reports.",
			"Find a page about %s, read it, and give me the precise number.",
			"Research %s on the web, read the leading source, and report its exact figure.",
		},
	},
	// Dependent-arg chain (result-usage): execute_agent_job returns a job id that
	// MUST be passed to get_agent_job_status, which only then returns the needle.
	// The needle exists only behind the chained id, so the whole trajectory is
	// unfakeable: a harness must read the first result and thread its value into
	// the second call. The "_job_chain" marker selects the dependent serving gate
	// (toolexec) and "_result_usage" the deterministic result-usage scoring.
	{
		name: "job_chain_result_usage", tools: []string{"execute_agent_job", "get_agent_job_status"},
		templates: []string{
			"Kick off a job to compute %s, then check its status and tell me the exact figure it returns.",
			"Dispatch a background job for %s, then look up that job's result and give me the precise number.",
			"Start a job to work out %s, then read the finished job's status and report its exact figure.",
		},
	},
	// Error recovery (result-usage): the FIRST call to the served tool returns a
	// transient error; the needle is delivered only on a RETRY. A harness that
	// gives up after one error cannot produce the answer value, so recovery is
	// enforced by the serving layer (no scorer change). allowExtra so the retry is
	// not charged as an over-budget extra call. The "recovery" marker selects the
	// transient-first serving behavior in toolexec.
	{
		name: "web_recovery_result_usage", tool: "search_web", allowExtra: true,
		templates: []string{
			"Search the web for the latest figure on %s and tell me the exact number (retry if the search flakes).",
			"Look up %s online and report the precise figure; if the first attempt errors, try again.",
			"Get the current number for %s from the web, retrying past any transient hiccup.",
		},
	},
}

// Tier-1a prompt-surface variation for the template-only categories whose
// prompt pool would otherwise be a handful of memorizable strings (the same
// machinery the persona haystack uses: a seed-chosen lead-in and trailer
// multiply the surface a template matcher must cover without changing the
// intent or any pinned argument). Empty entries weight toward the plain prompt.
var promptLeadIns = []string{
	"", "", "", "",
	"Hey, ", "Quick one: ", "Ok, so — ", "Real quick, ", "One thing: ",
	"When you get a sec, ", "Oh right — ",
}

var promptTrailers = []string{
	"", "", "", "",
	" Thanks!", " Cheers.", " No rush.", " Appreciate it.", " When you can.",
}

// resultUsageSuffix marks the categories whose correct answer must incorporate a
// tool's returned content (result-usage). Their prompt %s is the fixture needle's
// Subject and they are scored deterministically against the needle Value.
const resultUsageSuffix = "_result_usage"

// IsResultUsage reports whether a case category is a result-usage category. The
// pipeline scores these deterministically on trajectory + answer-incorporates-
// needle.
func IsResultUsage(category string) bool { return strings.HasSuffix(category, resultUsageSuffix) }

// v5 content-pool extensions. The thin open-ended CONTENT pools (a URL, an image
// prompt, a coding task, a research topic) are the tool suite's main memorization
// surface — with only ~5-9 entries a miner sees the same handful every seed. v5
// roughly doubles them, which is pure anti-memorization: a different URL or image
// subject is the SAME routing difficulty, so between-seed difficulty variance is
// unchanged while per-seed surface entropy rises. Value pools with a real finite
// space (themes, models, effort levels) are deliberately NOT extended — inventing
// fake values there would hurt realism, and the answer, not the surface, is what
// matters for them. Gated on v5 (poolV5), so v2/v3/v4 fillers are byte-identical.
var (
	topicsV5 = []string{
		"quantum error correction", "the housing market outlook", "olive oil adulteration",
		"deep-sea mining rules", "the Voyager probes", "sourdough hydration ratios",
		"urban heat islands", "the semiconductor supply chain", "coral reef restoration",
		"noise-cancelling headphone tech", "the history of the metric system",
	}
	urlsV5 = []string{
		"https://example.com/2026-outlook", "https://blog.example.org/rust-async",
		"https://news.example.net/energy-grid", "https://docs.example.io/api/v3",
		"https://example.com/longform/whales", "https://example.org/recipes/ramen",
		"https://research.example.edu/paper-42", "https://example.net/city-transit-plan",
	}
	imagePromptsV5 = []string{
		"a neon koi pond at dusk", "an isometric cozy reading nook", "a watercolor alpine village",
		"a retro-futurist train station", "a cross-section of a seed sprouting",
		"a lighthouse in a lightning storm", "a papercraft rainforest scene",
		"a chalkboard diagram of the water cycle",
	}
	agentTasksV5 = []string{
		"scaffold a CLI that renames photos by EXIF date", "write a script to dedupe my CSV export",
		"build a small Flask app that serves a health check", "add unit tests to my parser module",
		"convert this repo's config from JSON to YAML", "write a cron job that backs up a folder nightly",
		"refactor the utils file to remove duplication",
	}
	workflowGoalsV5 = []string{
		"audit my site for accessibility across pages, forms, and images",
		"research three CRM options on price, integrations, and support",
		"benchmark four JSON libraries on parse speed, memory, and API",
		"review a PR for correctness, tests, and style in parallel",
		"summarize a report from the finance, risk, and ops angles",
	}
	artifactKindsV5 = []string{
		"a habit-tracker web app", "a markdown resume", "a snake game",
		"a budgeting spreadsheet mockup", "a landing page for a coffee shop",
		"an interactive periodic table", "a pomodoro timer",
	}
	calendarTitlesV5 = []string{
		"dentist cleaning", "1:1 with Priya", "car service appointment",
		"book club", "flight to Denver", "quarterly review", "vet visit for the cat",
	}
)

// poolV5 returns base for pre-v5 contracts and base+extra for v5, so an expanded
// pool only affects v5 generation (v2/v3/v4 draws are unchanged).
func poolV5(base, extra []string, benchVersion int) []string {
	if benchVersion >= protocol.BenchVersionV5 {
		out := make([]string, 0, len(base)+len(extra))
		out = append(out, base...)
		out = append(out, extra...)
		return out
	}
	return base
}

// fillerFor returns a random entity string appropriate for a category (v2 pools).
func fillerFor(r *rand.Rand, cat string) string {
	return fillerForVersion(r, cat, protocol.BenchVersionV2)
}

// fillerForVersion is fillerFor under an explicit contract: v5 draws the thin
// content categories from the extended pools (poolV5), everything else from the
// historical pools, so v2/v3/v4 fillers are byte-identical.
func fillerForVersion(r *rand.Rand, cat string, benchVersion int) string {
	pick := func(base, extra []string) string {
		p := poolV5(base, extra, benchVersion)
		return p[r.Intn(len(p))]
	}
	switch cat {
	case "entity_lookup_chain":
		return pick(subjects, nil)
	case "web_search", "route_web_not_memory", "calendar_search", "stale_context_web":
		return pick(topics, topicsV5)
	case "link_read":
		return pick(urls, urlsV5)
	case "image_create":
		return pick(imagePrompts, imagePromptsV5)
	case "artifacts_create":
		return pick(artifactKinds, artifactKindsV5)
	case "agent_job", "agent_run_not_read":
		return pick(agentTasks, agentTasksV5)
	case "workflow_not_job", "agent_workflow":
		return pick(workflowGoals, workflowGoalsV5)
	case "calendar_create":
		return pick(calendarTitles, calendarTitlesV5)
	}
	return fillerForLegacy(r, cat)
}

// fillerForLegacy is the historical per-category pool selection for every category
// not extended in v5.
func fillerForLegacy(r *rand.Rand, cat string) string {
	switch cat {
	case "memory_lookup", "memory_subject":
		return subjects[r.Intn(len(subjects))]
	case "web_search":
		return topics[r.Intn(len(topics))]
	case "link_read":
		return urls[r.Intn(len(urls))]
	case "image_create":
		return imagePrompts[r.Intn(len(imagePrompts))]
	case "artifacts_create":
		return artifactKinds[r.Intn(len(artifactKinds))]
	case "agent_job":
		return agentTasks[r.Intn(len(agentTasks))]
	case "settings":
		return themes[r.Intn(len(themes))]
	case "route_memory_not_web":
		return subjects[r.Intn(len(subjects))]
	case "route_web_not_memory":
		return topics[r.Intn(len(topics))]
	case "agent_run_not_read":
		return agentTasks[r.Intn(len(agentTasks))]
	case "agent_read_not_run", "image_edit_not_create":
		return "" // templates have no placeholder
	case "workflow_not_job":
		return workflowGoals[r.Intn(len(workflowGoals))]
	case "memory_fetch":
		return memoryIDs[r.Intn(len(memoryIDs))]
	case "agent_workflow":
		return workflowGoals[r.Intn(len(workflowGoals))]
	case "feedback":
		return feedback[r.Intn(len(feedback))]
	case "set_model":
		return models[r.Intn(len(models))]
	case "set_effort":
		return efforts[r.Intn(len(efforts))]
	case "set_tool_prefs":
		return toolPrefs[r.Intn(len(toolPrefs))]
	case "automation_not_job":
		return schedules[r.Intn(len(schedules))]
	case "automation_list":
		return "" // template has no placeholder
	case "recipe_create", "recipe_apply":
		return recipeNames[r.Intn(len(recipeNames))]
	case "capability_discovery":
		return "" // template has no placeholder
	case "memory_save_not_search":
		return savableFacts[r.Intn(len(savableFacts))]
	case "memory_update", "memory_delete":
		return memoryIDs[r.Intn(len(memoryIDs))]
	case "calendar_create":
		return calendarTitles[r.Intn(len(calendarTitles))]
	case "calendar_search":
		return topics[r.Intn(len(topics))]
	case "email_send":
		return emailRecipients[r.Intn(len(emailRecipients))]
	case "set_accent":
		return accentColors[r.Intn(len(accentColors))]
	case "set_font":
		return chatFonts[r.Intn(len(chatFonts))]
	case "multi_web_read", "parallel_web_image":
		return topics[r.Intn(len(topics))]
	case "multi_subject_scope":
		return subjects[r.Intn(len(subjects))]
	case "multi_job_status":
		return agentTasks[r.Intn(len(agentTasks))]
	case "multi_image_edit":
		return imagePrompts[r.Intn(len(imagePrompts))]
	case "no_tool":
		return chitchat[r.Intn(len(chitchat))]
	case "abstention":
		return abstentions[r.Intn(len(abstentions))]
	case "arg_hallucination":
		return "" // templates omit the required value on purpose
	default:
		_ = people // reserved for future multi-entity templates
		return topics[r.Intn(len(topics))]
	}
}

// Generate preserves the frozen v2 package contract for legacy callers.
func Generate(seed int64, n int) protocol.Dataset {
	ds, err := GenerateForVersion(seed, n, protocol.BenchVersionV2)
	if err != nil {
		panic(err) // v2 is a compile-time supported contract.
	}
	return ds
}

// GenerateForVersion produces a deterministic tool-practice dataset for one
// explicit immutable benchmark contract. n is clamped to [1, 200].
func GenerateForVersion(seed int64, n, benchVersion int) (protocol.Dataset, error) {
	if n < 1 {
		n = 1
	}
	if n > 200 {
		n = 200
	}
	rotated, err := protocol.RotateSeedForVersion(seed, benchVersion)
	if err != nil {
		return protocol.Dataset{}, err
	}
	epoch, err := protocol.DatasetEpochForVersion(benchVersion)
	if err != nil {
		return protocol.Dataset{}, err
	}
	r := rand.New(rand.NewSource(rotated))
	cases, _ := GenerateCasesWithFillersForVersion(r, seed, n, benchVersion)
	return protocol.Dataset{
		Seed:        seed,
		GeneratedAt: epoch.Format("2006-01-02T15:04:05Z07:00"),
		ToolCases:   cases,
	}, nil
}

// stratifiedCategoryOrder returns n category indices with a FIXED per-category
// quota (each category appears floor(n/C) or ceil(n/C) times), then shuffles the
// order with the seeded RNG. Fixing the category MIX per run (rather than
// drawing each case's category uniformly at random) removes the multinomial
// category-draw variance that dominated dataset-to-dataset difficulty (the
// per-run score stddev scaled as sqrt(p(1-p)/n)). Every dataset now exercises
// the same balance of easy categories and routing traps, so a miner can't get a
// lucky-easy or unlucky-hard draw. Choosing n as a multiple of the category count
// gives a perfectly balanced set; otherwise the first n%C categories get one
// extra (deterministic, so it adds no between-run variance).
func stratifiedCategoryOrder(r *rand.Rand, n, nc int) []int {
	order := make([]int, 0, n)
	base, rem := n/nc, n%nc
	for ci := 0; ci < nc; ci++ {
		count := base
		if ci < rem {
			count++
		}
		for k := 0; k < count; k++ {
			order = append(order, ci)
		}
	}
	r.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
	return order
}

// toolCategoryWeightV7 is the bench_version 7 category mix weight, refit to the
// round-2 MEASURED per-family champion means (docs/v7-product-traceability.md).
// The lever is the MIX: give the largest share to the tool families the
// leaderboard harnesses actually FAIL — the dependent/served-content chains that
// separate the fleet (link_chain: scratch 1.0/starter 0.30; job_chain_recovery:
// 0.80/0.06; job_chain_result_usage: 0.47/0.40; web_recovery: 0.93/0.40) and the
// routing traps that bite the strong tier (stale_context_web: scratch 0.17;
// negation_no_tool: 0.50). The result-usage families the strong tier already
// aces (web_result_usage / multi_web_result_usage ~1.0) drop to coverage weight,
// and entity_lookup_chain is trajectory-scored (harnesses get the order right,
// so it is coverage, not a discriminator). Weights are seed-independent.
func toolCategoryWeightV7(name string) int {
	switch name {
	// measured separators / strong-tier biters — the honest tool discriminators.
	case "stale_context_web":
		return 8 // measured scratch 0.17
	case "job_chain_result_usage":
		return 7 // 0.47 / 0.40 — bites both tiers
	case "link_chain_result_usage", "job_chain_recovery_result_usage":
		return 6 // starter separators (starter 0.30 / 0.06)
	case "web_recovery_result_usage":
		return 5 // 0.93 / 0.40
	case "negation_no_tool":
		return 4 // scratch 0.50
	}
	switch {
	case IsResultUsage(name): // web_result_usage / multi_web_result_usage: scratch ~1.0
		return 2
	case strings.Contains(name, "_not_"),
		strings.HasPrefix(name, "route_"),
		strings.HasPrefix(name, "multi_"),
		name == "parallel_web_image",
		name == "arg_hallucination",
		name == "entity_lookup_chain",
		name == "tool_discovery":
		return 2
	default:
		return 1
	}
}

// stratifiedCategoryOrderWeighted is stratifiedCategoryOrder with a per-category
// weight: category ci receives ~n*w/totalW slots (deterministic remainder
// distribution: weight-desc then index-asc round robin), then the order is
// shuffled with the seeded RNG. Like the unweighted version, the per-run MIX is
// fixed and only the ordering varies by seed.
func stratifiedCategoryOrderWeighted(r *rand.Rand, n int, weights []int) []int {
	nc := len(weights)
	totalW := 0
	for _, w := range weights {
		if w < 1 {
			w = 1
		}
		totalW += w
	}
	counts := make([]int, nc)
	assigned := 0
	for ci, w := range weights {
		if w < 1 {
			w = 1
		}
		counts[ci] = n * w / totalW
		assigned += counts[ci]
	}
	fillOrder := make([]int, nc)
	for i := range fillOrder {
		fillOrder[i] = i
	}
	sort.SliceStable(fillOrder, func(i, j int) bool {
		wi, wj := weights[fillOrder[i]], weights[fillOrder[j]]
		if wi != wj {
			return wi > wj
		}
		return fillOrder[i] < fillOrder[j]
	})
	for k := 0; assigned < n; k = (k + 1) % nc {
		counts[fillOrder[k]]++
		assigned++
	}
	order := make([]int, 0, n)
	for ci, c := range counts {
		for j := 0; j < c; j++ {
			order = append(order, ci)
		}
	}
	r.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
	return order
}

// sampledCategoryOrderV8 varies the category histogram by seed while retaining
// the v7 expected weights. When the run can hold the catalog, every family gets
// one slot; smaller profiles sample without replacement first. Remaining slots
// are weighted draws. This keeps generation reproducible and prevents any
// family from being permanently dead across seeds.
func sampledCategoryOrderV8(r *rand.Rand, n int, weights []int) []int {
	if n <= 0 || len(weights) == 0 {
		return nil
	}
	counts := make([]int, len(weights))
	remaining := n
	perm := r.Perm(len(weights))
	floor := len(weights)
	if floor > n {
		floor = n
	}
	for _, ci := range perm[:floor] {
		counts[ci]++
		remaining--
	}
	totalWeight := 0
	for _, weight := range weights {
		if weight < 1 {
			weight = 1
		}
		totalWeight += weight
	}
	for ; remaining > 0; remaining-- {
		draw := r.Intn(totalWeight)
		for ci, weight := range weights {
			if weight < 1 {
				weight = 1
			}
			if draw < weight {
				counts[ci]++
				break
			}
			draw -= weight
		}
	}
	order := make([]int, 0, n)
	for ci, count := range counts {
		for range count {
			order = append(order, ci)
		}
	}
	r.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
	return order
}

// sampledCategoryOrderV9 keeps one explicit anti-extraction case in every
// non-empty profile, samples the rest of a partial catalog without replacement,
// and gives a full run one slot from every source family. Any slots left after
// that floor are independent weighted draws. The caller supplies a dedicated
// family-mix RNG so changes in prompt rendering, harness projection, or output
// permutation cannot silently change the histogram.
func sampledCategoryOrderV9(r *rand.Rand, n int, weights []int, mandatory int) []int {
	if n <= 0 || len(weights) == 0 {
		return nil
	}
	counts := make([]int, len(weights))
	available := make([]bool, len(weights))
	for i := range available {
		available[i] = true
	}
	remaining := n
	if mandatory >= 0 && mandatory < len(weights) {
		counts[mandatory]++
		available[mandatory] = false
		remaining--
	}

	// A full catalog gets a hard floor. A smaller profile gets a weighted sample
	// without replacement rather than the v8 uniform subset, so even its inclusion
	// probabilities continue to reflect the published family weights.
	floorSlots := len(weights) - 1
	if mandatory < 0 || mandatory >= len(weights) {
		floorSlots = len(weights)
	}
	if floorSlots > remaining {
		floorSlots = remaining
	}
	for range floorSlots {
		total := 0
		for i, weight := range weights {
			if available[i] {
				total += positiveWeight(weight)
			}
		}
		draw := r.Intn(total)
		for i, weight := range weights {
			if !available[i] {
				continue
			}
			weight = positiveWeight(weight)
			if draw < weight {
				counts[i]++
				available[i] = false
				remaining--
				break
			}
			draw -= weight
		}
	}

	totalWeight := 0
	for _, weight := range weights {
		totalWeight += positiveWeight(weight)
	}
	for ; remaining > 0; remaining-- {
		draw := r.Intn(totalWeight)
		for i, weight := range weights {
			weight = positiveWeight(weight)
			if draw < weight {
				counts[i]++
				break
			}
			draw -= weight
		}
	}

	order := make([]int, 0, n)
	for i, count := range counts {
		for range count {
			order = append(order, i)
		}
	}
	r.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
	return order
}

func positiveWeight(weight int) int {
	if weight < 1 {
		return 1
	}
	return weight
}

func toolCategoryMixRNG(seed int64, benchVersion, n int) *rand.Rand {
	return toolMixRNG(seed, benchVersion, n, "family")
}

func toolMixRNG(seed int64, benchVersion, n int, domain string) *rand.Rand {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-tool-mix:%s:%d:%d:%d", domain, seed, benchVersion, n)
	return rand.New(rand.NewSource(int64(h.Sum64() & ((1 << 63) - 1))))
}

// codeModeCategories are the bench_version 5 Code Mode categories: they exercise
// run_code (the in-process JavaScript compute/orchestration sandbox) and the
// discrimination between run_code (a pure in-context calculation, no side effects)
// and execute_agent_job (Ditto Code — real coding work needing fs/network/repo).
// This is where "code mode usage" is represented in the tool suite. Gated on v5
// via categoriesForVersion so v2/v3/v4 tool bytes stay frozen. Grammar-based
// prompts (self-contained numbers) so no new filler pool is needed.
var codeModeCategories = []category{
	{
		name: "code_compute", tool: "run_code",
		grammar: persona.Grammar{
			"root": {
				"#lead# I spent #a#, #b#, and #c# over the last three months — work out my average monthly spend and the total.",
				"#lead# my three test scores were #a#, #b#, and #c#. Give me the mean and how far the lowest sits below it.",
				"#lead# take #a#, #b#, and #c#, then give me their sum, their average, and the spread between highest and lowest.",
				"#lead# if I split a bill of #a# three ways and add a #b# tip, what does each person pay?",
			},
			"lead": {"Quick calc:", "Can you crunch this for me:", "Number-crunch this —", "Help me work this out:"},
			"a":    {"$1,240", "312", "48.5", "1,024", "$96"},
			"b":    {"$980", "277", "51.2", "18%", "2,048"},
			"c":    {"$1,510", "298", "46.9", "512", "$74"},
		},
	},
	{
		name: "code_compute_not_agent_job", tool: "run_code",
		grammar: persona.Grammar{
			"root": {
				"No need to spin up a whole project or touch my files — just compute #a# times #b# minus #c# right now.",
				"This is a one-off calculation, not a coding job: normalize #a#, #b#, and #c# to sum to 1 and give me the fractions.",
				"Don't build anything — just do the arithmetic: what's the compound total of #a#, #b#, and #c#?",
				"I don't need a script or a repo, just the answer in your head-scratchpad: what percent of #a# is #b#?",
			},
			"lead": {""},
			"a":    {"1,240", "312", "48.5", "1,024", "96"},
			"b":    {"9.5", "277", "51.2", "12", "2,048"},
			"c":    {"310", "298", "46.9", "512", "74"},
		},
	},
	{
		name: "tool_discovery", tool: "search_tools",
		grammar: persona.Grammar{
			"root": {
				"Before you write any code, which of your tools can #cap#? Look it up.",
				"I'm not sure what's available — search your tools for something that can #cap#.",
				"Find the right tool binding to #cap# before you run anything.",
				"What tool would you use to #cap#? Search for it first.",
			},
			"cap": {
				"convert a file between formats",
				"fetch live exchange rates",
				"resize a batch of images",
				"pull rows from a spreadsheet",
				"look up a package's latest version",
			},
		},
	},
}

// difficultyCategoriesV7 are the bench_version 7 tool categories (the
// difficulty release; see docs/bench-versions.md):
//
//   - negation_no_tool: the prompt NAMES a tool-cue ("search", "image", "agent")
//     while negating it — the correct behavior is no tool at all, so a keyword
//     router that fires on cue words is actively caught rather than merely
//     unlucky.
//   - stale_context_web: a memory-anchored phrasing ("I told you my take on X")
//     whose actual request is CURRENT public information — the tempting route is
//     search_memories, the correct one is search_web.
//   - link_chain_result_usage: a dependent-arg content chain. search_web serves
//     a STABLE page URL; read_links returns the answer needle ONLY when called
//     with that URL (toolexec link-chain gate), so the harness must read the
//     first result and thread the URL into the second call — the trajectory
//     cannot be faked and the snippet cannot be grepped (it carries the scored
//     decoy).
//   - job_chain_recovery_result_usage: the composed hard case — the dependent
//     job-id chain AND transient-error recovery at once. The first
//     get_agent_job_status call returns a transient error; the retry must carry
//     the job id execute_agent_job served. Both gates already exist in toolexec
//     and are selected by the category-name markers ("job_chain", "recovery").
//
// Gated on v7 via categoriesForVersion so v2..v6 tool bytes stay frozen.
var difficultyCategoriesV7 = []category{
	{
		name: "negation_no_tool", tool: "",
		grammar: persona.Grammar{
			"root": {
				"Don't search the web for this — just from general knowledge, #gk#?",
				"No need to run a web search; off the top of your head, #gk#?",
				"Skip the image tools — just describe #img# in words for me.",
				"Don't generate a picture; paint #img# for me in words instead.",
				"No background jobs please — just talk me through how you'd #plan#.",
				"Don't kick off an agent for this; simply outline how you'd #plan#, step by step.",
				"Leave my settings alone — I'm just curious what theme options exist besides dark and light.",
				"Don't touch my calendar; just tell me whether a Tuesday or a Thursday works better for a weekly review, generally.",
			},
			"gk": {
				"is espresso stronger than drip coffee per ounce",
				"do marathon runners train every single day",
				"is fresh pasta cooked faster than dried",
				"are more people left-handed or right-handed",
			},
			"img": {
				"a foggy harbor at dawn", "a maple leaf in late October",
				"a night market in the rain", "a lighthouse against a storm",
			},
			"plan": {
				"organize a garage sale", "plan a three-course dinner for six",
				"structure a two-week language-learning sprint", "lay out a small vegetable garden",
			},
		},
	},
	{
		// The mirror trap of route_web_not_memory, made more tempting: the prompt
		// explicitly ANCHORS on stored memory before asking for live information.
		name: "stale_context_web", tool: "search_web",
		templates: []string{
			"I know I told you my take on %s a while back, but what's the latest on it right now?",
			"Forget what I said about %s before — pull up what's current today.",
			"You have my old notes on %s; check what's actually changed since then.",
		},
	},
	{
		name: "link_chain_result_usage", tools: []string{"search_web", "read_links"},
		templates: []string{
			"Find the page about %s, open the link the search points to, and tell me the exact figure that page reports.",
			"Search for %s, follow the result link, and give me the precise number from the page itself.",
			"Look up %s, read the linked page (not just the snippet), and report the exact figure it cites.",
		},
	},
	{
		name: "job_chain_recovery_result_usage", tools: []string{"execute_agent_job", "get_agent_job_status"}, allowExtra: true,
		templates: []string{
			"Kick off a job to compute %s, then fetch that job's result — the status check can be flaky, so retry it if it errors — and tell me the exact figure.",
			"Dispatch a background job for %s, then read the job's status for the precise number; if the status call hiccups, try it again.",
			"Start a job to work out %s, then pull its result and report the exact figure — retry the status lookup past any transient error.",
		},
	},
	{
		// The production entity-lookup sequence the backend explicitly recommends
		// (pkg/mcp/server.go: "search_subjects -> search_memories_in_subjects ->
		// fetch_memories"): resolve the subject, pull the linked pairs, then fetch
		// the full memory. A three-hop ORDER-scored trajectory over memory tools
		// (not served, so scored on names+order); a harness that grabs
		// search_memories directly, or gets the hop order wrong, fails.
		name: "entity_lookup_chain", tools: []string{"search_subjects", "search_memories_in_subjects", "fetch_memories"},
		templates: []string{
			"Find the subject that covers %s, pull the memories linked to it, then open the full memory.",
			"Look up the topic for %s, get the pairs under that subject, and fetch the details.",
			"Which subject holds %s — retrieve its linked memories, then read the full entry.",
		},
	},
}

// applyV8WorldActions replaces 65% of the historical one-card tool prompts with
// tasks grounded in one shared personal/business world. The correct action or
// argument is derivable only by reconciling aliases, relationships, corrections,
// and business context seeded through the ordinary harness memory boundary.
//
// ExpectedTools is a capability SET, not a prescribed trace: FuzzyTrajectory
// and AllowExtraTools make exploratory agent trajectories legitimate, while
// the validator-observed final action and its seed-derived argument remain
// deterministic. MaxToolCalls describes the expected envelope; it is not a hard
// cap and creative agents may legitimately exceed it.
func applyV8WorldActions(seed int64, cases []protocol.ToolCase) {
	applyWorldActions(seed, cases, false)
}

const (
	// A full v9 run has 46 retained non-world families and seven world families.
	// Their one-per-family floor consumes 53 of 100 slots, so the mathematical
	// maximum world-action share is 54%. V9 targets 48% with a 43% hard floor,
	// leaving six to eleven slots beyond the non-world floor for the seed-weighted
	// residual source mix. Planted-context cases count toward the separate 50%
	// composed/evidence-bound minimum.
	V9FullToolCaseCount       = 100
	V9FullWorldActionTarget   = 48
	V9FullWorldActionMinimum  = 43
	V9FullComposedCaseMinimum = 50
)

// applyV9WorldActions retains the final semantic-family floor and an explicit
// full-run distribution contract. V8 deliberately converted at least 65% of
// ordinary cases, which can erase a one-slot family. That target is impossible
// alongside v9's 53-family/100-case floor: 46 non-world floors leave at most 54
// slots for seven world families. V9 therefore targets 48 world actions with a
// 43-action hard floor, preserving six to eleven non-world residual slots whose
// source counts retain the published seed-sampled weights. Together with planted
// stale-context and memory-fetch programs, at least half of the run remains
// evidence-bound or composed. The explicit legacy replacements below remain
// authoritative retirements of obsolete product behavior.
func applyV9WorldActions(seed int64, cases []protocol.ToolCase) {
	applyWorldActions(seed, cases, true)
}

// applyV10StateDependentActions removes the last easy prompt-to-tool shortcut
// from the shared-world suite. The visible request names the work but not the
// execution mode; a seed-scoped planning record decides whether the user
// approved a one-off Ditto Code job, creation of a reusable workflow, or a run
// of an existing workflow. The same prompt surface therefore maps to different
// correct tool outcomes across seeds while every answer remains deterministically
// recoverable from ordinary harness-visible state.
func applyV10StateDependentActions(seed int64, benchVersion int, cases []protocol.ToolCase) {
	if len(cases) == 0 {
		return
	}
	scale := 1
	if len(cases) >= 80 {
		scale = 3
	} else if len(cases) >= 30 {
		scale = 2
	}
	world := universe.Generate(seed, scale)
	routes := toolMixRNG(seed, protocol.BenchVersionV10, len(cases), "state-dependent-route")
	projectIndex := 0
	for i := range cases {
		if cases[i].Category != "world_agent_job_dispatch" {
			continue
		}
		project := world.Projects[projectIndex%len(world.Projects)]
		projectIndex++
		pairID := protocol.OpaqueCaseID(seed, "v10-tool-route", i)
		planningPrompt := fmt.Sprintf("Planning decision for dependency-risk work on %q at %s.", project.Alias, project.Client)
		prompt := fmt.Sprintf("Handle the dependency-risk review for %q using the operating choice I approved in our planning note. Start that work now.", project.Alias)
		routePrefix := "Approved route: "
		if benchVersion >= protocol.BenchVersionV11 {
			// v11 rotates every literal cue the measured rule engines matched
			// ("planning note", "operating choice", "Approved route:"). The
			// decision's semantics and the expected tool outcome are untouched.
			planningPrompt = fmt.Sprintf(v11ToolPick(seed, i, "plan", []string{
				"Decision log for the dependency-risk effort on %q at %s.",
				"Where we landed on the %q risk review for %s.",
				"Outcome of our scoping chat about %q (%s).",
			}), project.Alias, project.Client)
			prompt = fmt.Sprintf(v11ToolPick(seed, i, "ask", []string{
				"Kick off the dependency-risk review for %q the way we already agreed. Go ahead now.",
				"Time to start the risk review on %q — proceed exactly as we settled earlier. Begin now.",
				"Please begin the dependency-risk pass for %q following what we decided together. Start it.",
			}), project.Alias)
			routePrefix = v11ToolPick(seed, i, "route", []string{
				"What we settled on: ", "Agreed path — ", "Our decision stands: ", "Going with this: ",
			})
		}

		var planningResponse, behavior string
		var expected []protocol.ToolSpec
		switch routes.Intn(3) {
		case 0:
			planningResponse = routePrefix + "dispatch one one-off Ditto Code job. Do not create or run a reusable workflow."
			expected = []protocol.ToolSpec{{Name: "execute_agent_job"}}
			behavior = "resolve the planning decision and dispatch the approved one-off Ditto Code job for user approval"
		case 1:
			planningResponse = fmt.Sprintf(routePrefix+"create a new reusable workflow named %q. Do not dispatch a one-off job or run an existing workflow.", project.Name)
			expected = []protocol.ToolSpec{{Name: "create_workflow", RequiredArgs: map[string]string{"name": project.Name}}}
			behavior = "resolve the planning decision and create the approved reusable workflow under its canonical name"
		case 2:
			planningResponse = fmt.Sprintf(routePrefix+"run the existing workflow named %q. List saved workflows first; do not create a replacement or dispatch a one-off job.", project.Name)
			expected = []protocol.ToolSpec{{Name: "list_workflows"}, {Name: "run_workflow", RequiredArgs: map[string]string{"name": project.Name}}}
			behavior = "resolve the planning decision, list saved workflows, and run the approved existing workflow"
		}

		originalPrerequisites := cases[i].PrerequisitePairs
		originalProtected := cases[i].WritingProtected
		tc := fuzzyWorldTool(cases[i].ID, "v10_state_dependent_routing", prompt, expected, behavior)
		tc.PrerequisitePairs = append(append([]protocol.MemoryPair(nil), originalPrerequisites...), protocol.MemoryPair{
			PairID: pairID, SessionID: fmt.Sprintf("v10-tool-route-%02d", projectIndex), Timestamp: "2026-01-15T10:00:00Z",
			Prompt: planningPrompt, Response: planningResponse,
		})
		tc.WritingProtected = append(append([]string(nil), originalProtected...), project.Alias, project.Client, project.Name)
		cases[i] = tc
	}
}

func v9RetiredSourceFamily(category string) bool {
	switch category {
	case "email_send", "memory_delete", "memory_update", "link_read",
		"multi_job_status", "job_chain_result_usage", "job_chain_recovery_result_usage":
		return true
	default:
		return false
	}
}

func v9WorldFamily(category string) bool {
	return strings.HasPrefix(category, "world_")
}

func v9ComposedFamily(category string) bool {
	return v9WorldFamily(category) || category == "stale_context_web" || category == "memory_fetch"
}

func applyWorldActions(seed int64, cases []protocol.ToolCase, preserveSemanticFloor bool) {
	if len(cases) == 0 {
		return
	}
	remainingByCategory := make(map[string]int, len(cases))
	if preserveSemanticFloor {
		for _, tc := range cases {
			remainingByCategory[tc.Category]++
		}
	}
	scale := 1
	if len(cases) >= 80 {
		scale = 3
	} else if len(cases) >= 30 {
		scale = 2
	}
	world := universe.Generate(seed, scale)
	target := (65*len(cases) + 99) / 100
	if target >= len(cases) {
		target = len(cases) - 1 // retain at least one plain v7-style coverage case
	}
	if preserveSemanticFloor && len(cases) == V9FullToolCaseCount {
		retired := 0
		for _, tc := range cases {
			if v9RetiredSourceFamily(tc.Category) {
				retired++
			}
		}
		// Retired families become world actions in the authoritative tail pass.
		// Skip them in the generic converter and reserve only the additional
		// non-retired conversions needed for the final world-action target.
		target = V9FullWorldActionTarget - retired
		if target < 0 {
			target = 0
		}
	}
	converted := 0
	var worldMix *rand.Rand
	var worldFloor []int
	if preserveSemanticFloor {
		worldMix = toolMixRNG(seed, protocol.BenchVersionV9, len(cases), "world-family")
		worldFloor = worldMix.Perm(8)
	}
	attachedWorld := false
	worldCarrier := -1
	deleteTarget := 0
	updateTarget := 0
	convertAt := func(i int) {
		caseID := cases[i].ID
		sourceCategory := cases[i].Category
		var tc protocol.ToolCase
		worldKind := converted % 8
		if preserveSemanticFloor {
			if converted < len(worldFloor) {
				worldKind = worldFloor[converted]
			} else {
				worldKind = worldMix.Intn(8)
			}
		}
		switch worldKind {
		case 0, 5: // resolve a vague referent, research a live fact, and email it
			tc = v8WorldContactEmail(seed, caseID, world, converted+i)
		case 1: // description -> target pair -> destructive action
			tc = v8WorldMemoryDelete(caseID, world, deleteTarget)
			deleteTarget++
		case 2: // description -> target pair -> update action
			tc = v8WorldMemoryUpdate(caseID, world, updateTarget)
			updateTarget++
		case 3: // capability discovery + fuzzy/case-insensitive setting
			tc = fuzzyWorldTool(caseID, "world_theme_discover_set", fmt.Sprintf("Make Ditto use my usual %s-ish accent — the personal app preference, not one of the client brand colors. If I mangled the spelling, check the available appearance options first.", misspellAlias(world.Accent, converted)), []protocol.ToolSpec{{Name: "discover_capabilities"}, {Name: "set_accent_color", RequiredArgs: map[string]string{"color": world.Accent}}}, "discover the available appearance setting and apply the user's personal accent")
		case 4: // messy business context -> reusable workflow outcome
			p := world.Projects[(converted+i)%len(world.Projects)]
			lead := world.People[p.Lead]
			tc = fuzzyWorldTool(caseID, "world_business_workflow", fmt.Sprintf("Check whether I already have a workflow for %q, the project for %s. If not, create one under the project's formal name and put the current contact address for internal reviewer %s in its review step.", p.Alias, p.Client, lead.Nickname), []protocol.ToolSpec{{Name: "list_workflows"}, {Name: "create_workflow", RequiredArgs: map[string]string{"name": p.Name, "steps": lead.Email}}}, "resolve the project, its formal name, and current reviewer contact; then check existing workflows and create the requested reusable workflow")
		case 6: // outcome proves a search -> dynamic-link -> read chain
			tc = v8WorldLinkRead(seed, caseID)
		case 7: // Ditto App presents approval and owns the async job lifecycle
			tc = v8WorldAgentJob(caseID, world, converted+i)
		}
		if !attachedWorld {
			tc.PrerequisitePairs = append([]protocol.MemoryPair(nil), world.Pairs...)
			attachedWorld = true
			worldCarrier = i
		}
		cases[i] = tc
		if preserveSemanticFloor {
			remainingByCategory[sourceCategory]--
		}
		converted++
	}
	for i := range cases {
		if converted >= target {
			break
		}
		// Preserve bespoke prerequisites only when they already prove a composed
		// outcome; the shared world replaces ordinary/card-like cases first.
		if len(cases[i].PrerequisitePairs) != 0 {
			continue
		}
		if preserveSemanticFloor && remainingByCategory[cases[i].Category] <= 1 {
			continue
		}
		if preserveSemanticFloor && v9RetiredSourceFamily(cases[i].Category) {
			continue
		}
		convertAt(i)
	}
	if preserveSemanticFloor && len(cases) == V9FullToolCaseCount {
		retired := 0
		for _, tc := range cases {
			if v9RetiredSourceFamily(tc.Category) {
				retired++
			}
		}
		minimumConversions := V9FullWorldActionMinimum - retired
		// An unusually large weighted draw of stale-context or memory-fetch cases
		// can exhaust the ordinary duplicates before the pure-world floor. Convert
		// only as many duplicate prerequisite cases as needed for that floor; at
		// least eleven non-world residual slots remain at the 43-case minimum.
		for i := range cases {
			if converted >= minimumConversions {
				break
			}
			if len(cases[i].PrerequisitePairs) == 0 || remainingByCategory[cases[i].Category] <= 1 || v9RetiredSourceFamily(cases[i].Category) {
				continue
			}
			convertAt(i)
		}
	}

	// The remaining legacy-coverage tail must not reintroduce the exact IDs,
	// example.com recipients, or complete URLs that v8 removed. These categories
	// are always grounded in the same world even when the 65% replacement quota
	// happened to leave their original draw outside the dominant slice.
	for i := range cases {
		caseID := cases[i].ID
		switch cases[i].Category {
		case "email_send":
			cases[i] = v8WorldContactEmail(seed, caseID, world, i)
		case "memory_delete":
			cases[i] = v8WorldMemoryDelete(caseID, world, deleteTarget)
			deleteTarget++
		case "memory_update":
			cases[i] = v8WorldMemoryUpdate(caseID, world, updateTarget)
			updateTarget++
		case "link_read":
			cases[i] = v8WorldLinkRead(seed, caseID)
		case "multi_job_status", "job_chain_result_usage", "job_chain_recovery_result_usage":
			// Production stops the agent turn at execute_agent_job: Ditto App
			// presents the approval and owns subsequent progress/result display.
			// Do not reward benchmark-only polling behavior in the v8 tail.
			cases[i] = v8WorldAgentJob(caseID, world, i)
		}
	}
	// V9 memory cases are grounded in this same coherent world and deliberately
	// leave their own waves empty: the tool-prerequisite wave is the initial seed
	// boundary. Small profiles can contain world memory cases while sampling no
	// world tool at all (notably seeds 4, 6, and 9). Always nominate exactly one
	// tool case as the world carrier without changing its semantic family. Prefer
	// a world case, then an otherwise prerequisite-free case so bespoke programs
	// retain their compact local evidence block.
	if preserveSemanticFloor && !attachedWorld {
		worldCarrier = -1
		for i := range cases {
			if strings.HasPrefix(cases[i].Category, "world_") {
				worldCarrier = i
				break
			}
		}
		if worldCarrier < 0 {
			for i := range cases {
				if len(cases[i].PrerequisitePairs) == 0 {
					worldCarrier = i
					break
				}
			}
		}
		if worldCarrier < 0 {
			worldCarrier = 0
		}
		cases[worldCarrier].PrerequisitePairs = append(cases[worldCarrier].PrerequisitePairs, world.Pairs...)
		attachedWorld = true
	}
	protected := world.ProtectedTerms()
	for i := range cases {
		if strings.HasPrefix(cases[i].Category, "world_") || i == worldCarrier {
			cases[i].WritingProtected = append(cases[i].WritingProtected, protected...)
		}
		if strings.HasPrefix(cases[i].Category, "world_") {
			cases[i].WritingProtected = append(cases[i].WritingProtected, toolexec.NeedleForV8World(seed, cases[i].ID).Subject)
		} else {
			cases[i].WritingProtected = append(cases[i].WritingProtected, toolexec.NeedleFor(seed, cases[i].ID).Subject)
		}
	}
}

func v8WorldContactEmail(seed int64, caseID string, world universe.World, index int) protocol.ToolCase {
	p := world.People[index%len(world.People)]
	needle := toolexec.NeedleForV8World(seed, caseID)
	prompts := []string{
		"What is %s at right now? Forward the figure to %s — the %s in %s from the %s.",
		"Could you check the latest figure for %s and send it to %s? I mean my %s in %s, the one from the %s.",
		"Please look up %s and pass the current number along to %s, my %s in %s from the %s.",
		"I need the latest reading for %s sent to %s — my %s over in %s who handled the %s.",
		"Can you look up %s and email what you find to %s? They're my %s in %s from the %s.",
		"See where %s stands and send that number to %s, the %s in %s I know from the %s.",
		"Find the live value for %s, then get it over to %s — my %s in %s from the %s.",
		"What's the latest on %s? Email the number to %s, my %s in %s from the %s.",
	}
	prompt := fmt.Sprintf(prompts[index%len(prompts)], needle.Subject, p.Nickname, p.Relation, p.City, p.Context)
	return fuzzyWorldTool(caseID, "world_contact_research_email_result_usage", prompt, []protocol.ToolSpec{{Name: "search_web"}, {Name: "gmail_send", RequiredArgs: map[string]string{"to": p.Email, "body": needle.Value}}}, "resolve the person and current email, research the live value, and send that value to the right person")
}

func v8WorldMemoryDelete(caseID string, world universe.World, index int) protocol.ToolCase {
	p := world.People[index%len(world.People)]
	return fuzzyWorldTool(caseID, "world_memory_delete", fmt.Sprintf("You can bin that temporary note about fixing %s's email after the %s — they're my %s at %s. Just don't lose their actual contact history.", p.Nickname, p.Context, p.Relation, p.Employer), []protocol.ToolSpec{{Name: "delete_memory", RequiredArgs: map[string]string{"pair_id": p.ToolNotePairID}}}, "resolve the uniquely described disposable note and delete that pair without removing canonical contact facts")
}

func v8WorldMemoryUpdate(caseID string, world universe.World, index int) protocol.ToolCase {
	p := world.Projects[index%len(world.Projects)]
	return fuzzyWorldTool(caseID, "world_memory_update", fmt.Sprintf("Add to the handoff note for %q at %s that we're doing the handoff Friday. It's the %s project; update the scratchpad, not the project history.", p.Alias, p.Client, p.Purpose), []protocol.ToolSpec{{Name: "update_memory", RequiredArgs: map[string]string{"pair_id": p.ToolNotePairID, "content": "handoff is Friday"}}}, "resolve the project's mutable handoff note and update it without overwriting canonical project evidence")
}

func v8WorldLinkRead(seed int64, caseID string) protocol.ToolCase {
	needle := toolexec.NeedleForV8World(seed, caseID)
	return fuzzyWorldTool(caseID, "world_link_chain_result_usage", fmt.Sprintf("See what %s is at right now, and open the actual page rather than relying on the search blurb.", needle.Subject), []protocol.ToolSpec{{Name: "search_web"}, {Name: "read_links"}}, "find and read the live source, then report the served value")
}

func v8WorldAgentJob(caseID string, world universe.World, index int) protocol.ToolCase {
	project := world.Projects[index%len(world.Projects)]
	prompt := fmt.Sprintf("Have Ditto Code inspect the %s project for %s and prepare a concise dependency-risk report. Go ahead and start it; I'll approve the job when Ditto asks.", project.Name, project.Client)
	return fuzzyWorldTool(caseID, "world_agent_job_dispatch", prompt, []protocol.ToolSpec{{Name: "execute_agent_job"}}, "submit the requested Ditto Code job for user approval; the app owns approval, progress, and result display")
}

func fuzzyWorldTool(id, category, prompt string, expected []protocol.ToolSpec, behavior string) protocol.ToolCase {
	return protocol.ToolCase{
		ID: id, Category: category, Prompt: prompt, ExpectedTools: expected,
		MaxToolCalls: 15, AllowExtraTools: true, FuzzyTrajectory: true,
		ExpectedBehavior: behavior + "; tool order and harmless exploratory reads are not prescribed",
	}
}

func misspellAlias(s string, salt int) string {
	projected, _ := textnoise.Project(s, int64(salt), "alias:"+s, textnoise.Options{MaxEdits: 1})
	return projected
}

// applyV8WritingNoise projects ordinary mobile-keyboard typos and common
// grammatical errors onto a stable share of every semantic tool domain. It also
// projects user-authored prerequisite transcripts, except long stories (their
// structured compiler owns fact-safe projection). Exact required arguments and
// machine-like values stay canonical.
func applyV8WritingNoise(seed int64, cases []protocol.ToolCase) map[string]int {
	coverage := map[string]int{}
	byDomain := map[string][]string{}
	for _, tc := range cases {
		domain := toolWritingDomain(tc.Category)
		byDomain[domain] = append(byDomain[domain], tc.ID)
	}
	selected := map[string]bool{}
	for domain, ids := range byDomain {
		for id := range textnoise.Select(seed, "tool:"+domain, ids, 2_500) {
			selected[id] = true
		}
	}
	for i := range cases {
		if !selected[cases[i].ID] {
			continue
		}
		var protected []string
		for _, spec := range cases[i].ExpectedTools {
			for _, value := range spec.RequiredArgs {
				protected = append(protected, value)
			}
		}
		protected = append(protected, cases[i].WritingProtected...)
		projected, stats := textnoise.Project(cases[i].Prompt, seed, "tool:"+cases[i].ID, textnoise.Options{
			Grammar: true, MaxEdits: 1, Protected: protected,
		})
		if stats.Total() > 0 {
			cases[i].Prompt = projected
			coverage["prompt:"+toolWritingDomain(cases[i].Category)]++
		}
	}

	// Select prerequisite pairs globally by pair identity. A pair repeated on
	// multiple cases receives byte-identical noise because its projection key is
	// the pair id, not its attachment position.
	pairIDs := map[string][]string{}
	for _, tc := range cases {
		for _, pair := range tc.PrerequisitePairs {
			if strings.HasPrefix(pair.SessionID, "story-") {
				continue
			}
			domain := pairWritingDomain(pair.SessionID)
			pairIDs[domain] = append(pairIDs[domain], pair.PairID)
		}
	}
	selectedPairs := map[string]bool{}
	for domain, ids := range pairIDs {
		for id := range textnoise.Select(seed, "pair:"+domain, ids, 2_500) {
			selectedPairs[id] = true
		}
	}
	for i := range cases {
		for j := range cases[i].PrerequisitePairs {
			pair := &cases[i].PrerequisitePairs[j]
			if !selectedPairs[pair.PairID] || strings.HasPrefix(pair.SessionID, "story-") {
				continue
			}
			projected, stats := textnoise.Project(pair.Prompt, seed, "pair:"+pair.PairID, textnoise.Options{Grammar: true, MaxEdits: 1, Protected: cases[i].WritingProtected})
			if stats.Total() > 0 {
				pair.Prompt = projected
				coverage["pair:"+pairWritingDomain(pair.SessionID)]++
			}
		}
	}
	return coverage
}

func toolWritingDomain(category string) string {
	switch {
	case strings.HasPrefix(category, "world_contact"), strings.HasPrefix(category, "world_memory"):
		return "personal"
	case strings.HasPrefix(category, "world_business"), strings.Contains(category, "workflow"), strings.Contains(category, "job"):
		return "business"
	case strings.Contains(category, "web"), strings.Contains(category, "link"), strings.Contains(category, "research"):
		return "research"
	case strings.Contains(category, "setting"), strings.Contains(category, "theme"), strings.HasPrefix(category, "set_"):
		return "settings"
	default:
		return "general"
	}
}

func pairWritingDomain(sessionID string) string {
	switch {
	case strings.HasPrefix(sessionID, "people-"), strings.HasPrefix(sessionID, "trip-"), strings.Contains(sessionID, "personal"):
		return "personal"
	case strings.HasPrefix(sessionID, "project-"), strings.Contains(sessionID, "business"):
		return "business"
	default:
		return "general"
	}
}

// categoriesForVersion returns the tool category set for a bench version. v5 adds
// the Code Mode categories and v7 the difficulty categories; earlier versions get
// exactly the historical set, so their dataset bytes (and the known-vector
// hashes) are unchanged.
func categoriesForVersion(benchVersion int) []category {
	if benchVersion < protocol.BenchVersionV5 {
		return categories
	}
	out := make([]category, 0, len(categories)+len(codeModeCategories)+len(difficultyCategoriesV7))
	out = append(out, categories...)
	out = append(out, codeModeCategories...)
	if benchVersion >= protocol.BenchVersionV7 {
		out = append(out, difficultyCategoriesV7...)
	}
	if benchVersion >= protocol.BenchVersionV8 {
		// ChatV2 consolidated recipes, automations, and multi-agent creation into
		// workflows. Keep historical categories intact for v7, but advertise and
		// grade the current product tools in v8.
		for i := range out {
			switch out[i].name {
			case "code_compute":
				// V8 keeps each grammar slot type-consistent. Earlier versions retain
				// the frozen mixed pool byte-for-byte.
				out[i].grammar = persona.Grammar{
					"root": {
						"#lead# take #a#, #b#, and #c#, then give me their sum, average, and the spread from lowest to highest.",
						"#lead# my last three readings were #a#, #b#, and #c#. What is the mean, and how far apart are the high and low?",
					},
					"lead": {"Quick calculation:", "Can you crunch this for me?", "Help me work this out:"},
					"a":    {"1240", "312", "48.5", "1024", "96"},
					"b":    {"980", "277", "51.2", "18", "2048"},
					"c":    {"1510", "298", "46.9", "512", "74"},
				}
			case "workflow_not_job":
				out[i].tool = "create_workflow"
				out[i].argKey = ""
				out[i].templates = []string{
					"Build a reusable workflow for %s, with the independent parts running in parallel.",
					"Set up %s as a workflow I can inspect and run again.",
				}
			case "agent_workflow":
				out[i].tool = "create_workflow"
				out[i].argKey = ""
				out[i].templates = []string{
					"Create a workflow for %s.",
					"Turn %s into a reusable multi-step workflow.",
				}
			case "automation_not_job":
				out[i].tool = "create_workflow"
				out[i].argKey = ""
				out[i].templates = []string{
					"Create a workflow that sends my news digest %s.",
					"Set up my standup summary to run %s.",
				}
			case "recipe_create":
				out[i].tool = "create_workflow"
				out[i].argKey = ""
				out[i].templates = []string{
					"Make a reusable workflow called %s.",
					"Create a workflow named %s that I can run again.",
				}
			case "automation_list":
				out[i].tool = "list_schedules"
				out[i].grammar = nil
				out[i].templates = []string{
					"What workflows are scheduled to run?",
					"Show me my upcoming automatic runs.",
				}
			case "recipe_apply":
				out[i].tool = ""
				out[i].tools = []string{"list_workflows", "run_workflow"}
				out[i].argKey = ""
				out[i].templates = []string{
					"Run my %s workflow.",
					"Start the saved workflow called %s.",
				}
			case "set_tool_prefs":
				// Production takes individual boolean toggles, not one magic
				// preference string. The tool choice is deterministic; multiple
				// equivalent boolean payloads remain legitimate.
				out[i].argKey = ""
			case "agent_job", "feedback", "calendar_create", "calendar_search":
				// These schemas contain free-form language. Tool choice and the
				// surrounding deterministic state are scoreable; insisting on one
				// generated sentence would penalize valid LLM paraphrases.
				out[i].argKey = ""
			}
		}
	}
	return out
}

// GenerateCases emits n raw tool cases from an existing RNG. Exported so the gen
// package can build tool cases from the same templated ground truth. seed drives
// both the stable case IDs and each result-usage prompt's fabricated needle
// subject (via toolexec.NeedleFor), which must match the needle the mock endpoint
// serves.
func GenerateCases(r *rand.Rand, seed int64, n int) []protocol.ToolCase {
	cases, _ := GenerateCasesWithFillers(r, seed, n)
	return cases
}

// GenerateCasesWithFillers is GenerateCases plus, for each case, the concrete
// entity ("filler") substituted into its template. Uses the historical (pre-v5)
// category set; canonical versioned generation uses the ForVersion variant.
func GenerateCasesWithFillers(r *rand.Rand, seed int64, n int) ([]protocol.ToolCase, []string) {
	return GenerateCasesWithFillersForVersion(r, seed, n, protocol.BenchVersionV2)
}

// GenerateCasesWithFillersForVersion is GenerateCasesWithFillers under an explicit
// benchmark contract: v5 draws from the Code Mode-extended category set, earlier
// versions from the historical set (byte-identical to before). The filler is the
// ground-truth entity the prompt is about, exposed so a caller can assert the
// prompt and the scored ground truth stay coupled.
func GenerateCasesWithFillersForVersion(r *rand.Rand, seed int64, n, benchVersion int) ([]protocol.ToolCase, []string) {
	if n < 1 {
		n = 1
	}
	cats := categoriesForVersion(benchVersion)
	var order []int
	if benchVersion >= protocol.BenchVersionV9 {
		weights := make([]int, len(cats))
		mandatory := -1
		for i, c := range cats {
			weights[i] = toolCategoryWeightV7(c.name)
			if c.name == "set_effort" {
				mandatory = i
			}
		}
		order = sampledCategoryOrderV9(toolCategoryMixRNG(seed, benchVersion, n), n, weights, mandatory)
	} else if benchVersion >= protocol.BenchVersionV8 {
		weights := make([]int, len(cats))
		for i, c := range cats {
			weights[i] = toolCategoryWeightV7(c.name)
		}
		order = sampledCategoryOrderV8(r, n, weights)
	} else if benchVersion >= protocol.BenchVersionV7 {
		weights := make([]int, len(cats))
		for i, c := range cats {
			weights[i] = toolCategoryWeightV7(c.name)
		}
		order = stratifiedCategoryOrderWeighted(r, n, weights)
	} else {
		order = stratifiedCategoryOrder(r, n, len(cats))
	}
	cases := make([]protocol.ToolCase, 0, n)
	fillers := make([]string, 0, n)
	for i := 0; i < n; i++ {
		cat := cats[order[i]]
		var tmpl string
		if cat.grammar != nil {
			tmpl = persona.Expand(r, cat.grammar, "root")
		} else {
			tmpl = cat.templates[r.Intn(len(cat.templates))]
		}
		caseID := protocol.OpaqueCaseID(seed, "tool", i)
		// Result-usage cases: the filler is the fixture needle's Subject, derived
		// from the SAME (seed, caseID) the mock server uses to serve the answer, so
		// the question ("...figure on the Veltrix index...") and the served fact
		// ("the Veltrix index reached 3,418 points") are always coherent.
		var filler string
		if IsResultUsage(cat.name) {
			filler = toolexec.NeedleFor(seed, caseID).Subject
		} else {
			filler = fillerForVersion(r, cat.name, benchVersion)
		}
		prompt := tmpl

		// Expected tool sequence: multi-hop tools, else the single tool, else none.
		seq := cat.tools
		if len(seq) == 0 && cat.tool != "" {
			seq = []string{cat.tool}
		}

		// argValue is the value pinned into RequiredArgs (defaults to the filler; an
		// intent variant overrides it). usedFiller is the load-bearing entity the
		// prompt is about ("" = the prompt has no such entity).
		argValue := filler
		usedFiller := ""
		intents := cat.intents
		useIntent := len(intents) > 0 && r.Intn(2) == 0
		if benchVersion >= protocol.BenchVersionV8 && cat.argKey != "" {
			intents = v8ArgIntents[cat.name]
			useIntent = len(intents) > 0
		}
		switch {
		case useIntent:
			it := intents[r.Intn(len(intents))]
			prompt = it.prompt
			argValue = it.value
			// Only a near-miss intent (value appears literally) records the token; a
			// pure-intent prompt has no load-bearing entity to track.
			if strings.Contains(it.prompt, it.value) {
				usedFiller = it.value
			}
		case strings.Contains(tmpl, "%s"):
			prompt = fmt.Sprintf(tmpl, filler)
			// A real tool case has a load-bearing entity; no_tool / abstention fillers
			// ARE the whole message.
			if len(seq) > 0 {
				usedFiller = filler
			}
		}
		// Surface variation for wrap-flagged categories: the shared Tier-1a
		// lead-in/trailer engine, protecting a leading pinned-argument token from
		// the lowercase pass and leaving the intent untouched.
		if cat.wrap {
			prompt = persona.Wrap(r, prompt, argValue, promptLeadIns, promptTrailers)
		}

		tc := protocol.ToolCase{
			ID:              caseID,
			Category:        cat.name,
			Prompt:          prompt,
			AllowExtraTools: cat.allowExtra,
		}

		switch {
		case len(seq) == 0:
			tc.ExpectedTools = nil
			tc.MaxToolCalls = 0
			switch cat.name {
			case "abstention":
				tc.ExpectedBehavior = "answer or abstain without calling any tool"
			case "arg_hallucination":
				tc.ExpectedBehavior = "ask for the missing detail; do not call a tool with a fabricated argument"
			default:
				tc.ExpectedBehavior = "respond conversationally without calling any tool"
			}
		case len(seq) == 1:
			ts := protocol.ToolSpec{Name: seq[0]}
			// Exact-value arg ground truth: the filler for a verbatim case, or the
			// resolved value for an intent case (which may not appear in the prompt).
			if cat.argKey != "" && argValue != "" && (useIntent || usedFiller != "") {
				ts.RequiredArgs = map[string]string{cat.argKey: argValue}
			}
			tc.ExpectedTools = []protocol.ToolSpec{ts}
			tc.MaxToolCalls = 1
			tc.ExpectedBehavior = fmt.Sprintf("call %s exactly once", seq[0])
		default:
			tools := make([]protocol.ToolSpec, len(seq))
			for j, tn := range seq {
				tools[j] = protocol.ToolSpec{Name: tn}
			}
			tc.ExpectedTools = tools
			tc.MaxToolCalls = len(seq)
			if cat.unordered {
				tc.Unordered = true
				tc.ExpectedBehavior = "call " + strings.Join(seq, " and ") + " (in any order)"
			} else {
				tc.ExpectedBehavior = "call " + strings.Join(seq, " then ") + " in that order"
			}
		}

		if benchVersion >= protocol.BenchVersionV8 && cat.name == "memory_fetch" {
			pairID := protocol.OpaqueCaseID(seed, "tool-memory-fetch", i)
			phone := fmt.Sprintf("+1-212-555-%04d", r.Intn(10000))
			questions := []string{
				"What is the phone number of my accountant for 2024?",
				"Can you find the number for the accountant who handled my 2024 taxes?",
				"I need to call my 2024 accountant — what number do I have saved?",
			}
			tc.Prompt = questions[r.Intn(len(questions))]
			tc.ExpectedTools = []protocol.ToolSpec{
				{Name: "search_memories"},
				{Name: "fetch_memories", RequiredArgs: map[string]string{"pairIds": pairID}},
			}
			tc.MaxToolCalls = 2
			tc.ExpectedBehavior = "search for the relevant memory, then fetch the selected memory in full"
			tc.PrerequisitePairs = []protocol.MemoryPair{{
				PairID:    pairID,
				SessionID: fmt.Sprintf("accountant-%d", i),
				Timestamp: "2025-04-15T14:00:00Z",
				Prompt:    "Please remember that Morgan Lee handled my 2024 taxes.",
				Response:  "Morgan Lee's office number is " + phone + ".",
			}}
			usedFiller = ""
		}
		if benchVersion >= protocol.BenchVersionV8 && cat.name == "stale_context_web" {
			pairID := protocol.OpaqueCaseID(seed, "tool-stale-context", i)
			tc.PrerequisitePairs = []protocol.MemoryPair{{
				PairID: pairID, SessionID: fmt.Sprintf("current-context-%d", i), Timestamp: "2025-02-11T10:00:00Z",
				Prompt:   fmt.Sprintf("I was reading about %s last year and saved a few notes, but I know they may be out of date now.", filler),
				Response: "I’ll remember that as background, and I’ll check current sources whenever you ask what changed.",
			}}
		}
		if benchVersion >= protocol.BenchVersionV8 {
			applyV8CapabilityResolution(&tc, argValue, i)
		}

		cases = append(cases, tc)
		fillers = append(fillers, usedFiller)
	}
	if benchVersion >= protocol.BenchVersionV9 {
		applyV9WorldActions(seed, cases)
	} else if benchVersion >= protocol.BenchVersionV8 {
		applyV8WorldActions(seed, cases)
	}
	if benchVersion >= protocol.BenchVersionV10 {
		applyV10StateDependentActions(seed, benchVersion, cases)
	}
	if benchVersion >= protocol.BenchVersionV8 {
		applyV8WritingNoise(seed, cases)
		applyV8AssistantVoice(seed, cases)
	}
	return cases, fillers
}

func applyV8AssistantVoice(seed int64, cases []protocol.ToolCase) {
	userName := universe.UserName(seed)
	for i := range cases {
		for j := range cases[i].PrerequisitePairs {
			pair := &cases[i].PrerequisitePairs[j]
			pair.Prompt = uservoice.Render(pair.Prompt)
			pair.Response = assistantvoice.Render(seed, pair.PairID, pair.SessionID, userName, pair.Prompt, pair.Response)
		}
	}
}

// applyV8CapabilityResolution makes closed product choices behave like smart
// tools: users name a model family or an approximate appearance choice, then the
// agent may inspect the current catalog before applying the canonical value.
// The wire tool names remain the production ChatV2 names so v7 harnesses stay
// compatible; only v8's case semantics become outcome-driven.
func applyV8CapabilityResolution(tc *protocol.ToolCase, value string, salt int) {
	if len(tc.ExpectedTools) != 1 {
		return
	}
	var prompt string
	switch tc.Category {
	case "set_model":
		family := "GPT"
		if strings.Contains(strings.ToLower(value), "gemini") {
			family = "Gemini"
		} else if strings.Contains(strings.ToLower(value), "claude") {
			family = "Claude"
		}
		prompt = fmt.Sprintf("Use %s for my main chats. I don't know its exact model id, so resolve the current available option instead of making me type a slug.", family)
	case "settings":
		if value == "system" {
			prompt = "Match Ditto's color mode to my device. Check the available appearance settings if you need the canonical option."
		} else {
			prompt = fmt.Sprintf("Switch Ditto to %s-ish mode; check the available appearance options rather than guessing the setting name.", misspellAlias(value, salt))
		}
	case "set_accent":
		prompt = fmt.Sprintf("Make the app accent %s-ish. I may have mangled the spelling, so inspect the available appearance options first.", misspellAlias(value, salt))
	case "set_font":
		prompt = fmt.Sprintf("Use %s in chat. Treat that case-insensitively and check the available font options if the name is slightly off.", misspellAlias(value, salt))
	default:
		return
	}
	tc.Prompt = prompt
	tc.ExpectedTools = append([]protocol.ToolSpec{{Name: "discover_capabilities"}}, tc.ExpectedTools...)
	tc.MaxToolCalls = 15
	tc.AllowExtraTools = true
	tc.Unordered = false
	tc.FuzzyTrajectory = true
	tc.ExpectedBehavior = "resolve the user's approximate choice against current capabilities and apply the canonical setting; exploratory order is not prescribed"
}

// v11ToolPick is the seeded per-case surface chooser for the v11 rotation of
// the state-dependent routing family. It hashes outside the shared toolMixRNG
// stream so route outcomes stay byte-identical to v10.
func v11ToolPick(seed int64, index int, salt string, bank []string) string {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v11-tool:%d:%d:%s", seed, index, salt)
	return bank[h.Sum64()%uint64(len(bank))]
}
