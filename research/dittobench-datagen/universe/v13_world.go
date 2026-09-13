package universe

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/internal/humandata"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 world extensions.
//
// v13 keeps the v8 world byte-identical and APPENDS to it: a small set of
// absence-probe records (planted evidence the grounded-abstention families
// search), and a staging plan that moves a bounded share of ordinary
// corrections out of the initial seed into later /seed waves. Both are reached
// only through GenerateForVersion with bench_version >= 13; Generate (v8-v12)
// never sees them, so every earlier contract regenerates the same bytes.

// V13Probes are the planted absence-probe records of a v13 world. Every value
// is deterministic in the seed and appended AFTER the v8 pairs, so v8 pair
// identities, timestamps, and order are unchanged.
type V13Probes struct {
	NearMiss []V13NearMissProbe
	Handles  []V13HandleProbe
	Threads  []V13ThreadProbe
	Absent   []V13AbsentProbe
	Pairs    []protocol.MemoryPair
}

// V13AbsentProbe is the pure-absence family's subject: a coined name that
// appears in NO record, asked about at a real person's employer and event so
// the request reads exactly like the family's answerable twin. It plants no
// pair; its only invariant is the absence validateUnanswerablePlan proves.
type V13AbsentProbe struct {
	Person int
	Name   string
}

// V13NearMissProbe mentions a coined colleague of a real person in an unrelated
// context. The colleague's address is never stated; the real person's is the
// tempting sibling value.
type V13NearMissProbe struct {
	Person        int
	Name          string
	MentionPairID string
}

// V13HandleProbe plants a messaging handle for one person. Removed marks the
// stale/removed family: a later record withdraws the handle, so only superseded
// evidence supports the tempting value.
type V13HandleProbe struct {
	Person        int
	Handle        string
	HandlePairID  string
	RemovalPairID string
	Removed       bool
}

// V13ThreadProbe is an insufficient-composition thread: the approved invoice
// total is on record and a partial payment is acknowledged, but the payment
// amount is never stated, so the outstanding balance cannot be derived.
type V13ThreadProbe struct {
	Alias          string
	Vendor         string
	InvoiceID      string
	ApprovedCents  int
	ApprovalPairID string
	PaymentPairID  string
}

// appendV13Probes adds absence records after versioned world construction.
func appendV13Probes(w *World) {
	probes := buildV13Probes(w.Seed, w)
	w.Probes = &probes
	w.Pairs = append(w.Pairs, probes.Pairs...)
}

// V13AbsenceFamilies are the six absence-proof families of the v13 grounded
// abstention slice, in generation order. Pure absence is the only family whose
// evidence is not misleading; every other family plants a tempting value.
const (
	V13FamilyPureAbsence  = "pure-absence"
	V13FamilyNearMiss     = "near-miss"
	V13FamilyStaleRemoved = "stale-removed"
	V13FamilyFalsePremise = "false-premise"
	V13FamilyCrossUser    = "cross-user"
	V13FamilyInsufficient = "insufficient-composition"
	v13ProbeHandleNoun    = "Signal handle"
)

// V13AbsenceFamilies lists every family in generation order.
var V13AbsenceFamilies = []string{
	V13FamilyPureAbsence, V13FamilyNearMiss, V13FamilyStaleRemoved,
	V13FamilyFalsePremise, V13FamilyCrossUser, V13FamilyInsufficient,
}

// V13AbsenceFamilyCounts is the fixed per-family case budget for a world scale.
// The mix is seed-independent: misleading-evidence families carry at least half
// of the slice and pure absence at most a quarter (docs/bench-versions.md).
func V13AbsenceFamilyCounts(scale int) map[string]int {
	switch {
	case scale >= 3:
		return map[string]int{
			V13FamilyPureAbsence: 5, V13FamilyNearMiss: 5, V13FamilyStaleRemoved: 4,
			V13FamilyFalsePremise: 4, V13FamilyCrossUser: 3, V13FamilyInsufficient: 4,
		}
	case scale == 2:
		return map[string]int{
			V13FamilyPureAbsence: 2, V13FamilyNearMiss: 2, V13FamilyStaleRemoved: 1,
			V13FamilyFalsePremise: 1, V13FamilyCrossUser: 1, V13FamilyInsufficient: 1,
		}
	default:
		return map[string]int{}
	}
}

// V13AbsenceCaseCount is the total unanswerable case count for a scale.
func V13AbsenceCaseCount(scale int) int {
	total := 0
	for _, n := range V13AbsenceFamilyCounts(scale) {
		total += n
	}
	return total
}

// V13AsOfPairCounts is the number of as_of_twin pairs per correction chain kind
// (people, projects, trips) for a world scale.
func V13AsOfPairCounts(scale int) (people, projects, trips int) {
	switch {
	case scale >= 3:
		return 2, 2, 2
	case scale == 2:
		return 1, 1, 0
	default:
		return 0, 0, 0
	}
}

// V13StagedTripCount is how many trip corrections leave the initial seed and
// arrive in later waves: roughly a tenth of the world's ordinary corrections,
// drawn from trips because no v8 tool case depends on a trip record.
func V13StagedTripCount(scale int) int {
	switch {
	case scale >= 3:
		return 6
	case scale == 2:
		return 3
	default:
		return 0
	}
}

// Scale reports the world scale (1..3) from the people count Generate drew.
func (w World) Scale() int {
	switch {
	case len(w.People) >= 28:
		return 3
	case len(w.People) >= 14:
		return 2
	default:
		return 1
	}
}

// V13Allocation is the deterministic, non-overlapping assignment of world
// entities to the v13 families. Ranges are taken from the top of each entity
// list so they never collide with the isolation anchors (people 0..isoCases-1)
// except where the cross-user family deliberately reuses an even anchor whose
// primary-graph contact question the isolation suite does not ask.
type V13Allocation struct {
	PurePeople           []int
	NearPeople           []int
	StalePairs           [][2]int // [removed person, kept person]
	CrossPeople          []int
	FalseTrips           []int
	InsufficientProjects []int
	AsOfPeople           []int
	AsOfProjects         []int
	AsOfTrips            []int
	StagedTrips          []int
	// Spare* are reserved fallback entities for the answerable decision twins.
	// A twin first walks every surface variant of its own entity; only when each
	// variant trips the accidental lexical-shortcut exclusion does it move to the
	// next unused spare of the same kind (V13DecisionPairs). Spares are excluded
	// from the ordinary pool like the family entities, so a twin drawn from one
	// never duplicates an ordinary program's fact.
	SparePeople   []int
	SpareProjects []int
	SpareTrips    []int
}

// V13SpareCounts is the number of fallback entities reserved per kind for a
// world scale (see V13Allocation.SparePeople).
func V13SpareCounts(scale int) (people, projects, trips int) {
	switch {
	case scale >= 3:
		return 2, 1, 1
	case scale == 2:
		return 1, 1, 1
	default:
		return 0, 0, 0
	}
}

// V13Allocation computes the entity assignment for this world. isoCases is the
// profile's multi-graph isolation quota (the cross-user family needs at least
// one even anchor below it; with no isolation graph the family is empty).
func (w World) V13Allocation(isoCases int) V13Allocation {
	scale := w.Scale()
	counts := V13AbsenceFamilyCounts(scale)
	var a V13Allocation
	next := len(w.People)
	take := func(n int) []int {
		out := make([]int, 0, n)
		for i := 0; i < n && next > 0; i++ {
			next--
			out = append(out, next)
		}
		return out
	}
	a.PurePeople = take(counts[V13FamilyPureAbsence])
	a.NearPeople = take(counts[V13FamilyNearMiss])
	for i := 0; i < counts[V13FamilyStaleRemoved]; i++ {
		pair := take(2)
		if len(pair) == 2 {
			a.StalePairs = append(a.StalePairs, [2]int{pair[0], pair[1]})
		}
	}
	asOfPeople, asOfProjects, asOfTrips := V13AsOfPairCounts(scale)
	a.AsOfPeople = take(asOfPeople)
	sparePeople, spareProjects, spareTrips := V13SpareCounts(scale)
	a.SparePeople = take(sparePeople)
	for i := 0; len(a.CrossPeople) < counts[V13FamilyCrossUser] && i < isoCases && i < next; i += 2 {
		a.CrossPeople = append(a.CrossPeople, i)
	}

	nextProject := len(w.Projects)
	takeProjects := func(n int) []int {
		out := make([]int, 0, n)
		for i := 0; i < n && nextProject > 0; i++ {
			nextProject--
			out = append(out, nextProject)
		}
		return out
	}
	a.InsufficientProjects = takeProjects(counts[V13FamilyInsufficient])
	a.AsOfProjects = takeProjects(asOfProjects)
	a.SpareProjects = takeProjects(spareProjects)

	nextTrip := 0
	takeTrips := func(n int) []int {
		out := make([]int, 0, n)
		for i := 0; i < n && nextTrip < len(w.Trips); i++ {
			out = append(out, nextTrip)
			nextTrip++
		}
		return out
	}
	a.StagedTrips = takeTrips(V13StagedTripCount(scale))
	a.FalseTrips = takeTrips(counts[V13FamilyFalsePremise])
	a.AsOfTrips = takeTrips(asOfTrips)
	a.SpareTrips = takeTrips(spareTrips)
	return a
}

// ExcludeKeys returns the ordinary-question exclusion set for this allocation:
// every (oracle, index) whose fact a v13 family already asks, so the ordinary
// world pool never duplicates a twin's fact under a second case id.
func (a V13Allocation) ExcludeKeys() map[string]bool {
	out := map[string]bool{}
	for _, list := range [][]int{a.PurePeople, a.NearPeople, a.CrossPeople, a.AsOfPeople, a.SparePeople} {
		for _, i := range list {
			out[excludeKey(oracleContactCurrent, i)] = true
			out[excludeKey(oracleContactPrevious, i)] = true
		}
	}
	for _, pair := range a.StalePairs {
		for _, i := range pair {
			out[excludeKey(oracleContactCurrent, i)] = true
			out[excludeKey(oracleContactPrevious, i)] = true
		}
	}
	for _, i := range a.InsufficientProjects {
		out[excludeKey(oracleProjectOutstanding, i)] = true
	}
	for _, i := range a.SpareProjects {
		out[excludeKey(oracleProjectOutstanding, i)] = true
	}
	for _, i := range a.AsOfProjects {
		out[excludeKey(oracleProjectOutstanding, i)] = true
	}
	for _, i := range a.FalseTrips {
		out[excludeKey(oracleTripChangedLegCurrent, i)] = true
	}
	for _, i := range a.SpareTrips {
		out[excludeKey(oracleTripChangedLegCurrent, i)] = true
	}
	for _, i := range a.AsOfTrips {
		out[excludeKey(oracleTripChangedLegCurrent, i)] = true
		out[excludeKey(oracleTripChangedLegPrevious, i)] = true
	}
	return out
}

func excludeKey(kind string, index int) string { return fmt.Sprintf("%s:%d", kind, index) }

// StagedCorrectionWaves maps each staged trip correction pair to the /seed wave
// it arrives in. Trips alternate between waves 1 and 2 so both realism waves
// carry content; the result is clamped to the profile's wave count, so a
// single-wave profile still moves the membership out of the initial seed (into
// memory wave 0, seeded after the tool phase). Membership never depends on the
// wave count: the tool-side carrier (which knows no profile) and the memory
// side agree on exactly which records leave the initial seed. Empty for
// pre-v13 worlds.
func (w World) StagedCorrectionWaves(a V13Allocation, nWaves int) map[string]int {
	out := map[string]int{}
	if w.BenchVersion < protocol.BenchVersionV13 {
		return out
	}
	if nWaves < 1 {
		nWaves = 1
	}
	for k, tripIndex := range a.StagedTrips {
		wave := 1 + k%2
		if wave > nWaves-1 {
			wave = nWaves - 1
		}
		out[w.Trips[tripIndex].CorrectionPairID] = wave
	}
	return out
}

// StagedCorrectionMembership is the wave-independent set of staged records:
// every key of StagedCorrectionWaves for any wave count.
func (w World) StagedCorrectionMembership(a V13Allocation) map[string]int {
	return w.StagedCorrectionWaves(a, 1)
}

// InitialPairs returns the pairs seeded before any scored case: the whole
// world minus the corrections staged into later waves. Pre-v13 worlds return
// every pair.
func (w World) InitialPairs(staged map[string]int) []protocol.MemoryPair {
	if len(staged) == 0 {
		return w.Pairs
	}
	out := make([]protocol.MemoryPair, 0, len(w.Pairs))
	for _, pair := range w.Pairs {
		if _, isStaged := staged[pair.PairID]; !isStaged {
			out = append(out, pair)
		}
	}
	return out
}

// StagedPairs returns the pairs for one later wave, in world order.
func (w World) StagedPairs(staged map[string]int, wave int) []protocol.MemoryPair {
	var out []protocol.MemoryPair
	for _, pair := range w.Pairs {
		if assigned, isStaged := staged[pair.PairID]; isStaged && assigned == wave {
			out = append(out, pair)
		}
	}
	return out
}

// UnlockWaveFor is the first wave after which every record a plan requires has
// been seeded: the maximum staged wave among its evidence, 0 when none is
// staged.
func UnlockWaveFor(plan QuestionPlan, staged map[string]int) int {
	last := 0
	for _, id := range plan.RequiredPairIDs {
		if wave := staged[id]; wave > last {
			last = wave
		}
	}
	return last
}

// QuestionPlansExcluding is QuestionPlans without the candidates whose
// (oracle, index) key is excluded, with the allocation's staged-trip programs
// selected first so every correction that leaves the initial seed has at least
// one dependent case, and with the v13 story cap applied. With an empty
// exclusion it is exactly QuestionPlans, so v8..v12 bytes are untouched.
func (w World) QuestionPlansExcluding(count int, exclude map[string]bool) ([]QuestionPlan, error) {
	if len(exclude) == 0 {
		return w.QuestionPlans(count)
	}
	return w.questionPlansFiltered(count, exclude, nil)
}

// QuestionPlansV13 is the v13 selector: excluded keys are dropped, the
// staged-trip current-itinerary programs are required, and story programs are
// capped so ordinary programs keep a floor share.
func (w World) QuestionPlansV13(count int, a V13Allocation) ([]QuestionPlan, error) {
	require := map[string]bool{}
	for _, tripIndex := range a.StagedTrips {
		require[excludeKey(oracleTripCurrent, tripIndex)] = true
	}
	return w.questionPlansFiltered(count, a.ExcludeKeys(), require)
}

func v13ProbeRand(seed int64) *rand.Rand {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v13-world-probes:%d", seed)
	return rand.New(rand.NewSource(int64(h.Sum64() & ((1 << 63) - 1))))
}

// buildV13Probes plants the absence-probe records. It reads the finished v8
// world and appends; it never reorders or rewrites a v8 pair.
func buildV13Probes(seed int64, w *World) V13Probes {
	var probes V13Probes
	scale := w.Scale()
	counts := V13AbsenceFamilyCounts(scale)
	if len(counts) == 0 {
		return probes
	}
	r := v13ProbeRand(seed)
	a := w.V13Allocation(0)
	base := time.Date(2024, 1, 8, 9, 0, 0, 0, time.UTC)
	add := func(id, session, prompt, response string) {
		index := len(w.Pairs) + len(probes.Pairs)
		probes.Pairs = append(probes.Pairs, protocol.MemoryPair{
			PairID: id, SessionID: session,
			Timestamp: base.Add(time.Duration(index*137) * time.Hour).Format(time.RFC3339),
			Prompt:    prompt, Response: response,
		})
	}
	taken := map[string]bool{strings.ToLower(w.UserName): true}
	for _, p := range w.People {
		taken[strings.ToLower(p.Name)] = true
		taken[strings.ToLower(strings.Fields(p.Name)[0])] = true
		taken[strings.ToLower(p.Nickname)] = true
	}
	coinName := func(ordinal int) string {
		for attempt := 0; ; attempt++ {
			given := humandata.GivenName(r, ordinal+attempt)
			surname := humandata.Surname(r, ordinal+attempt)
			name := given + " " + surname
			if taken[strings.ToLower(name)] || taken[strings.ToLower(given)] {
				continue
			}
			taken[strings.ToLower(name)] = true
			taken[strings.ToLower(given)] = true
			return name
		}
	}

	for _, personIndex := range a.PurePeople {
		probes.Absent = append(probes.Absent, V13AbsentProbe{Person: personIndex, Name: coinName(len(probes.Absent))})
	}
	for k, personIndex := range a.NearPeople {
		p := w.People[personIndex]
		name := coinName(len(probes.Absent) + k)
		id := protocol.OpaqueCaseID(seed, "v13-probe-near-miss", k)
		mention := []string{
			fmt.Sprintf("%s introduced me to %s, who also works at %s — we mostly talked about the %s.", p.Nickname, name, p.Employer, p.Context),
			fmt.Sprintf("Met %s at %s through %s; they're on the same floor and were curious about the %s.", name, p.Employer, p.Nickname, p.Context),
			fmt.Sprintf("Side note from the %s: %s's colleague %s at %s asked to be kept in the loop.", p.Context, p.Nickname, name, p.Employer),
		}[r.Intn(3)]
		add(id, fmt.Sprintf("people-%02d-e", personIndex), mention, warmResponse(seed, id,
			"Nice — I’ll remember the introduction.",
			"Got it, another face from that circle.",
			"I’ll keep the connection in mind."))
		probes.NearMiss = append(probes.NearMiss, V13NearMissProbe{Person: personIndex, Name: name, MentionPairID: id})
	}

	for k, pair := range a.StalePairs {
		for side, personIndex := range pair {
			p := w.People[personIndex]
			ordinal := 2*k + side
			handle := persona.CoinShaped(seed, fmt.Sprintf("v13-handle|%d", ordinal))
			id := protocol.OpaqueCaseID(seed, "v13-probe-handle", ordinal)
			// The statement deliberately avoids the attribute words the question
			// uses ("handle", "reach") and the event context, so the twin's
			// question cannot be answered by lexical overlap with this record.
			statement := []string{
				fmt.Sprintf("%s goes by %s on Signal — quickest way to get a reply.", p.Nickname, handle),
				fmt.Sprintf("On Signal, %s is %s. Messages there get answered fastest.", p.Nickname, handle),
			}[r.Intn(2)]
			add(id, fmt.Sprintf("people-%02d-f", personIndex), statement, warmResponse(seed, id,
				"Saved — I’ll keep that handle with their contact.",
				"Got it, the handle is on file.",
				"I’ve noted the handle alongside their details."))
			probe := V13HandleProbe{Person: personIndex, Handle: handle, HandlePairID: id, Removed: side == 0}
			if side == 0 {
				removal := protocol.OpaqueCaseID(seed, "v13-probe-handle-removal", k)
				text := []string{
					fmt.Sprintf("Please drop the %s I gave you for %s — they closed that account and I don't want it on file.", v13ProbeHandleNoun, p.Nickname),
					fmt.Sprintf("%s retired their %s; remove the one I saved and don't hand it out again.", p.Nickname, v13ProbeHandleNoun),
				}[r.Intn(2)]
				add(removal, fmt.Sprintf("people-%02d-g", personIndex), text, warmResponse(seed, removal,
					"Done — I’ve removed that handle from their contact.",
					"Understood, that handle is gone from my notes.",
					"Removed. I’ll only keep their other details."))
				probe.RemovalPairID = removal
			}
			probes.Handles = append(probes.Handles, probe)
		}
	}

	seenCompanies := map[string]bool{w.UserCompany: true}
	for _, p := range w.People {
		seenCompanies[p.Employer] = true
		seenCompanies[p.PreviousEmployer] = true
	}
	for _, p := range w.Projects {
		seenCompanies[p.Client] = true
		seenCompanies[p.Vendor] = true
	}
	seenAliases := map[string]bool{}
	for _, p := range w.Projects {
		seenAliases[strings.ToLower(p.Alias)] = true
		seenAliases[strings.ToLower(p.Name)] = true
	}
	for k := 0; k < counts[V13FamilyInsufficient]; k++ {
		vendor := uniqueCompanyWith(r, seenCompanies, corpusCompany)
		alias := uniqueString(r, seenAliases, func(r *rand.Rand) string {
			return strings.ToLower(humandata.Surname(r, k) + " " + []string{"retainer", "refresh", "rollout", "audit", "pilot"}[r.Intn(5)])
		})
		approval := protocol.OpaqueCaseID(seed, "v13-probe-thread-approval", k)
		payment := protocol.OpaqueCaseID(seed, "v13-probe-thread-payment", k)
		invoice := "INV-" + strings.ToUpper(protocol.OpaqueCaseID(seed, "v13-probe-thread-invoice", k)[1:7])
		approved := (180000 + r.Intn(2600000)) / 100 * 100
		add(approval, fmt.Sprintf("project-v13-%02d-a", k),
			fmt.Sprintf("Kicking off the %s with %s. Their invoice %s was approved at %s; I'll paste the payment details when the bank export lands.", alias, vendor, invoice, money(approved)),
			warmResponse(seed, approval,
				"Got it — the approved total is on file.",
				"I’ve noted the approved figure for that thread.",
				"Saved the invoice and its approved total."))
		add(payment, fmt.Sprintf("project-v13-%02d-b", k),
			fmt.Sprintf("Update on the %s: we sent %s a partial payment against %s last week. The amount is in the bank export I still haven't pasted, so don't treat it as settled.", alias, vendor, invoice),
			warmResponse(seed, payment,
				"Understood — partial payment made, amount pending.",
				"Noted; I’ll wait for the export before doing any balance math.",
				"Got it, the payment happened but the figure isn’t here yet."))
		probes.Threads = append(probes.Threads, V13ThreadProbe{
			Alias: alias, Vendor: vendor, InvoiceID: invoice, ApprovedCents: approved,
			ApprovalPairID: approval, PaymentPairID: payment,
		})
	}
	return probes
}
