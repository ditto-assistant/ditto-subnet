package universe

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/humandata"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 semantic business-event programs.
//
// v10 through v12 graded every open program as ONE monetary integer, so the
// difficulty axis of the memory bench collapsed onto "read amounts, pick a
// recipe, subtract" — the exact capability a cents accumulator emulates. v13
// keeps everything the v10–v12 stack proved out (seed-scoped schema labels, a
// split compositional glossary, four independent renderers, per-seed record
// shuffling, relational subject binding, and four-member metamorphic groups
// with validator-side provenance) and changes WHAT is asked: typed semantic
// outcomes over real business events, with zero monetary groups.
//
// Seven families, one per metamorphic group, cycling when more groups are
// requested:
//
//   - owner-after-correction: who holds a role after a superseding reassignment;
//   - standing-status: the status that stands after a later decision superseded
//     the first, answered through the workspace's own status jargon OR plain
//     English (the accept cluster is the seed's alias term plus the canonical
//     synonym set);
//   - latest-event: the most recent dated event on record;
//   - client-vs-vendor: which organisation commissions and pays versus which one
//     is the supplier we pay;
//   - next-action-responsible: the standing next step and who owns it now;
//   - current-channel: the communication channel that currently stands;
//   - records-disagree: two records carry conflicting values for one field; the
//     correct answer names both values AND the conflict, and the case is exempt
//     from distractor scanning (there is no wrong same-attribute value to fire
//     on — both are what the records say).
//
// Every member carries grader-only protocol.Claims (typed, with the accept
// cluster) alongside the ordinary graded fields, so a v12-era grader still
// scores the case and the v13 grader branch reads the structured spec. The
// V10CaseProvenance.Relation of each member (base, renderer_invariant,
// distractor_invariant, causal_counterfactual) lets the scorer post-pass zero
// the base/counterfactual pair of a group whose counterfactual answer did not
// move.
//
// Everything is deterministic in (seed, group, variant). No group is monetary:
// no record carries a currency amount and no case grades AnswerMoney.

const V13ProvenanceRevision = "dittobench-v13-generator-spec-v1"

// V13Family names one v13 business-program family.
type V13Family string

const (
	V13FamilyOwnerAfterCorrection  V13Family = "owner-after-correction"
	V13FamilyStandingStatus        V13Family = "standing-status"
	V13FamilyLatestEvent           V13Family = "latest-event"
	V13FamilyClientVsVendor        V13Family = "client-vs-vendor"
	V13FamilyNextActionResponsible V13Family = "next-action-responsible"
	V13FamilyCurrentChannel        V13Family = "current-channel"
	V13FamilyRecordsDisagree       V13Family = "records-disagree"
)

// V13Families is the fixed family cycle: group g draws family g mod 7, so a
// 28-case request covers every family exactly once.
var V13Families = []V13Family{
	V13FamilyOwnerAfterCorrection,
	V13FamilyStandingStatus,
	V13FamilyLatestEvent,
	V13FamilyClientVsVendor,
	V13FamilyNextActionResponsible,
	V13FamilyCurrentChannel,
	V13FamilyRecordsDisagree,
}

// V13ProgramQuestionType is the validator-internal question type shared by
// every v13 business-program case. The family is provenance, never wire.
const V13ProgramQuestionType = "v13-open-program"

// v13Schema is the per-seed label set the glossary explains. Labels are drawn
// from the widened v12 superset and are collision-free within a seed.
type v13Schema struct {
	Entity      string
	Alias       string
	Owner       string
	Status      string
	Event       string
	Client      string
	Vendor      string
	Action      string
	Responsible string
	Channel     string
	Correction  string
}

func generateV13Schema(seed int64) v13Schema {
	used := map[string]bool{}
	next := func(salt string) string {
		for attempt := 0; ; attempt++ {
			candidate := v12Label(seed, fmt.Sprintf("v13-%s-%d", salt, attempt))
			if !used[candidate] {
				used[candidate] = true
				return candidate
			}
		}
	}
	return v13Schema{
		Entity: next("entity"), Alias: next("alias"), Owner: next("owner"),
		Status: next("status"), Event: next("event"), Client: next("client"),
		Vendor: next("vendor"), Action: next("action"), Responsible: next("responsible"),
		Channel: next("channel"), Correction: next("correction"),
	}
}

func digestV13Schema(schema v13Schema) (string, error) {
	raw, err := json.Marshal(schema)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func v13Ontology(schema v13Schema) []V10OntologyTerm {
	return []V10OntologyTerm{
		{Semantic: "workstream entity", Wire: schema.Entity},
		{Semantic: "user-facing alias", Wire: schema.Alias},
		{Semantic: "accountable owner", Wire: schema.Owner},
		{Semantic: "standing status", Wire: schema.Status},
		{Semantic: "dated event", Wire: schema.Event},
		{Semantic: "commissioning client (pays us)", Wire: schema.Client},
		{Semantic: "supplier we pay", Wire: schema.Vendor},
		{Semantic: "next action", Wire: schema.Action},
		{Semantic: "responsible party for the next action", Wire: schema.Responsible},
		{Semantic: "communication channel", Wire: schema.Channel},
		{Semantic: "replaces prior state, newest wins", Wire: schema.Correction},
	}
}

// ── Vocabulary ───────────────────────────────────────────────────────────────
//
// Every list is a component bank. Status, event, channel, and action values
// are CLASSES: a canonical plain-English term plus the synonym cluster an
// honest assistant may normalise to. Clusters are disjoint by construction —
// no term of one class is a bounded substring of any term of another — so a
// planted distractor from one class can never fire on an accepted form of the
// answer.

// V13TermClass is a canonical term plus every accepted equivalent surface.
type V13TermClass struct {
	Canonical string
	Accept    []string
}

// V13StatusClasses are the canonical standing-status classes. The workspace
// refers to each through a per-seed alias term (see v13StatusAliasTerms) that
// the glossary defines, so the accept cluster for a status claim is the alias
// term plus this synonym set.
var V13StatusClasses = []V13TermClass{
	{Canonical: "approved", Accept: []string{"approved", "green-lit", "signed off", "cleared to proceed", "sanctioned"}},
	{Canonical: "on hold", Accept: []string{"on hold", "paused", "parked", "suspended", "frozen"}},
	{Canonical: "superseded", Accept: []string{"superseded", "replaced by a newer decision", "overtaken", "retired in favour of a later decision"}},
	{Canonical: "cancelled", Accept: []string{"cancelled", "canceled", "scrapped", "called off", "withdrawn"}},
	{Canonical: "in review", Accept: []string{"in review", "under review", "pending review", "awaiting a decision"}},
	{Canonical: "active", Accept: []string{"active", "in progress", "underway", "running"}},
}

// v13StatusAliasTerms are the workspace jargon words a seed maps onto the
// status classes (a seeded permutation). They are deliberately not status
// words, so a harness that recognises only plain English must read the
// glossary to bind them.
var v13StatusAliasTerms = []string{"amber", "slate", "ember", "cobalt", "harbour", "quartz", "tundra", "velvet"}

// V13EventClasses are dated business events.
var V13EventClasses = []V13TermClass{
	{Canonical: "kickoff", Accept: []string{"kickoff", "kick-off", "kickoff meeting", "launch meeting"}},
	{Canonical: "vendor swap", Accept: []string{"vendor swap", "switched vendors", "changed the vendor", "supplier change"}},
	{Canonical: "incident", Accept: []string{"incident", "outage", "service disruption"}},
	{Canonical: "handoff", Accept: []string{"handoff", "hand-off", "handover"}},
	{Canonical: "audit", Accept: []string{"audit", "audit visit", "compliance check"}},
	{Canonical: "quote revision", Accept: []string{"quote revision", "revised quote", "requote"}},
	{Canonical: "reference call", Accept: []string{"reference call", "customer reference", "reference check"}},
}

// V13ChannelClasses are communication channels.
var V13ChannelClasses = []V13TermClass{
	{Canonical: "email", Accept: []string{"email", "e-mail", "mail thread"}},
	{Canonical: "phone", Accept: []string{"phone", "telephone", "by ringing them"}},
	{Canonical: "Slack", Accept: []string{"slack", "slack channel", "the shared slack"}},
	{Canonical: "ticket portal", Accept: []string{"ticket portal", "ticketing portal", "support portal"}},
	{Canonical: "video meeting", Accept: []string{"video meeting", "video call", "weekly video sync"}},
}

// V13ActionClasses are next steps on a business workstream.
var V13ActionClasses = []V13TermClass{
	{Canonical: "send the revised quote", Accept: []string{"send the revised quote", "revised quote", "requote them", "send them the new quote"}},
	{Canonical: "book the kickoff", Accept: []string{"book the kickoff", "schedule the kickoff", "kickoff booking"}},
	{Canonical: "countersign the NDA", Accept: []string{"countersign the nda", "sign the nda", "nda signature"}},
	{Canonical: "chase the deposit", Accept: []string{"chase the deposit", "collect the deposit", "deposit follow-up"}},
	{Canonical: "run the reference call", Accept: []string{"run the reference call", "reference call", "hold the reference call"}},
	{Canonical: "publish the launch note", Accept: []string{"publish the launch note", "launch note", "send the launch announcement"}},
}

// V13ConflictAccept is the accept cluster for the conflict marker of a
// records-disagree claim: any of these says the two records do not agree.
var V13ConflictAccept = []string{"disagree", "conflict", "conflicting", "inconsistent", "do not agree", "don't agree", "differ", "mismatch", "contradict"}

var v13OrgStems = []string{
	"Harrowgate", "Bellweather", "Northquay", "Silverline", "Copperfield", "Ravenscar",
	"Tidewater", "Kestrel", "Ashgrove", "Brightwater", "Oakhurst", "Fallowmere",
	"Redstone", "Greyfriar", "Lantern", "Wexford", "Marlow", "Thornbury",
	"Ironside", "Pemberley", "Saltmarsh", "Dunmore", "Holloway", "Ferncliff",
}

var v13OrgSuffixes = []string{"Logistics", "Analytics", "Holdings", "Studio", "Partners", "Systems", "Supply", "Labs"}

// Business domains (client ops, approvals, hiring, partnerships, launches,
// fundraising, vendor management, cash-flow decisions) as workstream remits.
var v13PurposeAdjectives = []string{"spring", "second-round", "offshore", "pilot", "renewal", "holdback", "cross-border", "quarter-end"}
var v13PurposeNouns = []string{
	"vendor consolidation", "seed-round data room", "hiring loop", "channel partnership",
	"product launch", "cash-flow review", "client onboarding", "approval backlog",
}

var v13GlossaryOpeners = []string{
	"For this batch of records,",
	"Within this workspace,",
	"Reading guide for the entries that follow:",
	"Local field conventions here:",
	"Before the data lands, note that",
	"Schema note for this workstream set:",
}

var v13QuestionOpeners = []string{
	"Work only from this batch's own field meanings.",
	"Read the local glossary before naming any field.",
	"Ground every label in this workspace's conventions.",
	"Resolve the workstream by its remit, then answer.",
	"Do not assume standard field names; use the ones defined here.",
	"Apply the per-run schema before you answer.",
}

// Subject descriptors bind the workstream by its remit, which lives in the
// binding record — never by its alias and never by a value the question asks
// for. The decoy workstream carries a different remit, so the descriptor
// resolves uniquely.
var v13SubjectForms = []string{
	"the workstream in this batch covering %s",
	"the entry in these records that handles %s",
	"the workstream whose remit here is %s",
	"the account in this batch set up for %s",
}

var v13AckLeads = []string{"Noted.", "Recorded.", "Logged.", "Kept.", "Tracked.", "Filed."}

var v13AckBodies = []string{
	"I'll bind each value to its role through this batch's glossary.",
	"Reading roles from the prose, not from row position.",
	"Later revisions will govern; I'll take the superseding entry.",
	"These local conventions decide which label means what.",
	"I'll resolve the workstream by its remit rather than by name.",
	"Where two records disagree I'll say so instead of picking one.",
}

var v13ConversationLeads = []string{
	"One more line from our custom workspace schema: ",
	"Adding a workspace record — read it with our local glossary: ",
	"Here is another entry for the batch: ",
	"Filing this under the same workstream set: ",
}

var v13DateMonths = []string{"January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"}

func v13Pick(seed int64, salt string, bank []string) string {
	return bank[int(v10Seed(seed, "v13:"+salt)%int64(len(bank)))]
}

func v13Index(seed int64, salt string, n int) int {
	return int(v10Seed(seed, "v13:"+salt) % int64(n))
}

// v13Perm returns a seeded Fisher-Yates permutation of [0,n).
func v13Perm(seed int64, salt string, n int) []int {
	idx := make([]int, n)
	for i := range idx {
		idx[i] = i
	}
	for i := n - 1; i > 0; i-- {
		j := int(v10Seed(seed, fmt.Sprintf("v13:%s:%d", salt, i)) % int64(i+1))
		idx[i], idx[j] = idx[j], idx[i]
	}
	return idx
}

// v13Org coins a collision-free organisation name for (seed, salt).
func v13Org(seed int64, salt string, used map[string]bool) string {
	for attempt := 0; ; attempt++ {
		v := v10Seed(seed, fmt.Sprintf("v13:org:%s:%d", salt, attempt))
		name := v13OrgStems[v%int64(len(v13OrgStems))] + " " + v13OrgSuffixes[(v/int64(len(v13OrgStems)))%int64(len(v13OrgSuffixes))]
		if !used[name] {
			used[name] = true
			return name
		}
	}
}

func v13Purpose(seed int64, salt string, used map[string]bool) string {
	for attempt := 0; ; attempt++ {
		v := v10Seed(seed, fmt.Sprintf("v13:purpose:%s:%d", salt, attempt))
		p := "the " + v13PurposeAdjectives[v%int64(len(v13PurposeAdjectives))] + " " + v13PurposeNouns[(v/int64(len(v13PurposeAdjectives)))%int64(len(v13PurposeNouns))]
		if !used[p] {
			used[p] = true
			return p
		}
	}
}

// V13Timestamp renders a business-hours-plausible RFC3339 timestamp for slot i
// of a record set. Gaps between slots are seeded (one to three days, a fresh
// hour and minute each), so no constant step identifies a slot.
func V13Timestamp(seed int64, salt string, i int) string {
	day := 2 + int(v10Seed(seed, "v13:ts-day:"+salt)%9)
	for k := 1; k <= i; k++ {
		day += 1 + int(v10Seed(seed, fmt.Sprintf("v13:ts-gap:%s:%d", salt, k))%3)
	}
	if day > 27 {
		day = 27
	}
	hour := 8 + int(v10Seed(seed, fmt.Sprintf("v13:ts-hour:%s:%d", salt, i))%10)
	minute := int(v10Seed(seed, fmt.Sprintf("v13:ts-min:%s:%d", salt, i)) % 60)
	return fmt.Sprintf("2026-02-%02dT%02d:%02d:00Z", day, hour, minute)
}

// V13DateAccept returns every unambiguous rendering of a calendar day the
// grader accepts for a day-granularity date claim. Slashed numeric forms are
// deliberately excluded: 03/04 is March 4 in one locale and April 3 in another.
func V13DateAccept(year, month, day int) []string {
	name := v13DateMonths[month-1]
	short := name[:3]
	ordinal := fmt.Sprintf("%d%s", day, v13Ordinal(day))
	return []string{
		fmt.Sprintf("%04d-%02d-%02d", year, month, day),
		fmt.Sprintf("%s %d", name, day),
		fmt.Sprintf("%s %d, %d", name, day, year),
		fmt.Sprintf("%s %d", short, day),
		fmt.Sprintf("%s %d, %d", short, day, year),
		fmt.Sprintf("%d %s", day, name),
		fmt.Sprintf("%d %s %d", day, name, year),
		fmt.Sprintf("%d %s", day, short),
		fmt.Sprintf("%s %s", name, ordinal),
		fmt.Sprintf("%s %s", short, ordinal),
		fmt.Sprintf("%s %s", ordinal, name),
		fmt.Sprintf("%s of %s", ordinal, name),
	}
}

func v13Ordinal(day int) string {
	switch {
	case day%100 >= 11 && day%100 <= 13:
		return "th"
	case day%10 == 1:
		return "st"
	case day%10 == 2:
		return "nd"
	case day%10 == 3:
		return "rd"
	}
	return "th"
}

// v13DateProse renders a date inside a record with a seeded form.
func v13DateProse(seed int64, salt string, month, day int) string {
	forms := []string{
		fmt.Sprintf("%s %d", v13DateMonths[month-1], day),
		fmt.Sprintf("%d %s", day, v13DateMonths[month-1]),
		fmt.Sprintf("2026-%02d-%02d", month, day),
		fmt.Sprintf("the %d%s of %s", day, v13Ordinal(day), v13DateMonths[month-1]),
	}
	return v13Pick(seed, "date-form-"+salt, forms)
}

// ── Latent scenario ──────────────────────────────────────────────────────────

// v13Date is a calendar day in the record year.
type v13Date struct{ Month, Day int }

func (d v13Date) ordinal() int { return d.Month*31 + d.Day }

// v13Group holds every value drawn for one metamorphic group. The base and
// counterfactual members are rendered from the same draw; the counterfactual
// swaps exactly the targeted fact (the Counter* field).
type v13Group struct {
	Family       V13Family
	Alias        string
	Purpose      string
	DecoyAlias   string
	DecoyPurpose string

	// People: [0]=first/holder-A, [1]=current/holder-B, [2]=counterfactual,
	// [3]=decoy.
	People [4]string
	// Class indexes into the family's term bank: [0]=first, [1]=current,
	// [2]=counterfactual, [3]=decoy.
	Classes [4]int
	// Events: three dated events (class index + date) plus the counterfactual
	// date shift target.
	Events [3]struct {
		Class int
		Date  v13Date
	}
	CounterEvent int // index of the event whose date the counterfactual moves
	CounterDate  v13Date
	DecoyEvent   int
	// Organisations: [0]=client, [1]=vendor, [2]=counterfactual client,
	// [3]=decoy client.
	Orgs [4]string
	// Neutral milestone date planted in the third state record.
	Milestone v13Date
}

// v13Draws holds the per-seed draw state shared across groups.
type v13Draws struct {
	seed      int64
	names     *rand.Rand
	nameOrd   int
	usedNames map[string]bool
	usedOrgs  map[string]bool
	usedPurp  map[string]bool
	statusMap []int // class index -> alias term index
}

// newV13Draws opens the per-seed draw state. stream separates the name RNG of
// independent generators (business vs personal) so neither perturbs the other.
func newV13Draws(seed int64, stream string) *v13Draws {
	return &v13Draws{
		seed:      seed,
		names:     rand.New(rand.NewSource(v10Seed(seed, "v13:people:"+stream))),
		usedNames: map[string]bool{},
		usedOrgs:  map[string]bool{},
		usedPurp:  map[string]bool{},
		statusMap: v13Perm(seed, "status-alias", len(v13StatusAliasTerms))[:len(V13StatusClasses)],
	}
}

// name draws a distinct full name from the 10k humandata wordbanks; surnames
// are unique within a seed so no two people share a bounded token.
func (d *v13Draws) name() string {
	for {
		given := humandata.GivenName(d.names, d.nameOrd)
		sur := humandata.Surname(d.names, d.nameOrd)
		d.nameOrd++
		full := given + " " + sur
		if d.usedNames[full] || d.usedNames["sur:"+strings.ToLower(sur)] || d.usedNames["given:"+strings.ToLower(given)] {
			continue
		}
		d.usedNames[full] = true
		d.usedNames["sur:"+strings.ToLower(sur)] = true
		d.usedNames["given:"+strings.ToLower(given)] = true
		return full
	}
}

// StatusAlias returns the seed's jargon term for a status class.
func (d *v13Draws) statusAlias(class int) string {
	return v13StatusAliasTerms[d.statusMap[class]]
}

// distinctClasses draws n distinct class indexes from a bank of size size.
func (d *v13Draws) distinctClasses(salt string, size, n int) [4]int {
	perm := v13Perm(d.seed, salt, size)
	var out [4]int
	for i := 0; i < n && i < 4; i++ {
		out[i] = perm[i]
	}
	return out
}

func (d *v13Draws) drawGroup(group int) v13Group {
	g := v13Group{Family: V13Families[group%len(V13Families)]}
	g.Alias = v12Label(d.seed, fmt.Sprintf("v13-scenario-%d", group))
	g.DecoyAlias = v12Label(d.seed, fmt.Sprintf("v13-decoy-%d", group))
	g.Purpose = v13Purpose(d.seed, fmt.Sprintf("remit-%d", group), d.usedPurp)
	g.DecoyPurpose = v13Purpose(d.seed, fmt.Sprintf("decoy-remit-%d", group), d.usedPurp)
	for i := range g.People {
		g.People[i] = d.name()
	}
	g.Milestone = v13Date{Month: 3 + v13Index(d.seed, fmt.Sprintf("milestone-m-%d", group), 8), Day: 1 + v13Index(d.seed, fmt.Sprintf("milestone-d-%d", group), 28)}
	switch g.Family {
	case V13FamilyStandingStatus:
		g.Classes = d.distinctClasses(fmt.Sprintf("status-classes-%d", group), len(V13StatusClasses), 4)
	case V13FamilyCurrentChannel:
		g.Classes = d.distinctClasses(fmt.Sprintf("channel-classes-%d", group), len(V13ChannelClasses), 4)
	case V13FamilyNextActionResponsible:
		g.Classes = d.distinctClasses(fmt.Sprintf("action-classes-%d", group), len(V13ActionClasses), 2)
	case V13FamilyLatestEvent:
		classes := v13Perm(d.seed, fmt.Sprintf("event-classes-%d", group), len(V13EventClasses))
		// Three distinct dates in the first half of the year, strictly ordered
		// so the latest is unambiguous.
		days := v13Perm(d.seed, fmt.Sprintf("event-days-%d", group), 28)
		months := v13Perm(d.seed, fmt.Sprintf("event-months-%d", group), 6)
		for i := 0; i < 3; i++ {
			g.Events[i].Class = classes[i]
			g.Events[i].Date = v13Date{Month: 1 + months[i], Day: 1 + days[i]}
		}
		g.DecoyEvent = classes[3]
		// The counterfactual moves a non-latest event past the latest one: a
		// different event becomes the most recent.
		latest := v13LatestEventIndex(g.Events)
		g.CounterEvent = (latest + 1 + v13Index(d.seed, fmt.Sprintf("counter-event-%d", group), 2)) % 3
		latestDate := g.Events[latest].Date
		g.CounterDate = v13Date{Month: 7 + v13Index(d.seed, fmt.Sprintf("counter-month-%d", group), 5), Day: 1 + days[3]}
		if g.CounterDate.ordinal() <= latestDate.ordinal() {
			g.CounterDate = v13Date{Month: 12, Day: 1 + days[3]}
		}
	case V13FamilyClientVsVendor:
		g.Orgs[0] = v13Org(d.seed, fmt.Sprintf("client-%d", group), d.usedOrgs)
		g.Orgs[1] = v13Org(d.seed, fmt.Sprintf("vendor-%d", group), d.usedOrgs)
		g.Orgs[2] = v13Org(d.seed, fmt.Sprintf("counter-client-%d", group), d.usedOrgs)
		g.Orgs[3] = v13Org(d.seed, fmt.Sprintf("decoy-client-%d", group), d.usedOrgs)
	}
	return g
}

func v13LatestEventIndex(events [3]struct {
	Class int
	Date  v13Date
}) int {
	latest := 0
	for i := 1; i < 3; i++ {
		if events[i].Date.ordinal() > events[latest].Date.ordinal() {
			latest = i
		}
	}
	return latest
}

// ── Rendered member ──────────────────────────────────────────────────────────

// v13Member is one rendered scenario member before it is wrapped into a
// MemoryCase: the three state records, the decoy clause, the question, and
// the typed answer specification.
type v13Member struct {
	Records     [3]string
	DecoyClause string
	Question    string
	Kind        string
	Expected    string
	AcceptAny   []string
	Items       []string
	ItemKinds   []string
	ItemAccept  [][]string
	Distractors []string
	Claims      []protocol.Claim
	Program     V10QueryNode
	Facts       []string
	Constraints []string
	Operations  []string
	Protected   []string
}

func v13Subject(seed int64, group int, purpose string) string {
	return fmt.Sprintf(v13Pick(seed, fmt.Sprintf("subj-%d", group), v13SubjectForms), purpose)
}

func v13Question(seed int64, group, variant int, forms []string, closers []string, args ...any) string {
	body := fmt.Sprintf(v13Pick(seed, fmt.Sprintf("qform-%d", group), forms), args...)
	return v13Pick(seed, fmt.Sprintf("qopen-%d-%d", group, variant), v13QuestionOpeners) + " " +
		strings.ToUpper(body[:1]) + body[1:] + " " +
		v13Pick(seed, fmt.Sprintf("qclose-%d-%d", group, variant), closers)
}

func v13Claim(kind, expected string, accept []string, weight float64) protocol.Claim {
	return protocol.Claim{Kind: kind, Expected: expected, Accept: append([]string(nil), accept...), Critical: true, Weight: weight}
}

// renderV13Member materialises one member of a group. counter selects the
// causal-counterfactual reading of the same draw.
func renderV13Member(d *v13Draws, schema v13Schema, group, variant int, g v13Group, counter bool) v13Member {
	seed := d.seed
	subject := v13Subject(seed, group, g.Purpose)
	milestone := v13DateProse(seed, fmt.Sprintf("milestone-%d", group), g.Milestone.Month, g.Milestone.Day)
	neutral := fmt.Sprintf(v13Pick(seed, fmt.Sprintf("neutral-%d", group), []string{
		"The first milestone review for %[1]s is pencilled in for %[2]s.",
		"%[1]s has its next checkpoint on %[2]s.",
		"A progress read-out on %[1]s is scheduled for %[2]s.",
	}), g.Alias, milestone)
	resolve := V10QueryNode{Op: "resolve_entity", Field: schema.Alias}
	latest := func(field string) V10QueryNode {
		return V10QueryNode{Op: "latest", Field: field, Children: []V10QueryNode{resolve}}
	}
	read := func(field string) V10QueryNode {
		return V10QueryNode{Op: "read", Field: field, Children: []V10QueryNode{resolve}}
	}
	m := v13Member{
		Kind:      protocol.AnswerValue,
		Protected: []string{g.Alias, schema.Entity, schema.Alias, schema.Correction},
	}
	m.Records[2] = neutral

	switch g.Family {
	case V13FamilyOwnerAfterCorrection:
		first, current := g.People[0], g.People[1]
		if counter {
			current = g.People[2]
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("owner-a-%d", group), []string{
			"%[1]s was first assigned to %[2]s as its %[3]s.",
			"At the outset the %[3]s on %[1]s was %[2]s.",
			"%[2]s took %[1]s as %[3]s when it opened.",
		}), g.Alias, first, schema.Owner)
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("owner-b-%d", group), []string{
			"Under this batch's %[3]s, the %[4]s on %[1]s passed to %[2]s; the earlier assignment no longer stands.",
			"A later %[3]s hands %[1]s's %[4]s to %[2]s — the newest entry governs.",
			"%[3]s note: %[2]s now holds %[4]s on %[1]s, replacing whoever held it before.",
		}), g.Alias, current, schema.Correction, schema.Owner)
		m.DecoyClause = fmt.Sprintf(" Its %s is %s.", schema.Owner, g.People[3])
		m.Question = v13Question(seed, group, variant, []string{
			"who currently holds the %[2]s role on %[1]s?",
			"after every recorded reassignment, who is the %[2]s for %[1]s?",
			"for %[1]s, which person stands as %[2]s today?",
		}, []string{"Just the name is fine.", "Give the person's name.", "Answer with the name only."}, subject, schema.Owner)
		m.Expected = current
		m.AcceptAny = []string{current}
		m.Distractors = []string{g.People[3]}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindPerson, current, []string{current}, 1)}
		m.Program = latest(schema.Owner)
		m.Protected = append(m.Protected, schema.Owner, first, current, g.People[3])
		m.Facts = []string{"per-run schema glossary", "entity alias", "initial owner assignment", "superseding reassignment"}
		m.Constraints = []string{g.Alias, schema.Owner, schema.Correction, g.Purpose}
		m.Operations = []string{"induce schema", "resolve subject by remit", "select latest owner assignment", "return the person"}

	case V13FamilyStandingStatus:
		first, current, decoy := g.Classes[0], g.Classes[1], g.Classes[3]
		if counter {
			current = g.Classes[2]
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("status-a-%d", group), []string{
			"%[1]s's %[2]s opened as %[3]s.",
			"When %[1]s was logged its %[2]s read %[3]s.",
			"The initial %[2]s on %[1]s was %[3]s.",
		}), g.Alias, schema.Status, d.statusAlias(first))
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("status-b-%d", group), []string{
			"A later %[3]s moved %[1]s's %[2]s to %[4]s; the newest entry governs.",
			"Under this batch's %[3]s the %[2]s on %[1]s now stands at %[4]s.",
			"%[3]s: %[1]s's %[2]s was revised to %[4]s, replacing the earlier reading.",
		}), g.Alias, schema.Status, schema.Correction, d.statusAlias(current))
		m.DecoyClause = fmt.Sprintf(" Its %s is %s.", schema.Status, d.statusAlias(decoy))
		m.Question = v13Question(seed, group, variant, []string{
			"what %[2]s currently stands for %[1]s?",
			"after the recorded revision, what is the standing %[2]s of %[1]s?",
			"for %[1]s, which %[2]s governs now?",
		}, []string{"Plain English or the workspace term is fine.", "Name the status.", "Answer with the status term."}, subject, schema.Status)
		cls := V13StatusClasses[current]
		m.Expected = cls.Canonical
		m.AcceptAny = append([]string{d.statusAlias(current)}, cls.Accept...)
		m.Distractors = []string{V13StatusClasses[decoy].Canonical, d.statusAlias(decoy)}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindStatus, cls.Canonical, m.AcceptAny, 1)}
		m.Program = latest(schema.Status)
		m.Protected = append(m.Protected, schema.Status, d.statusAlias(first), d.statusAlias(current), d.statusAlias(decoy))
		m.Facts = []string{"per-run schema glossary", "status alias legend", "entity alias", "initial status", "superseding status decision"}
		m.Constraints = []string{g.Alias, schema.Status, schema.Correction, g.Purpose}
		m.Operations = []string{"induce schema", "resolve subject by remit", "select latest status", "map alias term to plain status", "return the status"}

	case V13FamilyLatestEvent:
		events := g.Events
		if counter {
			events[g.CounterEvent].Date = g.CounterDate
		}
		forms := []string{
			"On %[3]s, %[1]s logged a %[2]s: %[4]s.",
			"%[1]s's %[2]s entry dated %[3]s reads %[4]s.",
			"A %[2]s for %[1]s — %[4]s — is on record for %[3]s.",
		}
		for i := 0; i < 3; i++ {
			ev := V13EventClasses[events[i].Class]
			m.Records[i] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("event-%d-%d", group, i), forms), g.Alias, schema.Event, v13DateProse(seed, fmt.Sprintf("event-%d-%d", group, i), events[i].Date.Month, events[i].Date.Day), ev.Canonical)
		}
		latestIdx := v13LatestEventIndex(events)
		cls := V13EventClasses[events[latestIdx].Class]
		m.DecoyClause = fmt.Sprintf(" Its only %s is a %s.", schema.Event, V13EventClasses[g.DecoyEvent].Canonical)
		m.Question = v13Question(seed, group, variant, []string{
			"which %[2]s is the most recent one recorded for %[1]s?",
			"of every dated %[2]s on %[1]s, which happened last?",
			"for %[1]s, what is the latest %[2]s on record?",
		}, []string{"Name the event.", "Just the event is fine.", "Answer with the event, not the date."}, subject, schema.Event)
		m.Expected = cls.Canonical
		m.AcceptAny = append([]string(nil), cls.Accept...)
		for i := 0; i < 3; i++ {
			if i != latestIdx {
				m.Distractors = append(m.Distractors, V13EventClasses[events[i].Class].Canonical)
			}
		}
		m.Distractors = append(m.Distractors, V13EventClasses[g.DecoyEvent].Canonical)
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindEvent, cls.Canonical, cls.Accept, 1)}
		m.Program = V10QueryNode{Op: "latest", Field: schema.Event, Children: []V10QueryNode{resolve, {Op: "order_by_date", Field: schema.Event}}}
		m.Protected = append(m.Protected, schema.Event)
		for _, ev := range events {
			m.Protected = append(m.Protected, V13EventClasses[ev.Class].Canonical)
		}
		m.Facts = []string{"per-run schema glossary", "entity alias", "three dated events", "date ordering"}
		m.Constraints = []string{g.Alias, schema.Event, g.Purpose}
		m.Operations = []string{"induce schema", "resolve subject by remit", "parse each event date", "select the latest", "return the event"}

	case V13FamilyClientVsVendor:
		client, vendor := g.Orgs[0], g.Orgs[1]
		if counter {
			// Exactly one record changes: the commissioning party is re-recorded
			// as a different organisation; the supplier record is untouched.
			client = g.Orgs[2]
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("client-a-%d", group), []string{
			"%[1]s's %[2]s is %[3]s.",
			"For %[1]s the %[2]s on record is %[3]s.",
			"%[3]s is logged as the %[2]s on %[1]s.",
		}), g.Alias, schema.Client, client)
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("vendor-a-%d", group), []string{
			"%[1]s's %[2]s is %[3]s.",
			"For %[1]s the %[2]s on record is %[3]s.",
			"%[3]s is logged as the %[2]s on %[1]s.",
		}), g.Alias, schema.Vendor, vendor)
		m.DecoyClause = fmt.Sprintf(" Its %s is %s.", schema.Client, g.Orgs[3])
		m.Question = v13Question(seed, group, variant, []string{
			"on %[1]s, which organisation commissions the work and pays us — as opposed to the supplier we pay?",
			"for %[1]s, name the organisation that is our paying customer rather than our supplier.",
			"which organisation is the commissioning party on %[1]s (not the one we pay)?",
		}, []string{"Just the organisation name.", "Name the organisation.", "Answer with the organisation only."}, subject)
		m.Expected = client
		m.AcceptAny = []string{client}
		m.Distractors = []string{vendor, g.Orgs[3]}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindOrganisation, client, []string{client}, 1)}
		m.Program = read(schema.Client)
		m.Protected = append(m.Protected, schema.Client, schema.Vendor, client, vendor, g.Orgs[3])
		m.Facts = []string{"per-run schema glossary", "entity alias", "client organisation", "vendor organisation"}
		m.Constraints = []string{g.Alias, schema.Client, schema.Vendor, g.Purpose}
		m.Operations = []string{"induce schema", "resolve subject by remit", "map role labels to commissioning vs supplier", "return the client organisation"}

	case V13FamilyNextActionResponsible:
		action := V13ActionClasses[g.Classes[0]]
		first, current := g.People[0], g.People[1]
		if counter {
			current = g.People[2]
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("action-a-%d", group), []string{
			"The %[2]s on %[1]s is to %[3]s, with %[4]s down as %[5]s.",
			"%[1]s's %[2]s: %[3]s; %[5]s at the time: %[4]s.",
			"Next up for %[1]s, per its %[2]s, is to %[3]s — %[4]s was named %[5]s.",
		}), g.Alias, schema.Action, action.Canonical, first, schema.Responsible)
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("action-b-%d", group), []string{
			"Under this batch's %[3]s, %[4]s for that step on %[1]s is now %[2]s; the step itself is unchanged.",
			"%[3]s: the %[4]s on %[1]s's next step moved to %[2]s. The step stays as recorded.",
			"A later %[3]s names %[2]s as %[4]s for %[1]s's pending step, replacing the earlier holder.",
		}), g.Alias, current, schema.Correction, schema.Responsible)
		decoyAction := V13ActionClasses[g.Classes[1]]
		m.DecoyClause = fmt.Sprintf(" Its %s is to %s, owned by %s.", schema.Action, decoyAction.Canonical, g.People[3])
		m.Question = v13Question(seed, group, variant, []string{
			"what is the pending %[2]s on %[1]s, and who is its %[3]s now?",
			"for %[1]s, state the standing %[2]s and the person currently named %[3]s.",
			"which step is next on %[1]s and who owns it after every recorded change?",
		}, []string{"Give both the step and the name.", "Two parts: the step, then the person.", "Answer with the action and the owner."}, subject, schema.Action, schema.Responsible)
		m.Kind = protocol.AnswerList
		m.Expected = action.Canonical + "; " + current
		m.Items = []string{action.Canonical, current}
		m.ItemKinds = []string{"", ""}
		m.ItemAccept = [][]string{append([]string(nil), action.Accept...), {current}}
		m.Distractors = []string{g.People[3], decoyAction.Canonical}
		m.Claims = []protocol.Claim{
			v13Claim(protocol.ClaimKindAction, action.Canonical, action.Accept, 0.5),
			v13Claim(protocol.ClaimKindPerson, current, []string{current}, 0.5),
		}
		m.Program = V10QueryNode{Op: "tuple", Children: []V10QueryNode{read(schema.Action), latest(schema.Responsible)}}
		m.Protected = append(m.Protected, schema.Action, schema.Responsible, first, current, g.People[3])
		m.Facts = []string{"per-run schema glossary", "entity alias", "next action", "initial responsible party", "superseding responsibility change"}
		m.Constraints = []string{g.Alias, schema.Action, schema.Responsible, schema.Correction, g.Purpose}
		m.Operations = []string{"induce schema", "resolve subject by remit", "read the standing action", "select latest responsible party", "return both"}

	case V13FamilyCurrentChannel:
		first, current, decoy := g.Classes[0], g.Classes[1], g.Classes[3]
		if counter {
			current = g.Classes[2]
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("channel-a-%d", group), []string{
			"%[1]s's %[2]s started out as %[3]s.",
			"The first %[2]s agreed for %[1]s was %[3]s.",
			"Early on, %[1]s ran its %[2]s over %[3]s.",
		}), g.Alias, schema.Channel, V13ChannelClasses[first].Canonical)
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("channel-b-%d", group), []string{
			"A later %[3]s moved %[1]s's %[2]s to %[4]s; the newest entry governs.",
			"Under this batch's %[3]s the %[2]s for %[1]s is now %[4]s.",
			"%[3]s: %[1]s's %[2]s switched to %[4]s, replacing the earlier arrangement.",
		}), g.Alias, schema.Channel, schema.Correction, V13ChannelClasses[current].Canonical)
		m.DecoyClause = fmt.Sprintf(" Its %s is %s.", schema.Channel, V13ChannelClasses[decoy].Canonical)
		m.Question = v13Question(seed, group, variant, []string{
			"which %[2]s currently stands for %[1]s?",
			"after the recorded change, how do we reach the other side on %[1]s — which %[2]s?",
			"for %[1]s, what %[2]s governs now?",
		}, []string{"Name the channel.", "Just the channel is fine.", "Answer with the channel."}, subject, schema.Channel)
		cls := V13ChannelClasses[current]
		m.Expected = cls.Canonical
		m.AcceptAny = append([]string(nil), cls.Accept...)
		m.Distractors = []string{V13ChannelClasses[decoy].Canonical}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindChannel, cls.Canonical, cls.Accept, 1)}
		m.Program = latest(schema.Channel)
		m.Protected = append(m.Protected, schema.Channel, V13ChannelClasses[first].Canonical, cls.Canonical, V13ChannelClasses[decoy].Canonical)
		m.Facts = []string{"per-run schema glossary", "entity alias", "initial channel", "superseding channel change"}
		m.Constraints = []string{g.Alias, schema.Channel, schema.Correction, g.Purpose}
		m.Operations = []string{"induce schema", "resolve subject by remit", "select latest channel", "return the channel"}

	case V13FamilyRecordsDisagree:
		a, b := g.People[0], g.People[1]
		if counter {
			b = g.People[2]
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("disagree-a-%d", group), []string{
			"%[1]s's launch sign-off is recorded as given by %[2]s.",
			"One entry has %[2]s signing off %[1]s.",
			"Per the first note, %[2]s approved %[1]s for launch.",
		}), g.Alias, a)
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("disagree-b-%d", group), []string{
			"A separate note records the sign-off on %[1]s as coming from %[2]s. Neither note cites the other.",
			"Elsewhere %[2]s is named as the person who signed off %[1]s; the two entries are independent.",
			"Another record lists %[2]s as %[1]s's launch approver, with no reference to any earlier entry.",
		}), g.Alias, b)
		m.DecoyClause = fmt.Sprintf(" Its launch was signed off by %s.", g.People[3])
		m.Question = v13Question(seed, group, variant, []string{
			"do the records agree on who gave the launch sign-off for %[1]s? Say whether they agree and give the name each record carries.",
			"is the launch sign-off consistent across the records for %[1]s? State agree or disagree and name whoever each record credits.",
			"according to the records, who gave the launch sign-off for %[1]s — and do those records agree with each other?",
		}, []string{"Name every person the records credit.", "Give both names and say whether they match.", "Be explicit about any conflict."}, subject)
		m.Kind = protocol.AnswerList
		m.Expected = a + "; " + b + "; disagree"
		m.Items = []string{a, b, "disagree"}
		m.ItemKinds = []string{"", "", ""}
		m.ItemAccept = [][]string{{a}, {b}, append([]string(nil), V13ConflictAccept...)}
		// Exempt from distractor scanning: both values are what the records say,
		// so there is no wrong same-attribute value to fire on.
		m.Distractors = nil
		m.Claims = []protocol.Claim{
			v13Claim(protocol.ClaimKindPerson, a, []string{a}, 1.0/3),
			v13Claim(protocol.ClaimKindPerson, b, []string{b}, 1.0/3),
			v13Claim(protocol.ClaimKindConflict, "disagree", V13ConflictAccept, 1.0/3),
		}
		m.Program = V10QueryNode{Op: "conflict", Field: schema.Owner, Children: []V10QueryNode{resolve}}
		m.Protected = append(m.Protected, a, b, g.People[3])
		m.Facts = []string{"per-run schema glossary", "entity alias", "first sign-off record", "independent second sign-off record"}
		m.Constraints = []string{g.Alias, g.Purpose}
		m.Operations = []string{"induce schema", "resolve subject by remit", "collect every sign-off record", "detect disagreement", "return both values and the conflict"}
	default:
		panic("unhandled v13 family")
	}
	return m
}

// v13Glossary renders the schema's semantics as two seeded sentences plus,
// for status groups, the workspace's status alias legend. No stable prefix.
func v13Glossary(d *v13Draws, group int, schema v13Schema, g v13Group) []string {
	seed := d.seed
	clauses := []string{
		fmt.Sprintf("%s %s a workstream", schema.Entity, v13Pick(seed, fmt.Sprintf("gentity-%d", group), v11NamesVerbs)),
		fmt.Sprintf("%s is its %s", schema.Alias, v13Pick(seed, fmt.Sprintf("galias-%d", group), v11AliasNouns)),
		fmt.Sprintf("%s %s", schema.Owner, v13Pick(seed, fmt.Sprintf("gowner-%d", group), []string{"names the accountable owner", "is whoever is accountable for it", "identifies the person on the hook for it"})),
		fmt.Sprintf("%s %s", schema.Status, v13Pick(seed, fmt.Sprintf("gstatus-%d", group), []string{"is its standing status", "records where it currently stands", "carries the governing status"})),
		fmt.Sprintf("%s %s", schema.Event, v13Pick(seed, fmt.Sprintf("gevent-%d", group), []string{"is a dated event on it", "marks something that happened on a given day", "logs one dated occurrence"})),
		fmt.Sprintf("%s %s", schema.Client, v13Pick(seed, fmt.Sprintf("gclient-%d", group), []string{"is the organisation that commissions it and pays us", "names our paying customer for it", "is the party that pays us for it"})),
		fmt.Sprintf("%s %s", schema.Vendor, v13Pick(seed, fmt.Sprintf("gvendor-%d", group), []string{"is the supplier we pay", "names the outside party we pay", "is the organisation billing us"})),
		fmt.Sprintf("%s %s", schema.Action, v13Pick(seed, fmt.Sprintf("gaction-%d", group), []string{"is the next step due", "names what happens next", "records the pending action"})),
		fmt.Sprintf("%s %s", schema.Responsible, v13Pick(seed, fmt.Sprintf("gresp-%d", group), []string{"is who owns that next step", "names the person carrying the next step", "identifies who must do it"})),
		fmt.Sprintf("%s %s", schema.Channel, v13Pick(seed, fmt.Sprintf("gchannel-%d", group), []string{"is how the two sides communicate", "names the agreed communication channel", "is the standing way to reach them"})),
		fmt.Sprintf("%s means %s", schema.Correction, v13Pick(seed, fmt.Sprintf("gcorr-%d", group), v11CorrectionNouns)),
	}
	rotation := v13Index(seed, fmt.Sprintf("grot-%d", group), len(clauses))
	rotated := append(append([]string(nil), clauses[rotation:]...), clauses[:rotation]...)
	split := len(rotated) / 2
	opener1 := v13Pick(seed, fmt.Sprintf("gopen1-%d", group), v13GlossaryOpeners)
	opener2 := v13Pick(seed, fmt.Sprintf("gopen2-%d", group), v13GlossaryOpeners)
	first := opener1 + " " + strings.Join(rotated[:split], "; ") + "."
	second := opener2 + " " + strings.Join(rotated[split:], "; ") + "."
	if g.Family == V13FamilyStandingStatus {
		// The status legend binds every alias term this group uses to its
		// plain-English class, so an honest reader can answer either way.
		legend := make([]string, 0, 4)
		for _, class := range g.Classes {
			legend = append(legend, fmt.Sprintf("%s means %s", d.statusAlias(class), V13StatusClasses[class].Canonical))
		}
		perm := v13Perm(seed, fmt.Sprintf("legend-%d", group), len(legend))
		ordered := make([]string, 0, len(legend))
		for _, p := range perm {
			ordered = append(ordered, legend[p])
		}
		second += " " + fmt.Sprintf(v13Pick(seed, fmt.Sprintf("glegend-%d", group), []string{
			"Status shorthand in this workspace: %s.",
			"For status values, %s.",
			"Our status words: %s.",
		}), strings.Join(ordered, ", "))
	}
	return []string{first, second}
}

// renderV13Record wraps one row in the renderer's framing with a composed
// acknowledgement. Shapes mirror the v10 renderer classes.
func renderV13Record(seed int64, group int, renderer V10Renderer, index int, row, alias string) (string, string) {
	ack := v13Pick(seed, fmt.Sprintf("acklead-%d-%d", group, index), v13AckLeads) + " " +
		v13Pick(seed, fmt.Sprintf("ackbody-%d-%d", group, index), v13AckBodies) +
		" (thread: " + alias + ")"
	switch renderer {
	case V10ConversationRenderer:
		return v13Pick(seed, fmt.Sprintf("convlead-%d-%d", group, index), v13ConversationLeads) + row, ack
	case V10EmailRenderer:
		return fmt.Sprintf("Subject: workstream note %d\nFrom: operations@example.invalid\n\n%s", index+1, row), ack
	case V10TableRenderer:
		return fmt.Sprintf("Pasted table row %d\n| payload |\n|---|\n| %s |", index+1, row), ack
	case V10OpsRenderer:
		return fmt.Sprintf("operations_dump[%d] { %s }", index, strings.ReplaceAll(row, "; ", ", ")), ack
	default:
		panic("unhandled v13 renderer")
	}
}

// GenerateV13Programs returns count scored business-event cases in complete
// four-member metamorphic groups (base, renderer invariant, distractor
// invariant, causal counterfactual). Count must be a positive multiple of
// four; 28 covers every family once.
func GenerateV13Programs(seed int64, count int) ([]V10GeneratedCase, error) {
	if count <= 0 || count%4 != 0 {
		return nil, fmt.Errorf("v13 program count must be a positive multiple of four, got %d", count)
	}
	schema := generateV13Schema(seed)
	ontology := v13Ontology(schema)
	schemaDigest, err := digestV13Schema(schema)
	if err != nil {
		return nil, err
	}
	d := newV13Draws(seed, "business")
	out := make([]V10GeneratedCase, 0, count)
	for group := 0; group < count/4; group++ {
		g := d.drawGroup(group)
		base := renderV13Member(d, schema, group, 0, g, false)
		counter := renderV13Member(d, schema, group, 3, g, true)
		if base.Expected == counter.Expected {
			return nil, fmt.Errorf("v13 causal mutation did not change group %d (%s) answer", group, g.Family)
		}
		groupID := protocol.OpaqueCaseID(seed, "v13-metamorphic-group", group)
		variants := []struct {
			relation string
			answer   string
			counter  bool
			distract bool
		}{
			{relation: protocol.RelationBase, answer: "base"},
			{relation: protocol.RelationRendererInvariant, answer: "same"},
			{relation: protocol.RelationDistractorInvariant, answer: "same", distract: true},
			{relation: protocol.RelationCausalCounterfactual, answer: "changed", counter: true},
		}
		for variant, spec := range variants {
			member := renderV13Member(d, schema, group, variant, g, spec.counter)
			renderer := v10Renderers[(group+variant)%len(v10Renderers)]
			generated := materializeV13Case(d, schema, schemaDigest, ontology, group, variant, groupID, renderer, g, member, spec.relation, spec.answer, spec.distract)
			out = append(out, generated)
		}
	}
	return out, nil
}

func materializeV13Case(
	d *v13Draws,
	schema v13Schema,
	schemaDigest string,
	ontology []V10OntologyTerm,
	group, variant int,
	groupID string,
	renderer V10Renderer,
	g v13Group,
	m v13Member,
	relation, answerRelation string,
	includeDistractor bool,
) V10GeneratedCase {
	seed := d.seed
	prefix := fmt.Sprintf("v13-%d-%d", group, variant)
	pairIDs := []string{
		protocol.OpaqueCaseID(seed, prefix+"-glossary-a", 0),
		protocol.OpaqueCaseID(seed, prefix+"-glossary-b", 0),
		protocol.OpaqueCaseID(seed, prefix+"-binding", 0),
		protocol.OpaqueCaseID(seed, prefix+"-state-a", 0),
		protocol.OpaqueCaseID(seed, prefix+"-state-b", 0),
		protocol.OpaqueCaseID(seed, prefix+"-state-c", 0),
	}
	glossary := v13Glossary(d, group, schema, g)
	binding := fmt.Sprintf("%s=%s; %s=%s. Its remit is %s.", schema.Entity, g.Alias, schema.Alias, g.Alias, g.Purpose)
	if includeDistractor {
		binding += fmt.Sprintf(" Separately, an unrelated workstream %s=%s handles %s.%s", schema.Entity, g.DecoyAlias, g.DecoyPurpose, m.DecoyClause)
	}
	movable := []string{binding, m.Records[0], m.Records[1], m.Records[2]}
	perm := v13Perm(seed, fmt.Sprintf("rows-%d", group), len(movable))
	rows := []string{glossary[0], glossary[1]}
	for _, p := range perm {
		rows = append(rows, movable[p])
	}
	pairs := make([]protocol.MemoryPair, 0, len(rows))
	for i, row := range rows {
		prompt, response := renderV13Record(seed, group, renderer, i, row, g.Alias)
		pairs = append(pairs, protocol.MemoryPair{
			PairID:    pairIDs[i],
			SessionID: protocol.OpaqueCaseID(seed, fmt.Sprintf("v13-session-%d", group), i),
			Timestamp: V13Timestamp(seed, fmt.Sprintf("v13-%d-%d", group, variant), i),
			Prompt:    prompt, Response: response,
		})
	}

	caseID := protocol.OpaqueCaseID(seed, "v13-program-case", group*4+variant)
	caseValue := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV13,
		ID:                caseID,
		QuestionID:        caseID,
		QuestionType:      V13ProgramQuestionType,
		Question:          m.Question,
		ExpectedAnswer:    m.Expected,
		AnswerKind:        m.Kind,
		AcceptAny:         append([]string(nil), m.AcceptAny...),
		AnswerItems:       append([]string(nil), m.Items...),
		AnswerItemKinds:   append([]string(nil), m.ItemKinds...),
		DistractorAnswers: append([]string(nil), m.Distractors...),
		WritingProtected:  append([]string(nil), m.Protected...),
		Claims:            append([]protocol.Claim(nil), m.Claims...),
	}
	if len(m.ItemAccept) > 0 {
		caseValue.AnswerItemAcceptAny = make([][]string, len(m.ItemAccept))
		for i, alts := range m.ItemAccept {
			caseValue.AnswerItemAcceptAny[i] = append([]string(nil), alts...)
		}
	}
	if relation != protocol.RelationCausalCounterfactual {
		caseValue.TwinGroup = groupID
	}
	plan := QuestionPlan{
		Case:            caseValue,
		RequiredPairIDs: append([]string(nil), pairIDs...),
		Facts:           append([]string(nil), m.Facts...),
		Constraints:     append([]string(nil), m.Constraints...),
		Operations:      append([]string(nil), m.Operations...),
	}
	provenance := V10CaseProvenance{
		Revision:         V13ProvenanceRevision,
		SchemaSHA256:     schemaDigest,
		Ontology:         append([]V10OntologyTerm(nil), ontology...),
		Program:          m.Program,
		Renderer:         renderer,
		MetamorphicGroup: groupID,
		Relation:         relation,
		AnswerRelation:   answerRelation,
		EvidencePairIDs:  append([]string(nil), pairIDs...),
	}
	return V10GeneratedCase{Plan: plan, Pairs: pairs, Provenance: provenance}
}

// V13FamilyOf returns the family a generated v13 case belongs to, derived from
// its group ordinal. Provenance-only: never on the wire.
func V13FamilyOf(caseIndex int) V13Family {
	return V13Families[(caseIndex/4)%len(V13Families)]
}
