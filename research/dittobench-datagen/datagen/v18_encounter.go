package datagen

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v18 tool encounters (docs/bench-versions.md, "Tool encounters").
// Reached only from bench_version >= 18.
//
// Up to v17 almost every tool case needed one or two calls: of 1,032 v18 tool
// cases over 20 seeds before this pass, 155 expected none, 648 one, 191 two
// and 6 three. Real Ditto chat episodes use a median of three tool calls (p75
// 7, p90 15; 65% use three or more), so the tool half stopped telling a
// capable agent from a lucky one.
//
// An encounter is one request that needs several dependent calls. Its mock
// world is argument-keyed (protocol.Encounter): the page a search returns is
// the page that carries the figure, that page points at a correction, a
// workflow run returns a job id that has to be polled until it finishes, a
// balance has to be emailed to the contact memory says is current. The case
// is graded on the outcome (grade.Encounter): the final answer, the required
// calls in any order, and a penalty for calls that have no business in the
// request. Every value is coined per seed and case, so a scenario shape is
// public but its answer is not.

// V18EncounterCategoryPrefix starts every encounter category.
const V18EncounterCategoryPrefix = "encounter_"

// Encounter archetypes. The category is V18EncounterCategoryPrefix + name.
const (
	V18EncounterErrata    = "errata_chain"
	V18EncounterReconcile = "reconcile_sources"
	V18EncounterJob       = "job_followthrough"
	V18EncounterHandoff   = "memory_handoff"
	V18EncounterTriage    = "status_triage"
)

// v18EncounterArchetypes is the rotation order of the pass.
var v18EncounterArchetypes = []string{
	V18EncounterErrata, V18EncounterReconcile, V18EncounterJob, V18EncounterHandoff, V18EncounterTriage,
}

// V18EncounterArchetypes returns the archetype names in rotation order.
func V18EncounterArchetypes() []string { return append([]string(nil), v18EncounterArchetypes...) }

// IsV18Encounter reports whether a category is a bench v18 encounter.
func IsV18Encounter(category string) bool {
	return strings.HasPrefix(category, V18EncounterCategoryPrefix)
}

// V18EncounterQuotaFor is the per-run encounter count for a tool-case count:
// 10 of 50 at full (two of each archetype), 5 of 24 at medium, 1 in the
// small smoke run.
func V18EncounterQuotaFor(n int) int {
	switch {
	case n >= 40:
		return 10
	case n >= 20:
		return 5
	case n >= 6:
		return 1
	default:
		return 0
	}
}

// v18EncounterSlots ranks the cases the encounter pass may replace: the v16
// connector slot order (duplicates of ordinary families first, singletons
// last), minus the connector and encounter cases already installed, then a
// last tier of ordinary non-world cases whose prerequisite memory is their
// own (a stale-context note): replacing one drops only its own pairs.
func v18EncounterSlots(seed int64, benchVersion int, cases []protocol.ToolCase) []int {
	var out []int
	used := map[int]bool{}
	for _, i := range v16ConnectorSlotsForVersion(seed, benchVersion, cases) {
		if !IsV18Encounter(cases[i].Category) && cases[i].Encounter == nil {
			out = append(out, i)
			used[i] = true
		}
	}
	referenced := map[string]bool{}
	for _, tc := range cases {
		if tc.RunAfterCaseID != "" {
			referenced[tc.RunAfterCaseID] = true
		}
	}
	for i, tc := range cases {
		if used[i] || len(tc.PrerequisitePairs) == 0 || referenced[tc.ID] || tc.Encounter != nil ||
			strings.HasPrefix(tc.Category, "world_") || v9WorldFamily(tc.Category) || v13WorldDerived(tc.Category) ||
			tc.Restraint != nil || tc.EffectAnswer != "" || tc.TwinGroup != "" || tc.RunAfterCaseID != "" ||
			len(tc.ForbiddenTools) != 0 || len(tc.AlternativeExpectedTools) != 0 ||
			IsV13Restraint(tc.Category) || v13IsUnexpectedFamily(tc.Category) || IsV16ConnectorCase(tc.Category) ||
			IsDecoyCorrect(tc.Category) || v13DiscoveryFamily(tc.Category) {
			continue
		}
		out = append(out, i)
	}
	return out
}

// applyV18EncounterBench installs the encounters after every other tool pass,
// so it replaces only cases no other pass built or depends on.
func applyV18EncounterBench(seed int64, benchVersion int, cases []protocol.ToolCase) {
	if benchVersion < protocol.BenchVersionV18 {
		return
	}
	quota := V18EncounterQuotaFor(len(cases))
	slots := v18EncounterSlots(seed, benchVersion, cases)
	if quota > len(slots) {
		quota = len(slots)
	}
	plan := catalog.V16ConnectorPlanForSeed(seed)
	offset := v13ToolPick(seed, 0, "v18-encounter-offset", len(v18EncounterArchetypes))
	for k := 0; k < quota; k++ {
		i := slots[k]
		archetype := v18EncounterArchetypes[(offset+k)%len(v18EncounterArchetypes)]
		cases[i] = v18EncounterCase(seed, cases[i].ID, archetype, plan, k, i)
	}
}

// v18EncounterWorld draws one encounter's coined values.
type v18EncounterWorld struct {
	seed  int64
	index int
	r     *rand.Rand
}

func newV18EncounterWorld(seed int64, id string, index int) *v18EncounterWorld {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v18-encounter:%d:%s", seed, id)
	return &v18EncounterWorld{seed: seed, index: index, r: rand.New(rand.NewSource(int64(h.Sum64())))}
}

// stem is a coined capitalised name, distinct per salt.
func (w *v18EncounterWorld) stem(salt string) string {
	s := catalog.CoinedStem(w.seed, fmt.Sprintf("v18-encounter-%s-%d", salt, w.index))
	return strings.ToUpper(s[:1]) + s[1:]
}

var v18EncounterFirstNames = []string{
	"Avery", "Blake", "Casey", "Dana", "Emery", "Finley", "Harper", "Jordan", "Kendall", "Logan",
	"Morgan", "Noel", "Parker", "Quinn", "Reese", "Rowan", "Sage", "Taylor", "Wren", "Sasha",
}

// person is a first name and a coined surname.
func (w *v18EncounterWorld) person(salt string) (full, surname string) {
	first := v18EncounterFirstNames[w.r.Intn(len(v18EncounterFirstNames))]
	surname = w.stem("person-" + salt)
	return first + " " + surname, surname
}

func (w *v18EncounterWorld) between(lo, hi int) int { return lo + w.r.Intn(hi-lo+1) }

var v18EncounterMonths = []string{"January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"}

// dates returns two dates in one month, the first strictly earlier.
func (w *v18EncounterWorld) dates() (earlier, later string) {
	month := v18EncounterMonths[w.r.Intn(len(v18EncounterMonths))]
	d1 := w.between(2, 12)
	d2 := d1 + w.between(3, 14)
	return fmt.Sprintf("%s %d", month, d1), fmt.Sprintf("%s %d", month, d2)
}

func (w *v18EncounterWorld) pick(options []string) string { return options[w.r.Intn(len(options))] }

// money renders whole dollars with thousands separators ("$18,420").
func money(n int) string { return "$" + commaInt(n) }

func commaInt(n int) string {
	s := fmt.Sprint(n)
	var b strings.Builder
	for i, c := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(c)
	}
	return b.String()
}

func v18EncounterCase(seed int64, id, archetype string, plan catalog.V16ConnectorPlan, ordinal, index int) protocol.ToolCase {
	w := newV18EncounterWorld(seed, id, index)
	var tc protocol.ToolCase
	switch archetype {
	case V18EncounterErrata:
		tc = w.errataChain(plan)
	case V18EncounterReconcile:
		tc = w.reconcileSources(plan, ordinal)
	case V18EncounterJob:
		tc = w.jobFollowthrough(plan, ordinal)
	case V18EncounterHandoff:
		tc = w.memoryHandoff(plan, ordinal)
	default:
		tc = w.statusTriage(plan, ordinal)
	}
	tc.ID = id
	tc.Category = V18EncounterCategoryPrefix + archetype
	tc.Encounter.Archetype = archetype
	for _, name := range tc.Encounter.EncounterRequiredTools() {
		tc.ExpectedTools = append(tc.ExpectedTools, protocol.ToolSpec{Name: name})
	}
	tc.AllowExtraTools = true
	tc.Unordered = true
	tc.FuzzyTrajectory = true
	tc.MaxToolCalls = V18EncounterMaxCalls
	return tc
}

// V18EncounterMaxCalls documents the call budget of an encounter case. The
// grader does not cap calls; wandering is priced by the cost factor and the
// run budget.
const V18EncounterMaxCalls = 16

// linkedApp picks one of the seed's linked apps by ordinal.
func linkedApp(plan catalog.V16ConnectorPlan, ordinal int) catalog.ConnectorApp {
	return plan.Linked[ordinal%len(plan.Linked)]
}

// otherLinkedWrites are the write tools of every linked app, the actions an
// answer-only request has no business making.
func otherLinkedWrites(plan catalog.V16ConnectorPlan, why string, except ...string) []protocol.EncounterCall {
	skip := map[string]bool{}
	for _, name := range except {
		skip[name] = true
	}
	var out []protocol.EncounterCall
	for _, app := range plan.Linked {
		if !skip[app.WriteTool] {
			out = append(out, protocol.EncounterCall{Tool: app.WriteTool, Why: why})
		}
	}
	return out
}

func readResult(app catalog.ConnectorApp, body string) string {
	return fmt.Sprintf(app.Script.ReadResult, app.Name, body)
}

// Encounter wording. A request and every served result are composed from
// several interchangeable phrasings, and each archetype branches into
// variants that call for a judgment at one step (which notice is in force,
// whether the later record was approved, which job carries the figure, which
// amount is due, which region's incident is ours). A script that pattern-
// matches the plain path fails a variant; a harness that reads, or asks a
// decision model to choose among the candidates it extracted, does not.

// say picks one phrasing.
func (w *v18EncounterWorld) say(options ...string) string { return w.pick(options) }

// sentence joins non-empty parts with single spaces.
func sentence(parts ...string) string {
	var kept []string
	for _, part := range parts {
		if part = strings.TrimSpace(part); part != "" {
			kept = append(kept, part)
		}
	}
	return strings.Join(kept, " ")
}

// shuffled returns lines in a seeded order.
func (w *v18EncounterWorld) shuffled(lines ...string) []string {
	out := append([]string(nil), lines...)
	w.r.Shuffle(len(out), func(i, j int) { out[i], out[j] = out[j], out[i] })
	return out
}

// variant draws one variant name, weighted.
func (w *v18EncounterWorld) variant(weighted ...any) string {
	total := 0
	for i := 1; i < len(weighted); i += 2 {
		total += weighted[i].(int)
	}
	roll := w.r.Intn(total)
	for i := 0; i < len(weighted); i += 2 {
		roll -= weighted[i+1].(int)
		if roll < 0 {
			return weighted[i].(string)
		}
	}
	return weighted[len(weighted)-2].(string)
}

// Encounter variants (protocol.Encounter.Variant).
const (
	v18ErrataPlain      = "correction"
	v18ErrataOtherPlan  = "notice_for_other_plan"
	v18ErrataSuperseded = "correction_superseded"

	v18ReconcileApproved = "later_approved"
	v18ReconcilePending  = "later_pending"

	v18JobPlain   = "single_run"
	v18JobRetried = "retried_run"

	v18HandoffPlain  = "balance"
	v18HandoffCredit = "balance_after_credit"

	v18TriageUntracked = "untracked"
	v18TriageTracked   = "tracked"
	v18TriageRegion    = "other_region_tracked"
)

// errataChain: search, read the pricing page, decide whether its notice
// applies to this plan (and whether it was itself superseded), and compute an
// annual total from the price in force.
func (w *v18EncounterWorld) errataChain(plan catalog.V16ConnectorPlan) protocol.ToolCase {
	variant := w.variant(v18ErrataPlain, 1, v18ErrataOtherPlan, 1, v18ErrataSuperseded, 1)
	vendor := w.stem("vendor")
	host := strings.ToLower(vendor)
	plans := w.shuffled("Team", "Growth", "Scale", "Business", "Pro", "Starter")
	planName, otherPlan := plans[0], plans[1]
	listed := w.between(18, 64)
	first := listed - w.between(3, 9)
	second := first - w.between(1, 4)
	otherListed := w.between(8, 90)
	otherFixed := otherListed - w.between(2, 6)
	forum := listed + w.between(2, 7)
	seats := w.between(12, 95)
	annual := func(price int) string { return commaInt(price * seats * 12) }
	pricing := fmt.Sprintf("https://%s.com/pricing", host)
	notice := fmt.Sprintf("https://%s.com/pricing/notice-%d", host, w.between(100, 499))
	later := fmt.Sprintf("https://%s.com/pricing/notice-%d", host, w.between(500, 999))
	thread := fmt.Sprintf("https://forum.%s.net/t/%s-pricing-%d", strings.ToLower(w.stem("forum")), host, w.between(1000, 9999))
	date1, date2 := w.dates()
	perSeat := func(price int) string {
		return w.say(money(price)+" per seat per month", money(price)+"/seat/month", money(price)+" a seat each month")
	}
	noticePlan := planName
	if variant == v18ErrataOtherPlan {
		noticePlan = otherPlan
	}
	pricingLines := w.shuffled(
		fmt.Sprintf("%s: %s.", planName, perSeat(listed)),
		fmt.Sprintf("%s: %s.", otherPlan, perSeat(otherListed)),
	)
	pricingPage := sentence(
		w.say(vendor+" pricing.", "Plans and pricing - "+vendor+".", vendor+" | Pricing"),
		strings.Join(pricingLines, " "),
		w.say("Billed annually.", "All plans are billed yearly.", ""),
		w.say(
			fmt.Sprintf("Notice: a correction to the %s price was published on %s, see %s.", noticePlan, date1, notice),
			fmt.Sprintf("Pricing notice (%s) for the %s plan: %s", date1, noticePlan, notice),
			fmt.Sprintf("We corrected the %s price on %s; details at %s.", noticePlan, date1, notice),
		),
	)
	var noticePage string
	switch variant {
	case v18ErrataOtherPlan:
		noticePage = sentence(
			w.say("Pricing correction ("+date1+"):", "Correction, "+date1+":"),
			fmt.Sprintf("the %s plan is %s, not %s as first listed.", otherPlan, perSeat(otherFixed), money(otherListed)),
			w.say("Other plans are unaffected.", "No other plan changed.", ""),
		)
	default:
		noticePage = sentence(
			w.say("Pricing correction ("+date1+"):", "Correction, "+date1+":"),
			fmt.Sprintf("the %s plan is %s, not %s as first listed.", planName, perSeat(first), money(listed)),
		)
		if variant == v18ErrataSuperseded {
			noticePage = sentence(noticePage, w.say(
				fmt.Sprintf("Update %s: this correction was itself superseded, see %s.", date2, later),
				fmt.Sprintf("This notice is out of date as of %s; the current one is %s.", date2, later),
			))
		}
	}
	laterPage := sentence(
		w.say("Pricing notice ("+date2+"):", "Updated correction, "+date2+":"),
		fmt.Sprintf("the %s plan is %s.", planName, perSeat(second)),
		w.say(fmt.Sprintf("This replaces the %s notice.", date1), fmt.Sprintf("It supersedes the correction of %s.", date1)),
	)
	searchLines := w.shuffled(
		fmt.Sprintf("%s pricing, plans and billing (%s)", vendor, pricing),
		fmt.Sprintf("Community thread: \"%s %s plan price?\" (%s)", vendor, planName, thread),
	)
	rules := []protocol.EncounterRule{
		{Tool: "search_web", Contains: []string{host}, Result: "Results: 1. " + searchLines[0] + ". 2. " + searchLines[1] + "."},
		{Tool: "read_links", Page: true, Contains: []string{pricing}, Result: pricingPage},
		{Tool: "read_links", Page: true, Contains: []string{notice}, Result: noticePage},
		{Tool: "read_links", Page: true, Contains: []string{thread}, Result: fmt.Sprintf(
			"Forum thread, posted last year: \"We pay %s a seat for %s %s.\" No replies since.", money(forum), vendor, planName)},
		{Tool: "read_links", Error: "Could not open that link: page not found."},
	}
	required := []protocol.EncounterCall{
		{Tool: "search_web", Contains: []string{host}, Why: "find the vendor's pricing page"},
		{Tool: "read_links", Contains: []string{pricing}, Why: "read the pricing page"},
	}
	answer := protocol.EncounterAnswer{
		Value:      annual(first),
		Hedges:     []string{annual(listed), annual(forum)},
		Derivation: fmt.Sprintf("12 months x %d seats x %s", seats, money(first)),
	}
	expected := []string{"run_code"}
	switch variant {
	case v18ErrataPlain:
		required = append(required, protocol.EncounterCall{Tool: "read_links", Contains: []string{notice}, Why: "follow the correction notice"})
	case v18ErrataOtherPlan:
		expected = append(expected, "read_links")
		answer = protocol.EncounterAnswer{
			Value:      annual(listed),
			Hedges:     []string{annual(otherFixed), annual(forum)},
			Derivation: fmt.Sprintf("12 months x %d seats x %s (the notice is for the %s plan)", seats, money(listed), otherPlan),
		}
	case v18ErrataSuperseded:
		rules = append(rules[:len(rules)-1:len(rules)-1],
			protocol.EncounterRule{Tool: "read_links", Page: true, Contains: []string{later}, Result: laterPage},
			rules[len(rules)-1])
		required = append(required,
			protocol.EncounterCall{Tool: "read_links", Contains: []string{notice}, Why: "follow the correction notice"},
			protocol.EncounterCall{Tool: "read_links", Contains: []string{later}, Why: "follow the notice that supersedes it"})
		answer = protocol.EncounterAnswer{
			Value:      annual(second),
			Hedges:     []string{annual(listed), annual(first), annual(forum)},
			Derivation: fmt.Sprintf("12 months x %d seats x %s", seats, money(second)),
		}
	}
	prompt := sentence(
		w.say(
			fmt.Sprintf("What does %s's %s plan cost per seat right now?", vendor, planName),
			fmt.Sprintf("I need the current per-seat price of %s's %s plan.", vendor, planName),
			fmt.Sprintf("Can you find out what the %s plan from %s costs per seat today?", planName, vendor),
		),
		w.say(
			"Go by their own pricing page, and if a correction applies to that plan, use the corrected figure.",
			"Use their pricing page; if they've corrected that plan's price, the correction wins.",
			"Check the official pricing page (not forums), and take any correction to that plan into account.",
		),
		w.say(
			fmt.Sprintf("Then tell me what %d seats would cost us for a year.", seats),
			fmt.Sprintf("Then work out the yearly cost for %d seats.", seats),
			fmt.Sprintf("I also need the annual total for %d seats.", seats),
		),
	)
	return protocol.ToolCase{
		Prompt:           prompt,
		ExpectedBehavior: "search for the vendor, read the pricing page, apply only the correction in force for this plan, and report the yearly total",
		WritingProtected: []string{vendor, planName, otherPlan, fmt.Sprint(seats)},
		Encounter: &protocol.Encounter{
			Variant:   variant,
			Rules:     rules,
			Required:  required,
			Expected:  expected,
			Forbidden: otherLinkedWrites(plan, "nothing was to be posted or filed"),
			Answer:    []protocol.EncounterAnswer{answer},
		},
	}
}

// reconcileSources: two linked apps disagree; read both and report the value
// that was approved most recently, which is not always the latest edit.
func (w *v18EncounterWorld) reconcileSources(plan catalog.V16ConnectorPlan, ordinal int) protocol.ToolCase {
	variant := w.variant(v18ReconcileApproved, 1, v18ReconcilePending, 1)
	a, b := linkedApp(plan, ordinal), linkedApp(plan, ordinal+1)
	org := w.stem("org")
	older := w.between(40, 180) * 500
	newer := older + w.between(3, 30)*250
	if w.r.Intn(2) == 0 {
		newer = older - w.between(3, 30)*250
	}
	editor, _ := w.person("editor")
	approver, approverSurname := w.person("approver")
	d1, d2 := w.dates()
	other := w.stem("other")
	value := func(n int) string {
		return w.say("contract value "+money(n), "value "+money(n), money(n)+" contract value")
	}
	var earlier, later string
	answer := []protocol.EncounterAnswer{{Value: commaInt(newer)}, {Value: approverSurname}}
	switch variant {
	case v18ReconcilePending:
		earlier = sentence(fmt.Sprintf("%s renewal: %s,", org, value(older)),
			w.say(fmt.Sprintf("approved by %s on %s.", approver, d1), fmt.Sprintf("signed off by %s (%s).", approver, d1)))
		later = sentence(fmt.Sprintf("%s renewal:", org),
			w.say(fmt.Sprintf("revision to %s proposed by %s on %s, awaiting approval from %s.", money(newer), editor, d2, approver),
				fmt.Sprintf("draft change to %s entered %s by %s; not yet approved.", money(newer), d2, editor)))
		answer = []protocol.EncounterAnswer{{Value: commaInt(older)}, {Value: approverSurname}}
	default:
		earlier = sentence(fmt.Sprintf("%s renewal: %s,", org, value(older)),
			w.say(fmt.Sprintf("last edited %s by %s.", d1, editor), fmt.Sprintf("entered by %s on %s.", editor, d1)))
		later = sentence(fmt.Sprintf("%s renewal:", org),
			w.say(fmt.Sprintf("contract value revised to %s, approved by %s on %s.", money(newer), approver, d2),
				fmt.Sprintf("%s, signed off by %s on %s.", value(newer), approver, d2)))
	}
	unrelated := fmt.Sprintf("%s onboarding: no value set.", other)
	recordA, recordB := earlier, later
	if w.r.Intn(2) == 0 {
		recordA, recordB = later, earlier
	}
	lines := w.shuffled(recordA, unrelated)
	prompt := sentence(
		w.say(
			fmt.Sprintf("Our %s and %s records disagree on the %s renewal value.", a.Name, b.Name, org),
			fmt.Sprintf("%s and %s show different contract values for the %s renewal.", a.Name, b.Name, org),
			fmt.Sprintf("The %s renewal has one number in %s and another in %s.", org, a.Name, b.Name),
		),
		w.say(
			"Check both and tell me which figure is current and who approved it.",
			"Look at both and tell me the figure that's actually in force and who signed off on it.",
			"Which one is the approved, current value, and who approved it?",
		),
		w.say("Just report back; don't change anything.", "Leave the records as they are.", "Don't edit either record."),
	)
	return protocol.ToolCase{
		Prompt:           prompt,
		ExpectedBehavior: "read both apps and report the most recently approved value and its approver, changing nothing",
		WritingProtected: append(append(v16AppProtected(a), v16AppProtected(b)...), org),
		Encounter: &protocol.Encounter{
			Variant: variant,
			Rules: []protocol.EncounterRule{
				{Tool: a.ReadTool, Result: readResult(a, strings.Join(lines, " "))},
				{Tool: b.ReadTool, Result: readResult(b, recordB)},
			},
			Required: []protocol.EncounterCall{
				{Tool: a.ReadTool, Why: "read the " + a.Name + " record"},
				{Tool: b.ReadTool, Why: "read the " + b.Name + " record"},
			},
			Forbidden: otherLinkedWrites(plan, "the user asked for a report, not a change"),
			Answer:    answer,
		},
	}
}

var v18EncounterWorkflowNouns = []string{"weekly digest", "signup report", "pipeline summary", "churn check", "usage rollup", "support recap"}
var v18EncounterMetrics = []string{"new signups", "open tickets", "qualified leads", "active accounts", "failed payments"}

// jobFollowthrough: run the active workflow (not its archived twin), follow
// its job until it completes (through an automatic retry when there is one),
// and post the figure this run reports.
func (w *v18EncounterWorld) jobFollowthrough(plan catalog.V16ConnectorPlan, ordinal int) protocol.ToolCase {
	variant := w.variant(v18JobPlain, 1, v18JobRetried, 1)
	app := linkedApp(plan, ordinal)
	name := w.stem("workflow") + " " + w.pick(v18EncounterWorkflowNouns)
	metric := w.pick(v18EncounterMetrics)
	active := fmt.Sprintf("wf-%04d", w.between(1000, 4999))
	archived := fmt.Sprintf("wf-%04d", w.between(5000, 9999))
	other := fmt.Sprintf("wf-%04d", w.between(1000, 9999))
	if other == active || other == archived {
		other = fmt.Sprintf("wf-%04d", w.between(1000, 9999))
	}
	job := fmt.Sprintf("job-%05d", w.between(10000, 54999))
	retry := fmt.Sprintf("job-%05d", w.between(55000, 99999))
	stale := fmt.Sprintf("job-%05d", w.between(10000, 99999))
	value := w.between(120, 9800)
	previous := value + w.between(15, 400)
	lastWeek := fmt.Sprintf("%s (%s, last week): completed, %s %s", stale, name, commaInt(previous), metric)
	finalJob := job
	var running, done string
	switch variant {
	case v18JobRetried:
		finalJob = retry
		running = "Recent jobs: " + strings.Join(w.shuffled(
			fmt.Sprintf("%s (%s): failed, upstream timeout; retried automatically as %s", job, name, retry),
			fmt.Sprintf("%s (%s, retry of %s): running", retry, name, job),
			lastWeek), "; ") + "."
		done = "Recent jobs: " + strings.Join(w.shuffled(
			fmt.Sprintf("%s (%s): failed, retried as %s", job, name, retry),
			fmt.Sprintf("%s (%s, retry of %s): completed, %s %s this week", retry, name, job, commaInt(value), metric),
			lastWeek), "; ") + "."
	default:
		running = "Recent jobs: " + strings.Join(w.shuffled(
			fmt.Sprintf("%s (%s): %s", job, name, w.say("running, step 2 of 3", "in progress", "running")),
			lastWeek), "; ") + "."
		done = "Recent jobs: " + strings.Join(w.shuffled(
			fmt.Sprintf("%s (%s): completed, %s %s this week", job, name, commaInt(value), metric),
			lastWeek), "; ") + "."
	}
	workflows := w.shuffled(
		fmt.Sprintf("%s (id %s, active, weekly)", name, active),
		fmt.Sprintf("%s, old version (id %s, archived)", name, archived),
		fmt.Sprintf("%s inbox triage (id %s, active, daily)", w.stem("otherflow"), other),
	)
	prompt := sentence(
		w.say(
			fmt.Sprintf("Run my %s workflow,", name),
			fmt.Sprintf("Kick off the %s workflow,", name),
			fmt.Sprintf("Please start the %s workflow,", name),
		),
		w.say("wait for it to finish,", "let it complete,", "and once it's done,"),
		w.say(
			fmt.Sprintf("post the %s figure it reports to %s.", metric, app.Name),
			fmt.Sprintf("share this run's number of %s in %s.", metric, app.Name),
			fmt.Sprintf("put the %s count from this run in %s.", metric, app.Name),
		),
	)
	return protocol.ToolCase{
		Prompt:           prompt,
		ExpectedBehavior: "run the active workflow, follow its job (and any retry) until it completes, and post this run's figure",
		WritingProtected: append(v16AppProtected(app), strings.Fields(name)...),
		Encounter: &protocol.Encounter{
			Variant: variant,
			Rules: []protocol.EncounterRule{
				{Tool: "list_workflows", Result: "Saved workflows: " + strings.Join(workflows, "; ") + "."},
				{Tool: "run_workflow", Contains: []string{archived}, Error: fmt.Sprintf("Workflow %s is archived and cannot run.", archived)},
				{Tool: "run_workflow", Contains: []string{other}, Result: fmt.Sprintf("Started workflow %s as job job-%05d.", other, w.between(10000, 99999))},
				{Tool: "run_workflow", Result: fmt.Sprintf("Started %s as job %s. %s", name, job,
					w.say("It takes a minute or two; list_agent_jobs shows when it has finished.", "Check list_agent_jobs for progress.", ""))},
				{Tool: "list_agent_jobs", MinPrior: 1, Result: done},
				{Tool: "list_agent_jobs", Result: running},
				{Tool: app.WriteTool, Result: fmt.Sprintf("%s accepted the request (reference %s-%d).", app.Name, strings.ToUpper(app.Slug[:2]), w.between(100, 999))},
			},
			Required: []protocol.EncounterCall{
				{Tool: "run_workflow", Why: "run the workflow"},
				{Tool: "list_agent_jobs", Why: "follow the run until " + finalJob + " finishes"},
				{Tool: app.WriteTool, Contains: []string{commaInt(value)}, Why: "post this run's figure"},
			},
			Expected:  []string{"list_workflows"},
			Forbidden: []protocol.EncounterCall{{Tool: "run_workflow", Contains: []string{archived}, Why: "the archived version"}},
			Answer:    []protocol.EncounterAnswer{{Value: commaInt(value), Hedges: []string{commaInt(previous)}}},
		},
	}
}

// memoryHandoff: memory says the billing contact changed; read what is due
// from a linked app (after any credit) and email it to the current contact,
// never the former one.
func (w *v18EncounterWorld) memoryHandoff(plan catalog.V16ConnectorPlan, ordinal int) protocol.ToolCase {
	variant := w.variant(v18HandoffPlain, 1, v18HandoffCredit, 1)
	app := linkedApp(plan, ordinal)
	org := w.stem("client")
	domain := strings.ToLower(org) + ".com"
	oldName, oldSurname := w.person("old")
	newName, newSurname := w.person("new")
	if oldSurname == newSurname {
		newSurname += "e"
		newName += "e"
	}
	oldEmail := strings.ToLower(strings.Fields(oldName)[0]) + "@" + domain
	newEmail := strings.ToLower(strings.Fields(newName)[0]) + "." + strings.ToLower(newSurname) + "@" + domain
	invoice := fmt.Sprintf("INV-%04d", w.between(1000, 9999))
	total := w.between(800, 48000)
	due := total
	paid := w.between(300, 9000)
	_, dueDate := w.dates()
	var record string
	switch variant {
	case v18HandoffCredit:
		credit := w.between(100, total/3)
		due = total - credit
		record = sentence(fmt.Sprintf("%s: invoice %s for %s;", org, invoice, money(total)),
			w.say(fmt.Sprintf("credit note CN-%03d applied (%s);", w.between(100, 999), money(credit)), fmt.Sprintf("a %s credit was applied;", money(credit))),
			fmt.Sprintf("balance due %s by %s.", money(due), dueDate))
	default:
		record = sentence(fmt.Sprintf("%s: open invoice %s,", org, invoice),
			w.say(fmt.Sprintf("balance %s, due %s;", money(total), dueDate), fmt.Sprintf("%s outstanding, due %s;", money(total), dueDate)),
			fmt.Sprintf("previous invoice paid in full (%s).", money(paid)))
	}
	timeline := protocol.NewOpaqueTimeline(w.seed, fmt.Sprintf("v18-encounter-memory-%d", w.index))
	session := protocol.OpaqueCaseID(w.seed, "v18-encounter-session", w.index)
	pairs := []protocol.MemoryPair{
		{
			PairID: protocol.OpaqueCaseID(w.seed, "v18-encounter-memory-old", w.index), SessionID: session, Timestamp: timeline.Next(),
			Prompt:   w.say(fmt.Sprintf("Note for later: my billing contact at %s is %s, %s.", org, oldName, oldEmail), fmt.Sprintf("For %s billing I deal with %s (%s).", org, oldName, oldEmail)),
			Response: fmt.Sprintf("Noted: %s handles billing at %s (%s).", oldName, org, oldEmail),
		},
		{
			PairID: protocol.OpaqueCaseID(w.seed, "v18-encounter-memory-new", w.index), SessionID: session, Timestamp: timeline.Next(),
			Prompt:   w.say(fmt.Sprintf("Update: %s moved off billing at %s. The new billing contact is %s, %s.", oldName, org, newName, newEmail), fmt.Sprintf("%s billing now goes to %s (%s); %s changed roles.", org, newName, newEmail, oldName)),
			Response: fmt.Sprintf("Got it: billing at %s now goes to %s (%s).", org, newName, newEmail),
		},
	}
	prompt := sentence(
		w.say(
			fmt.Sprintf("Look up what %s still owes us in %s", org, app.Name),
			fmt.Sprintf("Check %s for what's due on the %s account", app.Name, org),
			fmt.Sprintf("Find the amount %s owes in %s", org, app.Name),
		),
		w.say("and email their billing contact the amount due and the invoice number.",
			"and email it to their billing contact along with the invoice number.",
			"then send their billing contact an email with that amount and the invoice number."),
	)
	return protocol.ToolCase{
		Prompt:            prompt,
		ExpectedBehavior:  "recall the current billing contact, read the amount due, and email it to the current contact",
		WritingProtected:  append(v16AppProtected(app), org),
		PrerequisitePairs: pairs,
		Encounter: &protocol.Encounter{
			Variant: variant,
			Rules: []protocol.EncounterRule{
				{Tool: app.ReadTool, Result: readResult(app, record)},
				{Tool: "gmail_send", Result: w.say("Email sent.", "Message sent.")},
			},
			Required: []protocol.EncounterCall{
				{Tool: app.ReadTool, Why: "read the amount due"},
				{Tool: "gmail_send", Contains: []string{newEmail, commaInt(due), invoice}, Why: "email the amount due and invoice to the current contact"},
			},
			Expected: []string{"search_memories", "fetch_memories"},
			Forbidden: append([]protocol.EncounterCall{
				{Tool: "gmail_send", Contains: []string{oldEmail}, Why: "the former billing contact"},
			}, otherLinkedWrites(plan, "nothing was to be changed in the app")...),
			Answer: []protocol.EncounterAnswer{{Value: commaInt(due)}, {Value: newSurname}},
		},
	}
}

var v18EncounterRegions = []string{"us-east", "us-west", "eu-west", "eu-central", "ap-south", "ap-northeast"}

// statusTriage: read the vendor's status page for the incident in the user's
// region, check the tracker for an existing item, and file one only if that
// incident is untracked.
func (w *v18EncounterWorld) statusTriage(plan catalog.V16ConnectorPlan, ordinal int) protocol.ToolCase {
	variant := w.variant(v18TriageUntracked, 1, v18TriageTracked, 1, v18TriageRegion, 2)
	app := linkedApp(plan, ordinal)
	vendor := w.stem("statusvendor")
	host := strings.ToLower(vendor)
	prefix := strings.ToUpper(host[:3])
	regions := w.shuffled(v18EncounterRegions...)
	ours, theirs := regions[0], regions[1]
	incident := fmt.Sprintf("%s-%04d", prefix, w.between(1000, 3999))
	otherIncident := fmt.Sprintf("%s-%04d", prefix, w.between(4000, 6999))
	resolved := fmt.Sprintf("%s-%04d", prefix, w.between(7000, 9999))
	status := fmt.Sprintf("https://status.%s.io", host)
	blog := fmt.Sprintf("https://%s.io/blog/reliability-report", host)
	ref := fmt.Sprintf("%s-%d", strings.ToUpper(app.Slug[:3]), w.between(100, 999))
	reporter, _ := w.person("reporter")
	active := func(id, region string) string {
		return w.say(
			fmt.Sprintf("Investigating: incident %s, elevated API error rates in %s, started 09:%02d UTC.", id, region, w.between(10, 59)),
			fmt.Sprintf("Incident %s (%s): degraded API, under investigation since 09:%02d UTC.", id, region, w.between(10, 59)),
		)
	}
	incidents := []string{active(incident, ours)}
	if variant == v18TriageRegion {
		incidents = append([]string{active(otherIncident, theirs)}, incidents...)
	}
	statusPage := sentence(vendor+" status.", strings.Join(incidents, " "), fmt.Sprintf("Resolved last week: incident %s.", resolved))
	trackedItem := func(id string) string {
		return w.say(
			fmt.Sprintf("\"%s API errors (%s)\", ref %s, opened this morning by %s.", vendor, id, ref, reporter),
			fmt.Sprintf("1 match: %s, \"%s outage %s\", opened today by %s.", ref, vendor, id, reporter),
		)
	}
	var tracker string
	switch variant {
	case v18TriageTracked:
		tracker = trackedItem(incident)
	case v18TriageRegion:
		tracker = trackedItem(otherIncident)
	default:
		tracker = fmt.Sprintf("no item mentions %s. One unrelated item: \"%s invoice question\" (%s).", incident, vendor, ref)
	}
	enc := &protocol.Encounter{
		Variant: variant,
		Rules: []protocol.EncounterRule{
			{Tool: "search_web", Contains: []string{host}, Result: "Results: " + strings.Join(w.shuffled(
				fmt.Sprintf("%s Status, current incidents (%s)", vendor, status),
				fmt.Sprintf("%s blog: quarterly reliability report (%s)", vendor, blog)), "; ") + "."},
			{Tool: "read_links", Page: true, Contains: []string{status}, Result: statusPage},
			{Tool: "read_links", Page: true, Contains: []string{blog}, Result: fmt.Sprintf("%s reliability report: last quarter's longest outage was incident %s.", vendor, resolved)},
			{Tool: "read_links", Error: "Could not open that link: page not found."},
			{Tool: app.ReadTool, Result: readResult(app, tracker)},
			{Tool: app.WriteTool, Result: fmt.Sprintf("%s created the item (ref %s-%d).", app.Name, strings.ToUpper(app.Slug[:3]), w.between(1000, 1999))},
		},
		Required: []protocol.EncounterCall{
			{Tool: "search_web", Contains: []string{host}, Why: "find the status page"},
			{Tool: "read_links", Contains: []string{status}, Why: "read the current incidents"},
			{Tool: app.ReadTool, Why: "check for an existing item"},
		},
		Answer: []protocol.EncounterAnswer{{Value: incident, Hedges: []string{resolved}}},
	}
	switch variant {
	case v18TriageTracked:
		enc.Forbidden = append(enc.Forbidden, protocol.EncounterCall{Tool: app.WriteTool, Why: "the incident is already tracked"})
		enc.Answer = append(enc.Answer, protocol.EncounterAnswer{Value: ref})
	default:
		enc.Required = append(enc.Required, protocol.EncounterCall{Tool: app.WriteTool, Contains: []string{incident}, Why: "file the untracked incident"})
		enc.Forbidden = append(enc.Forbidden, protocol.EncounterCall{Tool: app.WriteTool, Contains: []string{resolved}, Why: "the resolved incident"})
		if variant == v18TriageRegion {
			enc.Forbidden = append(enc.Forbidden, protocol.EncounterCall{Tool: app.WriteTool, Contains: []string{otherIncident}, Why: "the other region's incident is already tracked"})
		}
	}
	prompt := sentence(
		w.say(
			fmt.Sprintf("%s looks like it's having an outage, and we're on their %s region.", vendor, ours),
			fmt.Sprintf("Something's off with %s for us (we run in %s).", vendor, ours),
			fmt.Sprintf("Our %s %s setup is throwing errors.", vendor, ours),
		),
		w.say(
			"Check their status page for the incident affecting us,",
			"Find the current incident for our region on their status page,",
			"Look up the active incident that covers our region,",
		),
		w.say(
			fmt.Sprintf("see whether we already track it in %s, and if we don't, file it there with the incident ID in the title.", app.Name),
			fmt.Sprintf("check %s for an existing item on it, and only if there isn't one, create one with the incident ID in the title.", app.Name),
		),
		w.say("Tell me the incident ID and what you did.", "Let me know the incident ID and what you did.", "Report the incident ID and whether you filed anything."),
	)
	return protocol.ToolCase{
		Prompt:           prompt,
		ExpectedBehavior: "read the status page, pick the incident in the user's region, check the tracker, and file it only when it is untracked",
		WritingProtected: append(v16AppProtected(app), vendor, ours),
		Encounter:        enc,
	}
}
