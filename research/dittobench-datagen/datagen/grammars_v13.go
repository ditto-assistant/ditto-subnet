package datagen

import "github.com/ditto-assistant/dittobench-datagen/persona"

// Bench v13 tool-surface grammars. Every category that still rendered from a
// flat template list in v12 renders from a persona.Grammar here, so no tool
// family exposes a handful of memorizable literal frames. Each root keeps the
// category's routing intent (a status check never reads as a dispatch, an edit
// never reads as a create, a result-usage prompt always asks for the served
// figure) and, for a filler category, carries exactly one %s so the pinned
// argument/needle fill and the RequiredArgs contract are unchanged. Banks are
// frozen with the contract; enlarging one changes bytes and needs a new version.
// At generation time each grammar passes through persona.SeedBank, so one
// dataset draws from a per-seed subset of every non-root bank.

// v13CategoryGrammars maps a category name to its v13 prompt grammar. Grammar
// categories that already existed (agent_read_not_run, image_edit_not_create,
// memory_fetch, capability_discovery, the Code Mode trio, negation_no_tool) keep
// their reviewed grammars and are absent here.
var v13CategoryGrammars = map[string]persona.Grammar{
	"memory_lookup": {
		"root": {
			"#recall# %s?",
			"#recall# %s — #memcue#.",
			"What #haveisaid# about %s?",
			"#memcue2#: what do you have on %s?",
			"Bring back whatever I told you about %s.",
			"Dig up my notes on %s, #please#.",
		},
		"recall":    {"Remind me about", "What did I say about", "Do you remember", "Jog my memory on", "What do you have saved about"},
		"haveisaid": {"have I said", "did I tell you", "have I mentioned", "did I note down"},
		"memcue":    {"I mentioned it before", "it should be in my notes", "we talked about it a while back", "I told you earlier"},
		"memcue2":   {"From what I've told you", "Out of my own notes", "From our earlier chats", "From what you've stored for me"},
		"please":    {"please", "when you can", "if you would"},
	},
	"memory_subject": {
		"root": {
			"#which# of my #topics# #cover# %s?",
			"Find the #topic# that #covers# %s.",
			"What #topics# do I have #noteson# %s?",
			"#which# #topic# groups everything about %s?",
			"Point me at the #topic# where %s lives.",
		},
		"which":   {"Which", "What"},
		"topics":  {"subjects", "topics", "topic clusters", "note groups"},
		"topic":   {"subject", "topic", "cluster"},
		"cover":   {"cover", "mention", "touch on", "relate to"},
		"covers":  {"covers", "mentions", "touches on", "relates to"},
		"noteson": {"notes on", "saved about", "filed under", "stored around"},
	},
	"web_search": {
		"root": {
			"#searchverb# %s.",
			"What's the latest on %s?",
			"Find #recent# #news# about %s.",
			"#searchverb# %s and tell me what's #current#.",
			"What are people saying about %s #now#?",
			"Pull up #recent# #news# on %s for me.",
		},
		"searchverb": {"Search the web for", "Look up", "Google", "Run a web search on", "Find me the latest on", "Check online for"},
		"recent":     {"recent", "the latest", "this week's", "fresh", "up-to-date"},
		"news":       {"news", "coverage", "reporting", "headlines", "articles"},
		"current":    {"current", "out there right now", "being reported", "new"},
		"now":        {"now", "these days", "at the moment", "lately"},
	},
	"link_read": {
		"root": {
			"#readverb# %s and #summ#.",
			"What does this page say: %s",
			"Open %s and #tellme# the #points#.",
			"#readverb# %s — #summ2#.",
			"Here's a link, %s — #tellme# what's on it.",
		},
		"readverb": {"Read", "Open", "Go through", "Skim", "Pull up"},
		"summ":     {"summarize it", "give me the gist", "boil it down", "summarize the key parts"},
		"summ2":    {"just the main takeaways", "the short version please", "a quick summary is fine"},
		"tellme":   {"tell me", "give me", "walk me through", "lay out"},
		"points":   {"main points", "key points", "highlights", "core arguments"},
	},
	"image_create": {
		"root": {
			"#genverb# an image of %s.",
			"#genverb# a picture of %s.",
			"Draw me %s.",
			"I'd love #artstyle# of %s — #genverb2# one.",
			"#genverb# %s as an image.",
			"Make me #artstyle# showing %s.",
		},
		"genverb":  {"Generate", "Create", "Render", "Paint", "Make"},
		"genverb2": {"generate", "create", "render", "make"},
		"artstyle": {"an illustration", "a picture", "a rendering", "an image", "a piece of art"},
	},
	"artifacts_create": {
		"root": {
			"Build me %s.",
			"Make %s I can #preview#.",
			"#createverb# %s as an interactive artifact.",
			"Can you #createverb2# %s I can #preview#?",
			"Put together %s — something #runnable#.",
			"I want %s as a #live# artifact.",
		},
		"preview":     {"preview", "open and try", "click around in", "see rendered"},
		"createverb":  {"Create", "Build", "Spin up", "Assemble"},
		"createverb2": {"build", "create", "put together", "mock up"},
		"runnable":    {"I can preview live", "that renders right here", "I can interact with", "I can actually try"},
		"live":        {"live", "working", "clickable", "rendered"},
	},
	"agent_job": {
		"root": {
			"#runverb# a background job to %s.",
			"Kick off an agent to %s.",
			"#dispatchverb# a task to %s.",
			"Send an agent off to %s #inbg#.",
			"#runverb# something #inbg# that will %s.",
			"Have a worker %s — #inbg#.",
		},
		"runverb":      {"Run", "Start", "Launch", "Fire off"},
		"dispatchverb": {"Dispatch", "Hand off", "Queue up", "Delegate"},
		"inbg":         {"in the background", "as a background task", "asynchronously", "while I keep working"},
	},
	"route_memory_not_web": {
		"root": {
			"#searchverb# what I told you about %s.",
			"Look up %s — I #mentioned# it before.",
			"Find that thing I #saved# about %s.",
			"#searchverb# %s in what I've #told# you, not online.",
			"Can you pull what I #saved# on %s?",
			"I #mentioned# %s a while ago — find it.",
		},
		"searchverb": {"Search for", "Look for", "Find", "Dig up"},
		"mentioned":  {"mentioned", "brought up", "talked about", "told you about"},
		"saved":      {"saved", "noted", "filed away", "stored"},
		"told":       {"told", "shared with", "said to"},
	},
	"route_web_not_memory": {
		"root": {
			"Remind me what the #latest# #news# on %s is.",
			"Do you recall the current #price# of %s?",
			"What's the #latest# status of %s right now?",
			"Remember to tell me what's #happening# with %s #today#.",
			"Do you know where %s stands #today#? I need the #latest#.",
			"Recall for me the #latest# on %s — as of #today#.",
		},
		"latest":    {"latest", "up-to-date", "current", "newest"},
		"news":      {"news", "word", "reporting", "coverage"},
		"price":     {"price", "state", "figure", "standing"},
		"happening": {"happening", "going on", "being reported", "changing"},
		"today":     {"today", "right now", "this minute", "at the moment"},
	},
	"agent_run_not_read": {
		"root": {
			"Go ahead and %s for me #now#.",
			"Please actually %s, #notjust#.",
			"Start working on this: %s.",
			"#now2#, %s — #notjust#.",
			"I want you to %s. #begin#.",
			"Get going and %s; #begin#.",
		},
		"now":     {"now", "right away", "today", "as soon as you can"},
		"notjust": {"don't just tell me how", "not a plan, the real thing", "I don't want an explanation, I want it done", "no walkthrough needed"},
		"now2":    {"Right now", "Today", "This afternoon", "When you're ready"},
		"begin":   {"Begin", "Kick it off", "Start it", "Go"},
	},
	"workflow_not_job": {
		"root": {
			"Build a reusable workflow for %s, with the #parts# running #inparallel#.",
			"Set up %s as a workflow I can #inspect# and run again.",
			"Turn %s into a #reusable# workflow — the #parts# #inparallel#.",
			"I want %s as a #reusable# workflow, not a one-off; run the #parts# #inparallel#.",
			"Design a workflow that produces %s and keeps its #parts# #inparallel#.",
		},
		"parts":      {"independent parts", "separate pieces", "distinct angles", "sub-steps"},
		"inparallel": {"in parallel", "side by side", "at the same time", "concurrently"},
		"inspect":    {"inspect", "review", "look over", "open up"},
		"reusable":   {"reusable", "repeatable", "saved", "standing"},
	},
	"no_tool": {
		"root": {
			"#greet#",
			"#thanks#",
			"#small#",
			"#greet# #small#",
			"#small# #thanks#",
		},
		"greet":  {"hey, how's it going?", "good morning!", "gm! ready when you are", "just saying hi", "hi there, hope your day's going well", "morning — quick hello before I dive in"},
		"thanks": {"thanks, that was helpful!", "appreciate you", "you're awesome", "cheers for earlier", "that was exactly what I meant, thanks", "nice one, thank you"},
		"small":  {"tell me a joke", "what's your favorite color?", "lol nice", "haha that one made me laugh", "ok cool cool cool", "nice, that's exactly what I meant", "how's your day been?", "any fun plans this weekend?"},
	},
	"abstention": {
		"root": {
			"#unknowable#",
			"#personal#",
			"#unknowable# #hedge#",
			"#personal# #hedge#",
		},
		"unknowable": {"what's the meaning of life?", "what will the weather be like next year?", "who will win the next election?", "what number am I thinking of?", "will it rain on my wedding day next June?", "how long am I going to live?", "what am I thinking right now?"},
		"personal":   {"should I quit my job?", "do you love me?", "will my startup succeed?", "is my sister secretly mad at me?", "did my neighbor take my package?", "am I making the right call moving cities?"},
		"hedge":      {"be honest.", "no guessing please.", "just tell me straight.", "I really want to know."},
	},
	"arg_hallucination": {
		"root": {
			"#changeverb# my #setting#.",
			"#changeverb# my #setting#, please.",
			"#readverb# that #doc# and summarize it.",
			"File that as feedback for the team.",
			"Spin up a parallel agent workflow for me.",
			"Put it on my calendar.",
			"Email them the update.",
			"Delete that memory for me.",
			"Run my saved workflow.",
			"#changeverb# the #setting# — you know the one.",
			"Send it over to them.",
			"Add that to my schedule.",
		},
		"changeverb": {"Change", "Switch", "Set", "Update", "Adjust"},
		"setting":    {"theme", "main model", "reasoning effort", "accent color", "chat font"},
		"readverb":   {"Read", "Open", "Go through"},
		"doc":        {"link", "page", "article"},
	},
	"agent_workflow": {
		"root": {
			"Create a workflow for %s.",
			"Turn %s into a #reusable# multi-step workflow.",
			"#setup# a #reusable# workflow that handles %s.",
			"I need %s as a workflow with #steps#.",
			"Make %s a #reusable# workflow I can #runagain#.",
		},
		"reusable": {"reusable", "repeatable", "saved", "standing"},
		"setup":    {"Set up", "Build", "Define", "Put together"},
		"steps":    {"clear steps", "separate stages", "its own steps", "a few defined steps"},
		"runagain": {"run again", "trigger later", "reuse", "kick off whenever"},
	},
	"feedback": {
		"root": {
			"Send this to the Ditto team: %s.",
			"File feedback for the team — %s.",
			"Report to the devs that %s.",
			"#passalong# to whoever builds this: %s.",
			"Let the team know %s.",
			"#feedbacklead# %s. #passalong2#",
		},
		"passalong":    {"Pass this along", "Forward this note", "Flag this", "Relay this"},
		"feedbacklead": {"Bit of feedback:", "For the product folks:", "Quick note for the team:", "Something to log:"},
		"passalong2":   {"Please send it their way.", "Get that to the devs.", "Log it as feedback.", "Pass it on."},
	},
	// settings / set_model / set_effort render their v13 intents
	// (v13ArgIntentGrammars) whenever the run pins an exact argument; these
	// grammars cover the verbatim-value frame the category would otherwise use.
	"settings": {
		"root": {
			"Switch to %s mode.",
			"Set my theme to %s.",
			"Change the app theme to %s.",
			"#flip# the whole app to %s mode.",
			"Put the interface in %s mode, please.",
		},
		"flip": {"Flip", "Move", "Switch", "Take"},
	},
	"set_model": {
		"root": {
			"Switch my chat model to %s.",
			"Use %s as my main model.",
			"Change my primary model to %s.",
			"#move# my everyday chats onto %s.",
			"Make %s the model behind my main chats.",
		},
		"move": {"Move", "Put", "Shift", "Switch"},
	},
	"set_effort": {
		"root": {
			"Set reasoning effort to %s.",
			"Make responses use %s effort.",
			"Change the thinking level to %s.",
			"Dial the reasoning effort to %s.",
			"Run my questions at %s reasoning effort.",
		},
	},
	"set_tool_prefs": {
		"root": {
			"Update my tool preferences: %s.",
			"Change my chat tools so you %s.",
			"Adjust which tools you use — %s.",
			"Going forward, %s — #savepref#.",
			"Tool settings: %s. #savepref#",
			"From now on, %s in our chats.",
		},
		"savepref": {"save that as my preference", "make that my default", "keep that setting", "lock that in"},
	},
	"automation_not_job": {
		"root": {
			"Create a workflow that sends my news digest %s.",
			"Set up my standup summary to run %s.",
			"I want my digest #delivered# %s — make that a workflow.",
			"Have my summary go out %s, on its own #timer#.",
			"Put my news digest on a #timer#: %s.",
		},
		"delivered": {"delivered", "sent", "posted", "compiled"},
		"timer":     {"timer", "schedule", "repeat", "cadence"},
	},
	"automation_list": {
		"root": {
			"What workflows are #ontimer# to run?",
			"Show me my upcoming #autoruns#.",
			"Which of my workflows #fire# on their own?",
			"#showverb# whatever is set to run #ontimer2#.",
			"What do I have #ontimer2# right now?",
			"Remind me which #autoruns# are coming up.",
		},
		"ontimer":  {"queued", "set", "lined up", "due"},
		"autoruns": {"automatic runs", "timed runs", "recurring runs", "hands-off runs"},
		"fire":     {"fire", "kick off", "go", "trigger"},
		"showverb": {"Show me", "Pull up", "Run down", "Lay out"},
		"ontimer2": {"on a timer", "automatically", "on repeat", "on a cadence"},
	},
	"recipe_create": {
		"root": {
			"Make a reusable workflow called %s.",
			"Create a workflow named %s that I can run again.",
			"Save a #reusable# workflow under the name %s.",
			"Set up %s as a #reusable# workflow for later.",
			"I want a workflow called %s I can #rerun#.",
		},
		"reusable": {"reusable", "repeatable", "standing", "saved"},
		"rerun":    {"rerun", "trigger again", "reuse", "run whenever"},
	},
	"recipe_apply": {
		"root": {
			"Run my %s workflow.",
			"Start the saved workflow called %s.",
			"#kick# my %s workflow #now#.",
			"Please #kick2# the %s workflow I saved.",
			"Time to run %s — the saved workflow.",
		},
		"kick":  {"Kick off", "Trigger", "Fire up", "Launch"},
		"kick2": {"run", "trigger", "start", "launch"},
		"now":   {"now", "please", "when you can", "right away"},
	},
	"memory_save_not_search": {
		"root": {
			"Remember that %s.",
			"Note for later: %s.",
			"Please keep in mind that %s.",
			"#savelead# %s.",
			"Just so you know, %s — #saveask#.",
			"FYI %s. #saveask2#",
		},
		"savelead": {"For the record,", "Filing this away:", "Heads up for future reference —", "Store this:"},
		"saveask":  {"hang on to that", "keep that on file", "remember it for me", "make a note"},
		"saveask2": {"Keep that in mind.", "Hold onto that.", "Remember it.", "Note it down."},
	},
	"memory_update": {
		"root": {
			"Update memory %s with my new address.",
			"Change what you saved in memory %s.",
			"Correct memory %s to the new value.",
			"Memory %s is out of date — #fixverb# it.",
			"#fixverb2# the entry stored as %s.",
		},
		"fixverb":  {"fix", "revise", "correct", "amend"},
		"fixverb2": {"Revise", "Amend", "Correct", "Fix up"},
	},
	"memory_delete": {
		"root": {
			"Delete memory %s.",
			"Forget what's in memory %s.",
			"Remove memory %s from my history.",
			"#dropverb# the entry saved as %s.",
			"I don't need %s anymore — #dropverb2# it.",
		},
		"dropverb":  {"Drop", "Erase", "Clear out", "Scrub"},
		"dropverb2": {"erase", "clear", "scrub", "drop"},
	},
	"calendar_create": {
		"root": {
			"Put %s on my calendar.",
			"Add %s to my calendar.",
			"Schedule %s on my calendar.",
			"#bookverb# %s for me.",
			"Can you #bookverb2# %s into my calendar?",
			"I need %s on the calendar — #bookverb2# it.",
		},
		"bookverb":  {"Book", "Pencil in", "Block out", "Slot in"},
		"bookverb2": {"pencil", "slot", "get", "put"},
	},
	"calendar_search": {
		"root": {
			"What's on my calendar about %s?",
			"Find the %s event on my calendar.",
			"Search my calendar for %s.",
			"#lookverb# my calendar — anything on %s?",
			"When is the %s thing on my calendar?",
			"Do I have %s #booked#?",
		},
		"lookverb": {"Check", "Look through", "Scan", "Go through"},
		"booked":   {"booked", "scheduled in", "on the calendar", "blocked off"},
	},
	"email_send": {
		"root": {
			"Email %s to let them know I'll be late.",
			"Send an email to %s with the update.",
			"Shoot %s a quick note about the meeting.",
			"#mailverb# %s — #mailwhat#.",
			"Can you #mailverb2# %s #mailwhat#?",
		},
		"mailverb":  {"Drop a line to", "Write to", "Ping", "Message"},
		"mailverb2": {"email", "write to", "message", "ping"},
		"mailwhat":  {"just say I'm running behind", "a short update on the meeting", "let them know the plan changed", "a heads-up about tomorrow"},
	},
	"set_accent": {
		"root": {
			"Set my accent color to %s.",
			"Make the accent color %s.",
			"Change the app accent to %s.",
			"#accentverb# my accent to %s.",
			"I'd like a %s accent in the app.",
		},
		"accentverb": {"Switch", "Flip", "Move", "Update"},
	},
	"set_font": {
		"root": {
			"Change my chat font to %s.",
			"Use %s as the chat font.",
			"Set the chat typeface to %s.",
			"#fontverb# the chat font to %s.",
			"I'd rather read chat in %s — #fontverb2# it.",
		},
		"fontverb":  {"Switch", "Flip", "Move", "Update"},
		"fontverb2": {"switch", "set", "change", "use"},
	},
	"multi_web_read": {
		"root": {
			"Look up %s online and open the top result.",
			"Find a page about %s and read it for me.",
			"Search for %s and #summ# the first source.",
			"#searchverb# %s, then open the #top# hit and #summ# it.",
			"Do a web search on %s and read the #top# page.",
		},
		"searchverb": {"Search", "Google", "Look up", "Run a search on"},
		"summ":       {"summarize", "give me the gist of", "boil down", "recap"},
		"top":        {"top", "first", "leading", "best"},
	},
	"parallel_web_image": {
		"root": {
			"Search the web for %s and also generate an image of it.",
			"Look up %s online, and separately make me a picture of it.",
			"Two things: find the latest on %s and create an image of it.",
			"#searchverb# %s and, #separately#, #genverb# an image of it.",
			"I want both: web results on %s and #genverb2# a picture of it.",
		},
		"searchverb": {"Search for", "Google", "Look up", "Run a search on"},
		"separately": {"separately", "in parallel", "at the same time", "independently"},
		"genverb":    {"generate", "render", "create", "make"},
		"genverb2":   {"generate", "render", "create", "make"},
	},
	"multi_subject_scope": {
		"root": {
			"Find my notes related to %s and pull the details.",
			"Which subject covers %s — then recall the specifics.",
			"Look through my topics on %s and fetch what I saved.",
			"Locate the #topic# for %s, then #pull# the notes under it.",
			"Start from the #topic# on %s and #pull# what's stored there.",
		},
		"topic": {"subject", "topic", "cluster"},
		"pull":  {"pull", "retrieve", "bring up", "gather"},
	},
	"multi_job_status": {
		"root": {
			"Kick off a job to %s and tell me its status.",
			"Start %s in the background, then check how it's going.",
			"Run %s and report the job status.",
			"#launch# a job to %s, then #check# it.",
			"Get %s going and #check# the job for me.",
		},
		"launch": {"Launch", "Dispatch", "Start", "Fire off"},
		"check":  {"check on", "report back on", "look in on", "give me the status of"},
	},
	"multi_image_edit": {
		"root": {
			"Make an image of %s, then brighten it.",
			"Generate %s and then add more detail.",
			"Create a picture of %s and tweak the colors.",
			"#genverb# %s, then #tweak# it.",
			"Render %s and afterwards #tweak# it.",
		},
		"genverb": {"Generate", "Create", "Render", "Draw"},
		"tweak":   {"brighten it", "warm up the tones", "add more detail", "tighten the crop", "tweak the colors"},
	},
	"web_result_usage": {
		"root": {
			"Search the web for the latest figure on %s and tell me the exact number.",
			"What number does the current top result report for %s?",
			"Look up %s online and give me the precise figure it cites.",
			"#searchverb# %s and #report# the exact #figure# you find.",
			"Check online: what precise #figure# is being reported for %s?",
		},
		"searchverb": {"Search for", "Google", "Look up", "Run a web search on"},
		"report":     {"tell me", "report", "give me", "read out"},
		"figure":     {"figure", "number", "value", "reading"},
	},
	"multi_web_result_usage": {
		"root": {
			"Look up %s online, open the top result, and tell me the exact figure it reports.",
			"Find a page about %s, read it, and give me the precise number.",
			"Research %s on the web, read the leading source, and report its exact figure.",
			"#searchverb# %s, open the #top# result, and #report# the exact #figure#.",
			"Get me the precise #figure# for %s: search, open the #top# page, read it.",
		},
		"searchverb": {"Search for", "Google", "Look up", "Run a web search on"},
		"top":        {"top", "first", "leading", "best"},
		"report":     {"tell me", "report", "give me", "read out"},
		"figure":     {"figure", "number", "value", "reading"},
	},
	"job_chain_result_usage": {
		"root": {
			"Kick off a job to compute %s, then check its status and tell me the exact figure it returns.",
			"Dispatch a background job for %s, then look up that job's result and give me the precise number.",
			"Start a job to work out %s, then read the finished job's status and report its exact figure.",
			"#launch# a job for %s; once it finishes, read its status and #report# the exact #figure#.",
		},
		"launch": {"Launch", "Dispatch", "Start", "Fire off"},
		"report": {"tell me", "report", "give me", "read out"},
		"figure": {"figure", "number", "value", "reading"},
	},
	"web_recovery_result_usage": {
		"root": {
			"Search the web for the latest figure on %s and tell me the exact number (retry if the search flakes).",
			"Look up %s online and report the precise figure; if the first attempt errors, try again.",
			"Get the current number for %s from the web, retrying past any transient hiccup.",
			"#searchverb# %s and #report# the exact #figure# — #retry#.",
		},
		"searchverb": {"Search for", "Google", "Look up", "Run a web search on"},
		"report":     {"tell me", "report", "give me", "read out"},
		"figure":     {"figure", "number", "value", "reading"},
		"retry":      {"if the search errors once, just retry", "don't give up on a transient error", "retry if it flakes", "try again if the first call fails"},
	},
	"stale_context_web": {
		"root": {
			"I know I told you my take on %s a while back, but what's the latest on it right now?",
			"Forget what I said about %s before — pull up what's current today.",
			"You have my old notes on %s; check what's actually changed since then.",
			"My saved notes on %s are #stale# — #fetch# what's #current#.",
			"Ignore my earlier notes on %s; I want #current# information.",
			"What I told you about %s is #stale#. What's #current#?",
		},
		"stale":   {"stale", "out of date", "old", "from last year"},
		"fetch":   {"find", "look up", "check", "pull"},
		"current": {"current", "true today", "the latest", "actually happening now"},
	},
	"link_chain_result_usage": {
		"root": {
			"Find the page about %s, open the link the search points to, and tell me the exact figure that page reports.",
			"Search for %s, follow the result link, and give me the precise number from the page itself.",
			"Look up %s, read the linked page (not just the snippet), and report the exact figure it cites.",
			"#searchverb# %s, open the #linked# page, and #report# the exact #figure# from the page itself.",
		},
		"searchverb": {"Search for", "Google", "Look up", "Run a web search on"},
		"linked":     {"linked", "result", "actual", "underlying"},
		"report":     {"tell me", "report", "give me", "read out"},
		"figure":     {"figure", "number", "value", "reading"},
	},
	"job_chain_recovery_result_usage": {
		"root": {
			"Kick off a job to compute %s, then fetch that job's result — the status check can be flaky, so retry it if it errors — and tell me the exact figure.",
			"Dispatch a background job for %s, then read the job's status for the precise number; if the status call hiccups, try it again.",
			"Start a job to work out %s, then pull its result and report the exact figure — retry the status lookup past any transient error.",
			"#launch# a job for %s, then read its result; #retry# and #report# the exact #figure#.",
		},
		"launch": {"Launch", "Dispatch", "Start", "Fire off"},
		"retry":  {"retry the status check if it errors", "the status lookup can flake, so try again", "don't stop at one transient error"},
		"report": {"tell me", "report", "give me", "read out"},
		"figure": {"figure", "number", "value", "reading"},
	},
	"entity_lookup_chain": {
		"root": {
			"Find the subject that covers %s, pull the memories linked to it, then open the full memory.",
			"Look up the topic for %s, get the pairs under that subject, and fetch the details.",
			"Which subject holds %s — retrieve its linked memories, then read the full entry.",
			"Resolve the #topic# for %s, #pull# the memories under it, then open the full record.",
		},
		"topic": {"subject", "topic", "cluster"},
		"pull":  {"pull", "retrieve", "gather", "collect"},
	},
}

// v13Intent is one intent-phrased grammar resolving to an exact argument value.
type v13Intent struct {
	grammar persona.Grammar
	value   string
}

// v13ArgIntentGrammars replaces v8ArgIntents for bench_version >= 13. Every
// prompt implies the closed product value rather than stating a slug; the
// capability-resolution pass then rewrites the appearance/model families onto
// their discover-then-set surface.
var v13ArgIntentGrammars = map[string][]v13Intent{
	"settings": {
		{persona.Grammar{"root": {"#match# Ditto's light or dark mode to my device.", "Let the app #follow# whatever my device is set to.", "Light or dark — just #follow# my system setting."}, "match": {"Please match", "Match", "Sync"}, "follow": {"follow", "mirror", "track"}}, "system"},
		{persona.Grammar{"root": {"Make the app dark mode.", "Switch the app over to the dark look.", "#go# dark for the whole app."}, "go": {"Go", "Turn it", "Flip it"}}, "dark"},
	},
	"set_model": {
		{persona.Grammar{"root": {"Use GPT for my main chats; pick the current standard GPT option.", "Put my main chats on GPT — whatever the standard GPT choice is.", "Move me to GPT for everyday chat; the usual GPT option is fine."}}, "gpt-5"},
		{persona.Grammar{"root": {"Switch my chats to Claude; use the available Sonnet option.", "Put me on Claude for chat — the Sonnet one that's available.", "Move my main model over to Claude Sonnet, whichever Sonnet is on offer."}}, "claude-sonnet-5"},
	},
	"set_effort": {
		{persona.Grammar{"root": {"#deep# from now on.", "#deep# — I'd rather wait than get a shallow answer.", "For my questions, #deep#."}, "deep": {"Reason as deeply and carefully as possible", "Take your time and think everything through thoroughly", "Go as deep as you can on the reasoning", "Think hard and carefully before answering"}}, "high"},
		{persona.Grammar{"root": {"#balanced# for everyday questions.", "#balanced# — nothing extreme either way.", "For ordinary stuff, #balanced#."}, "balanced": {"Keep the reasoning balanced", "Use a middle-of-the-road amount of thinking", "Keep the reasoning at a moderate level", "Stay balanced on how much you reason"}}, "medium"},
	},
	"set_accent": {
		{persona.Grammar{"root": {"Make my accent color teal-ish; check the option if I misspelled it.", "Give the app a teal-ish accent — verify the exact option name."}}, "teal"},
		{persona.Grammar{"root": {"Switch the app accent to INDIGO, whatever capitalization the setting uses.", "Indigo accent please, however the setting spells it."}}, "indigo"},
	},
	"set_font": {
		{persona.Grammar{"root": {"Use jetbrians mono in chat; check the available font spelling.", "Put chat in jetbrians mono — confirm the real font name first."}}, "JetBrains Mono"},
		{persona.Grammar{"root": {"Change my chat font to GEORGIA, case-insensitively.", "Georgia for chat, please — whatever case the setting wants."}}, "Georgia"},
	},
}

// v13MemoryFetchQuestionGrammar renders the planted-accountant memory_fetch
// question (bench_version >= 8 overrides the category prompt with it).
var v13MemoryFetchQuestionGrammar = persona.Grammar{
	"root": {
		"What is the phone number of my #acct# for #year#?",
		"Can you find the number for the #acct# who handled my #year# taxes?",
		"I need to call my #year# #acct# — what number do I have saved?",
		"#dig# the number for the #acct# who did my #year# taxes.",
		"Which phone number did I save for my #year# #acct#?",
	},
	"acct": {"accountant", "tax accountant", "tax person", "bookkeeper for taxes"},
	"year": {"2024"},
	"dig":  {"Dig up", "Pull up", "Find me", "Look up"},
}

// v13StaleContextPairGrammar renders the stale-context prerequisite note; the
// %s is the topic filler.
var v13StaleContextPairGrammar = persona.Grammar{
	"root": {
		"I was reading about %s last year and saved a few notes, but I know they may be out of date now.",
		"Last year I kept some notes on %s; they're probably #stale# by now.",
		"Filing my old thoughts on %s here — they're from a while back and could be #stale#.",
		"I had a take on %s a while ago. Keeping it as background; it may be #stale#.",
	},
	"stale": {"stale", "out of date", "behind the times", "overtaken by events"},
}

// ── World tool prompt grammars (slot-bound) ─────────────────────────────────
//
// Slots are bound by name through persona.ExpandSlots so a pinned alias,
// nickname or needle subject is never rewritten by expansion. Every root keeps
// the constraints the v8 prompt carried (who, where, which event/project), so
// the seeded-world resolution burden is unchanged.

var v13WorldContactEmailGrammar = persona.Grammar{
	"root": {
		"#ask# #subject# #now#? #send# #nickname# — the #relation# in #city# from the #context#.",
		"Could you #check# #subject# and #sendto# #nickname#? I mean my #relation# in #city#, the one from the #context#.",
		"Please #check# #subject# and #pass# #nickname#, my #relation# in #city# from the #context#.",
		"I need the latest reading for #subject# sent to #nickname# — my #relation# over in #city# who handled the #context#.",
		"#check2# #subject#, then #sendto# #nickname#. They're my #relation# in #city# from the #context#.",
		"See where #subject# stands and #pass# #nickname#, the #relation# in #city# I know from the #context#.",
		"What's the latest on #subject#? #send# #nickname#, my #relation# in #city# from the #context#.",
		"For #nickname# — my #relation# in #city#, from the #context# — #check# #subject# and #sendto# them.",
	},
	"ask":    {"What is", "Where is", "What's"},
	"now":    {"at right now", "sitting at today", "currently at", "at as of now"},
	"send":   {"Forward the figure to", "Email the number to", "Send that reading to", "Get the number over to"},
	"check":  {"look up", "check", "find the live value for", "pull the current figure for"},
	"check2": {"Look up", "Check", "Find the live value for", "Pull the current figure for"},
	"sendto": {"send it to", "email it to", "get it over to", "pass it along to"},
	"pass":   {"pass the current number along to", "email what you find to", "send that number on to", "forward the figure to"},
}

var v13WorldMemoryDeleteGrammar = persona.Grammar{
	"root": {
		"Go ahead and #bin# that temporary note about fixing #nickname#'s email after the #context# — they're my #relation# at #employer#. Just #keep# their actual contact history.",
		"#bin2# the throwaway note about sorting out #nickname#'s email after the #context#. #nickname# is my #relation# at #employer#; #keep# the real contact history.",
		"That disposable note on reconciling #nickname#'s address after the #context# can go — my #relation# at #employer#. #keep2# their contact records intact.",
		"Please #bin# the scratch note about #nickname#'s email fix from the #context# (my #relation# at #employer#), but #keep# every real contact detail.",
	},
	"bin":   {"bin", "toss", "get rid of", "clear out"},
	"bin2":  {"Bin", "Toss", "Get rid of", "Clear out"},
	"keep":  {"don't lose", "keep", "leave alone", "hold onto"},
	"keep2": {"Keep", "Leave", "Preserve"},
}

var v13WorldMemoryUpdateGrammar = persona.Grammar{
	"root": {
		"Please add to the handoff note for #alias# at #client# that we're doing the handoff Friday. It's the #purpose# project; update the scratchpad, not the project history.",
		"On the #alias# handoff note (#client#, the #purpose# work): the handoff is Friday. Put that in the scratchpad and leave the project record alone.",
		"Update the #alias# scratchpad for #client# — handoff Friday. That's the #purpose# project; don't touch the project history itself.",
		"Handoff for #alias# at #client# is Friday. #note# on the handoff scratchpad for that #purpose# project, not on the project identity record.",
	},
	"note": {"Note that", "Record that", "Add that", "Jot that"},
}

var v13WorldThemeDiscoverGrammar = persona.Grammar{
	"root": {
		"Have Ditto use my usual #accent#-ish accent — the personal app preference, not one of the client brand colors. If I mangled the spelling, check the available appearance options first.",
		"Set the app accent to my #accent#-ish one; that's my own preference, not a client palette. Spelling may be off, so #check# the appearance options.",
		"Switch Ditto to a #accent#-ish accent (my personal choice, not any client's brand). #check2# the available appearance settings if the name looks wrong.",
		"My accent should be the #accent#-ish one I always use — mine, not a client color. #check2# what appearance options exist before applying it.",
	},
	"check":  {"look at", "check", "inspect", "review"},
	"check2": {"Look at", "Check", "Inspect", "Review"},
}

var v13WorldBusinessWorkflowGrammar = persona.Grammar{
	"root": {
		"Can you check whether I already have a workflow for #alias#, the project for #client#? If not, create one under the project's formal name and put the current contact address for internal reviewer #nickname# in its review step.",
		"Do I already have a workflow for #alias# (the #client# project)? If there isn't one, create it under the formal project name, with #nickname#'s current address as the internal reviewer in the review step.",
		"For #alias#, our #client# project: see if a workflow exists; if not, make one under the formal name and set the review step to the current email of internal reviewer #nickname#.",
		"Look for an existing workflow on #alias# for #client#. Create one under the project's formal name if it's missing, and wire #nickname#'s current contact address into the review step.",
	},
}

var v13WorldLinkReadGrammar = persona.Grammar{
	"root": {
		"Check what #subject# is at right now, and open the actual page rather than relying on the search blurb.",
		"Check where #subject# stands today — open the real page, don't trust the search snippet.",
		"Find the current reading for #subject# and read it off the page itself, not the search summary.",
		"What's #subject# at now? Open the source page instead of going by the search preview.",
	},
}

var v13WorldAgentJobGrammar = persona.Grammar{
	"root": {
		"Could you have Ditto Code inspect the #project# project for #client# and prepare a concise dependency-risk report? Go ahead and start it; I'll approve the job when Ditto asks.",
		"Get Ditto Code going on the #project# project for #client#: a short dependency-risk report. Start it now — I'll approve when prompted.",
		"Please have Ditto Code look over #project# (the #client# project) and write up a brief dependency-risk report. Kick it off; I'll approve the job.",
		"Start a Ditto Code job on the #project# project for #client# to draft a concise dependency-risk report. I'll approve it when asked.",
	},
}

// v13RoutingPlanGrammar / v13RoutingAskGrammar / v13RoutingRouteGrammar render
// the state-dependent routing family (planning note, request, route prefix).
var v13RoutingPlanGrammar = persona.Grammar{
	"root": {
		"#log# #about# #alias# #at#",
		"#log# — #alias#, #client#.",
		"#about2# #alias# #at#: #log2#.",
	},
	"log":    {"Decision log", "Where we landed", "Outcome of our scoping chat", "Notes from planning", "Planning summary"},
	"log2":   {"decision log", "where we landed", "what we agreed", "planning notes"},
	"about":  {"for the risk review on", "about the dependency work for", "covering the review of", "on the dependency pass for"},
	"about2": {"Risk review on", "Dependency work for", "Review of"},
	"at":     {"at #client#.", "(#client#).", "for #client#."},
}

var v13RoutingAskGrammar = persona.Grammar{
	"root": {
		"#lead# the dependency-risk review for #alias# #tail#",
		"#lead# the dependency-risk review on #alias# — #tail#",
	},
	"lead": {"Kick off", "Time to start", "Please begin", "Go ahead and start", "Let's start"},
	"tail": {"the way we already agreed. Start now.", "exactly as we settled earlier. Begin.", "following what we decided together. Proceed.", "per what we landed on. Go."},
}

var v13RoutingRouteGrammar = persona.Grammar{
	"root": {"#what##join#"},
	"what": {"What we settled on", "Agreed path", "Our decision", "The plan we set", "Where we landed"},
	"join": {": ", " — ", " is: ", ", "},
}

// ── Capability-resolution grammars ──────────────────────────────────────────

var v13CapabilityModelGrammar = persona.Grammar{
	"root": {
		"Use #family# for my main chats. I don't know its exact model id, so #resolve# instead of making me type a slug.",
		"Put my main chats on #family#. I can't remember the exact id — #resolve#.",
		"Switch me to #family# for everyday chat; #resolve#, I don't have the slug handy.",
	},
	"resolve": {"resolve the current available option", "pick the current available option", "look up whichever option is available now", "match it to the option you can see"},
}

var v13CapabilitySystemModeGrammar = persona.Grammar{
	"root": {
		"Match Ditto's color mode to my device. Check the available appearance settings if you need the canonical option.",
		"Have Ditto follow my device's light/dark setting. #check# the appearance options if you need the exact name.",
		"Light or dark — just follow my system. #check# what the appearance setting is actually called.",
	},
	"check": {"Check", "Look at", "Inspect"},
}

var v13CapabilityModeGrammar = persona.Grammar{
	"root": {
		"Switch Ditto to #value#-ish mode; check the available appearance options rather than guessing the setting name.",
		"Put Ditto in #value#-ish mode. #check# the appearance options first instead of guessing the option's name.",
		"I want the #value#-ish mode — #check# what appearance settings exist and pick the right one.",
	},
	"check": {"Check", "Look through", "Inspect"},
}

var v13CapabilityAccentGrammar = persona.Grammar{
	"root": {
		"Make the app accent #value#-ish. I may have mangled the spelling, so inspect the available appearance options first.",
		"Set the accent to something #value#-ish; my spelling might be off — #check# the appearance options before applying it.",
		"#value#-ish accent, please. #check# the available appearance options in case I've misspelled it.",
	},
	"check": {"Check", "Look at", "Inspect"},
}

var v13CapabilityFontGrammar = persona.Grammar{
	"root": {
		"Use #value# in chat. Treat that case-insensitively and check the available font options if the name is slightly off.",
		"Put chat in #value#. Ignore the case, and #check# the font options if I've got the name slightly wrong.",
		"Chat font: #value#, case-insensitively. #check# the available fonts if that spelling isn't exact.",
	},
	"check": {"check", "look through", "inspect"},
}
