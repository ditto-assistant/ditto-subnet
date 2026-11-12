package universe

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v19 seeded phrase grammar (DITTO-2738, epic DITTO-2736).
//
// Through v18 every memory question family draws its wording from a short
// fixed list picked per seed: twelve story task frames per oracle, three body
// forms per business family, three per personal domain and abstention probe.
// The top v18 agents route on that wording: one agent's routing regexes hold
// 21 three-word terms, 20 of them verbatim from this package ("rule did i
// set", "owes the next", "has the ball", "as far as the records go").
//
// At bench_version >= 19 a question is composed per seed and per question
// identity from:
//   - intent frames, several per family, in more than one clause order
//     ("On March 3, which …" / "Which … as of March 3?");
//   - synonym slots for verbs, nouns and time phrases (<now>, <hold>, …);
//   - an optional politeness or filler lead ("Quick question:", "Remind me,",
//     "Do you know");
//   - an optional casual register (contractions, lower-case lead);
//   - an optional answer-format closer.
//
// Rendering is a pure function of (family, seed, identity, slots): every pick
// is a seeded hash draw (v19Draw) keyed by the family, the question identity
// and the decision, so adding a family or a question never moves another
// rendering, and no LLM runs at generation time. Slot values are filled verbatim after
// the template is composed, so the entity, alias, date and quantity the
// oracle and grader key on are never rewritten, and a frame never names a
// graded value. Every v19 family keeps the semantic markers its meaning
// rests on (v19Grammar.markers) and never carries a contradicting one
// (v19Grammar.forbid), so an as-of question cannot read as a current-state
// one; v19_phrasing_test.go proves both over every family.
//
// The v2..v18 fixed lists are untouched: each hook below renders them
// unchanged under 19, so earlier vectors stay byte-identical.

// The v19 phrase families. Story families are keyed by their oracle kind.
const (
	V19PhraseStoryAnchor     = "story-anchor"
	V19PhraseStoryAnchorTail = "story-anchor-tail"

	V19PhraseProgramOwnerCurrent   = "program-owner-current"
	V19PhraseProgramStatusCurrent  = "program-status-current"
	V19PhraseProgramLatestEvent    = "program-latest-event"
	V19PhraseProgramNextAction     = "program-next-action"
	V19PhraseProgramChannelCurrent = "program-channel-current"
	V19PhraseProgramDisagree       = "program-records-disagree"
	V19PhraseProgramSequenceNth    = "program-sequence-nth"
	V19PhraseProgramSequenceAfter  = "program-sequence-after"
	V19PhraseProgramSequenceBefore = "program-sequence-before"
	V19PhraseProgramChannelAsOf    = "program-channel-as-of"
	V19PhraseProgramOpenStep       = "program-open-step"
	V19PhraseProgramStepActor      = "program-step-actor"
	V19PhraseProgramItemHolder     = "program-item-holder"
	V19PhraseProgramOwnerContact   = "program-owner-contact"
	V19PhraseProgramOwnerName      = "program-owner-name"

	V19PhrasePersonalChore        = "personal-chore-holder"
	V19PhrasePersonalAppointment  = "personal-appointment-date"
	V19PhrasePersonalSchoolList   = "personal-school-list"
	V19PhrasePersonalMilestone    = "personal-milestone-status"
	V19PhrasePersonalSubscription = "personal-subscription-action"
	V19PhrasePersonalDeparture    = "personal-departure-time"
	V19PhrasePersonalClub         = "personal-club-organiser"

	V19PhraseContactEmail          = "contact-current-email"
	V19PhraseContactColleagueEmail = "contact-colleague-email"
	V19PhraseSignalHandle          = "signal-handle"
	V19PhraseTripLegDays           = "trip-leg-days"
	V19PhraseBalanceUnpaid         = "balance-unpaid"
	V19PhraseBalanceOutstanding    = "balance-outstanding"
)

// V19Slots are the values a family's frames interpolate, by slot name.
type V19Slots map[string]string

// v19Grammar is one family of the phrase grammar.
type v19Grammar struct {
	// args names the positional arguments of the program and personal hooks
	// (v13Member.ask), in the order the v18 forms take them.
	args []string
	// frames are the intent frames: {slot} is a value filled verbatim,
	// <class> a synonym class (family classes first, then v19SharedClasses),
	// [a|b|] an inline choice where an empty branch makes it optional.
	frames  []string
	classes map[string][]string
	// closers are answer-format sentences appended after the question; an
	// empty entry renders none.
	closers []string
	// plain suppresses the politeness lead (story parts the caller composes).
	plain bool
	// required slots appear verbatim in every rendering.
	required []string
	// markers: every rendering carries one term of each set (whole words,
	// case-insensitive; a trailing * matches a word prefix; {slot} is filled).
	markers [][]string
	// forbid: no rendering carries any of these terms.
	forbid []string
}

// v19SharedClasses are the synonym classes every family may use.
var v19SharedClasses = map[string][]string{
	// <now> closes a current-state question.
	"now": {"now", "currently", "today", "at present", "presently", "right now", "as we speak", "nowadays", "these days", "at the moment", "as of today"},
	// <bnow> is the business form: the newest record governs.
	"bnow": {
		"now", "today", "at present", "currently", "once every reassignment is applied", "after all the recorded changes",
		"following the most recent change", "going by the newest entry", "as of the latest record", "once the later entries are applied",
		"per the most recent record", "with every update taken into account",
	},
}

// v19Politeness are fillers that lead any frame; v19Indirect lead only a
// frame that opens with a question word ("Do you know who …").
var v19Politeness = []string{
	"", "", "", "", "", "", "Quick question: ", "Remind me, ", "Hey, ", "Okay, ", "So, ", "Real quick, ", "Sorry to ask again, but ",
	"Just checking: ", "I've lost track — ", "Help me out: ", "Need a hand — ", "Out of curiosity, ", "One for you: ", "Can't recall this one: ",
}

var v19Indirect = []string{"Do you know ", "Can you tell me ", "Could you check ", "Any idea ", "Mind checking ", "Can you look up ", "Do you remember "}

var v19WhWords = map[string]bool{"who": true, "what": true, "which": true, "where": true, "when": true, "how": true, "whose": true}

// v19Contractions are the casual register, applied to template text only.
var v19Contractions = [][2]string{
	{"who is ", "who's "}, {"what is ", "what's "}, {"where is ", "where's "}, {"when is ", "when's "}, {"how is ", "how's "},
	{"that is ", "that's "}, {"it is ", "it's "}, {"do not ", "don't "}, {"does not ", "doesn't "}, {"is not ", "isn't "},
	{"I would ", "I'd "}, {"they are ", "they're "}, {"we are ", "we're "},
	{"I am ", "I'm "}, {"there is ", "there's "}, {"have not ", "haven't "},
}

// V19PhraseFamilies returns every family name, sorted.
func V19PhraseFamilies() []string {
	out := make([]string, 0, len(v19Grammars))
	for name := range v19Grammars {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// V19Phrase renders one question of family for (seed, identity). identity
// names the question within the seed (its oracle kind and index, or group and
// variant), so two questions of one seed draw independently. Every slot the
// family's frames name must be present. It panics on an unknown family, as a
// missing grammar is a programming error.
func V19Phrase(family string, seed int64, identity string, slots V19Slots) string {
	g, ok := v19Grammars[family]
	if !ok {
		panic("v19 phrase grammar missing for " + family)
	}
	return v19Render(g, family, seed, identity, slots, !g.plain)
}

// v19Or renders family at bench_version >= 19 and returns fallback (the v18
// fixed-list wording) below it.
func (w World) v19Or(fallback, family, identity string, slots V19Slots) string {
	if w.BenchVersion < protocol.BenchVersionV19 {
		return fallback
	}
	return V19Phrase(family, w.Seed, identity, slots)
}

func v19Render(g v19Grammar, family string, seed int64, identity string, slots V19Slots, polite bool) string {
	pick := func(decision string, n int) int {
		return v19Draw(seed, family+"|"+identity+"|"+decision, n)
	}
	body := v19Expand(g, g.frames[pick("frame", len(g.frames))], pick)
	casual := pick("register", 4) == 0
	if casual {
		for _, c := range v19Contractions {
			body = strings.ReplaceAll(body, c[0], c[1])
			body = strings.ReplaceAll(body, v13Capitalize(c[0]), v13Capitalize(c[1]))
		}
	}
	if polite {
		bank := v19Politeness
		if v19WhWords[strings.ToLower(v19FirstWord(body))] {
			bank = append(append([]string(nil), v19Politeness...), v19Indirect...)
		}
		if lead := bank[pick("polite", len(bank))]; lead != "" {
			body = lead + v19Lower(body)
		}
	}
	if casual {
		body = v19Lower(body)
	} else {
		body = v19Upper(body)
	}
	// A leading slot keeps its value's case: slot values are never rewritten.
	out := v19Fill(body, slots, family)
	if len(g.closers) > 0 {
		if closer := g.closers[pick("closer", len(g.closers))]; closer != "" {
			out += " " + v19Fill(closer, slots, family)
		}
	}
	return out
}

// v19Expand resolves <class> and [a|b] choices, innermost first, drawing each
// occurrence independently.
func v19Expand(g v19Grammar, text string, pick func(string, int) int) string {
	for n := 0; n < 64; n++ {
		open := strings.LastIndexAny(text, "<[")
		if open < 0 {
			return text
		}
		closing := byte('>')
		if text[open] == '[' {
			closing = ']'
		}
		end := strings.IndexByte(text[open:], closing)
		if end < 0 {
			panic("v19 phrase: unclosed choice in " + text)
		}
		inner := text[open+1 : open+end]
		var options []string
		if closing == '>' {
			options = g.classes[inner]
			if options == nil {
				options = v19SharedClasses[inner]
			}
			if options == nil {
				panic("v19 phrase: unknown class <" + inner + ">")
			}
		} else {
			options = strings.Split(inner, "|")
		}
		choice := options[pick(fmt.Sprintf("choice:%d:%s", n, inner), len(options))]
		text = text[:open] + choice + text[open+end+1:]
	}
	panic("v19 phrase: choice depth exceeded in " + text)
}

var v19SlotPattern = regexp.MustCompile(`\{([a-z]+)\}`)

func v19Fill(text string, slots V19Slots, family string) string {
	return v19SlotPattern.ReplaceAllStringFunc(text, func(m string) string {
		name := m[1 : len(m)-1]
		value, ok := slots[name]
		if !ok {
			panic(fmt.Sprintf("v19 phrase %s: slot %q missing", family, name))
		}
		return value
	})
}

// v19Draw is a uniform draw in [0,n) keyed by (seed, key). The FNV hash's
// low bits depend only on the low bits of the state before the key's shared
// suffix, so draws over a small n would move together; a splitmix64
// finalizer decorrelates them.
func v19Draw(seed int64, key string, n int) int {
	x := uint64(v10Seed(seed, "v19-phrase:"+key))
	x ^= x >> 30
	x *= 0xbf58476d1ce4e5b9
	x ^= x >> 27
	x *= 0x94d049bb133111eb
	x ^= x >> 31
	return int(x % uint64(n))
}

func v19FirstWord(text string) string {
	end := strings.IndexFunc(text, func(r rune) bool { return !unicode.IsLetter(r) })
	if end < 0 {
		return text
	}
	return text[:end]
}

// v19Lower lower-cases a leading template letter; a leading slot keeps its
// value's case. "I" stays capital.
func v19Lower(text string) string {
	r, size := utf8.DecodeRuneInString(text)
	if r == '{' || !unicode.IsLetter(r) || (r == 'I' && (len(text) == size || !unicode.IsLetter(rune(text[size])))) {
		return text
	}
	return string(unicode.ToLower(r)) + text[size:]
}

func v19Upper(text string) string {
	r, size := utf8.DecodeRuneInString(text)
	if r == '{' || !unicode.IsLetter(r) {
		return text
	}
	return string(unicode.ToUpper(r)) + text[size:]
}

// ── Hooks ───────────────────────────────────────────────────────────────────

// ask renders a business-family question: v13Question's fixed forms below
// v19; at >= 19 the same opener (a schema instruction shared by every
// family) and a grammar body in place of the fixed form and closer.
func (m *v13Member) ask(d *v13Draws, family string, group, variant int, forms, closers []string, args ...any) {
	m.phrase = family
	if d.benchVersion < protocol.BenchVersionV19 {
		m.Question = v13Question(d.seed, group, variant, forms, closers, args...)
		return
	}
	opener := v13Pick(d.seed, fmt.Sprintf("qopen-%d-%d", group, variant), v13QuestionOpeners)
	m.Question = opener + " " + V19Phrase(family, d.seed, fmt.Sprintf("business:%d:%d", group, variant), v19ArgSlots(family, args))
}

// askPersonal renders a personal-domain question: v13PersonalQuestion's
// fixed opener, form and closer below v19, the grammar at >= 19.
func (m *v13Member) askPersonal(d *v13Draws, family string, group, variant int, forms, closers []string, args ...any) {
	m.phrase = family
	if d.benchVersion < protocol.BenchVersionV19 {
		m.Question = v13PersonalQuestion(d.seed, group, variant, forms, closers, args...)
		return
	}
	m.Question = V19Phrase(family, d.seed, fmt.Sprintf("personal:%d:%d", group, variant), v19ArgSlots(family, args))
}

func v19ArgSlots(family string, args []any) V19Slots {
	g := v19Grammars[family]
	if len(g.args) != len(args) {
		panic(fmt.Sprintf("v19 phrase %s: %d args, grammar names %d", family, len(args), len(g.args)))
	}
	slots := make(V19Slots, len(args))
	for i, name := range g.args {
		slots[name] = fmt.Sprint(args[i])
	}
	return slots
}

// v19StoryQuestion composes a story question from an anchor that names the
// thread (alias, nickname) and the oracle's task, in either order.
func v19StoryQuestion(seed int64, arcID, kind string, slots V19Slots) string {
	identity := arcID + ":" + kind
	task := v19Grammars[kind]
	if v19Draw(seed, "story-order|"+identity, 3) == 0 {
		return v19Render(task, kind, seed, identity, slots, true) + " " +
			v19Render(v19Grammars[V19PhraseStoryAnchorTail], V19PhraseStoryAnchorTail, seed, identity, slots, false)
	}
	return v19Render(v19Grammars[V19PhraseStoryAnchor], V19PhraseStoryAnchor, seed, identity, slots, true) + " " +
		v19Render(task, kind, seed, identity, slots, false)
}

// ── Grammar ─────────────────────────────────────────────────────────────────
//
// No frame names a status, event, channel, action or other graded value; a
// story frame never uses a story status word (storyStatusVocabulary), which
// the answer-leak proof would reject.

// v19Current is the marker set of a current-state family.
var v19Current = []string{
	"now", "currently", "today", "at present", "presently", "nowadays", "these days", "at the moment", "as we speak",
	"latest", "newest", "most recent", "after all", "once every", "once the later", "with every update", "since the", "current",
}

// v19NotAsOf are the terms an as-of or sequence question must not carry: any
// of them would turn "on March 3" into "now" or "after March 3".
var v19NotAsOf = []string{"now", "currently", "today", "these days", "nowadays", "at present", "latest", "newest", "since", "most recent"}

// v19Grammars is every family, by name.
var v19Grammars = v19Merge(v19StoryGrammars, v19ProgramGrammars, v19PersonalGrammars, v19AbstentionGrammars)

func v19Merge(banks ...map[string]v19Grammar) map[string]v19Grammar {
	out := map[string]v19Grammar{}
	for _, bank := range banks {
		for name, g := range bank {
			if _, dup := out[name]; dup {
				panic("v19 phrase family defined twice: " + name)
			}
			out[name] = g
		}
	}
	return out
}

var v19StoryGrammars = map[string]v19Grammar{
	V19PhraseStoryAnchor: {
		frames: []string{
			"<about> {alias}, <withnick>.",
			"{alias} — <withnick>.",
			"<recall> {alias}? <nickpart>",
			"{nick} and I <began> {alias}<anchorend>",
			"<asking> {alias}, <withnick>.",
			"Let's <revisit> {alias}, <withnick>.",
			"There's {alias}, <withnick>.",
			"Context: {alias}, <withnick>.",
		},
		classes: map[string][]string{
			"about":     {"Re", "Concerning", "On the subject of", "As for", "Turning to", "Switching to", "Now, about", "So, about"},
			"withnick":  {"{nick}'s [baby|pet project|initiative|idea]", "the one {nick} [got me into|pulled me into|talked me into|dragged me into]", "[shared|run|organised|cooked up] with {nick}", "which {nick} and I [set up|got going|kicked into gear]", "[with|alongside|together with] {nick}", "{nick} was [in on it|involved|part of it]"},
			"recall":    {"Remember", "Recall", "You'll recall", "Thinking of", "Picture"},
			"nickpart":  {"[It was|That was|That's] {nick}'s [baby|idea|initiative].", "{nick} [got me into it|was in on it|was part of it].", "[With|Alongside] {nick}."},
			"began":     {"set up", "launched", "took on", "got into"},
			"anchorend": {" [a while back|some time ago|together|between us].", "."},
			"asking":    {"I'm asking about", "My question is about", "This one's about", "This concerns", "I'm on"},
			"revisit":   {"talk about", "go over", "look at", "come back to"},
		},
		required: []string{"alias", "nick"},
	},
	V19PhraseStoryAnchorTail: {
		frames: []string{
			"I mean {alias}, <withnick>.",
			"(That's {alias}, <withnick>.)",
			"This is <re> {alias} — <withnick>.",
			"Context: {alias}, <withnick>.",
			"I'm thinking of {alias}, <withnick>.",
		},
		classes: map[string][]string{
			"re":       {"for", "on", "regarding", "about"},
			"withnick": {"{nick}'s [baby|pet project|initiative|idea]", "the one {nick} [got me into|pulled me into|talked me into]", "[shared|run|organised] with {nick}", "[with|alongside] {nick}", "{nick} was [in on it|involved]"},
		},
		required: []string{"alias", "nick"},
		plain:    true,
	},
	oracleStoryOwnerCurrent: {
		frames: []string{
			"Who <hold> it <now>?",
			"<nowlead>, who <hold> it?",
			"Which person <hold> it <now>?",
			"Whose <plate> is it on <now>?",
			"[Tell me|Name] the person <looking> it <now>.",
			"If I need its owner <now>, which person [do I contact|should I approach|is that]?",
			"Who's [got|taken] [charge of it|ownership of it|it over] <now>?",
		},
		classes: map[string][]string{
			"hold":    {"is looking after", "is handling", "has charge of", "is minding", "runs", "is steering", "has ownership of", "is the go-to for", "is carrying"},
			"nowlead": {"These days", "Right now", "At present", "Today", "As we speak", "Currently"},
			"plate":   {"plate", "desk", "watch", "patch"},
			"looking": {"looking after", "minding", "steering", "carrying", "in charge of"},
		},
		markers: [][]string{v19Current},
	},
	oracleStoryStatusCurrent: {
		frames: []string{
			"How's it looking <now>?",
			"Where has it [got to|landed|wound up] <now>?",
			"What stage has it reached <now>?",
			"Status check: where is it [at|sitting] <now>?",
			"[Can you give me|I'd like] a status [update|read] on it.",
			"What's the [situation|position] with it <now>?",
			"How [are things|is it] [going|looking] with it <now>?",
		},
		markers: [][]string{{"now", "currently", "today", "at present", "presently", "nowadays", "these days", "at the moment", "as we speak", "as of today", "status", "landed", "wound up", "got to", "stage"}},
	},
	oracleStoryStatusDisagree: {
		frames: []string{
			"<statusask> <conflictask>",
			"<conflictask> <statusask>",
		},
		classes: map[string][]string{
			"statusask":   {"What's its status <now>?", "Where has it landed?", "How do things look with it <now>?", "What stage has it reached <now>?"},
			"conflictask": {"Should my entries [contradict each other|tell different stories|point different ways], [give me both versions|spell out each one].", "[Flag|Point out|Mention] any clash between my entries.", "Do all my entries [agree on it|tell one story]? If not, [say which says what|list each version].", "Where the entries [differ|contradict each other], [give me both versions|spell out each one]."},
		},
		markers: [][]string{{"contradict", "clash", "different stories", "different ways", "agree on it", "one story", "differ"}},
	},
	oracleStoryOrder: {
		frames: []string{
			"Which {noun} did we <begin>, and which one <after>?",
			"Who was our {noun} <early>, and who <follow>?",
			"[Remind me of|Tell me] the <initial> {noun} and the <later> {noun}, in that order.",
			"<early>, which {noun} [did we use|was it]? And which {noun} <after>?",
			"Earlier {noun} versus later {noun}: [who were they|name them] in sequence?",
		},
		classes: map[string][]string{
			"begin":   {"begin with", "have initially", "have at the start", "go with originally"},
			"after":   {"came in after", "succeeded it", "stepped in later", "followed", "replaced the first"},
			"early":   {"at the beginning", "to begin with", "early on", "initially", "originally"},
			"follow":  {"followed", "came next", "succeeded them", "stepped in after"},
			"initial": {"initial", "earlier", "first", "original"},
			"later":   {"later", "newer", "follow-on", "replacement"},
		},
		required: []string{"noun"},
		markers:  [][]string{{"begin*", "initial*", "early", "earlier", "first", "original*", "beginning", "start"}, {"after", "later", "next", "succeeded", "followed", "replace*", "newer", "follow-on", "sequence", "order"}},
	},
	oracleStoryNextAction: {
		frames: []string{
			"What's the <upcoming> <task> on it? Also: <whose>, plus <channel>?",
			"<three>: the <task> <comingup>, the person doing it, the channel for it.",
			"Who [is up|goes] next on it? Give me the name, what they're doing, plus <channelnoun>.",
			"Next on it — who's doing what, using which channel?",
			"Lay out the <upcoming> <task>: <whose>? <channel>?",
		},
		classes: map[string][]string{
			"upcoming":    {"upcoming", "pending", "coming", "outstanding"},
			"task":        {"task", "step", "job", "move"},
			"comingup":    {"coming up", "still pending", "ahead"},
			"whose":       {"whose job is it", "who's taking it", "which person is doing it"},
			"channel":     {"what channel will they use", "over what medium", "using which channel", "by which contact method"},
			"three":       {"I need three things", "Break it down for me", "Three parts, please"},
			"channelnoun": {"the contact method", "the medium they'll use", "the channel"},
		},
		markers: [][]string{{"next", "upcoming", "pending", "coming", "outstanding", "ahead"}, {"who", "whose", "person"}, {"channel", "contact method", "medium"}},
	},
	oracleStoryQuantity: {
		frames: []string{
			"With the <change> <noted> factored in, what's the {unit} <total> <now>?",
			"<factoring> the <change> <noted>, what {unit} <total> [do we end up with|are we looking at]?",
			"What {unit} <total> do the records give once the <change> is [in|applied]?",
			"Run the <change> <noted> through: what's the {unit} <total> <now>?",
		},
		classes: map[string][]string{
			"total":     {"total", "tally", "number", "amount", "count", "value"},
			"change":    {"change", "adjustment", "update"},
			"noted":     {"I logged", "that was recorded", "from my notes", "on record"},
			"factoring": {"Factoring in", "Allowing for", "Counting", "Taking in"},
		},
		required: []string{"unit"},
		markers:  [][]string{{"change", "adjustment", "update"}},
	},
	oracleStoryLessonClaims: {
		frames: []string{
			"Once it was [over|finished|all said and done], which <lesson> came out of it for me?",
			"What <lesson> [came out of it|emerged from it] for me, in the end?",
			"Going forward from it, what was I going to [keep in mind|do another way]?",
			"Which <lesson> [had I recorded|was in my notes|got written down] once it [ended|was over]?",
			"Remind me of the <lesson> I [took|carried] from it.",
			"Looking back, what was my [big|main] <lesson> from it?",
		},
		classes: map[string][]string{"lesson": {"principle", "lesson", "conclusion", "rule of thumb", "insight"}},
		markers: [][]string{{"lesson", "principle", "conclusion", "rule of thumb", "insight", "keep in mind", "another way"}},
	},
	oracleStoryOwnerEmail: {
		frames: []string{
			"Which <mail> <reaches> <whoever> it <now>?",
			"<whoever> it <now> — what's their <mail>?",
			"I need the <mail> of <whoever> it <now>.",
			"Find <whoever> it <now> and give me their <mail>.",
			"How do I email <whoever> it at work <now>? Which address?",
		},
		classes: map[string][]string{
			"mail":    {"office email", "work inbox address", "email address at work", "work address for email", "email at work"},
			"reaches": {"reaches", "gets to", "will reach"},
			"whoever": {"whoever is looking after", "the person in charge of", "whoever runs", "the person steering", "whoever has charge of"},
		},
		markers: [][]string{{"email", "inbox", "address"}, {"work", "office"}, v19Current},
	},
}

// v19PersonCloser and the other closers state the answer format; the empty
// entries leave it to the frame.
var (
	v19PersonCloser  = []string{"", "", "", "A name will do.", "Only the person, please.", "I just need who.", "Person only — no contact details."}
	v19StatusCloser  = []string{"", "", "Either the local term or everyday words.", "The status word is enough.", "Use the batch's term or plain words.", "One status, please."}
	v19EventCloser   = []string{"", "", "The event name, not when.", "No date needed, only the event.", "Which event — leave out the date."}
	v19ChannelCloser = []string{"", "", "Channel name is enough.", "Only the channel, please.", "Channel only."}
	v19PairCloser    = []string{"", "", "The step, plus a name.", "The task first, then the name.", "Step plus person, please."}
)

// v19ProgramGrammars cover the business families of v13Member: {subject} is
// the remit descriptor and {field} the per-run schema label it is asked under.
var v19ProgramGrammars = map[string]v19Grammar{
	V19PhraseProgramOwnerCurrent: {
		args: []string{"subject", "field"},
		frames: []string{
			"Who is <listed> as {field} <on> {subject} <bnow>?",
			"<bnowlead>, which person <serves> {field} <on> {subject}?",
			"[Give me|I need] the name of the {field} <on> {subject} <bnow>.",
			"{subject}: who is its {field} <bnow>?",
			"Whose name <sits> the {field} [slot|field] <on> {subject} <bnow>?",
		},
		classes:  v19ProgramClasses,
		closers:  v19PersonCloser,
		required: []string{"subject", "field"},
		markers:  [][]string{v19Current},
	},
	V19PhraseProgramStatusCurrent: {
		args: []string{"subject", "field"},
		frames: []string{
			"Where does {subject} <land> on {field} <bnow>?",
			"[What|Which] {field} <applies> {subject} <bnow>?",
			"<bnowlead>, what's the {field} <on> {subject}?",
			"{subject} — which {field} does it [have|carry] <bnow>?",
		},
		classes:  v19ProgramClasses,
		closers:  v19StatusCloser,
		required: []string{"subject", "field"},
		markers:  [][]string{v19Current},
	},
	V19PhraseProgramLatestEvent: {
		args: []string{"subject", "field"},
		frames: []string{
			"What was the <final> {field} <logged> <on> {subject}?",
			"[Going by|By] date, which {field} <on> {subject} [comes|falls] last?",
			"Of the dated {field} entries <on> {subject}, which is the <final> one?",
			"Which {field} [closes out|ends] the timeline <on> {subject}?",
			"What's the <final> {field} we have <on> {subject}?",
		},
		classes:  v19ProgramClasses,
		closers:  v19EventCloser,
		required: []string{"subject", "field"},
		markers:  [][]string{{"last", "latest", "newest", "most recently", "final", "closes out", "ends"}},
		forbid:   []string{"before", "after", "prior", "first"},
	},
	V19PhraseProgramNextAction: {
		args: []string{"subject", "action", "responsible"},
		frames: []string{
			"<on> {subject}, what {action} is <waiting>? Plus the person <listed> as {responsible} <bnow>.",
			"[I need|Tell me] two things <on> {subject}: the {action} <stillopen>, then whoever serves as {responsible} <bnow>.",
			"What remains to be done as {action} <on> {subject}? Also, which person serves as its {responsible} <bnow>?",
		},
		classes:  v19ProgramClasses,
		closers:  v19PairCloser,
		required: []string{"subject", "action", "responsible"},
		markers:  [][]string{v19Current},
	},
	V19PhraseProgramChannelCurrent: {
		args: []string{"subject", "field"},
		frames: []string{
			"How are we <talking> the [other party|counterpart] <on> {subject} <bnow> — which {field}?",
			"[What|Which] {field} <inuse> <on> {subject} <bnow>?",
			"<bnowlead>, which {field} [covers|serves] {subject}?",
			"{subject}: what {field} do we [use|go through] <bnow>?",
		},
		classes:  v19ProgramClasses,
		closers:  v19ChannelCloser,
		required: []string{"subject", "field"},
		markers:  [][]string{v19Current},
	},
	V19PhraseProgramDisagree: {
		args: []string{"subject"},
		frames: []string{
			"<on> {subject}, <whoapproved>, [according to|going by|per] [each|every] [entry|record|note]? <match>",
			"Is there one <approver> on record for {subject}, or do the entries [name different people|point to different people|conflict]? <nameall>",
			"<match> I'm asking who <approvedlaunch> {subject}. <nameall>",
		},
		classes: v19MergeClasses(v19ProgramClasses, map[string][]string{
			"whoapproved":    {"who approved the launch", "who signed off on the launch", "whose approval cleared the launch"},
			"approvedlaunch": {"approved the launch of", "signed off on the launch of", "gave launch approval for"},
			"approver":       {"launch approver", "person who approved the launch", "launch approval"},
			"match":          {"Do those entries match up, or not?", "Are the entries consistent?", "Is it one name throughout, or do the entries conflict?", "Tell me if the entries name different people."},
			"nameall":        {"Name everyone credited.", "List each name given.", "Give each name and flag any conflict."},
		}),
		required: []string{"subject"},
		markers:  [][]string{{"approv*", "sign*"}, {"match", "consistent", "conflict", "different people"}},
	},
	V19PhraseProgramSequenceNth: {
		args: []string{"subject", "field", "ordinal"},
		frames: []string{
			"<sorting> the {field} entries <on> {subject} <bydate>, which is {ordinal}?",
			"<bydatelead>, what was the {ordinal} {field} <on> {subject}?",
			"What is the {ordinal} {field} <on> {subject}, counting [oldest first|in date sequence|from the oldest]?",
			"Which {field} <on> {subject} is {ordinal} [by date|chronologically|in calendar order]?",
		},
		classes: v19MergeClasses(v19ProgramClasses, map[string][]string{
			"sorting":    {"Sorting", "Lining up", "Ordering", "Arranging"},
			"bydate":     {"by date", "chronologically", "oldest first", "in calendar order"},
			"bydatelead": {"Chronologically", "By date", "In calendar order", "Oldest first"},
		}),
		closers:  v19EventCloser,
		required: []string{"subject", "field", "ordinal"},
		markers:  [][]string{{"date", "chronologically", "oldest", "calendar order", "sequence"}},
		forbid:   v19NotAsOf,
	},
	V19PhraseProgramSequenceAfter: {
		args: []string{"subject", "field", "named"},
		frames: []string{
			"What {field} <happened> [right|just|straight] after the {named} <on> {subject}?",
			"Once the {named} <on> {subject} was done, which {field} came next [by date|in the timeline]?",
			"Following the {named}, which {field} <on> {subject} [was the very next one|came up next]?",
			"<on> {subject}, which {field} <happened> directly after the {named}?",
		},
		classes:  v19MergeClasses(v19ProgramClasses, map[string][]string{"happened": {"was logged", "happened", "took place", "came along"}}),
		closers:  v19EventCloser,
		required: []string{"subject", "field", "named"},
		markers:  [][]string{{"after", "following", "next"}},
		forbid:   append([]string{"before", "prior", "preceded", "ahead of", "earlier"}, v19NotAsOf...),
	},
	V19PhraseProgramSequenceBefore: {
		args: []string{"subject", "field", "named"},
		frames: []string{
			"What {field} <happened> [right|just|directly] before the {named} <on> {subject}?",
			"Which {field} <on> {subject} [sits|falls|comes] [immediately|directly|right] [ahead of|prior to] the {named}?",
			"Just prior to the {named}, which {field} did {subject} log?",
			"<on> {subject}, which {field} <happened> directly before the {named}?",
		},
		classes:  v19MergeClasses(v19ProgramClasses, map[string][]string{"happened": {"was logged", "happened", "took place", "came along"}}),
		closers:  v19EventCloser,
		required: []string{"subject", "field", "named"},
		markers:  [][]string{{"before", "prior to", "ahead of"}},
		forbid:   append([]string{"after", "following", "followed", "next"}, v19NotAsOf...),
	},
	V19PhraseProgramChannelAsOf: {
		args: []string{"subject", "field", "date"},
		frames: []string{
			"As of {date}, which {field} was {subject} using?",
			"On {date}, what {field} [was in effect|did we use] <on> {subject}?",
			"What {field} [did|would] {subject} [run on|go through] on {date}?",
			"Back on {date}, which {field} [covered|served] {subject}?",
			"Which {field} held <on> {subject} as of {date}?",
			"{date}: which {field} was in use <on> {subject} that day?",
		},
		classes:  v19ProgramClasses,
		closers:  []string{"", "", "Only the channel used that day.", "The channel in effect then is enough.", "Channel only."},
		required: []string{"subject", "field", "date"},
		markers:  [][]string{{"on {date}", "as of {date}", "{date}:"}},
		forbid:   append([]string{"after", "before", "since"}, v19NotAsOf...),
	},
	V19PhraseProgramOpenStep: {
		args: []string{"subject", "action", "responsible"},
		frames: []string{
			"Among the {action} steps for {subject}, taken <inseq>, which <unfinished> one comes earliest? Also name the person serving as {responsible} <bnow>.",
			"Looking at the {action} steps <on> {subject} <inseq>, what's the earliest still pending — plus whoever is down as {responsible} <bnow>?",
			"{subject}: of its {action} steps, taken <inseq>, which comes first among the <unfinished> ones? And who's {responsible} <bnow>?",
		},
		classes: v19MergeClasses(v19ProgramClasses, map[string][]string{
			"unfinished": {"unfinished", "incomplete", "outstanding", "open"},
			"inseq":      {"in their intended sequence", "by the agreed running order", "going by the step list", "in the sequence set for them"},
		}),
		closers:  v19PairCloser,
		required: []string{"subject", "action", "responsible"},
		markers:  [][]string{{"sequence", "running order", "step list"}, {"unfinished", "incomplete", "outstanding", "open", "pending"}, {"first", "earliest"}},
	},
	V19PhraseProgramStepActor: {
		args: []string{"subject", "step"},
		frames: []string{
			"Which person actually {step} <on> {subject}?",
			"<on> {subject}, who was it that {step}?",
			"I need the name of whoever {step} <on> {subject}.",
			"{subject}: who {step}?",
			"Who's the one who {step} <on> {subject}?",
		},
		classes:  v19ProgramClasses,
		closers:  []string{"", "", "A name will do.", "Only who.", "Person's name only."},
		required: []string{"subject", "step"},
		forbid:   []string{"holding", "holds", "custody", "possession", "keeping", "now", "currently"},
	},
	V19PhraseProgramItemHolder: {
		args: []string{"subject", "item"},
		frames: []string{
			"Where is {item} <on> {subject} <now> — [who's got it|whose hands is it in|who is keeping it]?",
			"Who is <keeping> {item} <on> {subject} <now>?",
			"Once all the handovers are applied, [who ends up with|who's left holding|who has custody of] {item} <on> {subject}?",
			"{item} <on> {subject}: [who has it|in whose hands is it|who's got it] <now>?",
		},
		classes: v19MergeClasses(v19ProgramClasses, map[string][]string{
			"keeping": {"in possession of", "keeping", "looking after", "holding on to", "in custody of"},
		}),
		closers:  []string{"", "", "A name will do.", "Only who.", "Person's name only."},
		required: []string{"subject", "item"},
		markers:  [][]string{{"now", "currently", "today", "at present", "presently", "nowadays", "these days", "at the moment", "as we speak", "as of today", "handovers"}},
		forbid:   []string{"signed", "approved", "performed"},
	},
	V19PhraseProgramOwnerContact: {
		args: []string{"subject", "field"},
		frames: []string{
			"How do I [get hold of|reach] whoever is {field} <on> {subject} <bnow> — which [contact|contact detail] [should I use|works]?",
			"What <contactnoun> [belongs to|is listed for] the person serving as {field} <on> {subject} <bnow>?",
			"<bnowlead>, which <contactnoun> reaches the {field} <on> {subject}?",
		},
		classes: v19MergeClasses(v19ProgramClasses, map[string][]string{
			"contactnoun": {"contact detail", "directory contact", "listed contact", "contact handle"},
		}),
		closers:  []string{"", "Copy the contact as the directory shows it.", "The listed contact, verbatim.", "Contact only, as written in the directory."},
		required: []string{"subject", "field"},
		markers:  [][]string{{"contact", "reach"}, v19Current},
	},
	V19PhraseProgramOwnerName: {
		args: []string{"subject", "field"},
		frames: []string{
			"Who is <listed> as {field} <on> {subject} <bnow>? <notcontact>",
			"<bnowlead>, which person <serves> {field} <on> {subject}? <notcontact>",
			"{subject}: who is its {field} <bnow>? <notcontact>",
		},
		classes: v19MergeClasses(v19ProgramClasses, map[string][]string{
			"notcontact": {"I want their name, not how to reach them.", "Their name please — leave out the contact handle.", "Name the person; no contact details.", "Tell me the person's name, not a handle."},
		}),
		closers:  []string{"", "", "A name will do.", "Only the person, please."},
		required: []string{"subject", "field"},
		markers:  [][]string{{"name"}, {"not", "no", "leave out"}, v19Current},
	},
}

// v19ProgramClasses are the synonym classes the business families share.
var v19ProgramClasses = map[string][]string{
	"on":        {"on", "for"},
	"listed":    {"down", "listed", "named", "recorded"},
	"serves":    {"is", "serves as", "acts as", "stands in as"},
	"sits":      {"sits in", "appears in", "fills"},
	"bnowlead":  {"Today", "At present", "As of the latest record", "Going by the newest entry", "Once every reassignment is applied", "After all the recorded changes"},
	"land":      {"land", "sit", "stand"},
	"applies":   {"applies to", "is in effect for", "is recorded against", "holds for"},
	"final":     {"newest", "last", "final", "most recently dated"},
	"logged":    {"logged", "entered", "recorded"},
	"waiting":   {"waiting", "outstanding", "still to do"},
	"stillopen": {"still open", "not yet done", "coming up"},
	"talking":   {"talking to", "in touch with", "contacting"},
	"inuse":     {"is in use", "do we use", "is live", "applies"},
}

func v19MergeClasses(banks ...map[string][]string) map[string][]string {
	out := map[string][]string{}
	for _, bank := range banks {
		for name, options := range bank {
			out[name] = options
		}
	}
	return out
}

// v19PersonalGrammars cover the personal domains of v13Member. {subject}
// carries its article ("the book club"); {relation} is a household relation
// ("my sister").
var v19PersonalGrammars = map[string]v19Grammar{
	V19PhrasePersonalChore: {
		args: []string{"subject"},
		frames: []string{
			"Who's <doing> {subject} <now>?",
			"Since the [rota change|switch|swap], who is <doing> {subject}?",
			"{subject} — whose [task|responsibility] is it <now>?",
			"Whose [task|responsibility] is {subject} <now>?",
		},
		classes:  map[string][]string{"doing": {"doing", "covering", "down for", "taking care of", "responsible for"}},
		closers:  []string{"", "", "Name please.", "Just tell me who.", "Who, in a word."},
		required: []string{"subject"},
		markers:  [][]string{v19Current},
	},
	V19PhrasePersonalAppointment: {
		args: []string{"relation", "subject"},
		frames: []string{
			"What [day|date] is {relation}'s {subject} [set for|booked for|on] <now>?",
			"[Since|With] the [reschedule|rebooking|change], [when is|what day is] {relation}'s {subject}?",
			"When does {relation} have {subject} <now>?",
			"{relation}'s {subject}: which date is it <now>?",
		},
		closers:  []string{"", "", "A date is plenty.", "Day and month is fine.", "Date only, please."},
		required: []string{"relation", "subject"},
		markers:  [][]string{{"when", "date", "day"}, append([]string{"reschedule", "rebooking", "change"}, v19Current...)},
	},
	V19PhrasePersonalSchoolList: {
		args: []string{"relation", "subject"},
		frames: []string{
			"What [does|should] {relation} [bring|pack|take] for {subject} <now>?",
			"Since the school [updated|revised|changed] it, what's needed for {relation}'s {subject}?",
			"[Run me through|Give me] {relation}'s {subject} [packing list|kit list|list] as it reads <now>.",
			"For {relation}'s {subject}, which items [are required|have to go in the bag] <now>?",
		},
		closers:  []string{"", "", "Include every item.", "The whole list, please.", "Don't leave any item out."},
		required: []string{"relation", "subject"},
		markers:  [][]string{{"list", "bring", "pack", "take", "needed", "items"}, v19Current},
	},
	V19PhrasePersonalMilestone: {
		args: []string{"relation", "subject"},
		frames: []string{
			"How are things looking with {relation}'s {subject} <now>?",
			"Since the last message, where are we on {relation}'s {subject}?",
			"{relation}'s {subject} — what's the state of it <now>?",
			"What's the [plan|word] on {relation}'s {subject} <now>?",
		},
		closers:  []string{"", "", "A few words will do.", "Status only.", "Everyday words are fine."},
		required: []string{"relation", "subject"},
		markers:  [][]string{v19Current},
	},
	V19PhrasePersonalSubscription: {
		args: []string{"subject"},
		frames: []string{
			"What's the [plan|call|decision] for {subject} ahead of the renewal?",
			"Where did we land on {subject} <now>?",
			"{subject}: what was the final call before the renewal date?",
			"<now>, what are we doing about {subject} before renewal?",
		},
		closers:  []string{"", "", "Only the decision.", "A short phrase is fine.", "The final call, not how we got there."},
		required: []string{"subject"},
		markers:  [][]string{{"renewal", "now", "currently", "today", "at present", "presently", "nowadays", "these days", "at the moment", "as we speak", "as of today", "land"}},
	},
	V19PhrasePersonalDeparture: {
		args: []string{"subject", "leg"},
		frames: []string{
			"[What time|When] is the {leg} for {subject} [set to leave|scheduled to go] <now>?",
			"Since we rebooked, [when is|what time is] our {leg} on {subject}?",
			"[Remind me of|Tell me] the {leg} departure for {subject} <now>.",
			"{subject}: when does our {leg} [go|set off] <now>?",
		},
		closers:  []string{"", "", "Clock time will do.", "Time only.", "Hour and minute will do."},
		required: []string{"subject", "leg"},
		markers:  [][]string{{"time", "when", "departure"}, append([]string{"rebooked"}, v19Current...)},
	},
	V19PhrasePersonalClub: {
		args: []string{"subject"},
		frames: []string{
			"Who <leads> {subject} <now>?",
			"[Since|Following] the [handover|change of organiser], who is [running|coordinating] {subject}?",
			"{subject} — who's behind it <now>?",
			"Which person <leads> {subject} <now>?",
		},
		classes:  map[string][]string{"leads": {"organises", "coordinates", "heads up", "leads"}},
		closers:  []string{"", "", "Name please.", "Just tell me who.", "Who, in a word."},
		required: []string{"subject"},
		markers:  [][]string{append([]string{"handover", "change of organiser"}, v19Current...)},
	},
}

// v19AbstentionGrammars render each grounded-abstention probe and its
// answerable decision twin from ONE family, so the wording cannot tell the
// decline from the answer (v13_absence.go).
var v19AbstentionGrammars = map[string]v19Grammar{
	V19PhraseContactEmail: {
		frames: []string{
			"Is there a working email for {name} at {employer} <now>? <ctx> <why>",
			"{name} ({employer}) — what's the email to use for them <now>? <ctx> <why>",
			"<ctxlead>, I'd like to email {name} at {employer}. What address works for them <now>? <why>",
			"Got an email for {name} at {employer} that's still good <now>? <ctx> <why>",
			"How do I email {name} at {employer} <now> — which address is live? <ctx> <why>",
			"<why> Which email gets through to {name} at {employer} <now>? <ctx>",
		},
		classes: map[string][]string{
			"ctx":     {"Context: the {context}.", "Re the {context}.", "That's from the {context}.", "It goes back to the {context}."},
			"ctxlead": {"Re the {context}", "Following the {context}", "About the {context}"},
			// <why> is the reason for asking: it keeps a short question from
			// being mostly the person's own record words (contentOverlap).
			"why":     {"<sending> <note>, <worry>."},
			"sending": {"I'm sending", "I'm about to send", "I want to send", "I'm drafting", "I'm putting together"},
			"note":    {"a quick note", "the follow-up paperwork", "a short thank-you", "some updated figures", "a meeting invitation", "the signed documents"},
			"worry":   {"so it shouldn't bounce", "and I'd hate it landing in a dead inbox", "and it really needs to arrive", "so ignore any address that's gone stale", "and an older address might be floating around", "so please double-check which one works"},
		},
		required: []string{"name", "employer", "context"},
		markers:  [][]string{{"email"}, v19ContactCurrent},
	},
	V19PhraseContactColleagueEmail: {
		frames: []string{
			"{name}, <colleague> at {employer} — what email is on file for them <now>? <met>",
			"Is there a working email for {name}, <colleague> at {employer}? <met>",
			"I'm hoping to [email|write to] {name} at {employer} — <colleague>. Which email address works <now>? <met>",
		},
		classes: map[string][]string{
			"colleague": {"one of {nick}'s colleagues", "{nick}'s coworker", "who works with {nick}", "a colleague of {nick}'s"},
			"met":       {"Context: the {context}.", "That's from the {context}.", "We crossed paths at the {context}.", "It goes back to the {context}."},
		},
		required: []string{"name", "employer", "nick", "context"},
		markers:  [][]string{{"email"}, {"colleague*", "coworker", "works with"}},
	},
	V19PhraseSignalHandle: {
		frames: []string{
			"What's {name}'s <handle>? [My|The] {relation} from the {context}.",
			"[How do I|Can I] [message|ping|text] {name} on Signal — which username [is saved|did I note]? [They're|That's] my {relation} from the {context}.",
			"{name}, my {relation} from the {context}: <which> <handle> [is on file|did I keep]?",
			"Do I have a <handle> for {name}? [They're|That's] my {relation}, from the {context}.",
		},
		classes: map[string][]string{
			"handle": {"Signal username", "handle on Signal", "Signal ID", "Signal contact handle"},
			"which":  {"which", "what"},
		},
		required: []string{"name", "relation", "context"},
		markers:  [][]string{{"signal"}},
	},
	V19PhraseTripLegDays: {
		frames: []string{
			"On {alias} (<our> {purpose} trip, {when}), what's the <length> of the {country} <leg> <changed>?",
			"{country} on {alias} — <our> {purpose} trip from {when}: for how long [are we there|is that <leg>] <changed>?",
			"With the <revised> plan for {alias}, <our> {purpose} trip ({when}), how much time [do we get|is set aside|have we got] in {country}?",
			"How much of {alias}, <our> {purpose} trip back in {when}, [goes to|is spent in] {country} <changed>?",
		},
		classes: map[string][]string{
			"our":     {"our", "the"},
			"length":  {"length", "day count", "number of days"},
			"leg":     {"leg", "stay", "part", "stop"},
			"changed": {"now", "in the revised plan", "since the update", "as it's planned now", "with the edit applied"},
			"revised": {"revised", "updated", "changed"},
		},
		closers:  []string{"", "In days, please.", "A day count is fine.", "Count it in days."},
		required: []string{"alias", "purpose", "when", "country"},
		markers:  [][]string{{"now", "revised", "update", "updated", "changed", "edit"}},
	},
	V19PhraseBalanceUnpaid: {
		frames:   v19BalanceFrames,
		classes:  v19MergeClasses(v19BalanceClasses, map[string][]string{"detail": {"({invoice})", "(invoice {invoice})", "under {invoice}", "on {invoice}"}, "fix": {""}}),
		closers:  []string{"", "", "Just the figure.", "An amount is enough."},
		required: []string{"vendor", "alias", "invoice"},
		markers:  [][]string{{"unpaid", "outstanding", "open balance", "owe*", "balance", "open"}, {"payment", "paid", "money we've sent"}},
	},
	V19PhraseBalanceOutstanding: {
		frames: v19BalanceFrames,
		classes: v19MergeClasses(v19BalanceClasses, map[string][]string{
			"detail": {"({client}'s {purpose} work)", "(the {purpose} job for {client})", "for {client}'s {purpose} work"},
			"fix":    {" Use the approved invoice figure.", " Apply the correction first.", " Go by the corrected invoice."},
		}),
		closers:  []string{"", "", "Just the figure.", "An amount is enough."},
		required: []string{"vendor", "alias", "purpose", "client"},
		markers:  [][]string{{"unpaid", "outstanding", "open balance", "owe*", "balance", "open"}, {"payment", "paid", "money we've sent"}},
	},
}

// v19ContactCurrent marks the contact family: the answerable twin asks for
// the corrected address, so every rendering asks for the current one.
var v19ContactCurrent = append([]string{"live", "still good", "working"}, v19SharedClasses["now"]...)

var v19BalanceFrames = []string{
	"{vendor}'s balance on the {alias} <detail>: <howmuch> <unpaid> <afterpay>?<fix>",
	"[What|How much] is <unpaid> to {vendor} for the {alias} <detail>, <netof>?<fix>",
	"After <subtract> our payment, what's the <openamount> with {vendor} on the {alias} <detail>?<fix>",
	"Our payment's gone out — so how much [remains open|is left open|stays unpaid] with {vendor} for the {alias} <detail>?<fix>",
	"<netof>, how much do we owe {vendor} on the {alias} <detail>?<fix>",
}

var v19BalanceClasses = map[string][]string{
	"howmuch":    {"how much is", "what's"},
	"unpaid":     {"still unpaid", "unpaid", "outstanding"},
	"afterpay":   {"after our payment", "once you count our payment", "with our payment taken off"},
	"netof":      {"net of the payment we sent", "minus what we've paid", "after the money we've sent", "after subtracting the payment"},
	"subtract":   {"subtracting", "deducting", "taking off"},
	"openamount": {"unpaid amount", "open balance", "outstanding amount"},
}

// v19HandleQuestion renders the Signal-handle question the stale-removed probe
// and its answerable twin share: handleQuestion below v19.
func (w World) v19HandleQuestion(p Person, kind string, index, variant int) string {
	return w.v19Or(handleQuestion(p, variant), V19PhraseSignalHandle, fmt.Sprintf("%s:%d:%d", kind, index, variant), V19Slots{"name": p.Name, "relation": p.Relation, "context": p.Context})
}
