package datagen

import "github.com/ditto-assistant/dittobench-datagen/persona"

// Grammar-driven prompt surfaces for v2 hardening.
// The audited low-variety categories moved from flat template lists to small
// context-free grammars (persona.Grammar): clause order, synonym slots, and
// optional phrases multiply the surface a template matcher must cover to
// hundreds of distinct prompts per category before the lead-in/trailer wrap
// composes on top. Every alternative is hand-reviewed against the category's
// routing intent: a production must never flip the correct tool (a status
// check must not read as a dispatch, an edit must not read as a create) and
// must never leak an answer. The %s fill for pinned arguments stays OUT of
// the grammar: datagen expands first, then fills, so pinned tokens are never
// rewritten by expansion.

// agentReadGrammar covers the run-vs-read trap's READ side: the user asks
// about background jobs that already exist. No production may ask to start
// anything new.
var agentReadGrammar = persona.Grammar{
	"root": {
		"#checkverb# #jobsref#.#tail#",
		"What's the status of #jobsref#?#tail#",
		"Are any of #jobsref# still #going#?",
		"Did #onejob# #finish# yet?",
		"How's #onejob# coming along?",
		"Where do things stand with #jobsref#?",
		"#anynews# on #jobsref#?",
	},
	"checkverb": {
		"Check on", "Give me an update on", "Give me a rundown of",
		"Catch me up on", "Report the status of", "Show me the state of",
	},
	"jobsref": {
		"my background jobs", "the agent jobs I kicked off",
		"the tasks I dispatched earlier", "my running background tasks",
		"the jobs sitting in my queue", "the background work I've queued",
		"my in-flight agent tasks", "the agents I set loose earlier",
	},
	"onejob": {
		"that task I dispatched", "the job I kicked off earlier",
		"that background task from before", "the agent I sent off",
	},
	"going":   {"running", "going", "in progress", "churning"},
	"finish":  {"finish", "wrap up", "complete"},
	"anynews": {"Any update", "Any news", "Any movement", "Any progress"},
	"tail": {
		"", "", "",
		" Anything done?", " Curious where things stand.",
		" Just want the current state.",
	},
}

// imageEditGrammar covers the edit-vs-create trap's EDIT side: every
// production references an EXISTING image. No production may ask for a new
// picture from scratch.
var imageEditGrammar = persona.Grammar{
	"root": {
		"#editverb# #imgref# #change#.",
		"On #imgref#, #directchange#.",
		"Take #imgref# and #directchange#.",
		"#directchange2# on #imgref#, please.",
		"Can you #directchange# on #imgref#?",
	},
	"editverb": {
		"Tweak", "Touch up", "Adjust", "Rework", "Update", "Fix up",
	},
	"imgref": {
		"the last image", "that picture you made", "the image you generated",
		"the one you just made", "the picture from before",
		"that latest render", "the image from earlier",
	},
	"change": {
		"so the sky is purple", "so it's black and white",
		"with warmer tones", "so the background is a quiet beach",
		"with a tighter crop around the subject", "so it's a bit brighter",
		"to give it a night-time look", "so the colors pop more",
	},
	"directchange": {
		"make the sky purple", "add a small hat", "remove the text in the corner",
		"swap the background for a quiet beach", "make it black and white",
		"warm up the tones", "crop it tighter around the subject",
		"brighten it up a little", "give it a night-time look",
	},
	"directchange2": {
		"A quick color pass", "A tighter crop", "A brightness bump",
		"A background swap", "A small touch-up",
	},
}

// automationListGrammar covers the list-automations read: the user asks what
// runs on a schedule. No production may ask to create or run anything.
var automationListGrammar = persona.Grammar{
	"root": {
		"#showverb# #autoref#.#tail#",
		"What #autothings# do I have #inplace#?#tail#",
		"Do I have any #autothings# #inplace# right now?",
		"Which of my #autothings# are #active#?",
		"Remind me what I've got #onschedule#.",
		"What's #onmyroster# these days?",
	},
	// showverb deliberately avoids the tool-name word "list": the no-model
	// router signals list_automations on {list, existing, scheduled,
	// automations}, and stacking those words hands the trap a free solve.
	"showverb": {
		"Show me", "Pull up", "Lay out", "Walk me through", "Bring up",
		"Run down",
	},
	"autoref": {
		"my automation roster", "the recurring tasks I've set up",
		"everything I've set to run automatically",
		"my standing routines", "whatever runs for me on a timer",
		"the stuff I've got on repeat",
	},
	"autothings": {
		"recurring routines", "recurring tasks", "repeating jobs",
		"timed routines", "standing tasks",
	},
	"inplace": {"set up", "configured", "in place", "on the books"},
	"active":  {"still active", "currently on", "live right now", "enabled"},
	"onschedule": {
		"running on a schedule", "set to repeat", "firing automatically",
		"going on timers",
	},
	"onmyroster": {
		"on my automation roster", "set to run on its own",
		"going off on a timer", "running automatically for me",
	},
	// tail avoids the tool-name word "list" for the same reason as showverb.
	"tail": {"", "", "", " Just the rundown.", " Everything counts."},
}

// capabilityGrammar covers discover_capabilities: "what can you do / where is
// X" questions about the assistant or the app itself, never about the user's
// data or the public web.
var capabilityGrammar = persona.Grammar{
	"root": {
		"#tellme# what #youref# #cando#.",
		"What kinds of things can I #askfor#?",
		"How do I #settingtask# in #appref#?",
		"Where do I find the #control# for #settingarea#?",
		"Is there a way to #settingtask#?",
		"What #features# do you have for #featurearea#?",
	},
	"tellme": {
		"Walk me through", "Give me a sense of", "Break down", "Explain",
	},
	"youref": {"you", "this assistant", "this app"},
	"cando": {
		"can actually do", "are able to do", "are capable of",
		"can help with",
	},
	// features avoids the tool-name word "capabilities" (discover_capabilities):
	// it is a distinctive name token, so emitting it hands the no-model reference
	// router a free solve for the capability-discovery trap. See
	// TestGrammarCategoriesNoRouterKeywordLeak.
	"askfor": {
		"ask you for", "get your help with", "have you handle",
		"lean on you for",
	},
	"settingtask": {
		"turn on dark mode", "connect my calendar",
		"set up a recurring reminder", "change how the app looks",
		"manage which tools you're allowed to use", "adjust the chat font",
	},
	"appref":  {"this app", "here", "the settings"},
	"control": {"setting", "switch", "option", "control"},
	"settingarea": {
		"connecting my calendar", "the app's appearance",
		"tool permissions", "notification behavior",
	},
	"features": {"features", "abilities", "options"},
	"featurearea": {
		"working with images", "scheduling things", "background tasks",
		"customizing the interface",
	},
}

// memoryFetchGrammar covers fetch_memories without stating the tool's own
// keywords (fetch/memory/memories/pair are banned by
// TestMemoryFetchPromptNotVerbatim; the old verbatim surface was solved by a
// no-model keyword router at floor 1.0). The %s slot is the pinned pair-ID
// argument, filled after expansion.
var memoryFetchGrammar = persona.Grammar{
	"root": {
		"#pullverb# the #wholething# #savedunder# %s.",
		"#showverb# everything #inref# %s.",
		"What exactly #lives# under %s? #showall#",
		"I need the #wholething# behind %s — #bringitup#.",
		"Open up %s and #showverb2# the entire thing.",
	},
	"pullverb": {
		"Pull up", "Open up", "Bring back", "Bring up", "Dig out",
	},
	// wholething avoids "full" and "conversation": both are content words in
	// the fetch_memories description, so reusing them lets the no-model router
	// route the case with no retrieval (raised the floor 0.20 -> 0.275).
	"wholething": {
		"whole exchange", "entire thread", "complete record",
		"entire history", "whole back-and-forth", "whole log",
	},
	"savedunder": {
		"saved under", "filed under", "stored under", "kept under",
		"logged under",
	},
	"showverb":  {"Show me", "Give me", "Lay out"},
	"showverb2": {"show me", "give me", "put in front of me"},
	"inref": {
		"we said in", "that's recorded in", "captured in", "kept inside",
	},
	"lives":   {"is stored", "is kept", "sits", "is filed"},
	"showall": {"Show me all of it.", "I want the whole thing.", "Every bit of it."},
	"bringitup": {
		"bring it up", "put it in front of me", "open it for me",
	},
}
