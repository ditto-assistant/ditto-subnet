package universe

import (
	"fmt"
	"math/rand"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/persona"
)

// Story v2 (bench_version >= 13): a per-seed typed event DAG replaces the fixed
// 61-sentence origin→decision→outcome script of story.go.
//
// The hidden multi-memory join is kept (owner note 2): a question anchors on a
// person and an informal subject, the first memory binds that anchor to a join
// key, the second memory binds the join key to a second reference, and every
// later record speaks only in the second reference. What changes is the SHAPE:
// each arc is a seed-drawn causal DAG of 6–9 typed events with preconditions and
// effects, shuffled within its constraints, folded into 3–5 memories across at
// least two opaque sessions, with one spurious near-name decoy thread and, on at
// least half of the arcs, a contradictory update (revision) that supersedes an
// earlier record. Rendering goes through persona.Grammar banks — no complete
// sentence is stored anywhere — and is the pre-pass input for the v13 surface
// pass. Honest framing (issue #1839): the catalog and frames are public, so a
// CFG matcher can recover the DAG from raw frames; parse-resistance is the
// private surface pass's job and is measured by cmd/parserprobe, not claimed
// here.
//
// Every value the oracles grade (owner, status, ordered entities, next action,
// quantity, lesson) is a typed field on StoryArcV2, so tests and the
// generation-time oracle reason about state, never about prose.

// StoryEventKind is one typed event in the story v2 catalog.
type StoryEventKind string

const (
	// Business events.
	EventKickoff             StoryEventKind = "kickoff"
	EventQuote               StoryEventKind = "quote"
	EventApprovalCapped      StoryEventKind = "approval_capped"
	EventContactRouteChanged StoryEventKind = "contact_route_changed"
	EventVendorSwapped       StoryEventKind = "vendor_swapped"
	EventIncident            StoryEventKind = "incident"
	EventCorrection          StoryEventKind = "correction"
	EventHandoffAssigned     StoryEventKind = "handoff_assigned"
	EventFollowUp            StoryEventKind = "follow_up"
	EventOutcome             StoryEventKind = "outcome"

	// Personal theme events. Each personal arc is rooted in exactly one of them;
	// it fixes the subject vocabulary, the provider noun, and the natural
	// quantity unit for the whole arc.
	EventMove                  StoryEventKind = "move"
	EventMedicalCourse         StoryEventKind = "medical_course"
	EventSchoolLogistics       StoryEventKind = "school_logistics"
	EventWedding               StoryEventKind = "wedding"
	EventRenovation            StoryEventKind = "renovation"
	EventBillDispute           StoryEventKind = "bill_dispute"
	EventTripReplan            StoryEventKind = "trip_replan"
	EventSubscriptionCancelled StoryEventKind = "subscription_cancelled"

	// Personal structural events: the personal counterparts of quote and
	// vendor_swapped. They exist so a personal arc has the same two-hop join and
	// the same ordered-entity oracle as a business arc.
	EventBooking         StoryEventKind = "booking"
	EventProviderSwapped StoryEventKind = "provider_swapped"

	// EventOutcomeDisputed is the second, disagreeing outcome record on a
	// "records disagree" arc. It is always rendered in its own memory and
	// session, so the two records have equal standing and no timestamp resolves
	// them.
	EventOutcomeDisputed StoryEventKind = "outcome_disputed"
)

// StoryEvent is one drawn event with its resolved slots. Position is the index
// in the arc timeline after the constrained topological shuffle; Memory is the
// index of the arc memory (chunk) that renders it.
type StoryEvent struct {
	Kind     StoryEventKind `json:"kind"`
	Position int            `json:"position"`
	Memory   int            `json:"memory"`
	Revision bool           `json:"revision,omitempty"`
	// Slots are the resolved rendering slots (who, what, channel, from, to,
	// unit, ...). Values are canonical strings; prose is compiled from them.
	Slots map[string]string `json:"slots,omitempty"`
	// Retained at the state draw, never recovered from qtyphrase. Excluded
	// from legacy serialization so the public generator contract is unchanged.
	QuantityEffect *StoryQuantityEffect `json:"-"`
}

// StoryQuantityEffect contains evidence operands, never a computed answer.
// replace uses Operand as the replacement and Operand2 as the superseded value;
// add/subtract use Operand as the base and Operand2 as the change.
type StoryQuantityEffect struct {
	Kind     string
	Op       string
	Operand  string
	Operand2 string
}

// StoryNextAction is the typed {who, what, channel} claim set of the latest
// follow-up or handoff record.
type StoryNextAction struct {
	Who        int    `json:"who"` // index into World.People
	What       string `json:"what"`
	WhatAccept []string
	Channel    string `json:"channel"`
}

// StoryQuantity is the arc's one rotating quantity: a record-stated operation
// over operands, never a bare planted total.
type StoryQuantity struct {
	Kind     string `json:"kind"` // money | nights | seats | days | percent
	Value    int    `json:"value"`
	Base     int    `json:"base"`
	Delta    int    `json:"delta"`
	Op       string `json:"op"` // add | subtract | replace
	Memory   int    `json:"memory"`
	Operand  string `json:"operand_a"`
	Operand2 string `json:"operand_b"`
}

// StoryArcV2 is the typed outcome state of one story v2 arc. It replaces the
// six *Cents fields of the v8 script: every graded value is a typed field, and
// the oracle in questions_v13.go reads only these fields.
type StoryArcV2 struct {
	Kind             StoryKind       `json:"kind"`
	Theme            StoryEventKind  `json:"theme"` // root event kind
	Subject          string          `json:"subject"`
	SubjectAlias     string          `json:"subject_alias"`
	Events           []StoryEvent    `json:"events"`
	Memories         int             `json:"memories"`
	Sessions         int             `json:"sessions"`
	PairIDs          []string        `json:"pair_ids"`
	SessionIDs       []string        `json:"session_ids"`
	DecoyPairID      string          `json:"decoy_pair_id"`
	DecoyAlias       string          `json:"decoy_alias"`
	JoinKey1         string          `json:"join_key_1"`
	JoinKey2         string          `json:"join_key_2"`
	OwnerInitial     int             `json:"owner_initial"`
	Owner            int             `json:"owner"` // owner-current (World.People index)
	OwnerMemory      int             `json:"owner_memory"`
	OwnerHistory     []int           `json:"owner_history"`
	Status           string          `json:"status"`         // canonical status key
	StatusHistory    []string        `json:"status_history"` // every canonical status the arc held, in order
	StatusSurface    string          `json:"status_surface"`
	StatusMemory     int             `json:"status_memory"`
	Disagree         bool            `json:"disagree,omitempty"`
	StatusAlt        string          `json:"status_alt,omitempty"`
	StatusAltSurface string          `json:"status_alt_surface,omitempty"`
	StatusAltMemory  int             `json:"status_alt_memory,omitempty"`
	Sequence         [2]string       `json:"sequence"` // ordered entities (first, replacement)
	SequenceNoun     string          `json:"sequence_noun"`
	SequenceMemories [2]int          `json:"sequence_memories"`
	Next             StoryNextAction `json:"next"`
	NextMemory       int             `json:"next_memory"`
	Quantity         *StoryQuantity  `json:"quantity,omitempty"`
	Lesson           *lessonSet      `json:"-"`
	LessonMemory     int             `json:"lesson_memory"`
	HasRevision      bool            `json:"has_revision"`
}

// eventSpec is the catalog entry: preconditions (every listed kind must already
// be in the timeline), the domain it belongs to, and whether it is the arc's
// terminal event.
type eventSpec struct {
	kind     StoryEventKind
	requires []StoryEventKind
	terminal bool
	personal bool
	business bool
	// revises names the earlier event kind whose stated fact this one supersedes
	// (a contradictory update). Empty for first-statement events.
	revises StoryEventKind
}

var storyEventCatalog = []eventSpec{
	{kind: EventKickoff, business: true},
	{kind: EventQuote, requires: []StoryEventKind{EventKickoff}, business: true},
	{kind: EventApprovalCapped, requires: []StoryEventKind{EventQuote}, business: true},
	{kind: EventContactRouteChanged, requires: []StoryEventKind{EventKickoff}, business: true},
	{kind: EventVendorSwapped, requires: []StoryEventKind{EventQuote}, business: true},
	{kind: EventIncident, requires: []StoryEventKind{EventQuote, EventBooking}, business: true, personal: true},
	{kind: EventCorrection, requires: []StoryEventKind{EventQuote, EventBooking}, business: true, personal: true, revises: EventQuote},
	{kind: EventHandoffAssigned, requires: []StoryEventKind{EventKickoff}, business: true, personal: true},
	{kind: EventFollowUp, requires: []StoryEventKind{EventHandoffAssigned}, business: true, personal: true},
	{kind: EventOutcome, requires: []StoryEventKind{EventHandoffAssigned, EventFollowUp}, terminal: true, business: true, personal: true},
	{kind: EventBooking, personal: true},
	{kind: EventProviderSwapped, requires: []StoryEventKind{EventBooking}, personal: true},
}

var personalThemes = []StoryEventKind{
	EventMove, EventMedicalCourse, EventSchoolLogistics, EventWedding,
	EventRenovation, EventBillDispute, EventTripReplan, EventSubscriptionCancelled,
}

// personalTheme fixes the vocabulary of one personal arc.
type personalTheme struct {
	subjects  []string // informal subject alias frames, {mod} is a coined modifier
	provider  string   // the replaceable entity noun (ordered-entity oracle)
	providers []string // coined-name frames for providers, {coin} is the coin
	unit      string   // natural quantity unit
	statuses  []string // terminal canonical statuses natural to the theme
}

var personalThemeBank = map[StoryEventKind]personalTheme{
	EventMove: {
		subjects: []string{"the {mod} street move", "the {mod} flat move", "moving to {mod} road", "the {mod} house move"},
		provider: "moving company", providers: []string{"{coin} Movers", "{coin} Removals", "{coin} Van Line"},
		unit: "days", statuses: []string{"completed", "postponed", "rebooked"},
	},
	EventMedicalCourse: {
		subjects: []string{"the {mod} physio course", "the {mod} clinic sessions", "the {mod} treatment plan", "the {mod} rehab block"},
		provider: "clinic", providers: []string{"{coin} Clinic", "{coin} Physio", "{coin} Health Rooms"},
		unit: "days", statuses: []string{"completed", "postponed", "rebooked"},
	},
	EventSchoolLogistics: {
		subjects: []string{"the {mod} school run", "the {mod} after-school plan", "the {mod} term logistics", "the {mod} pickup rota"},
		provider: "club", providers: []string{"{coin} Kids Club", "{coin} After-School", "{coin} Activity Hub"},
		unit: "seats", statuses: []string{"confirmed", "completed", "cancelled"},
	},
	EventWedding: {
		subjects: []string{"the {mod} wedding", "the {mod} wedding plan", "the {mod} reception", "the {mod} ceremony weekend"},
		provider: "venue", providers: []string{"{coin} Hall", "{coin} Barn", "{coin} House"},
		unit: "seats", statuses: []string{"confirmed", "rebooked", "postponed"},
	},
	EventRenovation: {
		subjects: []string{"the {mod} kitchen job", "the {mod} renovation", "the {mod} bathroom refit", "the {mod} loft work"},
		provider: "contractor", providers: []string{"{coin} Joinery", "{coin} Builders", "{coin} Fit-Out"},
		unit: "days", statuses: []string{"completed", "postponed", "cancelled"},
	},
	EventBillDispute: {
		subjects: []string{"the {mod} energy bill dispute", "the {mod} water bill row", "the {mod} broadband bill dispute", "the {mod} billing dispute"},
		provider: "supplier", providers: []string{"{coin} Energy", "{coin} Utilities", "{coin} Broadband"},
		unit: "percent", statuses: []string{"settled", "disputed", "cancelled"},
	},
	EventTripReplan: {
		subjects: []string{"the {mod} coast trip", "the {mod} weekend away", "the {mod} island trip", "the {mod} spring break"},
		provider: "hotel", providers: []string{"{coin} Inn", "{coin} Lodge", "{coin} Guesthouse"},
		unit: "nights", statuses: []string{"confirmed", "rebooked", "cancelled"},
	},
	EventSubscriptionCancelled: {
		subjects: []string{"the {mod} subscription cleanup", "the {mod} streaming cancellation", "the {mod} membership cancellation", "the {mod} plan cancellation"},
		provider: "service", providers: []string{"{coin} Stream", "{coin} Box", "{coin} Plus"},
		unit: "percent", statuses: []string{"cancelled", "settled", "disputed"},
	},
}

var businessProviderFrames = []string{"{coin} Fabrication", "{coin} Studio", "{coin} Logistics", "{coin} Print Works", "{coin} Systems", "{coin} Catering", "{coin} Consulting", "{coin} Media"}

// storyStatusVocabulary maps a canonical status key to the reviewed synonym set
// an honest assistant may normalise to. The per-seed surface term used in prose
// is drawn from the same set, so grading accepts the seed's term ∪ the canonical
// synonyms (issue #1841).
var storyStatusVocabulary = map[string][]string{
	"started":     {"started", "kicked off", "underway", "opened", "begun"},
	"quoted":      {"quoted", "priced", "estimated", "costed"},
	"approved":    {"approved", "signed off", "green-lit", "cleared", "authorised", "authorized"},
	"on hold":     {"on hold", "paused", "stalled", "frozen", "suspended"},
	"corrected":   {"corrected", "revised", "amended", "restated"},
	"in progress": {"in progress", "moving", "in motion", "being worked", "in hand"},
	"delivered":   {"delivered", "handed over", "shipped", "fulfilled"},
	"closed":      {"closed", "wrapped up", "finished", "done", "complete"},
	"cancelled":   {"cancelled", "canceled", "called off", "scrapped", "dropped"},
	"planned":     {"planned", "pencilled in", "penciled in", "sketched out", "mapped out"},
	"booked":      {"booked", "reserved", "locked in", "secured"},
	"confirmed":   {"confirmed", "firmed up", "nailed down", "definite", "set"},
	"postponed":   {"postponed", "pushed back", "delayed", "deferred", "put off"},
	"rebooked":    {"rebooked", "moved", "rescheduled", "switched over", "re-arranged"},
	"disputed":    {"disputed", "contested", "challenged", "under query", "in dispute"},
	"settled":     {"settled", "resolved", "sorted", "squared away", "agreed"},
	"completed":   {"completed", "finished", "done", "wrapped up", "all done"},
}

// storyStatusOrder lists canonical statuses so tests and distractor draws walk
// a stable order (map iteration is never used for generation).
var storyStatusOrder = []string{
	"started", "quoted", "approved", "on hold", "corrected", "in progress", "delivered", "closed", "cancelled",
	"planned", "booked", "confirmed", "postponed", "rebooked", "disputed", "settled", "completed",
}

var businessTerminalStatuses = []string{"delivered", "closed", "cancelled"}

// storyNextActions are the reviewed {what} concepts with the paraphrases an
// honest reply may use. Each arc draws one for the handoff and one for the
// follow-up; a revision draws a third so the latest record disagrees with the
// earlier one.
var storyNextActions = []struct {
	what   string
	accept []string
}{
	{"send the revised quote", []string{"revised quote", "updated quote", "new quote", "re-send the quote"}},
	{"confirm the delivery date", []string{"delivery date", "confirm the date", "confirm when it arrives"}},
	{"book the site visit", []string{"site visit", "arrange the visit", "schedule the visit"}},
	{"chase the deposit", []string{"deposit", "chase payment", "follow up on the deposit"}},
	{"share the final headcount", []string{"headcount", "final numbers", "guest count"}},
	{"return the signed form", []string{"signed form", "sign and return", "send the form back"}},
	{"upload the photos", []string{"photos", "send the pictures", "share the images"}},
	{"cancel the old plan", []string{"cancel the old", "close the old plan", "stop the previous plan"}},
	{"reschedule the appointment", []string{"reschedule", "move the appointment", "new appointment time"}},
	{"approve the change order", []string{"change order", "approve the change", "sign off the change"}},
	{"collect the keys", []string{"keys", "pick up the keys", "key collection"}},
	{"dispute the extra charge", []string{"extra charge", "query the charge", "challenge the charge"}},
}

var storyChannels = []struct {
	channel string
	accept  []string
}{
	{"email", []string{"email", "mail", "e-mail", "over email"}},
	{"phone", []string{"phone", "call", "ring", "by phone", "telephone"}},
	{"chat", []string{"chat", "message", "text", "dm", "messenger"}},
	{"in person", []string{"in person", "face to face", "meet up", "meeting", "drop by"}},
}

// storyJoinKeyShapes are the >= 6 join-key shapes. A per-arc draw picks one
// shape for each of the two hidden references, so no fixed regex recovers the
// join from a single prefix.
var storyJoinKeyShapes = []struct {
	noun  string
	frame string // {hex6} / {hex8} / {num5} / {alpha2}
}{
	{"case", "CASE-{num4}-{hex6}"},
	{"ticket", "TKT-{num5}"},
	{"reference", "REF {alpha2}-{hex6}"},
	{"purchase order", "PO-{hex8}"},
	{"booking reference", "BK{num5}{alpha2}"},
	{"job number", "JOB-{alpha2}{num5}"},
	{"claim number", "CLM-{num4}-{hex6}"},
	{"quote number", "Q-{hex8}"},
	{"file number", "F/{num5}/{alpha2}"},
}

// storyTextureGrammar supplies life texture so no memory is only its records.
var storyTextureGrammar = persona.Grammar{
	"texture": {
		"#opener# #detail#.", "#detail#, #aside#.", "#opener# #detail#, #aside#.", "#detail#; #detail2#.",
		"#opener# #detail# and #detail2#.", "#detail#. #tailnote#", "#detail#, #aside#. #tailnote#",
	},
	"opener": {"Meanwhile,", "Around then,", "On the same day,", "Somewhere in the middle of that,", "For what it's worth,", "As an aside,", "In the background,", "Between all that,", "That same week,", "Unrelated, but"},
	"detail": {
		"the {city} weather turned properly grim", "my commute was two buses and a long walk", "the kettle at home finally gave up",
		"I kept forgetting to eat lunch", "the neighbours were having their floors done", "{nick} sent a string of photos from {city}",
		"I had a stack of unread messages from the {context}", "the printer jammed three times", "the dog decided the sofa was hers",
		"we tried the new bakery on the corner", "my calendar was double-booked twice", "the train home was quieter than usual",
		"I finally fixed the wobbly shelf", "the plants on the windowsill needed rescuing", "I lost an hour to a parking app",
		"someone left the office windows open overnight", "the radio kept playing the same song", "my phone battery died twice",
		"the recycling went out on the wrong day", "I found the missing charger in a coat pocket", "the lift was out again",
		"a delivery for the neighbours ended up on our step", "the heating clicked on for the first time this year", "I re-potted the basil",
		"the cafe by the station changed its hours", "I walked home the long way past the river", "the smoke alarm needed a new battery",
		"the bike had a slow puncture", "we ran out of coffee filters", "the window cleaner came a day early",
	},
	"detail2": {
		"I still owe {nick} a reply about the {context}", "the week got away from me", "half my notes are voice memos",
		"I promised myself an early night and failed", "the weekend plan changed twice", "I was on hold for forty minutes about something else",
		"I kept confusing the two threads with similar names", "the to-do list grew faster than it shrank", "I spent the evening sorting receipts",
	},
	"aside": {
		"none of which mattered to the thread itself", "which is beside the point but it was that kind of week", "so my notes are a bit scattered",
		"and I wrote most of this down on the back of an envelope", "which explains the typos", "so I am reconstructing this from memory and a few screenshots",
		"which is why I want it recorded properly", "and I nearly forgot the whole thing", "hence the late-night note",
		"but the timing is what I want to keep straight", "and I would rather over-record than lose the sequence", "so bear with the detour",
	},
	"tailnote": {
		"Anyway.", "Back to the point.", "Moving on.", "That is the background noise.", "Enough of that.", "None of that matters here.",
	},
	"context": {
		"{nick} is my {relation}, for anyone reading this later, and we have known each other since the {context}.",
		"For context, {nick} — my {relation} — is the one who first raised this, back around the {context}.",
		"{nick} still lives in {city}, which is why half of this happened over messages.",
		"My {relation} {nick} works at {employer} these days, which has nothing to do with this thread but keeps coming up.",
		"The {context} is where {nick} and I got talking about it in the first place.",
		"I keep {nick}'s side of this separate from the paperwork; they are my {relation}, not a supplier.",
		"{nick} would tell this differently, but this is my version.",
		"If you need the back story, {nick} and the {context} are where it starts.",
		"Most of the messages in this thread are between me and {nick}, who is in {city}.",
		"{nick} — {relation}, based in {city} — is the constant across all of these notes.",
	},
}

// storyEventGrammars hold >= 8 frames per event kind. Slots are resolved by
// storyFill after expansion so canonical values are never rewritten by the
// grammar. {who} {prev} {from} {to} {noun} {key1} {key1noun} {key2} {key2noun}
// {subject} {alias} {status} {what} {channel} {nick} {city} {context}.
var storyEventGrammars = map[StoryEventKind]persona.Grammar{
	EventKickoff: {
		"frame": {
			"#lead# {nick} and I finally got {alias} moving; {who} agreed to run point on it #tail#.",
			"{alias} started properly #when#, with {who} as the person holding the thread #tail#.",
			"#lead# it was {nick} who pushed {alias} into being a real piece of work, and {who} took it on #tail#.",
			"The thing everyone calls {alias} kicked off #when#; {who} is the one accountable for it #tail#.",
			"#lead# {who} said yes to owning {alias} after {nick} and I talked it through #tail#.",
			"{alias} got its first proper conversation #when# and {who} walked away owning it #tail#.",
			"#lead# we gave {alias} a start date and a name on it: {who} #tail#.",
			"Once {nick} stopped hedging, {alias} became {who}'s to run #tail#.",
			"#lead# {alias} moved from idea to thread with {who} in charge #tail#.",
		},
		"lead": {"So,", "Right,", "Quick recap:", "For the record,", "Background first:", "Context:", "Where it began:", "Start of the thread:"},
		"when": {"last week", "a couple of weeks back", "the Monday after the {context}", "the same week as the {context}", "over a long phone call", "at the {context}", "in a rushed lunch break"},
		"tail": {"before anyone else volunteered", "with everyone's blessing", "which felt right", "after some persuading", "for now at least", "and I wrote that down", "so nobody could later claim otherwise", "with {nick} nodding along"},
	},
	EventQuote: {
		"frame": {
			"{from} came back with the first quote for {alias} #tail#; that is the {noun} we started from.",
			"The first {noun} on {alias} was {from} #tail#.",
			"#lead# {from} put a number on {alias} #tail#, so they were the opening {noun}.",
			"{alias} got its first price from {from} #tail#.",
			"#lead# the {noun} we began with on {alias} was {from} #tail#.",
			"{from} was first to quote on {alias} #tail#.",
			"We opened {alias} with {from} as the {noun} #tail#.",
			"#lead# {from} sent over their proposal for {alias} #tail#.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"tail": {"within a couple of days", "after one site call", "with a lot of caveats", "faster than expected", "with a neat one-page summary", "after chasing twice", "and it looked reasonable", "which surprised nobody"},
	},
	EventBooking: {
		"frame": {
			"We booked {alias} through {from} #tail#, so they were the first {noun}.",
			"The first {noun} we lined up for {alias} was {from} #tail#.",
			"#lead# {from} was where {alias} started #tail#.",
			"{alias} was originally with {from} #tail#.",
			"#lead# we went with {from} as the {noun} for {alias} #tail#.",
			"{from} took the first booking for {alias} #tail#.",
			"For {alias}, the opening {noun} was {from} #tail#.",
			"#lead# {from} was pencilled in for {alias} #tail#.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"tail": {"after a lot of comparing", "on a recommendation from {nick}", "mostly because of the dates", "without much drama", "with a small deposit", "after one phone call", "because everywhere else was full", "which felt like progress"},
	},
	EventApprovalCapped: {
		"frame": {
			"Finance signed off {alias} but capped it: #capsentence#.",
			"#lead# {alias} got approval with a ceiling — #capsentence#.",
			"The approval on {alias} came with a hard cap; #capsentence#.",
			"#lead# {alias} is approved, capped: #capsentence#.",
			"They approved {alias} on the condition of a cap. #capsentence#.",
			"#lead# the cap on {alias} is the thing to remember: #capsentence#.",
			"{alias} cleared approval with a ceiling attached; #capsentence#.",
			"#lead# for {alias}, #capsentence#, and that is the approved envelope.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"capsentence": {
			"the ceiling is {cap}, {spent} of it is already committed, so what is still open is the difference",
			"we may spend up to {cap}; {spent} has already gone out, and the remainder is what we have left to play with",
			"{cap} is the most it can cost, {spent} is already spoken for, and the gap between them is what remains",
			"the envelope is {cap} in total, of which {spent} is committed — the rest is the open amount",
		},
	},
	EventContactRouteChanged: {
		"frame": {
			"#lead# the review route for {alias} moved from {from} to {to} #tail#.",
			"Anything on {alias} now goes to {to} rather than {from} #tail#.",
			"#lead# {from} is dead for {alias}; use {to} #tail#.",
			"The inbox for {alias} switched: {from} out, {to} in #tail#.",
			"#lead# please treat {to} as the {alias} route and retire {from} #tail#.",
			"{alias} correspondence should hit {to} now, not {from} #tail#.",
			"#lead# they changed where {alias} mail lands — {to} replaces {from} #tail#.",
			"Note for {alias}: {to} is current, {from} is old #tail#.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"tail": {"after a bounce", "because the old one was a personal mailbox", "at the client's request", "with no fuss", "effective immediately", "and I updated the invite", "which caught two people out", "after the {context}"},
	},
	EventVendorSwapped: {
		"frame": {
			"#lead# we swapped the {noun} on {alias}: {from} is out, {to} is in #tail#.",
			"{alias} moved from {from} to {to} #tail#.",
			"#lead# {to} replaced {from} on {alias} #tail#.",
			"The {noun} for {alias} is now {to}, not {from} #tail#.",
			"#lead# {from} dropped off {alias} and {to} picked it up #tail#.",
			"We changed {noun} on {alias} — {to} instead of {from} #tail#.",
			"#lead# after {from} fell through, {to} took over {alias} #tail#.",
			"{alias} now sits with {to}; {from} is history #tail#.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"tail": {"after a missed deadline", "on price", "because of availability", "after {nick} flagged a problem", "with everyone relieved", "and the paperwork followed", "after two awkward calls", "which fixed the timing"},
	},
	EventProviderSwapped: {
		"frame": {
			"#lead# {alias} moved from {from} to {to} #tail#.",
			"We switched the {noun} for {alias}: {to} instead of {from} #tail#.",
			"#lead# {to} took over {alias} from {from} #tail#.",
			"{from} is out for {alias}; {to} is the {noun} now #tail#.",
			"#lead# after {from} let us down, {alias} went to {to} #tail#.",
			"The {noun} on {alias} changed — {from} first, then {to} #tail#.",
			"#lead# {alias} is with {to} now, having started at {from} #tail#.",
			"We rebooked {alias} away from {from} and onto {to} #tail#.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"tail": {"after a double booking", "because the dates slipped", "on {nick}'s advice", "for the better location", "without losing the deposit", "after a long evening of calls", "which everyone preferred", "after the first place cancelled"},
	},
	EventIncident: {
		"frame": {
			"#lead# {alias} hit a snag #tail#, so everything is {status} for the moment; #qty#.",
			"Bad news on {alias}: #tail#. It is {status} until further notice; #qty#.",
			"#lead# {alias} is {status} after a problem #tail#; #qty#.",
			"Something went wrong with {alias} #tail#. Status: {status}. #qty#.",
			"#lead# we had to put {alias} {status} #tail#; #qty#.",
			"{alias} stalled #tail#, and it is {status} now; #qty#.",
			"#lead# an incident on {alias} #tail# left it {status}; #qty#.",
			"{alias} took a hit #tail#. For now it is {status}; #qty#.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"tail": {"when a delivery went missing", "after a burst pipe", "when the key contact went on leave", "after a permit question", "when the numbers did not reconcile", "after a scheduling clash", "when a supplier went quiet", "after an access problem"},
		"qty":  {"{qtyphrase}"},
	},
	EventCorrection: {
		"frame": {
			"#lead# a correction on {alias}: #qty#. The earlier figure is superseded and things are {status}.",
			"Correction for {alias} — #qty#; treat the old number as withdrawn. It is {status}.",
			"#lead# {alias} has been {status}: #qty#, replacing what was first written down.",
			"On {alias}, #qty#. The first version no longer applies; status {status}.",
			"#lead# the {alias} record was {status}. #qty#.",
			"Update to {alias}: #qty#. That overrides the original and leaves it {status}.",
			"#lead# please note {alias} is {status} — #qty#.",
			"{alias} correction: #qty#. Old figure void. Status {status}.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"qty":  {"{qtyphrase}"},
	},
	EventHandoffAssigned: {
		"frame": {
			"#lead# {alias} was handed to {who} #tail#; {prev} stepped back. {who} will {what} by {channel}.",
			"{who} now owns {alias} instead of {prev} #tail#. Next from them: {what}, over {channel}.",
			"#lead# ownership of {alias} moved to {who} #tail#. {who} is going to {what} ({channel}).",
			"{prev} passed {alias} to {who} #tail#; the next move is {who} to {what} by {channel}.",
			"#lead# {who} picked up {alias} from {prev} #tail#, and will {what} via {channel}.",
			"For {alias}, {who} is the owner now, not {prev} #tail#. They said they would {what} by {channel}.",
			"#lead# {alias} is {who}'s now #tail# — {prev} is off it — and {who} will {what}, by {channel}.",
			"The handoff on {alias}: {prev} to {who} #tail#. First action from {who}: {what}, {channel}.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"tail": {"because of holidays", "so it had one owner", "after the {context}", "at {nick}'s suggestion", "to spread the load", "since they knew the history", "with a proper written note", "and nobody objected"},
	},
	EventFollowUp: {
		"frame": {
			"#lead# on {alias} the next step is now {who} to {what}, by {channel} #tail#.",
			"Follow-up for {alias}: {who} will {what} over {channel} #tail#.",
			"#lead# {who} is going to {what} for {alias}, via {channel} #tail#.",
			"The open action on {alias} is with {who}: {what}, {channel} #tail#.",
			"#lead# {alias} is waiting on {who} to {what} ({channel}) #tail#.",
			"Next on {alias}: {who}, {what}, by {channel} #tail#.",
			"#lead# {who} agreed to {what} on {alias} and will do it by {channel} #tail#.",
			"For {alias} the ball is with {who}, who will {what} by {channel} #tail#.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"tail": {"before the end of the month", "once the paperwork clears", "as soon as {nick} confirms", "this week if possible", "so we can move on", "and I will remind them", "which unblocks the rest", "and that replaces the earlier plan"},
	},
	EventOutcome: {
		"frame": {
			"#lead# {alias} is {status} #tail#.",
			"Where {alias} landed: {status} #tail#.",
			"#lead# as of now {alias} stands {status} #tail#.",
			"{alias} ended up {status} #tail#.",
			"#lead# the current state of {alias} is {status} #tail#.",
			"Final word on {alias} for now: {status} #tail#.",
			"#lead# {alias} — {status} #tail#.",
			"So {alias} is {status} #tail#.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"tail": {"and I am glad to see the back of it", "with a few loose ends", "after everything", "which is more than I expected", "and everyone has been told", "pending one signature", "and {nick} is pleased", "at least on paper"},
	},
	EventOutcomeDisputed: {
		"frame": {
			"#lead# {source} insists {alias} is {status}, whatever the other note says #tail#.",
			"According to {source}, {alias} is {status} #tail#.",
			"#lead# {source} has {alias} down as {status} #tail#.",
			"{source} told me {alias} is {status} #tail#.",
			"#lead# the version from {source}: {alias} is {status} #tail#.",
			"Per {source}, {alias} stands {status} #tail#.",
			"#lead# {source}'s record shows {alias} as {status} #tail#.",
			"If you ask {source}, {alias} is {status} #tail#.",
		},
		"lead": {"Then,", "Next,", "After that,", "A few days on,", "The following week,", "Not long after,", "Once that settled,", "Soon after,"},
		"tail": {"and they were quite firm about it", "which does not match my other note", "and I have not reconciled the two", "so the records do not agree", "which I am recording without resolving", "and I trust them about as much as the other source", "so treat both as live", "for what that is worth"},
	},
}

// personalRootGrammar renders the root theme event of a personal arc. The theme
// vocabulary comes from personalThemeBank via {subject}; the frames are shared.
var personalRootGrammar = persona.Grammar{
	"frame": {
		"#lead# {nick} and I got {alias} off the ground #when#; {who} is the one keeping track of it #tail#.",
		"{alias} became a real thing #when#, and {who} took charge of it #tail#.",
		"#lead# {alias} started #when# with {who} holding the plan #tail#.",
		"It was {nick} who made {alias} happen #when#; {who} agreed to run it #tail#.",
		"#lead# {who} volunteered to look after {alias} #when# #tail#.",
		"{alias} kicked off #when#. {who} owns the arrangements #tail#.",
		"#lead# we gave {alias} a proper start #when# and {who} is on point #tail#.",
		"After {nick} kept bringing it up, {alias} got going #when# with {who} in charge #tail#.",
		"#lead# {alias} began #when#; the person to ask is {who} #tail#.",
	},
	"lead": {"So,", "Right,", "Quick recap:", "For the record,", "Background first:", "Context:", "Where it began:", "Start of the thread:"},
	"when": {"last month", "a couple of weekends ago", "just after the {context}", "the week of the {context}", "over dinner", "on a long drive", "during a rainy Sunday"},
	"tail": {"before anyone else could", "with everyone's blessing", "which felt right", "after some persuading", "for now at least", "and I wrote that down", "so nobody could later claim otherwise", "with {nick} nodding along"},
}

// storyFactGrammars produce 4–6 renderings per planted fact. The single %s is
// the fact value; the caller expands the grammar first, then fills.
var storyFactGrammars = map[string]persona.Grammar{
	"key1": {
		"r": {
			"#lead# {key1noun} %s#tail#", "The {key1noun} on it is %s#tail#", "It was logged as {key1noun} %s#tail#",
			"%s is the {key1noun} for this#tail#", "I filed it under {key1noun} %s#tail#", "#lead# %s ({key1noun})#tail#",
		},
		"lead": {"Everything is under", "For lookup:", "The thread carries", "Note the", "Reference:", "Filed as"},
		"tail": {".", ", which I copied into my notes.", " — keep that handy.", ", so nothing gets confused with the other threads.", "; that is the identifier that matters.", ", same as on the paperwork."},
	},
	"key1link": {
		"r": {
			"This is the same thread as {key1noun} %s#tail#", "It links back to {key1noun} %s#tail#", "The {key1noun} from before, %s, is the one this belongs to#tail#",
			"%s — the {key1noun} — is what ties this to the earlier conversation#tail#", "For the avoidance of doubt, this is {key1noun} %s#tail#", "Same {key1noun}: %s#tail#",
		},
		"tail": {".", ", not the look-alike thread.", "; I checked twice.", ", so the history joins up.", " (I keep mixing up the two).", "."},
	},
	"key2": {
		"r": {
			"From here on the records use {key2noun} %s#tail#", "The {key2noun} they issued is %s#tail#", "Everything after this is filed under {key2noun} %s#tail#",
			"%s is the {key2noun} on the paperwork#tail#", "Their side calls it {key2noun} %s#tail#", "It now carries {key2noun} %s#tail#",
		},
		"tail": {".", ", which is what later notes will quote.", " — the other reference stays on the old paperwork.", "; that is the one to search for.", ", and I have stopped using the old reference.", "."},
	},
	"key2link": {
		"r": {
			"This is against {key2noun} %s#tail#", "Under {key2noun} %s#tail#", "The paperwork quotes {key2noun} %s#tail#",
			"Same {key2noun} as before: %s#tail#", "Filed to {key2noun} %s#tail#", "{key2noun} %s, for the record#tail#",
		},
		"tail": {".", ", same as the earlier note.", "; nothing else changed on the reference.", ", so it is the same job.", " — do not mix it up with the similarly named one.", "."},
	},
	"lesson": {
		"r": {
			"The thing I took from it: %s#tail#", "If there is a lesson, it is this — %s#tail#", "Rule for next time: %s#tail#",
			"What stuck with me was simple: %s#tail#", "I wrote one line on a sticky note afterwards: %s#tail#", "Takeaway: %s#tail#",
		},
		"tail": {".", ", and I mean it this time.", "; I have said it before.", ", which sounds obvious now.", ".", " — cheap advice, expensive to ignore."},
	},
}

// storyFill resolves {slot} placeholders after grammar expansion.
func storyFill(text string, slots map[string]string) string {
	keys := make([]string, 0, len(slots))
	for k := range slots {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	pairs := make([]string, 0, 2*len(keys))
	for _, k := range keys {
		pairs = append(pairs, "{"+k+"}", slots[k])
	}
	return strings.NewReplacer(pairs...).Replace(text)
}

// storyExpandFact expands a fact grammar into 4–6 distinct renderings, each
// carrying exactly one %s.
func storyExpandFact(r *rand.Rand, key string, slots map[string]string) []string {
	g := storyFactGrammars[key]
	seen := map[string]bool{}
	out := make([]string, 0, 6)
	for attempt := 0; attempt < 40 && len(out) < 6; attempt++ {
		text := storyFill(persona.Expand(r, g, "r"), slots)
		if strings.Count(text, "%s") != 1 || seen[text] {
			continue
		}
		seen[text] = true
		out = append(out, text)
	}
	for len(out) < 4 {
		out = append(out, storyFill(g["r"][len(out)%len(g["r"])], slots))
	}
	return out
}

// eventSpecFor returns the catalog entry for a kind (personal theme roots share
// the kickoff spec shape).
func eventSpecFor(kind StoryEventKind) eventSpec {
	for _, spec := range storyEventCatalog {
		if spec.kind == kind {
			return spec
		}
	}
	for _, theme := range personalThemes {
		if theme == kind {
			return eventSpec{kind: kind, personal: true}
		}
	}
	return eventSpec{kind: kind}
}

// isRootEvent reports whether kind opens an arc (kickoff or a personal theme).
func isRootEvent(kind StoryEventKind) bool {
	if kind == EventKickoff {
		return true
	}
	for _, theme := range personalThemes {
		if theme == kind {
			return true
		}
	}
	return false
}

// drawStoryEvents draws one arc's typed event multiset and orders it with a
// constrained topological shuffle: the root is first, the quote/booking hop is
// second (so the two-hop join is always established before any state record),
// the terminal outcome is last, and every other event is placed uniformly among
// the events whose preconditions are already in the timeline. Revisions (a
// second handoff or follow-up, a correction) are placed after the record they
// supersede by construction of their preconditions.
func drawStoryEvents(r *rand.Rand, kind StoryKind, theme StoryEventKind, forceRevision, disagree bool, forced []StoryEventKind) []StoryEvent {
	root := EventKickoff
	hop := EventQuote
	swap := EventVendorSwapped
	optional := []StoryEventKind{EventApprovalCapped, EventContactRouteChanged, EventIncident, EventCorrection}
	if kind == StoryPersonal {
		root, hop, swap = theme, EventBooking, EventProviderSwapped
		optional = []StoryEventKind{EventIncident, EventCorrection}
	}
	kinds := []StoryEventKind{root, hop, swap, EventHandoffAssigned, EventFollowUp, EventOutcome}
	// Forced optional events (a money arc needs its approval cap) come first;
	// 0–2 further optional events keep the arc within 6–9 events after revisions.
	kinds = append(kinds, forced...)
	optN := r.Intn(3)
	r.Shuffle(len(optional), func(i, j int) { optional[i], optional[j] = optional[j], optional[i] })
	for _, k := range optional {
		if optN == 0 {
			break
		}
		if containsKind(kinds, k) {
			continue
		}
		kinds = append(kinds, k)
		optN--
	}
	revisions := r.Intn(3) // 0–2 contradictory updates
	if forceRevision && revisions == 0 && !containsKind(kinds, EventCorrection) {
		revisions = 1
	}
	revisionPool := []StoryEventKind{EventHandoffAssigned, EventFollowUp}
	for i := 0; i < revisions; i++ {
		kinds = append(kinds, revisionPool[r.Intn(len(revisionPool))])
	}
	for len(kinds) > 9 {
		kinds = kinds[:len(kinds)-1]
	}
	// Constrained topological shuffle.
	placed := []StoryEvent{{Kind: root, Position: 0}, {Kind: hop, Position: 1}}
	remaining := make([]StoryEventKind, 0, len(kinds))
	for _, k := range kinds[2:] {
		if k != EventOutcome {
			remaining = append(remaining, k)
		}
	}
	seenKind := map[StoryEventKind]int{root: 1, hop: 1}
	for len(remaining) > 0 {
		ready := make([]int, 0, len(remaining))
		for i, k := range remaining {
			ok := true
			for _, need := range eventSpecFor(k).requires {
				if !preconditionMet(seenKind, need) {
					ok = false
				}
			}
			if ok {
				ready = append(ready, i)
			}
		}
		if len(ready) == 0 {
			// Preconditions are acyclic by construction; fall back to timeline order
			// so a catalog edit can never hang generation.
			ready = []int{0}
		}
		pick := ready[r.Intn(len(ready))]
		k := remaining[pick]
		event := StoryEvent{Kind: k, Position: len(placed), Revision: seenKind[k] > 0 || k == EventCorrection}
		placed = append(placed, event)
		seenKind[k]++
		remaining = append(remaining[:pick], remaining[pick+1:]...)
	}
	placed = append(placed, StoryEvent{Kind: EventOutcome, Position: len(placed)})
	if disagree {
		placed = append(placed, StoryEvent{Kind: EventOutcomeDisputed, Position: len(placed)})
	}
	return placed
}

// preconditionMet resolves a catalog precondition against the timeline so far.
// The kickoff precondition is satisfied by any root event (a personal theme
// opens its arc the way kickoff opens a business arc), and the quote
// precondition by either hop kind (quote or booking).
func preconditionMet(seen map[StoryEventKind]int, need StoryEventKind) bool {
	switch need {
	case EventKickoff:
		if seen[EventKickoff] > 0 {
			return true
		}
		for _, theme := range personalThemes {
			if seen[theme] > 0 {
				return true
			}
		}
		return false
	case EventQuote, EventBooking:
		return seen[EventQuote]+seen[EventBooking] > 0
	default:
		return seen[need] > 0
	}
}

func containsKind(kinds []StoryEventKind, kind StoryEventKind) bool {
	for _, k := range kinds {
		if k == kind {
			return true
		}
	}
	return false
}

// storyJoinKey renders one hidden reference in the drawn shape.
func storyJoinKey(r *rand.Rand, shape int, hex string) string {
	frame := storyJoinKeyShapes[shape].frame
	num4 := fmt.Sprintf("%04d", 2024+r.Intn(3))
	num5 := fmt.Sprintf("%05d", 10000+r.Intn(89999))
	alpha := string(rune('A'+r.Intn(26))) + string(rune('A'+r.Intn(26)))
	return strings.NewReplacer(
		"{hex6}", strings.ToUpper(hex[:6]), "{hex8}", strings.ToUpper(hex[:8]),
		"{num4}", num4, "{num5}", num5, "{alpha2}", alpha,
	).Replace(frame)
}

// levenshtein is the edit distance used by the anchor-uniqueness construction
// and tests: subject aliases and decoy aliases are kept >= 4 edits apart so
// 1–3 typo edits can never move an anchor onto another thread.
func levenshtein(a, b string) int {
	ra, rb := []rune(strings.ToLower(a)), []rune(strings.ToLower(b))
	prev := make([]int, len(rb)+1)
	cur := make([]int, len(rb)+1)
	for j := range prev {
		prev[j] = j
	}
	for i := 1; i <= len(ra); i++ {
		cur[0] = i
		for j := 1; j <= len(rb); j++ {
			cost := 1
			if ra[i-1] == rb[j-1] {
				cost = 0
			}
			cur[j] = minInt(prev[j]+1, cur[j-1]+1, prev[j-1]+cost)
		}
		prev, cur = cur, prev
	}
	return prev[len(rb)]
}

func minInt(values ...int) int {
	m := values[0]
	for _, v := range values[1:] {
		if v < m {
			m = v
		}
	}
	return m
}
