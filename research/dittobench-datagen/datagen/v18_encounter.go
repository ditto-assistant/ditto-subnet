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

// errataChain: search, read the pricing page, follow its correction notice,
// and compute an annual total from the corrected per-seat price.
func (w *v18EncounterWorld) errataChain(plan catalog.V16ConnectorPlan) protocol.ToolCase {
	vendor := w.stem("vendor")
	host := strings.ToLower(vendor)
	plans := []string{"Team", "Growth", "Scale", "Business", "Pro"}
	planName := w.pick(plans)
	listed := w.between(18, 64)
	corrected := listed - w.between(3, 9)
	forum := listed + w.between(2, 7)
	seats := w.between(12, 95)
	total, listedTotal, forumTotal := corrected*seats*12, listed*seats*12, forum*seats*12
	notice := w.between(100, 999)
	pricing := fmt.Sprintf("https://%s.com/pricing", host)
	correction := fmt.Sprintf("https://%s.com/pricing/notice-%d", host, notice)
	thread := fmt.Sprintf("https://forum.%s.net/t/%s-pricing-%d", strings.ToLower(w.stem("forum")), host, w.between(1000, 9999))
	date, _ := w.dates()
	pricingPage := fmt.Sprintf("%s pricing. %s plan: %s per seat per month, billed annually. Starter: %s per seat per month. Notice: a correction to the %s price was published on %s, see %s.",
		vendor, planName, money(listed), money(listed/2), planName, date, correction)
	correctionPage := fmt.Sprintf("Pricing correction (%s): the %s plan is %s per seat per month, not %s as first listed. Billing has always used %s.",
		date, planName, money(corrected), money(listed), money(corrected))
	prompts := []string{
		"What does %[1]s's %[2]s plan cost per seat right now? Check their pricing page, and if it points to a correction, go by the corrected figure. Then tell me what %[3]d seats would cost us for a year.",
		"I need the current per-seat price of the %[2]s plan from %[1]s, straight from their pricing page (if there's a correction notice, use the corrected number), and the yearly cost for %[3]d seats.",
	}
	return protocol.ToolCase{
		Prompt:           fmt.Sprintf(w.pick(prompts), vendor, planName, seats),
		ExpectedBehavior: "search for the vendor, read the pricing page, follow its correction notice, and report the yearly total at the corrected price",
		WritingProtected: []string{vendor, planName, fmt.Sprint(seats)},
		Encounter: &protocol.Encounter{
			Rules: []protocol.EncounterRule{
				{Tool: "search_web", Contains: []string{host}, Result: fmt.Sprintf(
					"Results: 1. %s pricing, plans and billing (%s). 2. Community thread: \"%s %s plan price?\" (%s).", vendor, pricing, vendor, planName, thread)},
				{Tool: "read_links", Contains: []string{pricing, correction}, Result: pricingPage + "\n\n" + correctionPage},
				{Tool: "read_links", Contains: []string{correction}, Result: correctionPage},
				{Tool: "read_links", Contains: []string{thread}, Result: fmt.Sprintf(
					"Forum thread, posted last year: \"We pay %s a seat for %s %s.\" No replies since.", money(forum), vendor, planName)},
				{Tool: "read_links", Contains: []string{pricing}, Result: pricingPage},
				{Tool: "read_links", Error: "Could not open that link: page not found."},
			},
			Required: []protocol.EncounterCall{
				{Tool: "search_web", Contains: []string{host}, Why: "find the vendor's pricing page"},
				{Tool: "read_links", Contains: []string{pricing}, Why: "read the pricing page"},
				{Tool: "read_links", Contains: []string{correction}, Why: "follow the correction notice"},
			},
			Expected:  []string{"run_code"},
			Forbidden: otherLinkedWrites(plan, "nothing was to be posted or filed"),
			Answer: []protocol.EncounterAnswer{{
				Value:      commaInt(total),
				Hedges:     []string{commaInt(listedTotal), commaInt(forumTotal)},
				Derivation: fmt.Sprintf("12 months x %d seats x %s", seats, money(corrected)),
			}},
		},
	}
}

// reconcileSources: two linked apps disagree; read both and report the newer,
// approved figure without changing either.
func (w *v18EncounterWorld) reconcileSources(plan catalog.V16ConnectorPlan, ordinal int) protocol.ToolCase {
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
	prompts := []string{
		"Our %[1]s and %[2]s records disagree on the %[3]s renewal value. Check both and tell me which figure is current and who signed off on it. Just report back; don't change anything.",
		"%[1]s and %[2]s show different contract values for the %[3]s renewal. Look at both, tell me which one is the current number and who approved it, and leave the records as they are.",
	}
	return protocol.ToolCase{
		Prompt:           fmt.Sprintf(w.pick(prompts), a.Name, b.Name, org),
		ExpectedBehavior: "read both apps, report the later approved figure and its approver, and change nothing",
		WritingProtected: append(append(v16AppProtected(a), v16AppProtected(b)...), org),
		Encounter: &protocol.Encounter{
			Rules: []protocol.EncounterRule{
				{Tool: a.ReadTool, Result: readResult(a, fmt.Sprintf("%s renewal: contract value %s, last edited %s by %s. (Also: %s onboarding, no value set.)", org, money(older), d1, editor, other))},
				{Tool: b.ReadTool, Result: readResult(b, fmt.Sprintf("%s renewal: contract value revised to %s, approved by %s on %s.", org, money(newer), approver, d2))},
			},
			Required: []protocol.EncounterCall{
				{Tool: a.ReadTool, Why: "read the " + a.Name + " record"},
				{Tool: b.ReadTool, Why: "read the " + b.Name + " record"},
			},
			Forbidden: otherLinkedWrites(plan, "the user asked for a report, not a change"),
			Answer: []protocol.EncounterAnswer{
				{Value: commaInt(newer)},
				{Value: approverSurname},
			},
		},
	}
}

var v18EncounterWorkflowNouns = []string{"weekly digest", "signup report", "pipeline summary", "churn check", "usage rollup", "support recap"}
var v18EncounterMetrics = []string{"new signups", "open tickets", "qualified leads", "active accounts", "failed payments"}

// jobFollowthrough: find the active workflow (not its archived twin), run it,
// poll the job list until the run finishes, and post the figure it reports.
func (w *v18EncounterWorld) jobFollowthrough(plan catalog.V16ConnectorPlan, ordinal int) protocol.ToolCase {
	app := linkedApp(plan, ordinal)
	name := w.stem("workflow") + " " + w.pick(v18EncounterWorkflowNouns)
	metric := w.pick(v18EncounterMetrics)
	active := fmt.Sprintf("wf-%04d", w.between(1000, 4999))
	archived := fmt.Sprintf("wf-%04d", w.between(5000, 9999))
	other := fmt.Sprintf("wf-%04d", w.between(1000, 9999))
	if other == active || other == archived {
		other = fmt.Sprintf("wf-%04d", w.between(1000, 9999))
	}
	job := fmt.Sprintf("job-%05d", w.between(10000, 99999))
	stale := fmt.Sprintf("job-%05d", w.between(10000, 99999))
	value := w.between(120, 9800)
	previous := value + w.between(15, 400)
	prompts := []string{
		"Run my %[1]s workflow, wait for it to finish, and post the %[2]s figure it reports to %[3]s.",
		"Kick off the %[1]s workflow, and once it has finished, share the number of %[2]s it reports in %[3]s.",
	}
	return protocol.ToolCase{
		Prompt:           fmt.Sprintf(w.pick(prompts), name, metric, app.Name),
		ExpectedBehavior: "run the active workflow, check its job until it completes, and post the reported figure",
		WritingProtected: append(v16AppProtected(app), strings.Fields(name)...),
		Encounter: &protocol.Encounter{
			Rules: []protocol.EncounterRule{
				{Tool: "list_workflows", Result: fmt.Sprintf(
					"Saved workflows: %s (id %s, active, weekly); %s, old version (id %s, archived); %s (id %s, active, daily).",
					name, active, name, archived, w.stem("otherflow")+" inbox triage", other)},
				{Tool: "run_workflow", Contains: []string{archived}, Error: fmt.Sprintf("Workflow %s is archived and cannot run.", archived)},
				{Tool: "run_workflow", Contains: []string{other}, Result: fmt.Sprintf("Started workflow %s as job job-%05d.", other, w.between(10000, 99999))},
				{Tool: "run_workflow", Result: fmt.Sprintf("Started %s as job %s. It takes a minute or two; list_agent_jobs shows when it has finished.", name, job)},
				{Tool: "list_agent_jobs", MinPrior: 1, Result: fmt.Sprintf(
					"Recent jobs: %s (%s): completed, %s %s this week; %s (%s, last week): completed, %s %s.",
					job, name, commaInt(value), metric, stale, name, commaInt(previous), metric)},
				{Tool: "list_agent_jobs", Result: fmt.Sprintf(
					"Recent jobs: %s (%s): running, step 2 of 3; %s (%s, last week): completed, %s %s.",
					job, name, stale, name, commaInt(previous), metric)},
				{Tool: app.WriteTool, Result: fmt.Sprintf("%s accepted the request (reference %s-%d).", app.Name, strings.ToUpper(app.Slug[:2]), w.between(100, 999))},
			},
			Required: []protocol.EncounterCall{
				{Tool: "run_workflow", Why: "run the workflow"},
				{Tool: "list_agent_jobs", Why: "check the run until it finishes"},
				{Tool: app.WriteTool, Contains: []string{commaInt(value)}, Why: "post this run's figure"},
			},
			Expected:  []string{"list_workflows"},
			Forbidden: []protocol.EncounterCall{{Tool: "run_workflow", Contains: []string{archived}, Why: "the archived version"}},
			Answer:    []protocol.EncounterAnswer{{Value: commaInt(value), Hedges: []string{commaInt(previous)}}},
		},
	}
}

// memoryHandoff: memory says the billing contact changed; read the balance
// from a linked app and email it to the current contact, never the old one.
func (w *v18EncounterWorld) memoryHandoff(plan catalog.V16ConnectorPlan, ordinal int) protocol.ToolCase {
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
	balance := w.between(800, 48000)
	paid := w.between(300, 9000)
	_, due := w.dates()
	timeline := protocol.NewOpaqueTimeline(w.seed, fmt.Sprintf("v18-encounter-memory-%d", w.index))
	session := protocol.OpaqueCaseID(w.seed, "v18-encounter-session", w.index)
	pairs := []protocol.MemoryPair{
		{
			PairID: protocol.OpaqueCaseID(w.seed, "v18-encounter-memory-old", w.index), SessionID: session, Timestamp: timeline.Next(),
			Prompt:   fmt.Sprintf("Note for later: my billing contact at %s is %s, %s.", org, oldName, oldEmail),
			Response: fmt.Sprintf("Noted: %s handles billing at %s (%s).", oldName, org, oldEmail),
		},
		{
			PairID: protocol.OpaqueCaseID(w.seed, "v18-encounter-memory-new", w.index), SessionID: session, Timestamp: timeline.Next(),
			Prompt:   fmt.Sprintf("Update: %s moved off billing at %s. The new billing contact is %s, %s.", oldName, org, newName, newEmail),
			Response: fmt.Sprintf("Got it: billing at %s now goes to %s (%s).", org, newName, newEmail),
		},
	}
	prompts := []string{
		"Look up what %[1]s still owes us in %[2]s and email their billing contact the balance and the invoice number.",
		"Check %[2]s for the open balance on the %[1]s account, then email it to their billing contact with the invoice number.",
	}
	return protocol.ToolCase{
		Prompt:            fmt.Sprintf(w.pick(prompts), org, app.Name),
		ExpectedBehavior:  "recall the current billing contact, read the balance, and email it to the current contact",
		WritingProtected:  append(v16AppProtected(app), org),
		PrerequisitePairs: pairs,
		Encounter: &protocol.Encounter{
			Rules: []protocol.EncounterRule{
				{Tool: app.ReadTool, Result: readResult(app, fmt.Sprintf("%s: open invoice %s, balance %s, due %s; previous invoice paid in full (%s).", org, invoice, money(balance), due, money(paid)))},
				{Tool: "gmail_send", Result: "Email sent."},
			},
			Required: []protocol.EncounterCall{
				{Tool: app.ReadTool, Why: "read the open balance"},
				{Tool: "gmail_send", Contains: []string{newEmail, commaInt(balance), invoice}, Why: "email the balance and invoice to the current contact"},
			},
			Expected: []string{"search_memories", "fetch_memories"},
			Forbidden: append([]protocol.EncounterCall{
				{Tool: "gmail_send", Contains: []string{oldEmail}, Why: "the former billing contact"},
			}, otherLinkedWrites(plan, "nothing was to be changed in the app")...),
			Answer: []protocol.EncounterAnswer{{Value: commaInt(balance)}, {Value: newSurname}},
		},
	}
}

// statusTriage: read the vendor's status page for the current incident,
// check the tracker for an existing item, and file one only if none exists.
func (w *v18EncounterWorld) statusTriage(plan catalog.V16ConnectorPlan, ordinal int) protocol.ToolCase {
	app := linkedApp(plan, ordinal)
	vendor := w.stem("statusvendor")
	host := strings.ToLower(vendor)
	prefix := strings.ToUpper(host[:3])
	incident := fmt.Sprintf("%s-%04d", prefix, w.between(1000, 4999))
	resolved := fmt.Sprintf("%s-%04d", prefix, w.between(5000, 9999))
	status := fmt.Sprintf("https://status.%s.io", host)
	blog := fmt.Sprintf("https://%s.io/blog/reliability-report", host)
	ref := fmt.Sprintf("%s-%d", strings.ToUpper(app.Slug[:3]), w.between(100, 999))
	exists := w.r.Intn(2) == 0
	reporter, _ := w.person("reporter")
	statusPage := fmt.Sprintf("%s status. Investigating: incident %s, elevated API error rates in us-east, started 09:%02d UTC. Resolved last week: incident %s.", vendor, incident, w.between(10, 59), resolved)
	tracker := readResult(app, fmt.Sprintf("no item mentions %s. One unrelated item: \"%s invoice question\" (%s).", incident, vendor, ref))
	if exists {
		tracker = readResult(app, fmt.Sprintf("\"%s API errors (%s)\", ref %s, opened this morning by %s.", vendor, incident, ref, reporter))
	}
	prompts := []string{
		"%[1]s looks like it's having an outage. Check their status page for the current incident, see whether we already track it in %[2]s, and if we don't, file it there with the incident ID in the title. Tell me the incident ID and what you did.",
		"Something's off with %[1]s. Find the current incident on their status page, check %[2]s for an existing item on it, and only if there isn't one, create one with the incident ID in the title. Let me know the incident ID and what you did.",
	}
	enc := &protocol.Encounter{
		Rules: []protocol.EncounterRule{
			{Tool: "search_web", Contains: []string{host}, Result: fmt.Sprintf("Results: 1. %s Status, current incidents (%s). 2. %s blog: quarterly reliability report (%s).", vendor, status, vendor, blog)},
			{Tool: "read_links", Contains: []string{status}, Result: statusPage},
			{Tool: "read_links", Contains: []string{blog}, Result: fmt.Sprintf("%s reliability report: last quarter's longest outage was incident %s.", vendor, resolved)},
			{Tool: "read_links", Error: "Could not open that link: page not found."},
			{Tool: app.ReadTool, Result: tracker},
			{Tool: app.WriteTool, Result: fmt.Sprintf("%s created the item (ref %s-%d).", app.Name, strings.ToUpper(app.Slug[:3]), w.between(1000, 1999))},
		},
		Required: []protocol.EncounterCall{
			{Tool: "search_web", Contains: []string{host}, Why: "find the status page"},
			{Tool: "read_links", Contains: []string{status}, Why: "read the current incident"},
			{Tool: app.ReadTool, Why: "check for an existing item"},
		},
		Answer: []protocol.EncounterAnswer{{Value: incident, Hedges: []string{resolved}}},
	}
	if exists {
		enc.Forbidden = append(enc.Forbidden, protocol.EncounterCall{Tool: app.WriteTool, Why: "the incident is already tracked"})
		enc.Answer = append(enc.Answer, protocol.EncounterAnswer{Value: ref})
	} else {
		enc.Required = append(enc.Required, protocol.EncounterCall{Tool: app.WriteTool, Contains: []string{incident}, Why: "file the untracked incident"})
		enc.Forbidden = append(enc.Forbidden, protocol.EncounterCall{Tool: app.WriteTool, Contains: []string{resolved}, Why: "the resolved incident"})
	}
	return protocol.ToolCase{
		Prompt:           fmt.Sprintf(w.pick(prompts), vendor, app.Name),
		ExpectedBehavior: "read the status page, check the tracker, and file the incident only when it is untracked",
		WritingProtected: append(v16AppProtected(app), vendor),
		Encounter:        enc,
	}
}
