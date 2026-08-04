package persona

import (
	"fmt"
	"math/rand"
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// FactKind classifies a fact for question derivation.
type FactKind string

const (
	// KindScalar is a single-valued attribute (city, occupation, car). Some
	// scalars carry an update chain (Superseded set on the current fact).
	KindScalar FactKind = "scalar"
	// KindList is one item of a multi-valued attribute (projects, trips, pets):
	// the material for multi-session "how many / list all" synthesis questions.
	KindList FactKind = "list"
	// KindPreference is a taste the persona holds (cuisine, dietary style,
	// color): recall plus preference-application questions.
	KindPreference FactKind = "preference"
	// KindOpinion is a reversible stance on a hobby (loved → later can't stand):
	// the raw material for contradiction / change-of-mind questions.
	KindOpinion FactKind = "opinion"
	// KindDistractor is a near-miss decoy about another entity, drawn from the
	// SAME pools as a target fact (same attribute, different entity/value). It is
	// never a correct answer; it exists to pressure retrieval.
	KindDistractor FactKind = "distractor"
	// KindAsstRec is an assistant-side recommendation: the ASSISTANT proposed a
	// value the user never stated, so its Value lives only in AsstText. Recalling it
	// tests reading the assistant's past turn (assistant-side recall), not the
	// user's, a capability user-stated facts don't exercise.
	KindAsstRec FactKind = "asst_rec"
	// KindRecurring is one mention of a single recurring topic the user raises
	// several times across sessions. Counting the mentions (aggregation) is harder
	// than counting distinct list items: a retriever that dedupes the repeated
	// topic undercounts. All mentions share one Attribute; the count is the answer.
	KindRecurring FactKind = "recurring"
	// KindCanary is the per-seed verification nonce seeded into the conversation.
	// Its value is a coined high-entropy token (CanaryNonce), so recalling it
	// proves genuine in-context retrieval this run. It cannot be cached across
	// runs or known to a base model. A bait decoy (KindDistractor, same attribute)
	// is seeded too, so echoing any nonce-shaped token fails.
	KindCanary FactKind = "canary"
)

// BeatKind classifies a session beat.
type BeatKind string

const (
	BeatFact  BeatKind = "fact"  // introduces / updates / reverses a fact
	BeatNoise BeatKind = "noise" // chit-chat, carries no ground truth
)

// Fact is a typed atom of the persona universe: (entity, attribute, value) with
// a creation session and timeline position. Value is the CANONICAL answer
// token: a memory answer is checked for normalized containment of Value
// verbatim, so it must survive surface realization unchanged (verified in
// Layer 2). Display is the natural phrase used when the fact is spoken in a beat.
type Fact struct {
	ID        string
	Kind      FactKind
	Entity    string // "self" for the persona; a decoy name for distractors
	Attribute string // canonical key, e.g. "city", "occupation", "project"
	Value     string // canonical answer token (verbatim-preserved)
	Display   string // natural phrase for the value, e.g. "a tortoise named Biscuit"
	Session   int    // session index that introduces this fact
	Seq       int    // global timeline order (0-based, unique)
	// Supersedes is the ID of the fact this one replaces (an update chain or a
	// reversal); "" for an original assertion. Reversal distinguishes a
	// change-of-mind ("I can't stand X anymore") from a plain value update.
	Supersedes string
	Reversal   bool
	// Current is true for the fact that holds as of the end of the timeline
	// (latest value wins). A superseded fact has Current=false.
	Current bool
	// UserText / AsstText are the Layer-1 TEMPLATE rendering of the beat that
	// introduces this fact: deterministic, always carrying Value verbatim. They
	// are copied into the fact's session Beat and are the fallback surface if LLM
	// realization fails verification.
	UserText string
	AsstText string
}

// Beat is one turn-pair script in a session. A fact beat renders into a
// user/assistant MemoryPair that asserts Fact; a noise beat renders into
// on-topic chit-chat with no recoverable fact.
type Beat struct {
	Kind   BeatKind
	FactID string // set for BeatFact
	Topic  string // set for BeatNoise
	// UserText / AsstText are the Layer-1 TEMPLATE rendering: deterministic,
	// always present, and the fallback if LLM surface realization fails
	// verification. They already carry Fact.Value verbatim.
	UserText string
	AsstText string
}

// Session is an ordered run of beats with a seed-derived day offset (days after
// the persona timeline's start; anchored to the pinned dataset epoch by the
// caller, never the wall clock).
type Session struct {
	Index     int
	DayOffset int
	Beats     []Beat
}

// Plan is the complete Layer-1 ground truth for one seed: the persona name, the
// full fact timeline (including superseded values and decoy distractors), and
// the session scripts that introduce them. It is a pure function of (seed,
// Opts); see BuildPlan.
type Plan struct {
	Seed         int64
	BenchVersion int
	Name         string
	Facts        []Fact
	Sessions     []Session
}

// FactByID returns the fact with the given ID (and whether it was found).
func (p *Plan) FactByID(id string) (Fact, bool) {
	for _, f := range p.Facts {
		if f.ID == id {
			return f, true
		}
	}
	return Fact{}, false
}

// Opts are the deterministic size knobs for a plan. They are part of the
// plan's identity alongside the seed: the same (seed, Opts) yields an identical plan.
// The list-family sizes (Projects/Trips/Pets/DomainItems) and LongChain are
// CENTERS of seeded ranges, not exact counts: BuildPlan jitters each around its
// knob so count/list-all/history answers vary per seed (a constant count is a
// hardcodable answer). Opts are derived from the run profile; DefaultOpts is
// used by tests and as the medium baseline.
type Opts struct {
	Sessions     int // number of conversation sessions
	Projects     int // list-attribute items (multi-session synthesis material)
	Trips        int
	Pets         int
	UpdateChains int // scalar attributes that receive a value update
	Reversals    int // opinion facts that get reversed (contradiction material)
	DecoyPeople  int // near-miss distractor entities
	DomainItems  int // items drawn for each professional-domain list family
	// LongChain is the length of ONE extended update trajectory (≥3 → an N-state
	// chain for state-tracking questions; <3 → all chains stay 2-state). The long
	// chain is drawn from the same attributes eligible for an update.
	LongChain int
}

// DefaultOpts is a medium-sized, well-populated universe: enough facts for the
// full memory suite's stratified question quotas with headroom.
func DefaultOpts() Opts {
	return Opts{
		Sessions:     7,
		Projects:     5,
		Trips:        4,
		Pets:         3,
		UpdateChains: 3,
		Reversals:    2,
		DecoyPeople:  6,
		DomainItems:  3,
		LongChain:    3,
	}
}

func (o Opts) normalized() Opts {
	if o.Sessions < 2 {
		o.Sessions = 2
	}
	if o.Projects < 0 {
		o.Projects = 0
	}
	if o.Trips < 0 {
		o.Trips = 0
	}
	if o.Pets < 0 {
		o.Pets = 0
	}
	if o.UpdateChains < 0 {
		o.UpdateChains = 0
	}
	if o.Reversals < 0 {
		o.Reversals = 0
	}
	if o.DecoyPeople < 0 {
		o.DecoyPeople = 0
	}
	if o.DomainItems < 0 {
		o.DomainItems = 0
	}
	if o.LongChain < 0 {
		o.LongChain = 0
	}
	return o
}

// scalarSpec describes one single-valued persona attribute and how to speak it.
type scalarSpec struct {
	attr      string
	label     string // human noun for the attribute (recall question + distractors)
	pool      []string
	updatable bool
	// stmt/ack are chosen by seed; %s is the value. update{Stmt,Ack} phrase a
	// LATER change of the same attribute (used for update chains).
	stmt       []string
	ack        []string
	updateStmt []string
	updateAck  []string
}

// scalarSpecs is the ordered scalar-attribute registry. Order is fixed (a slice,
// never a map range) so the plan is reproducible.
var scalarSpecs = []scalarSpec{
	{
		attr: "city", label: "city", pool: cities, updatable: true,
		stmt:       []string{"I just moved to %s.", "We relocated to %s a few weeks ago.", "I've settled into %s now."},
		ack:        []string{"%s is a lovely place — hope the move went smoothly.", "Noted that you live in %s now."},
		updateStmt: []string{"Update: we've moved again, this time to %s.", "I've since relocated to %s."},
		updateAck:  []string{"Got it — updating your city to %s.", "Noted, %s is your home now."},
	},
	{
		attr: "occupation", label: "job", pool: occupations, updatable: true,
		stmt:       []string{"I work as a %s.", "My job is being a %s.", "Professionally I'm a %s."},
		ack:        []string{"A %s — that's a fascinating line of work.", "Noted that you work as a %s."},
		updateStmt: []string{"I've changed careers — I'm a %s now.", "Career update: I retrained as a %s."},
		updateAck:  []string{"Congratulations on the switch to being a %s.", "Updating your job to %s."},
	},
	{
		attr: "employer", label: "employer", pool: companies, updatable: true,
		stmt:       []string{"I work at %s.", "My employer is %s.", "I joined %s recently."},
		ack:        []string{"%s — good to know where you work.", "Noted, you're at %s."},
		updateStmt: []string{"I've moved on to a new job at %s.", "I left and now work at %s."},
		updateAck:  []string{"Noted your new employer, %s.", "Updating your workplace to %s."},
	},
	{
		attr: "car", label: "car", pool: carModels, updatable: true,
		stmt:       []string{"I drive a %s.", "My car is a %s.", "I get around in a %s."},
		ack:        []string{"A %s — solid choice.", "Noted that you drive a %s."},
		updateStmt: []string{"I traded the old car in for a %s.", "I've upgraded to a %s."},
		updateAck:  []string{"Nice upgrade to the %s.", "Updating your car to a %s."},
	},
	{
		attr: "hometown", label: "hometown", pool: cities,
		stmt: []string{"I grew up in %s.", "I'm originally from %s.", "My hometown is %s."},
		ack:  []string{"%s — that must have shaped a lot of memories.", "Noted, you're from %s."},
	},
	{
		attr: "partner", label: "partner's name", pool: firstNames,
		stmt: []string{"My partner's name is %s.", "I live with my partner, %s.", "%s and I have been together for years."},
		ack:  []string{"Lovely — say hi to %s.", "Noted your partner, %s."},
	},
	{
		attr: "instrument", label: "instrument", pool: instruments,
		stmt: []string{"I play the %s.", "I've been learning the %s.", "My instrument is the %s."},
		ack:  []string{"The %s is a beautiful instrument.", "Noted that you play the %s."},
	},
	{
		attr: "alma_mater", label: "university", pool: universities,
		stmt: []string{"I studied at %s.", "I went to university in %s.", "My alma mater is %s."},
		ack:  []string{"%s has a great reputation.", "Noted, you studied at %s."},
	},
}

// listSpec describes a multi-valued attribute.
type listSpec struct {
	attr  string
	pool  []string
	stmt  []string
	ack   []string
	isPet bool // pets pair a type + name for the Display phrase
}

var (
	projectSpec = listSpec{
		attr: "project", pool: projectNames,
		stmt: []string{"I've started working on %s.", "This week I picked up %s.", "I'm making progress on %s."},
		ack:  []string{"%s sounds like a rewarding project.", "Noted your project, %s."},
	}
	tripSpec = listSpec{
		attr: "trip", pool: cities,
		stmt: []string{"I just got back from a trip to %s.", "I traveled to %s last month.", "I spent a week in %s."},
		ack:  []string{"%s must have been a wonderful trip.", "Noted your trip to %s."},
	}
	petSpec = listSpec{
		attr: "pet", pool: petNames, isPet: true,
		stmt: []string{"We adopted %s.", "Our household now includes %s.", "I got %s recently."},
		ack:  []string{"%s sounds adorable.", "Noted your pet, %s."},
	}
)

// prefSpec describes a preference attribute (recall + application).
type prefSpec struct {
	attr string
	pool []string
	stmt []string
	ack  []string
}

var prefSpecs = []prefSpec{
	{
		attr: "favorite_cuisine", pool: cuisines,
		stmt: []string{"My favorite cuisine is %s.", "I could eat %s food every day.", "I'm crazy about %s cooking."},
		ack:  []string{"%s food is a great pick.", "Noted, you love %s cuisine."},
	},
	{
		attr: "dietary", pool: dietaryStyles,
		stmt: []string{"I'm %s, by the way.", "Just so you know, I eat %s.", "I follow a %s diet."},
		ack:  []string{"Good to know you're %s — I'll keep that in mind.", "Noted your %s diet."},
	},
	{
		attr: "favorite_color", pool: colors,
		stmt: []string{"My favorite color is %s.", "I'm drawn to anything %s.", "I love the color %s."},
		ack:  []string{"%s is a wonderful color.", "Noted, your favorite color is %s."},
	},
}

// domainSpec bundles the professional-domain fact families layered onto a
// persona (software / medical / legal). Exactly one domain is chosen per seed
// (deterministically) and its scalars + list are emitted alongside the universal
// personal facts, so every run carries a specialist register (motivated by
// BEIR's cross-domain retrieval collapse and LongMemEval-V2's
// professional reframe). Adding a family here flows through DeriveQuestions with
// no change to its loops: it reads currentScalarFacts / listAttributesPresent
// and looks phrasing up by attribute (scalarAsk / listCountAsk / factLabel).
type domainSpec struct {
	name    string
	scalars []scalarSpec
	lists   []listSpecCount // list families + their per-persona item counts
}

// listSpecCount pairs a list family with how many items to draw for it.
type listSpecCount struct {
	spec  listSpec
	count int
}

// domains is the ordered domain registry (a slice, never a map range, so domain
// choice is reproducible from the seed).
var domains = []domainSpec{
	{
		name: "software",
		scalars: []scalarSpec{
			{
				attr: "primary_language", label: "primary programming language", pool: softwareLanguages, updatable: true,
				stmt:       []string{"My primary language is %s these days.", "I mostly write %s at work.", "I do most of my coding in %s."},
				ack:        []string{"%s is a solid choice for that.", "Noted that you work mainly in %s."},
				updateStmt: []string{"I've switched my main language to %s.", "We migrated the codebase, so I'm writing %s now."},
				updateAck:  []string{"Got it — updating your primary language to %s.", "Noted, %s is your main language now."},
			},
			{
				attr: "code_editor", label: "code editor", pool: softwareEditors, updatable: true,
				stmt:       []string{"My editor of choice is %s.", "I do all my work in %s.", "I've settled on %s as my editor."},
				ack:        []string{"%s — a fine setup.", "Noted that you use %s."},
				updateStmt: []string{"I've switched editors to %s.", "I gave up my old editor and moved to %s."},
				updateAck:  []string{"Noted your new editor, %s.", "Updating your editor to %s."},
			},
		},
		lists: []listSpecCount{{
			spec: listSpec{
				attr: "service", pool: softwareServices,
				stmt: []string{"I maintain the %s service.", "I own the %s service now.", "I picked up the %s service this sprint."},
				ack:  []string{"Noted you maintain %s.", "Got it — %s is one of yours."},
			},
		}},
	},
	{
		name: "medical",
		scalars: []scalarSpec{
			{
				attr: "diagnosis", label: "medical diagnosis", pool: medicalDiagnoses, updatable: true,
				stmt:       []string{"I was diagnosed with %s.", "My doctor says I have %s.", "I'm managing %s."},
				ack:        []string{"Thanks for telling me — I'll remember your %s.", "Noted your diagnosis of %s."},
				updateStmt: []string{"My diagnosis was revised — it's actually %s.", "Update from my doctor: it's now %s, not what we thought."},
				updateAck:  []string{"Understood — updating your diagnosis to %s.", "Noted the change to %s."},
			},
			{
				attr: "medication", label: "medication", pool: medicalMedications, updatable: true,
				stmt:       []string{"I take %s daily.", "My doctor put me on %s.", "I'm currently on %s."},
				ack:        []string{"Noted that you take %s.", "Got it — %s is your current medication."},
				updateStmt: []string{"My doctor switched me from that to %s.", "I've changed medication — I'm on %s now."},
				updateAck:  []string{"Understood — updating your medication to %s.", "Noted the switch to %s."},
			},
		},
		lists: []listSpecCount{{
			spec: listSpec{
				attr: "allergy", pool: medicalAllergies,
				stmt: []string{"I'm allergic to %s.", "I have an allergy to %s.", "I react badly to %s."},
				ack:  []string{"Noted your %s allergy.", "Got it — I'll remember you react to %s."},
			},
		}},
	},
	{
		name: "legal",
		scalars: []scalarSpec{
			{
				attr: "practice_area", label: "area of law", pool: legalPracticeAreas, updatable: true,
				stmt:       []string{"I practice %s.", "My specialty is %s.", "I work in %s."},
				ack:        []string{"Noted that you practice %s.", "Got it — %s is your field."},
				updateStmt: []string{"I've moved my practice into %s.", "I switched specialties — I'm doing %s now."},
				updateAck:  []string{"Noted your move into %s.", "Updating your practice area to %s."},
			},
			{
				attr: "bar_admission", label: "state bar", pool: legalJurisdictions,
				stmt: []string{"I'm admitted to the %s bar.", "I passed the %s bar.", "I'm licensed to practice in %s."},
				ack:  []string{"Noted you're admitted in %s.", "Got it — %s bar."},
			},
		},
		lists: []listSpecCount{{
			spec: listSpec{
				attr: "legal_matter", pool: legalMatters,
				stmt: []string{"I'm handling %s.", "I picked up %s this week.", "I'm lead counsel on %s."},
				ack:  []string{"Noted your matter, %s.", "Got it — %s is on your docket."},
			},
		}},
	},
	{
		name: "finance",
		scalars: []scalarSpec{
			{
				attr: "risk_tolerance", label: "risk tolerance", pool: financeRiskProfiles, updatable: true,
				stmt:       []string{"My risk tolerance is %s.", "I'd describe my investing style as %s.", "I'm a %s investor."},
				ack:        []string{"Noted — a %s approach.", "Got it, a %s risk tolerance."},
				updateStmt: []string{"After the last downturn I've shifted to %s.", "I've rebalanced to a %s stance."},
				updateAck:  []string{"Understood — updating your risk tolerance to %s.", "Noted the shift to %s."},
			},
			{
				attr: "brokerage", label: "brokerage", pool: financeBrokerages,
				stmt: []string{"I hold my accounts at %s.", "My brokerage is %s.", "I invest through %s."},
				ack:  []string{"Noted, your brokerage is %s.", "Got it — %s."},
			},
		},
		lists: []listSpecCount{{
			spec: listSpec{
				attr: "holding", pool: financeHoldings,
				stmt: []string{"I hold %s in my portfolio.", "I added %s to my portfolio.", "I bought some %s."},
				ack:  []string{"Noted your position in %s.", "Got it — %s is in your portfolio."},
			},
		}},
	},
}

// sometimesSpecs are the sometimes-present attributes: per seed each one is
// self-stated (a normal recall answer), stated only about a decoy person (a
// false-premise abstention), or entirely absent (a pure abstention). The old
// fixed absent-question list was a static decline rule a pattern-matching
// harness could hard-code; rolling presence per seed makes decline-vs-answer
// decidable only by reading the seeded haystack. Never updatable: they exist
// to randomize the abstention boundary, not to carry update chains.
var sometimesSpecs = []scalarSpec{
	{
		attr: "shoe_size", label: "shoe size", pool: shoeSizes,
		stmt: []string{"My shoe size is %s, for the record.", "I wear a %s shoe.", "For sizing, I'm a %s."},
		ack:  []string{"Noted your shoe size, %s.", "Got it — %s."},
	},
	{
		attr: "height", label: "height", pool: heightsCm,
		stmt: []string{"I'm %s tall.", "My height is %s.", "For reference, I stand %s."},
		ack:  []string{"Noted your height, %s.", "Got it — %s."},
	},
	{
		attr: "favorite_song", label: "favorite song", pool: favoriteSongs,
		stmt: []string{"My favorite song is %s.", "The song I love most is %s.", "%s is my all-time favorite song."},
		ack:  []string{"%s — great pick.", "Noted, %s is your favorite song."},
	},
	{
		attr: "star_sign", label: "star sign", pool: starSigns,
		stmt: []string{"My star sign is %s.", "Astrologically, I'm a %s.", "I was born under %s."},
		ack:  []string{"Noted, you're a %s.", "Got it — %s."},
	},
	{
		attr: "middle_name", label: "middle name", pool: firstNames,
		stmt: []string{"My middle name is %s.", "For the record, my middle name is %s.", "%s is my middle name."},
		ack:  []string{"Noted your middle name, %s.", "Got it — %s."},
	},
	{
		attr: "eye_color", label: "eye color", pool: eyeColors,
		stmt: []string{"My eyes are %s.", "I have %s eyes.", "My eye color is %s."},
		ack:  []string{"Noted, %s eyes.", "Got it — %s."},
	},
	{
		attr: "blood_type", label: "blood type", pool: bloodTypes,
		stmt: []string{"My blood type is %s.", "I'm %s, blood-wise.", "For medical records, my blood type is %s."},
		ack:  []string{"Noted your blood type, %s.", "Got it — %s."},
	},
	{
		attr: "birthday_month", label: "birthday month", pool: birthdayMonths,
		stmt: []string{"My birthday is in %s.", "I was born in %s.", "My birthday month is %s."},
		ack:  []string{"Noted, a %s birthday.", "Got it — %s."},
	},
	{
		attr: "sports_team", label: "favorite sports team", pool: sportsTeams,
		stmt: []string{"I support %s above all.", "My team is %s.", "I'm a lifelong %s fan."},
		ack:  []string{"Noted, you support %s.", "Got it — %s."},
	},
	{
		attr: "favorite_film", label: "favorite film", pool: favoriteFilms,
		stmt: []string{"My favorite film is %s.", "The film I love most is %s.", "%s is my favorite movie."},
		ack:  []string{"%s — a classic.", "Noted, %s is your favorite film."},
	},
}

// Presence thresholds for sometimesSpecs: a seed roll below pSometimesSelf
// states the fact as the user's own; between the two, a decoy person holds it
// (false premise); above both, it is absent.
const (
	pSometimesSelf  = 0.45
	pSometimesDecoy = 0.75
)

// allScalarSpecs is the full ordered scalar-attribute registry across the
// universal specs, every professional domain, and the sometimes-present
// attributes. The question layer walks it to derive abstention questions for
// attributes the plan did NOT state for the user, so the abstention surface is
// phrased from the same registry as answerable recall.
func allScalarSpecs() []scalarSpec {
	out := make([]scalarSpec, 0, len(scalarSpecs)+8+len(sometimesSpecs))
	out = append(out, scalarSpecs...)
	for _, d := range domains {
		out = append(out, d.scalars...)
	}
	out = append(out, sometimesSpecs...)
	return out
}

// BuildPlan produces the Layer-1 plan for (seed, opts). It is a PURE function:
// the only entropy source is a math/rand stream seeded from seed; there is no
// wall clock, no crypto-rand, and every iteration is over an ordered slice (no
// Go map range), so the same inputs yield a byte-identical plan.
func BuildPlan(seed int64, opts Opts) *Plan {
	plan, _ := BuildPlanForVersion(seed, opts, protocol.BenchVersionV2)
	return plan
}

// BuildPlanForVersion builds a plan using an explicit immutable generation
// contract. Canonical generation uses this entry point; BuildPlan remains a v2
// compatibility wrapper for existing library callers.
func BuildPlanForVersion(seed int64, opts Opts, benchVersion int) (*Plan, error) {
	opts = opts.normalized()
	// Seed the plan RNG from the version-rotated seed so the persona surface
	// rotates per bench_version (protocol.RotateSeed); still a pure function of
	// (seed, bench_version). Seed-pure answer material keeps using the raw seed.
	rotated, err := protocol.RotateSeedForVersion(seed, benchVersion)
	if err != nil {
		return nil, err
	}
	r := rand.New(rand.NewSource(rotated))

	// List sizes are drawn from a seeded range around each Opts knob so the
	// count/list-all/history answers vary per seed instead of being one constant
	// per run profile (a hardcodable integer). Floors keep every per-family
	// question quota satisfiable: trips ≥3 (DRM lure precondition), domain items
	// ≥2 (a countable list), pets ≥1. A knob of 0 stays 0 (the family is off).
	opts.Projects = jitterSize(r, opts.Projects, 3)
	opts.Trips = jitterSize(r, opts.Trips, 3)
	opts.Pets = jitterSize(r, opts.Pets, 1)
	opts.DomainItems = jitterSize(r, opts.DomainItems, 2)
	// The long-chain length varies in [3, LongChain+1] so ordered-history answers
	// are not a fixed length either.
	if opts.LongChain >= 3 {
		opts.LongChain = 3 + r.Intn(opts.LongChain-1)
	}

	name := pick(r, firstNames) + " " + pick(r, lastNames)
	p := &Plan{Seed: seed, BenchVersion: benchVersion, Name: name}

	// Assign one professional domain (seed-derived) and fold its scalar families
	// into the universal registry, so scalar recall / update-chain / distractor
	// logic below treats domain attributes uniformly. The domain's list families
	// are emitted with the universal list families further down.
	domain := domains[r.Intn(len(domains))]
	scalars := make([]scalarSpec, 0, len(scalarSpecs)+len(domain.scalars))
	scalars = append(scalars, scalarSpecs...)
	scalars = append(scalars, domain.scalars...)

	seq := 0
	nextSeq := func() int { s := seq; seq++; return s }
	// spread assigns an introduction session in [lo,hi).
	spread := func(lo, hi int) int {
		if hi <= lo {
			return lo
		}
		return lo + r.Intn(hi-lo)
	}

	// --- scalar facts (some with update chains) ---
	// Choose which updatable scalars get a value update this run. Domain scalars
	// are in `scalars` too, so a knowledge-update can land on a professional
	// attribute (a re-diagnosis, a language migration), the dynamic-state case.
	updatableIdx := make([]int, 0, len(scalars))
	for i, s := range scalars {
		if s.updatable {
			updatableIdx = append(updatableIdx, i)
		}
	}
	shuffleInts(r, updatableIdx)
	// isAnchor marks the attributes the computed-answer (filtered-aggregation)
	// questions join against; filtAggSpecs (questions.go) is the single source
	// of truth, so a new family added there gets every plan-side guarantee.
	isAnchor := func(attr string) bool {
		for _, spec := range filtAggSpecs {
			if spec.chainAttr == attr {
				return true
			}
		}
		return false
	}
	// Computed-answer anchor: guarantee at least one anchor attribute is among
	// the updated scalars; without this the computed questions' precondition
	// holds in only ~2/3 of seeds. The swap lands in the LAST update slot so
	// the long-chain pick (slot 0) keeps its own seeded variety (when
	// UpdateChains is 1 that slot IS the long-chain slot, so such a profile
	// would pin the trajectory to an anchor attribute — no current profile
	// combines UpdateChains 1 with LongChain ≥ 3), and which anchor is swapped
	// in stays a seeded draw.
	if opts.UpdateChains > 0 && opts.UpdateChains <= len(updatableIdx) {
		var anchorPos []int
		anchored := false
		for pos, idx := range updatableIdx {
			if isAnchor(scalars[idx].attr) {
				if pos < opts.UpdateChains {
					anchored = true
					break
				}
				anchorPos = append(anchorPos, pos)
			}
		}
		if !anchored && len(anchorPos) > 0 {
			pos := anchorPos[r.Intn(len(anchorPos))]
			updatableIdx[opts.UpdateChains-1], updatableIdx[pos] = updatableIdx[pos], updatableIdx[opts.UpdateChains-1]
		}
	}
	updates := map[int]bool{}
	for i := 0; i < opts.UpdateChains && i < len(updatableIdx); i++ {
		updates[updatableIdx[i]] = true
	}
	// One updated attribute becomes an N-state trajectory (opts.LongChain states)
	// for state-tracking / trajectory questions; the rest stay 2-state.
	longChainIdx := -1
	if opts.LongChain >= 3 && opts.UpdateChains >= 1 && len(updatableIdx) > 0 {
		longChainIdx = updatableIdx[0]
	}

	half := opts.Sessions / 2
	if half < 1 {
		half = 1
	}
	// Anchor attributes get their change session clamped off the final session
	// so a list event can land after the change (the straddle post-pass below).
	for i, s := range scalars {
		// N-state trajectory: opts.LongChain distinct values over increasing
		// sessions, each superseding the previous, only the last current. The
		// material for previous-value / ordered-history / state-at-event questions.
		if i == longChainIdx {
			k := opts.LongChain
			vals := pickN(r, s.pool, k)
			if len(vals) < 3 { // pool too small for a real trajectory → fall through
				longChainIdx = -1
			} else {
				k = len(vals)
				hi := opts.Sessions - k + 1
				if isAnchor(s.attr) {
					hi-- // keep a session after the last change for the straddle
				}
				base := spread(0, maxInt(1, hi))
				var prevID string
				for j, v := range vals {
					id := "f-" + s.attr
					if j > 0 {
						id = fmt.Sprintf("f-%s-%d", s.attr, j+1)
					}
					f := Fact{
						ID:         id,
						Kind:       KindScalar,
						Entity:     "self",
						Attribute:  s.attr,
						Value:      v,
						Display:    v,
						Session:    minInt(base+j, opts.Sessions-1),
						Seq:        nextSeq(),
						Supersedes: prevID,
						Current:    j == len(vals)-1,
					}
					if j == 0 {
						f.UserText, f.AsstText = varySurface(r, fill(pickStr(r, s.stmt), v), v), fill(pickStr(r, s.ack), v)
					} else {
						f.UserText, f.AsstText = varySurface(r, fill(pickStr(r, s.updateStmt), v), v), fill(pickStr(r, s.updateAck), v)
					}
					p.Facts = append(p.Facts, f)
					prevID = id
				}
				continue
			}
		}
		v1 := pick(r, s.pool)
		stmt := pickStr(r, s.stmt)
		ack := pickStr(r, s.ack)
		introSession := spread(0, half) // originals land early
		f1 := Fact{
			ID:        "f-" + s.attr,
			Kind:      KindScalar,
			Entity:    "self",
			Attribute: s.attr,
			Value:     v1,
			Display:   v1,
			Session:   introSession,
			Seq:       nextSeq(),
			Current:   true,
		}
		if updates[i] {
			v2 := pickDistinct(r, s.pool, v1)
			f1.Current = false
			updHi := opts.Sessions
			if isAnchor(s.attr) && opts.Sessions-1 > half {
				updHi-- // keep a session after the change for the straddle
			}
			f2 := Fact{
				ID:         "f-" + s.attr + "-2",
				Kind:       KindScalar,
				Entity:     "self",
				Attribute:  s.attr,
				Value:      v2,
				Display:    v2,
				Session:    spread(half, updHi), // update lands later
				Seq:        nextSeq(),
				Supersedes: f1.ID,
				Current:    true,
			}
			f1.UserText, f1.AsstText = varySurface(r, fill(stmt, v1), v1), fill(ack, v1)
			f2.UserText, f2.AsstText = varySurface(r, fill(pickStr(r, s.updateStmt), v2), v2), fill(pickStr(r, s.updateAck), v2)
			p.Facts = append(p.Facts, f1, f2)
		} else {
			f1.UserText, f1.AsstText = varySurface(r, fill(stmt, v1), v1), fill(ack, v1)
			p.Facts = append(p.Facts, f1)
		}
	}

	// --- list facts (projects, trips, pets) ---
	appendList := func(spec listSpec, count int) {
		vals := pickN(r, spec.pool, count)
		for j, v := range vals {
			display, value := v, v
			userText := varySurface(r, fill(pickStr(r, spec.stmt), display), display)
			if spec.isPet {
				t := pick(r, petTypes)
				display = fmt.Sprintf("a %s named %s", t, v)
				value = v // the pet's name is the canonical answer token
				userText = varySurface(r, fill(pickStr(r, spec.stmt), display), display)
			}
			p.Facts = append(p.Facts, Fact{
				ID:        fmt.Sprintf("f-%s-%d", spec.attr, j),
				Kind:      KindList,
				Entity:    "self",
				Attribute: spec.attr,
				Value:     value,
				Display:   display,
				Session:   spread(0, opts.Sessions),
				Seq:       nextSeq(),
				Current:   true,
				UserText:  userText,
				AsstText:  fill(pickStr(r, spec.ack), value),
			})
		}
	}
	appendList(projectSpec, opts.Projects)
	appendList(tripSpec, opts.Trips)
	appendList(petSpec, opts.Pets)
	// domain list families (services / allergies / legal matters).
	for _, lc := range domain.lists {
		appendList(lc.spec, opts.DomainItems)
	}

	// Straddle guarantee for the computed-answer (filtered-aggregation) joins
	// (filtAggSpecs, questions.go): when the anchor chain updated, its joined
	// list family gets at least one event strictly BEFORE and one strictly
	// AFTER the change, and none ON the change session. Both sides populated
	// keeps the filtered count off the guessable 0/full-list extremes; clearing
	// the change session itself keeps the ground truth unambiguous (an event in
	// that session renders after the change inside the conversation, but a
	// strict Session comparison would not count it). Sessions are reassigned
	// only when a rule is violated, so most seeds keep their natural spread.
	ensureStraddle := func(chainAttr, listAttr string) {
		c := -1
		for _, f := range p.Facts {
			if f.Kind == KindScalar && f.Entity == "self" && f.Attribute == chainAttr && f.Current && f.Supersedes != "" {
				c = f.Session
			}
		}
		if c < 1 || c > opts.Sessions-2 {
			return // chain never updated, or no room on one side
		}
		var items []int
		for i := range p.Facts {
			if p.Facts[i].Kind == KindList && p.Facts[i].Attribute == listAttr {
				items = append(items, i)
			}
		}
		if len(items) < 2 {
			return
		}
		moveBefore := func(i int) { p.Facts[i].Session = r.Intn(c) }
		moveAfter := func(i int) { p.Facts[i].Session = c + 1 + r.Intn(opts.Sessions-1-c) }
		// Clear the change session first, alternating sides to keep balance.
		side := 0
		for _, i := range items {
			if p.Facts[i].Session == c {
				if side%2 == 0 {
					moveBefore(i)
				} else {
					moveAfter(i)
				}
				side++
			}
		}
		before, after := 0, 0
		for _, i := range items {
			if p.Facts[i].Session < c {
				before++
			} else {
				after++
			}
		}
		// Every item now sits strictly on one side, so with len ≥ 2 an empty
		// side can be fed from the other without emptying it.
		if before == 0 {
			moveBefore(items[0])
		}
		if after == 0 {
			moveAfter(items[len(items)-1])
		}
	}
	for _, spec := range filtAggSpecs {
		ensureStraddle(spec.chainAttr, spec.listAttr)
	}

	// --- preference facts ---
	for _, s := range prefSpecs {
		v := pick(r, s.pool)
		p.Facts = append(p.Facts, Fact{
			ID:        "f-" + s.attr,
			Kind:      KindPreference,
			Entity:    "self",
			Attribute: s.attr,
			Value:     v,
			Display:   v,
			Session:   spread(0, opts.Sessions),
			Seq:       nextSeq(),
			Current:   true,
			UserText:  varySurface(r, fill(pickStr(r, s.stmt), v), v),
			AsstText:  fill(pickStr(r, s.ack), v),
		})
	}

	// --- opinion facts: reversals + an EQUAL number of standing opinions ---
	// Symmetric counts cap a retrieval-free stance guess at ~50% of the
	// contradiction tier: with only reversed opinions asked, "you no longer do
	// it" was a free win, and with a single standing opinion a hedged cessation
	// template still took 75%. Both kinds share one question surface AND one
	// statement surface (opinionStmts), so which stance holds is decidable only
	// from memory.
	revHobbies := pickN(r, hobbies, opts.Reversals*2)
	for j := 0; j < opts.Reversals && j < len(revHobbies); j++ {
		h := revHobbies[j]
		orig := Fact{
			ID:        fmt.Sprintf("f-opinion-%d", j),
			Kind:      KindOpinion,
			Entity:    "self",
			Attribute: "hobby_opinion",
			Value:     h,
			Display:   h,
			Session:   spread(0, half),
			Seq:       nextSeq(),
			Current:   false, // the reversal supersedes the original stance
			UserText:  varySurface(r, fill(pickStr(r, opinionStmts), h), h),
			AsstText:  fill(pickStr(r, opinionAcks), h),
		}
		rev := Fact{
			ID:         fmt.Sprintf("f-opinion-%d-rev", j),
			Kind:       KindOpinion,
			Entity:     "self",
			Attribute:  "hobby_opinion",
			Value:      h,
			Display:    h,
			Session:    spread(half, opts.Sessions),
			Seq:        nextSeq(),
			Supersedes: orig.ID,
			Reversal:   true,
			Current:    true,
			UserText:   varySurface(r, fill(pickStr(r, []string{"Honestly, I can't stand %s anymore — I've given it up.", "I've completely gone off %s; I don't do it now.", "I used to love %s but I've quit it entirely."}), h), h),
			AsstText:   fill(pickStr(r, []string{"Understood — you no longer do %s.", "Noted that you've given up %s."}), h),
		}
		p.Facts = append(p.Facts, orig, rev)
	}
	for j := opts.Reversals; j < 2*opts.Reversals && j < len(revHobbies); j++ {
		h := revHobbies[j]
		p.Facts = append(p.Facts, Fact{
			ID:        fmt.Sprintf("f-opinion-still-%d", j-opts.Reversals),
			Kind:      KindOpinion,
			Entity:    "self",
			Attribute: "hobby_opinion",
			Value:     h,
			Display:   h,
			Session:   spread(0, opts.Sessions),
			Seq:       nextSeq(),
			Current:   true,
			UserText:  varySurface(r, fill(pickStr(r, opinionStmts), h), h),
			AsstText:  fill(pickStr(r, opinionAcks), h),
		})
	}

	// --- near-miss distractors: same attributes, different (decoy) entities ---
	relations := []string{"my colleague", "my neighbor", "my cousin", "my old roommate", "a friend", "my sister"}
	for d := 0; d < opts.DecoyPeople; d++ {
		who := pick(r, firstNames)
		rel := relations[d%len(relations)]
		// Round-robin over the scalar specs (not random) so decoys reliably COVER
		// the self attributes being recalled: every recall/knowledge-update
		// question then faces a same-attribute, different-value near-miss, instead
		// of coverage being a coin flip. Includes the specialist attributes, where
		// jargon most pressures retrieval.
		s := scalars[d%len(scalars)]
		v := pick(r, s.pool)
		p.Facts = append(p.Facts, Fact{
			ID:        fmt.Sprintf("f-distractor-%d", d),
			Kind:      KindDistractor,
			Entity:    who,
			Attribute: s.attr,
			Value:     v,
			Display:   v,
			Session:   spread(0, opts.Sessions),
			Seq:       nextSeq(),
			UserText:  fmt.Sprintf(pickStr(r, notYouFrames), rel, who, s.label, v),
			AsstText:  fmt.Sprintf(pickStr(r, notYouAcks), s.label, who),
		})
	}

	// --- sometimes-present attributes: roll each one per seed. Self-stated →
	// a normal recall fact; decoy-held → a false-premise near-miss (the hard
	// abstention material: retrieval surfaces a friend's value, the correct
	// behavior is still to decline); absent → pure abstention. The roll is what
	// makes the abstention boundary seed-dependent instead of a fixed list. ---
	for d, s := range sometimesSpecs {
		roll := r.Float64()
		switch {
		case roll < pSometimesSelf:
			v := pick(r, s.pool)
			p.Facts = append(p.Facts, Fact{
				ID:        "f-" + s.attr,
				Kind:      KindScalar,
				Entity:    "self",
				Attribute: s.attr,
				Value:     v,
				Display:   v,
				Session:   spread(0, opts.Sessions),
				Seq:       nextSeq(),
				Current:   true,
				UserText:  varySurface(r, fill(pickStr(r, s.stmt), v), v),
				AsstText:  fill(pickStr(r, s.ack), v),
			})
		case roll < pSometimesDecoy:
			who := pick(r, firstNames)
			rel := relations[d%len(relations)]
			v := pick(r, s.pool)
			p.Facts = append(p.Facts, Fact{
				ID:        "f-fp-" + s.attr,
				Kind:      KindDistractor,
				Entity:    who,
				Attribute: s.attr,
				Value:     v,
				Display:   v,
				Session:   spread(0, opts.Sessions),
				Seq:       nextSeq(),
				UserText:  fmt.Sprintf(pickStr(r, notYouFrames), rel, who, s.label, v),
				AsstText:  fmt.Sprintf(pickStr(r, notYouAcks), s.label, who),
			})
		}
	}

	// --- canary nonce + bait (integrity probe) ---
	// A per-seed high-entropy verification code the user states once, plus a
	// plausible-but-wrong decoy code attributed to someone else. Recalling the
	// user's own code proves genuine in-context retrieval this run (the value
	// cannot be cached across runs or known to a base model); echoing the decoy
	// or any nonce-shaped token fails. The question layer emits a QTCanary
	// question and the scorer treats a miss/leak as an integrity disqualifier.
	{
		nonce := CanaryNonce(seed)
		bait := canaryBait(seed)
		p.Facts = append(p.Facts, Fact{
			ID:        "f-canary",
			Kind:      KindCanary,
			Entity:    "self",
			Attribute: "session_code",
			Value:     nonce,
			Display:   nonce,
			Session:   spread(0, opts.Sessions),
			Seq:       nextSeq(),
			Current:   true,
			UserText:  varySurface(r, fmt.Sprintf("My verification code for this session is %s.", nonce), nonce),
			AsstText:  fmt.Sprintf("Got it — I've noted your verification code %s.", nonce),
		})
		who := pick(r, firstNames)
		p.Facts = append(p.Facts, Fact{
			ID:        "f-canary-bait",
			Kind:      KindDistractor,
			Entity:    who,
			Attribute: "session_code",
			Value:     bait,
			Display:   bait,
			Session:   spread(0, opts.Sessions),
			Seq:       nextSeq(),
			UserText:  fmt.Sprintf("For reference, %s's verification code is %s, not mine.", who, bait),
			AsstText:  fmt.Sprintf("Understood — %s is %s's code, not yours.", bait, who),
		})
	}

	// --- assistant-side recommendations ---
	// The assistant proposes a value the user never states (a book, a gadget, …),
	// so its Value lives ONLY in AsstText: recalling it requires reading the
	// assistant's past turn, not the user's. Spread across the timeline like other
	// facts; sampled down by the question layer.
	for _, s := range asstRecSpecs {
		v := pick(r, s.pool)
		p.Facts = append(p.Facts, Fact{
			ID:        "f-" + s.attr,
			Kind:      KindAsstRec,
			Entity:    "self",
			Attribute: s.attr,
			Value:     v,
			Display:   v,
			Session:   spread(0, opts.Sessions),
			Seq:       nextSeq(),
			Current:   true,
			UserText:  s.user,                // request WITHOUT the value
			AsstText:  fmt.Sprintf(s.ack, v), // value appears only here
		})
	}

	// --- recurring-topic mentions (aggregation / counting) ---
	// One topic the user raises K times across DISTINCT sessions. The count is the
	// answer; a retriever that collapses the repeated mentions undercounts.
	rec := recurringSpecs[r.Intn(len(recurringSpecs))]
	kRec := 2 + r.Intn(7) // 2..8 mentions (wide range: the count answer carries ~2.8 bits)
	if kRec > opts.Sessions {
		kRec = opts.Sessions
	}
	for j := 0; j < kRec; j++ {
		sess := j * opts.Sessions / kRec // spread across distinct sessions
		p.Facts = append(p.Facts, Fact{
			ID:        fmt.Sprintf("f-%s-%d", rec.attr, j),
			Kind:      KindRecurring,
			Entity:    "self",
			Attribute: rec.attr,
			Value:     rec.label,
			Display:   rec.label,
			Session:   sess,
			Seq:       nextSeq(),
			Current:   true,
			UserText:  varySurface(r, fill(recurringMentionTmpls[j%len(recurringMentionTmpls)], rec.label), rec.label),
			AsstText:  fill(pickStr(r, []string{"Noted — %s has come up before.", "Understood, thanks for the update on %s.", "Got it, %s again."}), rec.label),
		})
	}

	// --- assign facts to session scripts (ordered), interleaved with noise ---
	p.Sessions = buildSessions(r, p.Facts, opts.Sessions)
	return p, nil
}

// opinionStmts/opinionAcks phrase BOTH a to-be-reversed opinion's original
// statement and a standing (never-reversed) opinion. The two MUST stay
// surface-identical: if they diverge, reversed-vs-standing becomes decidable
// from phrasing alone and the contradiction questions stop testing memory.
var (
	opinionStmts = []string{"I absolutely love %s.", "I've really gotten into %s lately.", "%s is my favorite way to spend a weekend."}
	opinionAcks  = []string{"%s sounds like a joy.", "Great that you enjoy %s."}
)

// recurringSpec scripts a topic the user mentions repeatedly. label is the spoken
// noun phrase (appears in every mention); asks are the count-question phrasings
// (askVariant picks one per seed, so the aggregation ask is not mono-phrased).
type recurringSpec struct {
	attr  string
	label string
	asks  []string
}

var recurringSpecs = []recurringSpec{
	{"recur_backpain", "my ongoing back pain", []string{
		"How many separate times have I brought up my ongoing back pain?",
		"Across all our chats, on how many occasions has my ongoing back pain come up?",
		"Count the distinct times I've complained to you about my ongoing back pain.",
	}},
	{"recur_account", "the Barton account", []string{
		"How many separate times have I mentioned the Barton account?",
		"On how many distinct occasions has the Barton account come up with me?",
		"Count the separate times I've raised the Barton account with you.",
	}},
	{"recur_starter", "my sourdough starter", []string{
		"How many times have I brought up my sourdough starter?",
		"On how many separate occasions have I mentioned my sourdough starter?",
		"Count how many different times my sourdough starter has come up.",
	}},
	{"recur_thesis", "my thesis revisions", []string{
		"How many separate times did I mention my thesis revisions?",
		"On how many distinct occasions have my thesis revisions come up?",
		"Count the separate times I've talked about my thesis revisions.",
	}},
}

// recurringAskFor returns a seed-keyed count-question phrasing for a
// recurring-topic attribute.
func recurringAskFor(seed int64, attr string) string {
	for _, s := range recurringSpecs {
		if s.attr == attr {
			return askVariant(seed, "agg:"+attr, s.asks)
		}
	}
	return "How many separate times have I brought that topic up?"
}

// recurringMentionTmpls phrase one mention of the recurring topic (%s = label).
var recurringMentionTmpls = []string{
	"I brought up %s again today.",
	"%s came up for me once more.",
	"I mentioned %s again this week.",
	"Talked about %s yet again.",
	"%s was on my mind again today.",
}

// asstRecSpec scripts one assistant-side recommendation: a user request that
// carries NO value and an assistant reply (ack, one %s) that supplies it.
type asstRecSpec struct {
	attr string
	user string
	ack  string
	pool []string
}

var asstRecSpecs = []asstRecSpec{
	{"rec_novel", "Can you recommend a novel I should read next?", "I'd recommend reading %s.", asstNovels},
	{"rec_gadget", "What's a good gadget for my home office?", "I'd go with %s.", asstGadgets},
	{"rec_podcast", "Suggest a podcast for my commute.", "You should try %s.", asstPodcasts},
	{"rec_trail", "Where should I go hiking this weekend?", "I'd suggest %s.", asstTrails},
}

// buildSessions groups facts into their assigned sessions (fact order within a
// session follows Seq), interleaves a seed-chosen noise beat or two, and assigns
// each session a strictly increasing day offset.
func buildSessions(r *rand.Rand, facts []Fact, nSessions int) []Session {
	bySession := make([][]Fact, nSessions)
	for _, f := range facts {
		s := f.Session
		if s < 0 {
			s = 0
		}
		if s >= nSessions {
			s = nSessions - 1
		}
		bySession[s] = append(bySession[s], f)
	}

	sessions := make([]Session, 0, nSessions)
	day := r.Intn(20) // small seed-derived start offset
	for i := 0; i < nSessions; i++ {
		facts := bySession[i]
		sort.Slice(facts, func(a, b int) bool { return facts[a].Seq < facts[b].Seq })
		beats := make([]Beat, 0, len(facts)+2)
		// A leading noise beat on most sessions so not every turn is a fact.
		if r.Intn(3) != 0 {
			beats = append(beats, noiseBeat(r))
		}
		for _, f := range facts {
			beats = append(beats, Beat{Kind: BeatFact, FactID: f.ID, UserText: f.UserText, AsstText: f.AsstText})
			if r.Intn(4) == 0 { // occasional interstitial chit-chat
				beats = append(beats, noiseBeat(r))
			}
		}
		if len(beats) == 0 { // never emit an empty session
			beats = append(beats, noiseBeat(r))
		}
		sessions = append(sessions, Session{Index: i, DayOffset: day, Beats: beats})
		// Strictly increasing gaps, mixing week-scale (1..10 days) with an
		// occasional month-scale jump (15..55 days) so elapsed-duration answers
		// span days through months instead of clustering at the small buckets.
		gap := 1 + r.Intn(10)
		if r.Intn(4) == 0 {
			gap += 14 + r.Intn(31)
		}
		day += gap
	}
	return sessions
}

func noiseBeat(r *rand.Rand) Beat {
	t := pick(r, noiseTopics)
	tmpl := noiseTemplates[r.Intn(len(noiseTemplates))]
	return Beat{
		Kind:     BeatNoise,
		Topic:    t,
		UserText: capitalize(fill(tmpl.user, t)),
		AsstText: fill(tmpl.asst, t),
	}
}
