package parserprobe

import (
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// store is the world model the parser rebuilds from the public /seed stream of
// one memory graph (user_id). Nothing in it comes from the artifact's grading
// fields: every value below was read out of a pair's prompt or response.
type store struct {
	// version is the artifact bench_version; it selects the program record
	// grammar (programGrammars) and nothing else.
	version  int
	people   []*person
	projects []*project
	trips    []*trip
	stories  []*storyMem
	programs map[string]*program // by thread alias
	// programOrder lists thread aliases in first-appearance order. The v12
	// program question never names its workstream, so the only wire-visible
	// binding from a question to its group is position: the k-th program
	// question of a run belongs to the k/4-th group seeded.
	programOrder []string
	accounts     map[string]*account // by lowercase subject name
	dentist      []string            // current dentist names, newest last
	bank         []string
	invoices     map[string]int // project (lowercase) -> billed dollars
	doorCodes    []int
	prefs        map[string]string // "accent color" / "interface font" / "color mode"
	canary       string
	routes       map[string]route // project alias (lowercase) -> planning route
	accountants  []string         // pair ids of the accountant notes, in seed order
	pairs        []protocol.MemoryPair

	pendingEmails []pendingEmailFact
	pendingCorrs  []pendingCorrFact
	pendingNotes  []pendingNoteFact
}

type person struct {
	name, nickname, relation   string
	previousEmployer, employer string
	role, city, context        string
	previousEmail, email       string
	toolNotePairID             string
}

type project struct {
	alias, name, client, vendor, leadName, recordID string
	originalCents, paidCents, correctedCents        int
	hasOriginal, hasCorrection                      bool
	toolNotePairID                                  string
}

type trip struct {
	alias, purpose, when string
	countries            [3]string
	companion, relation  string
	planNick             string
	oldLegs              [3]int
	planCountries        [3]string
	hasPlan              bool
	deltaDays            int // signed
	deltaCountry         string
	hasCorrection        bool
}

// storyMem is one long story memory with the facts recoverable from its prose.
type storyMem struct {
	pairID, sessionID      string
	text                   string
	personName, nickname   string
	tripAlias              string
	caseID, purchaseOrder  string
	emails                 []string
	baseCents, paidCents   int
	hasBase, hasPaid       bool
	deltaCents             int // signed, 0 when absent
	hasDelta               bool
	costCents, creditCents int
	hasCost, hasCredit     bool
	lesson                 string
}

// program is one v12/v11 metamorphic-group scenario recovered from its records.
type program struct {
	alias                                                           string
	labels                                                          map[string]string // label -> role
	unit                                                            string
	draft, paid, approved, approved2, adjustment                    int
	hasDraft, hasPaid, hasApproved, hasLatest, hasAdjust, hasLarger bool
	decoyApproved                                                   int
	hasDecoy                                                        bool
}

type account struct {
	subject                       string
	shape                         string // plain, adjusted, superseded, capped, forgiven, referred
	approved, paid, first, second int
	draft, adjust                 int
	adjustWord                    string
	referredTo                    string
	hasRecord                     bool
}

type route struct {
	kind        string // job, create, run
	projectName string
}

var (
	reEmail   = regexp.MustCompile(`[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`)
	reMoney   = regexp.MustCompile(`\$([0-9][0-9,]*)(?:\.([0-9]{2}))?`)
	rePO      = regexp.MustCompile(`\bPO-[A-Z0-9]{8}\b`)
	reCase    = regexp.MustCompile(`\bCASE-[0-9]{4}-[A-Z0-9]{6}\b`)
	reInt     = regexp.MustCompile(`\b[0-9]+\b`)
	reQuoted  = regexp.MustCompile(`“([^”]+)”|"([^"]+)"`)
	reMinor   = regexp.MustCompile(`\b([0-9]+) (USD|CAD|EUR) cents\b`)
	reBinding = regexp.MustCompile(`\b([a-z]+)=([a-z0-9-]+)`)
)

// --- record frame banks (copied from the generator so the drift test in
// gen/parserprobe_test.go can prove they still cover every emitted record) ---

var shortLeads = []string{"FYI: ", "Tiny note — ", "Remember: ", "One thing: ", ""}

func withLeads(frames ...string) []string {
	var out []string
	for _, lead := range shortLeads {
		for _, f := range frames {
			out = append(out, lead+f)
		}
	}
	return out
}

var (
	personIdentityFrames = compileFrames(withLeads("%s is my %s. Everyone there calls them “%s.”")...)
	personWorkFrames     = compileFrames("%s used to work at %s. These days they’re the %s at %s in %s; that’s how they ended up handling the %s.")
	personEmailFrames    = compileFrames("Back when they were at %s, the work email I had saved was %s.")
	personCorrFrames     = compileFrames("After %s moved to %s, the new work email is %s.")
	personToolNoteFrames = compileFrames("Contact-maintenance receipt for %s after the %s: I finished reconciling the stale address. This receipt can be deleted once reviewed.")

	projectContextFrames  = compileFrames("When I say “%s” I mean %s for %s, not the similarly named client work. %s owns it internally; %s is the vendor, and the AP record is %s.")
	projectLedgerFrames   = compileFrames("Accounts payable record %s: the original invoice was %s; we have already paid %s against it.")
	projectCorrFrames     = compileFrames("Approval correction for AP record %s: the approved invoice total is %s, replacing the earlier %s figure. The partial payment is unchanged.")
	projectToolNoteFrames = compileFrames("Handoff scratchpad for %q at %s: add scheduling details here rather than editing the project identity or ownership record.")

	tripContextFrames = compileFrames("Our %s was the %s trip in %s — the one through %s, %s, and %s that I planned with %s, my %s.")
	tripPlanFrames    = compileFrames("When %s and I first mapped that trip out, we had %d days in %s, %d in %s, then %d in %s.")
	tripCorrFrames    = compileFrames(
		"Quick update on the trip %s and I planned: we’re adding %d days to our time in %s, but leaving the other two stays alone.",
		"Quick update on the trip %s and I planned: we’re cutting %d days from our time in %s, but leaving the other two stays alone.",
	)

	preferenceFrames = compileFrames(
		"For Ditto itself I like a %s accent, but client brand colors should never override my app preference.",
		"I spend long days in Ditto, so please keep my own interface in %s even when a client's deck uses a different typeface.",
		"My Ditto appearance should follow %s mode. That's just for my workspace; it has nothing to do with a project's brand treatment.",
	)
	canaryFrames = compileFrames("For my own attendee check-in at the %s, the registration code they assigned me is %s.")

	accountFrames = []struct {
		shape string
		frame frame
	}{
		{"plain", compileFrame("Account notes for %s: the invoice was approved at %s, and a payment of %s has cleared against it. Nothing else is on the account.")},
		{"adjusted", compileFrame("Account notes for %s: the invoice was approved at %s. A later adjustment %s the approved amount by %s. A payment of %s has since cleared.")},
		{"superseded", compileFrame("Account notes for %s: the invoice was first approved at %s. That figure was later corrected — the current approved amount is %s. A payment of %s has cleared.")},
		{"capped", compileFrame("Account notes for %s: the invoice was drafted at %s but approved at the lower figure of %s. Per this account's policy the higher of the drafted and approved amounts is what stands. A payment of %s has cleared.")},
		{"forgiven", compileFrame("Account notes for %s: the invoice was approved at %s and %s had been paid, but the remaining balance was then written off in full — the debt is fully forgiven and nothing further is owed.")},
		{"referred", compileFrame("Account notes for %s: %s's balance is billed through %s's account and always equals %s's current balance. For reference, %s's own draft was %s with %s paid on it. %s's account: approved at %s with %s cleared.")},
		{"cf", compileFrame("Account notes for %s: the invoice was approved at %s, and a payment of %s has cleared against it.")},
	}

	divergenceNegationFrames   = compileFrames("One thing to keep straight in my records: my dentist is not Dr. %s. My current dentist is Dr. %s.")
	divergenceRetractionFrames = compileFrames("Update to my finances: you used to have me down as banking with %s. As of last month I moved every account over to %s, so %s is my bank now.")
	divergenceHypoFrames       = compileFrames("Note on the %s invoice: if it had been approved it would have come to %s, but it was rejected. The amount actually billed is %s.")
	divergenceReportedFrames   = compileFrames("%s insisted the front-door code was %d, but my own note says it is %d. My note is the one to trust.")
)

// v12 record row frames. %[1]=alias %[2]=label %[3]=value %[4]=unit and the
// adjust/latest/larger forms carry their extra slots in generator order.
var (
	v12DraftFrames = compileFrames(
		"The %s first drafted for %s came to %s %s cents.",
		"For %s, an initial %s of %s %s cents was drafted.",
		"%s opened with a drafted %s of %s %s cents.",
	)
	v12PaidFrames = compileFrames(
		"Against %s, a %s of %s %s cents has already cleared.",
		"%s has settled a %s of %s %s cents.",
		"A %s of %s %s cents was paid out on %s.",
	)
	v12ApprovedFrames = compileFrames(
		"On review, the %s for %s was approved at %s %s cents.",
		"%s's %s was sanctioned at %s %s cents.",
		"The approved %s standing for %s is %s %s cents.",
	)
	v12AdjustFrames = compileFrames(opsVariant(
		"The %s for %s was approved at %s %s cents; a logged %s then %s that figure by %s %s cents before settlement.",
		"%s's %s was approved at %s %s cents, after which a recorded %s %s it by %s %s cents.",
		"For %s the %s stood at %s %s cents until a %s %s the approved amount by %s %s cents.",
	)...)
	v12LatestFrames = compileFrames(opsVariant(
		"An earlier note approved %s's %s at %s %s cents, but under this batch's %s a later revision supersedes it at %s %s cents.",
		"%s's %s was first put at %s %s cents; a subsequent %s replaces that, and the standing figure is now %s %s cents.",
		"For %s the %s once read %s %s cents, but the newest %s governs: %s %s cents.",
	)...)
	v12LargerFrames = compileFrames(opsVariant(
		"The %s for %s was approved at %s %s cents; the governing figure is whichever of the %s and the %s is larger.",
		"%s's %s came in at %s %s cents — but the amount that stands is the greater of its %s and its %s.",
		"For %s, take whichever is larger, the %s or the %s (approved at %s %s cents), as the governing figure.",
	)...)
	v12ConversationLeads = []string{
		"One more line from our custom workspace schema: ",
		"Adding a workspace record — read it with our local glossary: ",
		"Here is another entry for the batch: ",
	}
)

// glossaryRoles maps a fuzzy keyword phrase inside a glossary clause to the
// schema role it defines. The phrases are the v11/v12 glossary noun banks.
var glossaryRoles = []struct{ phrase, role string }{
	{"a workstream", "entity"},
	{"everyday alias", "alias"}, {"working shorthand", "alias"}, {"informal handle", "alias"}, {"day-to-day name", "alias"},
	{"initial drafted amount", "draft"}, {"preliminary figure", "draft"}, {"early working amount", "draft"},
	{"replaces the draft once approved", "approved"}, {"sanctioned replacement figure", "approved"}, {"supersedes the draft on approval", "approved"},
	{"settled payment", "paid"}, {"already paid out", "paid"}, {"disbursement that has cleared", "paid"},
	{"minor-unit convention", "unit"}, {"minor currency unit", "unit"}, {"every figure is quoted in", "unit"},
	{"later value supersedes", "correction"}, {"newest wins", "correction"}, {"later overrides what came before", "correction"},
	{"signed adjustment", "adjustment"}, {"post-approval correction", "adjustment"}, {"increment or decrement", "adjustment"},
}

var glossaryVerbs = map[string]bool{"names": true, "designates": true, "labels": true, "identifies": true, "marks": true,
	"is": true, "carries": true, "records": true, "states": true, "means": true, "denotes": true}

// buildStores ingests every public pair of the artifact grouped by memory graph.
func buildStores(a gen.DatasetArtifact) map[string]*store {
	stores := map[string]*store{}
	get := func(user string) *store {
		if user == "" {
			user = gen.PrimaryUser
		}
		s, ok := stores[user]
		if !ok {
			s = newStore(a.BenchVersion)
			stores[user] = s
		}
		return s
	}
	for _, tc := range a.ToolCases {
		s := get(gen.PrimaryUser)
		for _, p := range tc.PrerequisitePairs {
			s.ingest(p)
		}
	}
	for _, w := range a.MemoryWaves {
		s := get(w.UserID)
		for _, p := range w.Pairs {
			s.ingest(p)
		}
	}
	for _, s := range stores {
		s.finish()
	}
	return stores
}

func newStore(benchVersion int) *store {
	return &store{
		version:  benchVersion,
		programs: map[string]*program{}, accounts: map[string]*account{}, invoices: map[string]int{},
		prefs: map[string]string{}, routes: map[string]route{},
	}
}

// ingest parses one pair. Frame banks are tried in a fixed order; the first
// matching frame classifies the record.
func (s *store) ingest(p protocol.MemoryPair) {
	s.pairs = append(s.pairs, p)
	prompt := strings.TrimSpace(p.Prompt)
	if len(prompt) > 1500 {
		s.ingestStory(p)
		return
	}
	if _, slots, ok := matchAny(personIdentityFrames, prompt); ok {
		pe := s.upsertPerson(slots[0])
		pe.nickname, pe.relation = slots[2], slots[1]
		return
	}
	if _, slots, ok := matchAny(personWorkFrames, prompt); ok {
		pe := s.upsertPerson(slots[0])
		pe.previousEmployer, pe.role, pe.employer, pe.city, pe.context = slots[1], slots[2], slots[3], slots[4], slots[5]
		return
	}
	if _, slots, ok := matchAny(personEmailFrames, prompt); ok {
		s.pendingEmail(slots[0], slots[1])
		return
	}
	if _, slots, ok := matchAny(personCorrFrames, prompt); ok {
		s.pendingCorrection(slots[0], slots[1], slots[2])
		return
	}
	if _, slots, ok := matchAny(personToolNoteFrames, prompt); ok {
		s.pendingToolNote(slots[0], slots[1], p.PairID)
		return
	}
	if _, slots, ok := matchAny(projectContextFrames, prompt); ok {
		pr := s.upsertProject(slots[0])
		pr.name, pr.client, pr.leadName, pr.vendor, pr.recordID = slots[1], slots[2], slots[3], slots[4], slots[5]
		return
	}
	if _, slots, ok := matchAny(projectLedgerFrames, prompt); ok {
		pr := s.projectByRecord(slots[0])
		pr.originalCents, pr.paidCents, pr.hasOriginal = moneyCents(slots[1]), moneyCents(slots[2]), true
		return
	}
	if _, slots, ok := matchAny(projectCorrFrames, prompt); ok {
		pr := s.projectByRecord(slots[0])
		pr.correctedCents, pr.hasCorrection = moneyCents(slots[1]), true
		return
	}
	if _, slots, ok := matchAny(projectToolNoteFrames, prompt); ok {
		s.upsertProject(slots[0]).toolNotePairID = p.PairID
		return
	}
	if _, slots, ok := matchAny(tripContextFrames, prompt); ok {
		t := s.upsertTrip(slots[0])
		t.purpose, t.when = slots[1], slots[2]
		t.countries = [3]string{slots[3], slots[4], slots[5]}
		t.companion, t.relation = slots[6], slots[7]
		return
	}
	if _, slots, ok := matchAny(tripPlanFrames, prompt); ok {
		t := &trip{planNick: slots[0], hasPlan: true}
		t.oldLegs = [3]int{atoi(slots[1]), atoi(slots[3]), atoi(slots[5])}
		t.planCountries = [3]string{slots[2], slots[4], slots[6]}
		s.trips = append(s.trips, t)
		return
	}
	if i, slots, ok := matchAny(tripCorrFrames, prompt); ok {
		delta := atoi(slots[1])
		if i == 1 {
			delta = -delta
		}
		s.trips = append(s.trips, &trip{planNick: slots[0], deltaDays: delta, deltaCountry: slots[2], hasCorrection: true})
		return
	}
	if i, slots, ok := matchAny(preferenceFrames, prompt); ok {
		s.prefs[[]string{"accent color", "interface font", "color mode"}[i]] = slots[0]
		return
	}
	if _, slots, ok := matchAny(canaryFrames, prompt); ok {
		s.canary = slots[1]
		return
	}
	if s.ingestAccount(prompt) || s.ingestDivergence(prompt) || s.ingestProgram(p) || s.ingestRoute(p) {
		return
	}
	if containsPhraseFuzzy(prompt, "handled my 2024 taxes") {
		s.accountants = append(s.accountants, p.PairID)
	}
}

// pending person facts are keyed by employer/nickname and resolved in finish.
type pendingEmailFact struct{ employer, email string }
type pendingCorrFact struct{ nickname, employer, email string }
type pendingNoteFact struct{ nickname, context, pairID string }

func (s *store) pendingEmail(employer, email string) {
	s.pendingEmails = append(s.pendingEmails, pendingEmailFact{employer, email})
}
func (s *store) pendingCorrection(nick, employer, email string) {
	s.pendingCorrs = append(s.pendingCorrs, pendingCorrFact{nick, employer, email})
}
func (s *store) pendingToolNote(nick, context, pairID string) {
	s.pendingNotes = append(s.pendingNotes, pendingNoteFact{nick, context, pairID})
}

func (s *store) upsertPerson(name string) *person {
	for _, pe := range s.people {
		if strings.EqualFold(pe.name, name) {
			return pe
		}
	}
	pe := &person{name: name}
	s.people = append(s.people, pe)
	return pe
}

func (s *store) upsertProject(alias string) *project {
	for _, pr := range s.projects {
		if strings.EqualFold(pr.alias, alias) {
			return pr
		}
	}
	pr := &project{alias: alias}
	s.projects = append(s.projects, pr)
	return pr
}

func (s *store) projectByRecord(recordID string) *project {
	for _, pr := range s.projects {
		if pr.recordID == recordID {
			return pr
		}
	}
	pr := &project{recordID: recordID}
	s.projects = append(s.projects, pr)
	return pr
}

func (s *store) upsertTrip(alias string) *trip {
	for _, t := range s.trips {
		if strings.EqualFold(t.alias, alias) {
			return t
		}
	}
	t := &trip{alias: alias}
	s.trips = append(s.trips, t)
	return t
}

// finish joins the pending person facts (old email by prior employer, new
// email by nickname+employer, tool note by nickname) and merges the trip plan
// and correction rows onto their context row by companion nickname.
func (s *store) finish() {
	// The context row names the alias and the AP record id; the ledger and
	// correction rows carry only the record id and can arrive first (the seed
	// stream is permuted), so merge entries that share a record id.
	byRecord := map[string]*project{}
	merged := make([]*project, 0, len(s.projects))
	for _, pr := range s.projects {
		if pr.recordID == "" {
			merged = append(merged, pr)
			continue
		}
		if prior, ok := byRecord[pr.recordID]; ok {
			if pr.alias != "" {
				prior.alias, prior.name, prior.client, prior.vendor, prior.leadName = pr.alias, pr.name, pr.client, pr.vendor, pr.leadName
			}
			if pr.hasOriginal {
				prior.originalCents, prior.paidCents, prior.hasOriginal = pr.originalCents, pr.paidCents, true
			}
			if pr.hasCorrection {
				prior.correctedCents, prior.hasCorrection = pr.correctedCents, true
			}
			if pr.toolNotePairID != "" {
				prior.toolNotePairID = pr.toolNotePairID
			}
			continue
		}
		byRecord[pr.recordID] = pr
		merged = append(merged, pr)
	}
	s.projects = merged
	for _, f := range s.pendingEmails {
		for _, pe := range s.people {
			if strings.EqualFold(pe.previousEmployer, f.employer) && pe.previousEmail == "" {
				pe.previousEmail = f.email
				break
			}
		}
	}
	// Nicknames are not on every graph's protected list (the isolation wave
	// protects name, employer, and event only), so a correction row can carry a
	// one-edit nickname; the employer join disambiguates, so match fuzzily.
	// Exact nickname joins run before fuzzy ones so "Lindy" and "Indy" (one
	// edit apart, both real nicknames) bind to their own rows.
	for _, exact := range []bool{true, false} {
		for _, f := range s.pendingCorrs {
			for _, pe := range s.people {
				if pe.email != "" || !strings.EqualFold(pe.employer, f.employer) || !nickMatch(pe.nickname, f.nickname, exact) {
					continue
				}
				pe.email = f.email
				break
			}
		}
		for _, f := range s.pendingNotes {
			for _, pe := range s.people {
				if pe.toolNotePairID != "" || !strings.EqualFold(pe.context, f.context) || !nickMatch(pe.nickname, f.nickname, exact) {
					continue
				}
				pe.toolNotePairID = f.pairID
				break
			}
		}
	}
	s.pendingEmails, s.pendingCorrs, s.pendingNotes = nil, nil, nil
	// Merge plan/correction rows into context rows. A companion can plan more
	// than one trip (Companion = (2i+1) mod people), so rows are matched by
	// nickname AND by the country set, which the plan repeats verbatim.
	var contexts, plans, corrs []*trip
	for _, t := range s.trips {
		switch {
		case t.alias != "":
			contexts = append(contexts, t)
		case t.hasPlan:
			plans = append(plans, t)
		case t.hasCorrection:
			corrs = append(corrs, t)
		}
	}
	usedPlan := map[*trip]bool{}
	usedCorr := map[*trip]bool{}
	for _, c := range contexts {
		for _, pl := range plans {
			if usedPlan[pl] || !strings.EqualFold(pl.planNick, c.companion) || !sameCountries(pl.planCountries, c.countries) {
				continue
			}
			c.oldLegs, c.hasPlan, usedPlan[pl] = pl.oldLegs, true, true
			break
		}
		for _, co := range corrs {
			if usedCorr[co] || !strings.EqualFold(co.planNick, c.companion) || !hasCountry(c.countries, co.deltaCountry) {
				continue
			}
			c.deltaDays, c.deltaCountry, c.hasCorrection, usedCorr[co] = co.deltaDays, co.deltaCountry, true, true
			break
		}
	}
	s.trips = contexts
	sort.Slice(s.stories, func(i, j int) bool { return s.stories[i].sessionID < s.stories[j].sessionID })
}

func nickMatch(a, b string, exact bool) bool {
	if exact {
		return strings.EqualFold(a, b)
	}
	return fuzzyName(a, b)
}

// fuzzyName compares two names word by word with the projector's edit budget.
func fuzzyName(a, b string) bool {
	aw, bw := strings.Fields(strings.ToLower(a)), strings.Fields(strings.ToLower(b))
	if len(aw) != len(bw) || len(aw) == 0 {
		return false
	}
	for i := range aw {
		if !fuzzyWord(aw[i], bw[i]) {
			return false
		}
	}
	return true
}

func sameCountries(a, b [3]string) bool {
	for i := range a {
		if !strings.EqualFold(a[i], b[i]) {
			return false
		}
	}
	return true
}

func hasCountry(cs [3]string, c string) bool {
	for _, x := range cs {
		if strings.EqualFold(x, c) || strings.EqualFold(strings.TrimPrefix(x, "the "), strings.TrimPrefix(c, "the ")) {
			return true
		}
	}
	return false
}

func (s *store) ingestAccount(prompt string) bool {
	for _, af := range accountFrames {
		slots, ok := af.frame.match(prompt)
		if !ok {
			continue
		}
		ac := &account{subject: slots[0], shape: af.shape, hasRecord: true}
		switch af.shape {
		case "plain", "cf":
			ac.approved, ac.paid = dollars(slots[1]), dollars(slots[2])
			if af.shape == "cf" {
				ac.shape = "plain"
			}
		case "adjusted":
			ac.approved, ac.adjustWord, ac.adjust, ac.paid = dollars(slots[1]), slots[2], dollars(slots[3]), dollars(slots[4])
		case "superseded":
			ac.first, ac.second, ac.paid = dollars(slots[1]), dollars(slots[2]), dollars(slots[3])
		case "capped":
			ac.draft, ac.approved, ac.paid = dollars(slots[1]), dollars(slots[2]), dollars(slots[3])
		case "forgiven":
			ac.approved, ac.paid = dollars(slots[1]), dollars(slots[2])
		case "referred":
			// %s: %s's balance is billed through %s's account ... %s's own draft was
			// %s with %s paid on it. %s's account: approved at %s with %s cleared.
			ac.referredTo = slots[2]
			ac.draft, ac.paid = dollars(slots[5]), dollars(slots[6])
			ac.approved = dollars(slots[8])
			ac.second = dollars(slots[9]) // the referenced account's cleared payment
		}
		s.accounts[strings.ToLower(ac.subject)] = ac
		return true
	}
	return false
}

func (s *store) ingestDivergence(prompt string) bool {
	if _, slots, ok := matchAny(divergenceNegationFrames, prompt); ok {
		s.dentist = append(s.dentist, slots[1])
		return true
	}
	if _, slots, ok := matchAny(divergenceRetractionFrames, prompt); ok {
		s.bank = append(s.bank, slots[1])
		return true
	}
	if _, slots, ok := matchAny(divergenceHypoFrames, prompt); ok {
		s.invoices[strings.ToLower(slots[0])] = dollars(slots[2])
		return true
	}
	if _, slots, ok := matchAny(divergenceReportedFrames, prompt); ok {
		s.doorCodes = append(s.doorCodes, atoi(slots[2]))
		return true
	}
	return false
}

// ingestRoute reads a v10+ planning note (a tool prerequisite pair): the
// prompt names the project alias, the response the approved route.
func (s *store) ingestRoute(p protocol.MemoryPair) bool {
	alias := ""
	if m := reQuoted.FindStringSubmatch(p.Prompt); m != nil {
		alias = m[1] + m[2]
	}
	if alias == "" {
		return false
	}
	resp := strings.TrimSpace(p.Response)
	// The assistant voice pass appends a tail to the planning reply, so the
	// route is read from its load-bearing clause rather than a whole-line frame.
	switch {
	case containsPhraseFuzzy(resp, "one-off Ditto Code job"):
		s.routes[strings.ToLower(alias)] = route{kind: "job"}
		return true
	case containsPhraseFuzzy(resp, "reusable workflow named"):
		if m := reQuoted.FindStringSubmatch(resp); m != nil {
			s.routes[strings.ToLower(alias)] = route{kind: "create", projectName: m[1] + m[2]}
			return true
		}
	case containsPhraseFuzzy(resp, "existing workflow named"):
		if m := reQuoted.FindStringSubmatch(resp); m != nil {
			s.routes[strings.ToLower(alias)] = route{kind: "run", projectName: m[1] + m[2]}
			return true
		}
	}
	return false
}

// ingestProgram reads one program record. Key=value contracts (v10/v11) take
// their own path; the v12 prose path follows. The renderer wrapper is stripped
// first; the thread alias in the acknowledgement groups records into a scenario.
func (s *store) ingestProgram(p protocol.MemoryPair) bool {
	if g, ok := programGrammarFor(s.version); ok && g.records == programRecordsKeyValue {
		return s.ingestKeyValueProgram(p)
	}
	resp := p.Response
	i := strings.LastIndex(resp, "(thread: ")
	if i < 0 || !strings.HasSuffix(strings.TrimSpace(resp), ")") {
		return false
	}
	alias := strings.TrimSuffix(strings.TrimSpace(resp[i+len("(thread: "):]), ")")
	row := stripV12Renderer(p.Prompt)
	pg, ok := s.programs[alias]
	if !ok {
		pg = &program{alias: alias, labels: map[string]string{}}
		s.programs[alias] = pg
		s.programOrder = append(s.programOrder, alias)
	}
	if m := reBinding.FindAllStringSubmatch(row, -1); len(m) >= 2 && strings.Contains(row, "=") {
		for _, b := range m {
			if b[2] == alias {
				pg.labels[b[1]] = firstNonEmpty(pg.labels[b[1]], "entity-or-alias")
			}
		}
		if strings.Contains(row, "approved figure of") {
			if n := reInt.FindAllString(row[strings.Index(row, "approved figure of"):], 1); len(n) == 1 {
				pg.decoyApproved, pg.hasDecoy = atoi(n[0]), true
			}
		}
		return true
	}
	if _, slots, ok := matchAny(v12AdjustFrames, row); ok {
		// alias/label order differs per form; values are the two "N unit cents".
		vals := reMinor.FindAllStringSubmatch(row, -1)
		if len(vals) == 2 {
			pg.approved, pg.adjustment, pg.unit = atoi(vals[0][1]), atoi(vals[1][1]), vals[0][2]
			if containsFuzzy(row, "lowers") || containsFuzzy(row, "lowered") {
				pg.adjustment = -pg.adjustment
			}
			pg.hasApproved, pg.hasAdjust = true, true
		}
		_ = slots
		return true
	}
	if _, _, ok := matchAny(v12LatestFrames, row); ok {
		vals := reMinor.FindAllStringSubmatch(row, -1)
		if len(vals) == 2 {
			pg.approved, pg.approved2, pg.unit = atoi(vals[0][1]), atoi(vals[1][1]), vals[0][2]
			pg.hasApproved, pg.hasLatest = true, true
		}
		return true
	}
	if _, _, ok := matchAny(v12LargerFrames, row); ok {
		vals := reMinor.FindAllStringSubmatch(row, -1)
		if len(vals) == 1 {
			pg.approved, pg.unit = atoi(vals[0][1]), vals[0][2]
			pg.hasApproved, pg.hasLarger = true, true
		}
		return true
	}
	if _, _, ok := matchAny(v12ApprovedFrames, row); ok {
		vals := reMinor.FindAllStringSubmatch(row, -1)
		if len(vals) == 1 {
			pg.approved, pg.unit, pg.hasApproved = atoi(vals[0][1]), vals[0][2], true
		}
		return true
	}
	if _, _, ok := matchAny(v12DraftFrames, row); ok {
		vals := reMinor.FindAllStringSubmatch(row, -1)
		if len(vals) == 1 {
			pg.draft, pg.unit, pg.hasDraft = atoi(vals[0][1]), vals[0][2], true
		}
		return true
	}
	if _, _, ok := matchAny(v12PaidFrames, row); ok {
		vals := reMinor.FindAllStringSubmatch(row, -1)
		if len(vals) == 1 {
			pg.paid, pg.unit, pg.hasPaid = atoi(vals[0][1]), vals[0][2], true
		}
		return true
	}
	// Glossary line: "<label> <verb> <noun phrase>" clauses.
	if s.ingestGlossary(pg, row) {
		return true
	}
	return true
}

// stripV12Renderer removes the renderer wrapper (conversation lead, email
// header, table cell, operations dump) around one v12 record row. Wrapper
// words are user prose the projector may touch, so they are matched fuzzily;
// the operations renderer also rewrites "; " to ", " inside the row, which is
// undone here so one frame bank covers every renderer.
func stripV12Renderer(prompt string) string {
	row := strings.TrimSpace(prompt)
	for _, lead := range v12ConversationLeads {
		if len(row) > len(lead) && fuzzyPrefix(row, lead) {
			return strings.TrimSpace(row[len(lead):])
		}
	}
	if fuzzyPrefix(row, "Subject: reconciliation note") {
		if i := strings.Index(row, "\n\n"); i >= 0 {
			return strings.TrimSpace(row[i+2:])
		}
	}
	if fuzzyPrefix(row, "Pasted table row") {
		if i := strings.LastIndex(row, "| "); i >= 0 {
			return strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(row[i+2:]), "|"))
		}
	}
	if i := strings.Index(row, "] { "); strings.Contains(strings.SplitN(row, "[", 2)[0], "_dump") || (i >= 0 && i < 24) {
		if j := strings.Index(row, "{ "); j >= 0 {
			inner := strings.TrimSuffix(strings.TrimSpace(row[j+2:]), "}")
			return strings.TrimSpace(inner)
		}
	}
	return row
}

// opsVariant is the operations-dump spelling of a record frame: the renderer
// rewrites every "; " to ", ".
func opsVariant(frames ...string) []string {
	out := append([]string(nil), frames...)
	for _, f := range frames {
		if strings.Contains(f, "; ") {
			out = append(out, strings.ReplaceAll(f, "; ", ", "))
		}
	}
	return out
}

// fuzzyPrefix reports whether text starts with lead allowing the typo budget
// on each lead word (the lead is user prose the projector may touch).
func fuzzyPrefix(text, lead string) bool {
	lw := strings.Fields(lead)
	tw := strings.Fields(text)
	if len(tw) < len(lw) {
		return false
	}
	for i, w := range lw {
		if !fuzzyWord(strings.ToLower(w), strings.ToLower(tw[i])) {
			return false
		}
	}
	return true
}

func (s *store) ingestGlossary(pg *program, row string) bool {
	words := strings.Fields(row)
	found := false
	for i := 0; i+1 < len(words); i++ {
		verb := strings.ToLower(strings.Trim(words[i+1], ",.;"))
		if !glossaryVerbs[verb] {
			continue
		}
		label := strings.ToLower(strings.Trim(words[i], ",.;:"))
		if len(label) < 5 || strings.ContainsAny(label, "'’") || !isLabelLike(label) {
			continue
		}
		// The clause runs until the next label+verb pair or the end.
		end := len(words)
		for j := i + 2; j+1 < len(words); j++ {
			if glossaryVerbs[strings.ToLower(strings.Trim(words[j+1], ",.;"))] && isLabelLike(strings.ToLower(strings.Trim(words[j], ",.;:"))) && len(strings.Trim(words[j], ",.;:")) >= 5 {
				end = j
				break
			}
		}
		clause := strings.Join(words[i+2:end], " ")
		for _, gr := range glossaryRoles {
			if containsPhraseFuzzy(clause, gr.phrase) {
				pg.labels[label] = gr.role
				found = true
				break
			}
		}
	}
	return found
}

// isLabelLike reports whether a token looks like a coined v10/v11/v12 schema
// label: lowercase letters only, no English function word.
func isLabelLike(tok string) bool {
	if tok == "" {
		return false
	}
	for _, r := range tok {
		if r < 'a' || r > 'z' {
			return false
		}
	}
	switch tok {
	case "batch", "workspace", "figure", "value", "amount", "records", "entries", "which", "later", "earlier", "there", "these", "those", "whatever", "before", "after":
		return false
	}
	return true
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

// ingestStory extracts the anchored facts of one long story memory.
func (s *store) ingestStory(p protocol.MemoryPair) {
	st := &storyMem{pairID: p.PairID, sessionID: p.SessionID, text: p.Prompt}
	text := p.Prompt
	if m := reCase.FindString(text); m != "" {
		st.caseID = m
	}
	if m := rePO.FindString(text); m != "" {
		st.purchaseOrder = m
	}
	st.emails = reEmail.FindAllString(text, -1)
	for _, sent := range sentences(text) {
		lower := strings.ToLower(sent)
		amounts := reMoney.FindAllStringSubmatch(sent, -1)
		if len(amounts) == 1 {
			cents := moneyCents(amounts[0][0])
			switch {
			case strings.Contains(lower, " by $") && (containsFuzzy(lower, "increased") || containsFuzzy(lower, "reduced") || containsFuzzy(lower, "delta") || containsFuzzy(lower, "direction")):
				st.deltaCents, st.hasDelta = cents, true
				if containsFuzzy(lower, "reduced") {
					st.deltaCents = -cents
				}
			case containsFuzzy(lower, "payment") || containsFuzzy(lower, "paid"):
				st.paidCents, st.hasPaid = cents, true
			case containsFuzzy(lower, "credit"):
				st.creditCents, st.hasCredit = cents, true
			case containsFuzzy(lower, "charge") || containsFuzzy(lower, "cost") || containsFuzzy(lower, "expense"):
				st.costCents, st.hasCost = cents, true
			case containsFuzzy(lower, "approved") || containsFuzzy(lower, "budget") || containsFuzzy(lower, "settled"):
				st.baseCents, st.hasBase = cents, true
			}
		}
		if st.lesson == "" {
			st.lesson = lessonFromSentence(sent)
		}
	}
	// Origin anchors: "I ran into <Name>, my <relation>, during the <context>. We
	// had not properly caught up since the <trip alias>, ..." and "Everyone
	// calls them <Nick>." Lead words are user prose, so they are matched fuzzily.
	if i := indexFuzzyPhrase(text, "I ran into"); i >= 0 && i < 20 {
		head := text[i:]
		if j := strings.Index(head, ","); j > 0 {
			st.personName = head[:j]
		}
	}
	if i := indexFuzzyPhrase(text, "caught up since the"); i >= 0 {
		rest := text[i:]
		if j := strings.IndexAny(rest, ",."); j > 0 {
			st.tripAlias = rest[:j]
		}
	}
	for _, sent := range sentences(text) {
		if i := indexFuzzyPhrase(sent, "Everyone calls them"); i == len("Everyone calls them")+1 {
			nick := sent[i:]
			if j := strings.Index(nick, "."); j > 0 {
				st.nickname = nick[:j]
			}
		}
	}
	s.stories = append(s.stories, st)
}

// lessonFromSentence recovers the lesson value from its three renderings.
func lessonFromSentence(sent string) string {
	if strings.Contains(sent, "“") && (containsPhraseFuzzy(sent, "advice that stuck with me") || containsFuzzy(sent, "wrapped")) {
		if m := reQuoted.FindStringSubmatch(sent); m != nil {
			return strings.TrimSuffix(m[1]+m[2], ".")
		}
	}
	for _, lead := range []string{"but the takeaway was useful: ", "one practical rule for next time: "} {
		if i := indexFuzzyPhrase(sent, lead); i >= 0 {
			return strings.TrimSuffix(strings.TrimSpace(sent[i:]), ".")
		}
	}
	return ""
}

// indexFuzzyPhrase returns the byte offset just after phrase in sent, matching
// the phrase words fuzzily; -1 when absent.
func indexFuzzyPhrase(sent, phrase string) int {
	pw := strings.Fields(strings.ToLower(phrase))
	words := strings.Fields(sent)
	if len(words) < len(pw) {
		return -1
	}
	offsets := make([]int, len(words))
	pos := 0
	for i, w := range words {
		idx := strings.Index(sent[pos:], w)
		offsets[i] = pos + idx
		pos = offsets[i] + len(w)
	}
outer:
	for i := 0; i+len(pw) <= len(words); i++ {
		for k, p := range pw {
			_, core, suf := splitPunct(strings.ToLower(words[i+k]))
			_, pcore, psuf := splitPunct(p)
			if !fuzzyWord(pcore, core) || suf != psuf {
				continue outer
			}
		}
		last := i + len(pw) - 1
		return offsets[last] + len(words[last]) + 1
	}
	return -1
}

func moneyCents(s string) int {
	m := reMoney.FindStringSubmatch(s)
	if m == nil {
		return 0
	}
	whole := atoi(strings.ReplaceAll(m[1], ",", ""))
	frac := 0
	if m[2] != "" {
		frac = atoi(m[2])
	}
	return whole*100 + frac
}

func dollars(s string) int { return moneyCents(s) / 100 }

func atoi(s string) int {
	n, _ := strconv.Atoi(strings.ReplaceAll(strings.TrimSpace(s), ",", ""))
	return n
}
