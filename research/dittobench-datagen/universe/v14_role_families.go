package universe

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v14 role families (SN-359, SN-360).
//
// The v13 business cycle has one role-disambiguation family,
// client-vs-vendor, and every record in it names its role label outright
// ("X's client is Y"): picking the right label solves it. Owner families never
// separate the person who did a step from the person who holds the item
// afterwards, and every person appears under one canonical name, so nothing
// asks the agent to join a contact handle to a named person.
//
// V14 adds two families and rotates them through the role slot of the cycle:
//
//   - holder-vs-actor (SN-360): records describe actions whose doer differs
//     from the resulting holder ("A signed the release and passed the keys to
//     B"), over a one- or two-hop custody chain. The question asks who holds
//     the item now (a holder fold over the chain) or who performed a named
//     step. The answer shares its sentence with a same-type distractor and no
//     role label separates them. The second hop names the item only by its
//     bare noun. Chains are capped at two hops: generator analogs with three or
//     more hops sat at 0/5 solves.
//   - identity-join (SN-359): a directory record binds each person to one
//     synthetic handle (an example.invalid address or a chat handle), stated
//     once and explicitly. The owner records use only one side of the binding,
//     so the answer string never appears in the governing record: either the
//     records use handles and the question asks for the person's name, or the
//     records use names (or initial and surname) and the question asks for the
//     contact handle. The distractors are the previous owner and a near-miss
//     handle that belongs to another person (never an edit-distance-1
//     neighbour).
//
// Both families come directly from the generator competition. The
// step-doer-versus-holder and holder-order shapes, and the initial-form owner
// reference joined to a contact, were contributed by a generator-competition
// miner (generator D, rounds 25955-25963); the custody chain whose second hop uses
// only the item's bare noun by another (generator C, rounds 25955-25963). The
// contact-to-person join was also evidenced by generator J (round 25946). The v14 versions
// keep those shapes and add exact ground truth: unique handles and surnames,
// one holder per chain, no pronouns, and a counterfactual that moves one
// record.
//
// The role slot sits at cycle positions 3, 7 and 8. Bench v13 fills it with
// client-vs-vendor at position 3 and has no positions 7 or 8, so every v13 seed
// renders byte-identically. From v14 each seed draws a permutation of the three
// role families for those positions; positions 0-2 and 4-6 keep their v13
// families up to the temporal-slot draw (V14TemporalFamily) of 2, 4 and 5. The full profile spends four legacy business groups, so its
// fourth group is client-vs-vendor, holder-vs-actor, or identity-join with
// equal probability across seeds, and a 36-case request covers every family.

const (
	V14FamilyHolderVsActor V13Family = "holder-vs-actor"
	V14FamilyIdentityJoin  V13Family = "identity-join"
)

// V14Families is the v14 business cycle in slot order. Role-slot positions
// (v14RoleSlots) are filled per seed from v14RoleFamilies; the entries here at
// those positions are only the identity order of that draw.
var V14Families = []V13Family{
	V13FamilyOwnerAfterCorrection,
	V13FamilyStandingStatus,
	V13FamilyLatestEvent,
	V13FamilyClientVsVendor,
	V13FamilyNextActionResponsible,
	V13FamilyCurrentChannel,
	V13FamilyRecordsDisagree,
	V14FamilyHolderVsActor,
	V14FamilyIdentityJoin,
}

// v14RoleSlots are the V14Families positions whose family is drawn per seed.
var v14RoleSlots = []int{3, 7, 8}

// v14RoleFamilies are the role-disambiguation families the role slot rotates.
var v14RoleFamilies = []V13Family{V13FamilyClientVsVendor, V14FamilyHolderVsActor, V14FamilyIdentityJoin}

// V14FamilyFor returns the family of business group group at v14: the role
// slot draws per seed from v14RoleFamilies, and the temporal slot
// (V14TemporalFamily) permutes latest-event, next-action-responsible and
// current-channel.
func V14FamilyFor(seed int64, group int) V13Family {
	slot := group % len(V14Families)
	for i, roleSlot := range v14RoleSlots {
		if slot == roleSlot {
			perm := v14Perm(seed, "v14-role-slot", len(v14RoleFamilies))
			return v14RoleFamilies[perm[i]]
		}
	}
	return V14TemporalFamily(seed, V14Families[slot])
}

// V13FamilyOfVersion returns the family a generated business case belongs to
// under an explicit contract. Provenance-only: never on the wire.
func V13FamilyOfVersion(seed int64, caseIndex, benchVersion int) V13Family {
	if benchVersion < protocol.BenchVersionV14 {
		return V13FamilyOf(caseIndex)
	}
	return V14FamilyFor(seed, caseIndex/4)
}

// family returns the family of a group under the draw's contract.
func (d *v13Draws) family(group int) V13Family {
	if d.benchVersion < protocol.BenchVersionV14 {
		return V13Families[group%len(V13Families)]
	}
	return V14FamilyFor(d.seed, group)
}

// v14GroupExtra carries the draws only the v14 role families use. v13 groups
// leave it nil.
type v14GroupExtra struct {
	// Holder-vs-actor.
	Hops        int       // custody hops, 1 or 2
	AskActor    bool      // ask who performed a step instead of who holds the item
	ActorHop    int       // which hop's actor the question names (0 or 1)
	Item        [2]string // custody item: full name, bare noun
	Steps       [2]string // the named step of each record
	ExtraPeople [2]string // [0]=second-record actor, [1]=second-hop recipient

	// Identity-join.
	AskContact bool      // ask for the current owner's handle, not the name
	Initials   bool      // contact questions: owner records name people as "E. Page"
	NearMiss   string    // another person whose handle is a near miss of the current owner's
	Handles    [6]string // [0]=first, [1]=current, [2]=counterfactual, [3]=decoy, [4]=near miss, [5]=current owner's counterfactual handle
}

// v14DrawState is per-seed draw state only the v14 families touch.
type v14DrawState struct {
	usedHandles map[string]bool
}

func (d *v13Draws) v14State() *v14DrawState {
	if d.v14 == nil {
		d.v14 = &v14DrawState{usedHandles: map[string]bool{}}
	}
	return d.v14
}

// v14CustodyItems pair an item's full name with the bare noun people use in
// passing. Bare nouns are unique across the bank, so "the binder" names one
// item. The second hop of a two-hop chain uses only the bare noun, so the
// chain links by the item and its giver, not by a repeated phrase.
// (Contributed by a generator-competition miner, generator C, rounds 25955-25963.)
var v14CustodyItems = [][2]string{
	{"the vault keys", "the keys"}, {"the signed master copy", "the master copy"},
	{"the escrow binder", "the binder"}, {"the release token", "the token"},
	{"the demo laptop", "the laptop"}, {"the courier pouch", "the pouch"},
	{"the site access badge", "the badge"}, {"the audit folder", "the folder"},
	{"the brass seal stamp", "the stamp"}, {"the spare server drive", "the drive"},
	{"the tender lockbox", "the lockbox"}, {"the prototype casing", "the casing"},
	{"the loading bay fob", "the fob"}, {"the sample crate", "the crate"},
	{"the archive ledger", "the ledger"}, {"the board deck printout", "the printout"},
	{"the warehouse padlock", "the padlock"}, {"the encrypted backup tape", "the tape"},
	{"the field radio", "the radio"}, {"the survey tablet", "the tablet"},
	{"the bid envelope", "the envelope"}, {"the fleet van keycard", "the keycard"},
	{"the mock-up board", "the mock-up"}, {"the inspection camera", "the camera"},
}

// v14CustodySteps are regular-verb steps, so one form reads as both simple past
// and past participle. None hands the item over by itself.
var v14CustodySteps = []string{
	"signed the release", "approved the transfer", "countersigned the handover form",
	"logged the inspection", "cleared the security review", "verified the seals",
	"checked the inventory", "stamped the dispatch slip", "audited the access log",
	"witnessed the count", "filed the custody report", "booked the courier",
	"scanned the barcode", "photographed the condition", "notarised the affidavit",
	"reconciled the manifest", "updated the asset register", "tested the lock",
	"initialled the receipt", "certified the weight", "recorded the serial number",
	"inspected the packaging", "confirmed the insurance", "cleaned the contacts",
}

// v14HandleWords are opaque handle stems. None is a role, status, channel, or
// organisation word of the v13 banks.
var v14HandleWords = []string{
	"tern", "quill", "mosaic", "cobble", "lumen", "fjord", "basalt", "cinder",
	"pylon", "tarn", "vesper", "gannet", "lattice", "marram", "nimbus", "ochre",
	"plinth", "spindle", "trellis", "umber", "weir", "kestrelline", "dunlin", "sorrel",
	"alder", "bramble", "corvid", "drumlin", "egret", "flint", "gorse", "heron",
}

var v14HandleDomains = []string{
	"ops", "desk", "crew", "relay", "team", "hub", "post", "unit",
	"wing", "bay", "cell", "deck", "dock", "yard", "loft", "annex",
	"bureau", "depot", "lodge", "camp", "forge", "mill", "works", "guild",
}

const (
	v14HandleEmail = iota
	v14HandleChat
)

func (d *v13Draws) drawV14Group(g *v13Group, group int) {
	x := &v14GroupExtra{}
	g.V14 = x
	switch g.Family {
	case V14FamilyHolderVsActor:
		x.Hops = 1 + v14Index(d.seed, fmt.Sprintf("v14-hops-%d", group), 2)
		x.AskActor = v14Index(d.seed, fmt.Sprintf("v14-ask-actor-%d", group), 2) == 1
		if x.Hops == 2 {
			x.ActorHop = v14Index(d.seed, fmt.Sprintf("v14-actor-hop-%d", group), 2)
		}
		x.Item = v14CustodyItems[v14Index(d.seed, fmt.Sprintf("v14-item-%d", group), len(v14CustodyItems))]
		steps := v14Perm(d.seed, fmt.Sprintf("v14-steps-%d", group), len(v14CustodySteps))
		x.Steps = [2]string{v14CustodySteps[steps[0]], v14CustodySteps[steps[1]]}
		x.ExtraPeople = [2]string{d.name(), d.name()}
	case V14FamilyIdentityJoin:
		x.AskContact = v14Index(d.seed, fmt.Sprintf("v14-ask-contact-%d", group), 2) == 1
		x.NearMiss = d.name()
		if x.AskContact {
			x.Initials = v14Index(d.seed, fmt.Sprintf("v14-initials-%d", group), 2) == 1
		}
		style := v14Index(d.seed, fmt.Sprintf("v14-handle-style-%d", group), 2)
		domain := v14Pick(d.seed, fmt.Sprintf("v14-handle-domain-%d", group), v14HandleDomains)
		people := []string{g.People[0], g.People[1], g.People[2], g.People[3], x.NearMiss}
		state := d.v14State()
		words := v14Perm(d.seed, fmt.Sprintf("v14-handle-words-%d", group), len(v14HandleWords))
		next := 0
		word := func() string {
			for ; next < len(words); next++ {
				w := v14HandleWords[words[next]]
				if !v14WordInNames(w, people) {
					next++
					return w
				}
			}
			panic("v14 identity-join: handle word bank exhausted")
		}
		// Each person's stem is distinct inside the group. The near miss
		// reuses the current owner's stem with a number that differs in both
		// digits, so the two handles are never edit-distance-1 neighbours.
		stems := [6]string{word(), word(), word(), word(), "", word()}
		stems[4] = stems[1]
		for i := range x.Handles {
			for attempt := 0; ; attempt++ {
				num := 10 + v14Index(d.seed, fmt.Sprintf("v14-handle-num-%d-%d-%d", group, i, attempt), 90)
				if i == 4 {
					base := v14HandleNumber(x.Handles[1])
					if num/10 == base/10 || num%10 == base%10 {
						continue
					}
				}
				h := v14Handle(style, stems[i], num, domain)
				if state.usedHandles[h] {
					continue
				}
				state.usedHandles[h] = true
				x.Handles[i] = h
				break
			}
		}
	}
}

func v14Handle(style int, stem string, num int, domain string) string {
	if style == v14HandleChat {
		return fmt.Sprintf("@%s-%02d", stem, num)
	}
	return fmt.Sprintf("%s.%02d@%s.example.invalid", stem, num, domain)
}

// v14HandleNumber recovers the two-digit number of a rendered handle.
func v14HandleNumber(h string) int {
	digits := ""
	for _, r := range h {
		if r >= '0' && r <= '9' {
			digits += string(r)
		} else if digits != "" {
			break
		}
	}
	n := 0
	fmt.Sscanf(digits, "%d", &n)
	return n
}

// v14Initials renders a full name as its first initial and the rest, e.g.
// "Elizabeth Page" -> "E. Page".
func v14Initials(name string) string {
	parts := strings.Fields(name)
	if len(parts) < 2 {
		return name
	}
	return parts[0][:1] + ". " + strings.Join(parts[1:], " ")
}

func v14WordInNames(word string, names []string) bool {
	for _, name := range names {
		for _, part := range strings.Fields(strings.ToLower(name)) {
			if strings.Contains(part, word) || strings.Contains(word, part) {
				return true
			}
		}
	}
	return false
}

// v14Handover is one custody record: Actor performed Step; when Giver is set
// the item moved from Giver to Recipient in the same record. Record indexes
// the v13Member record that states it.
type v14Handover struct {
	Record    int
	Actor     string
	Step      string
	Giver     string
	Recipient string
}

// renderV14HolderVsActor fills a holder-vs-actor member. Exactly one record
// moves in the counterfactual: the record naming the final recipient (holder
// questions) or the named step's actor (actor questions).
func renderV14HolderVsActor(d *v13Draws, schema v13Schema, group, variant int, g v13Group, counter bool, subject string, m *v13Member) {
	seed := d.seed
	x := g.V14
	actor1, holder1, actor2, holder2 := g.People[0], g.People[1], x.ExtraPeople[0], x.ExtraPeople[1]
	if counter {
		switch {
		case x.AskActor && x.ActorHop == 0:
			actor1 = g.People[2]
		case x.AskActor:
			actor2 = g.People[2]
		case x.Hops == 1:
			holder1 = g.People[2]
		default:
			holder2 = g.People[2]
		}
	}
	final := holder1
	if x.Hops == 2 {
		final = holder2
	}
	// Record 0: the first hop. Its actor hands the item over; the recipient
	// may come first in the sentence, so "last name wins" is not a reading.
	m.Records[0] = fmt.Sprintf(v14Pick(seed, fmt.Sprintf("v14-hop-a-%d", group), []string{
		"%[1]s %[4]s on %[3]s and passed %[5]s to %[2]s.",
		"Once %[1]s had %[4]s for %[3]s, %[5]s went over to %[2]s.",
		"%[2]s collected %[5]s for %[3]s from %[1]s, who had %[4]s.",
	}), actor1, holder1, g.Alias, x.Steps[0], x.Item[0])
	if x.Hops == 2 {
		// Record 1: the second hop. A different person performs the step;
		// the giver is named, so the chain links regardless of filing order.
		// The item appears only by its bare noun.
		m.Records[1] = fmt.Sprintf(v14Pick(seed, fmt.Sprintf("v14-hop-b-%d", group), []string{
			"%[1]s %[5]s, and %[2]s handed %[6]s for %[4]s on to %[3]s.",
			"With %[1]s having %[5]s, %[3]s took %[6]s for %[4]s over from %[2]s.",
			"%[2]s passed %[6]s for %[4]s to %[3]s after %[1]s %[5]s.",
		}), actor2, holder1, holder2, g.Alias, x.Steps[1], x.Item[1])
	} else {
		// Record 1: a later step whose doer never takes the item.
		m.Records[1] = fmt.Sprintf(v14Pick(seed, fmt.Sprintf("v14-step-b-%d", group), []string{
			"%[1]s %[3]s on %[2]s; %[4]s did not change hands.",
			"Later %[1]s %[3]s for %[2]s without taking %[4]s.",
			"%[1]s %[3]s on %[2]s, and %[4]s stayed where it was.",
		}), actor2, g.Alias, x.Steps[1], x.Item[1])
	}
	m.custody = []v14Handover{{Record: 0, Actor: actor1, Step: x.Steps[0], Giver: actor1, Recipient: holder1}}
	if x.Hops == 2 {
		m.custody = append(m.custody, v14Handover{Record: 1, Actor: actor2, Step: x.Steps[1], Giver: holder1, Recipient: holder2})
	} else {
		m.custody = append(m.custody, v14Handover{Record: 1, Actor: actor2, Step: x.Steps[1]})
	}
	resolve := V10QueryNode{Op: "resolve_entity", Field: schema.Alias}
	closers := []string{"Just the name.", "Name only.", "Give the person's name."}
	m.Kind = protocol.AnswerValue
	people := []string{actor1, holder1, actor2}
	if x.Hops == 2 {
		people = append(people, holder2)
	}
	if x.AskActor {
		step, actor := x.Steps[0], actor1
		if x.ActorHop == 1 {
			step, actor = x.Steps[1], actor2
		}
		m.DecoyClause = fmt.Sprintf(" There, %s %s.", g.People[3], step)
		m.Question = v13Question(seed, group, variant, []string{
			"who %[2]s on %[1]s?",
			"on %[1]s, which person %[2]s?",
			"name the person who %[2]s for %[1]s.",
		}, closers, subject, step)
		m.Expected, m.AcceptAny = actor, []string{actor}
		// The holders named beside the actor are the same-type distractors.
		for _, p := range people {
			if p != actor && (p == holder1 || p == holder2) {
				m.Distractors = append(m.Distractors, p)
			}
		}
		m.Program = V10QueryNode{Op: "read", Field: "actor:" + step, Children: []V10QueryNode{resolve}}
		m.Facts = []string{"entity alias", "custody records", "step performer"}
		m.Operations = []string{"resolve subject by remit", "find the record naming the step", "separate the step's doer from the recipient", "return the person"}
	} else {
		m.DecoyClause = fmt.Sprintf(" Its copy of %s sits with %s.", x.Item[0], g.People[3])
		m.Question = v13Question(seed, group, variant, []string{
			"who has %[2]s for %[1]s now?",
			"after every recorded handover, who is holding %[2]s on %[1]s?",
			"for %[1]s, which person currently has %[2]s?",
		}, closers, subject, x.Item[0])
		m.Expected, m.AcceptAny = final, []string{final}
		// Every actor is a distractor; so is the first recipient once the
		// item has moved on.
		m.Distractors = []string{actor1, actor2}
		if x.Hops == 2 {
			m.Distractors = append(m.Distractors, holder1)
		}
		m.Program = V10QueryNode{Op: "fold_holder", Field: "custody", Children: []V10QueryNode{resolve}}
		m.Facts = []string{"entity alias", "custody records", "handover chain"}
		m.Operations = []string{"resolve subject by remit", "link each handover by its giver", "fold the chain to the final holder", "return the person"}
	}
	m.Distractors = append(m.Distractors, g.People[3])
	m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindPerson, m.Expected, m.AcceptAny, 1)}
	m.Protected = append(m.Protected, x.Item[0], x.Item[1], x.Steps[0], x.Steps[1], g.People[3])
	m.Protected = append(m.Protected, people...)
	m.Constraints = []string{g.Alias, x.Item[0], g.Purpose}
}

// renderV14IdentityJoin fills an identity-join member. Record 2 is the contact
// directory; it is the only record the counterfactual rebinds.
func renderV14IdentityJoin(d *v13Draws, schema v13Schema, group, variant int, g v13Group, counter bool, subject string, m *v13Member) {
	seed := d.seed
	x := g.V14
	names := [5]string{g.People[0], g.People[1], g.People[2], g.People[3], x.NearMiss}
	handles := [5]string{x.Handles[0], x.Handles[1], x.Handles[2], x.Handles[3], x.Handles[4]}
	// Directory entries: first owner, current owner, decoy owner, near miss.
	type entry struct{ handle, name string }
	entries := []entry{{handles[0], names[0]}, {handles[1], names[1]}, {handles[3], names[3]}, {handles[4], names[4]}}
	answer := names[1]
	if x.AskContact {
		answer = handles[1]
		if counter {
			// The current owner is given a different handle.
			entries[1].handle = x.Handles[5]
			answer = x.Handles[5]
		}
	} else if counter {
		// The current owner's handle is rebound to a different person.
		entries[1].name = names[2]
		answer = names[2]
	}
	perm := v14Perm(seed, fmt.Sprintf("v14-directory-%d", group), len(entries))
	form := v14Index(seed, fmt.Sprintf("v14-directory-form-%d", group), 3)
	parts := make([]string, 0, len(entries))
	for _, p := range perm {
		e := entries[p]
		switch form {
		case 0:
			parts = append(parts, fmt.Sprintf("%s is %s", e.handle, e.name))
		case 1:
			parts = append(parts, fmt.Sprintf("%s belongs to %s", e.handle, e.name))
		default:
			parts = append(parts, fmt.Sprintf("%s uses %s", e.name, e.handle))
		}
	}
	m.Records[2] = fmt.Sprintf(v14Pick(seed, fmt.Sprintf("v14-directory-lead-%d", group), []string{
		"Contact directory for this batch: %s.",
		"Who's who in these records: %s.",
		"Directory entries for the people named in this case file: %s.",
	}), strings.Join(parts, "; "))
	// The owner records name each person by one side of the binding only.
	first, current, decoy := handles[0], handles[1], handles[3]
	if x.AskContact {
		first, current, decoy = names[0], names[1], names[3]
		if x.Initials {
			// Surnames are unique within a seed, so the initial and surname
			// resolve to one directory name. (Initial-form owner references:
			// contributed by a generator-competition miner, generator D,
			// rounds 25955-25963.)
			first, current, decoy = v14Initials(names[0]), v14Initials(names[1]), v14Initials(names[3])
		}
	}
	m.Records[0] = fmt.Sprintf(v14Pick(seed, fmt.Sprintf("owner-a-%d", group), []string{
		"%[1]s was first assigned to %[2]s as its %[3]s.",
		"At the outset the %[3]s on %[1]s was %[2]s.",
		"%[2]s took %[1]s as %[3]s when it opened.",
	}), g.Alias, first, schema.Owner)
	m.Records[1] = fmt.Sprintf(v14Pick(seed, fmt.Sprintf("owner-b-%d", group), []string{
		"Under this batch's %[3]s, the %[4]s on %[1]s passed to %[2]s; the earlier assignment no longer stands.",
		"A later %[3]s hands %[1]s's %[4]s to %[2]s — the newest entry governs.",
		"%[3]s note: %[2]s now holds %[4]s on %[1]s, replacing whoever held it before.",
	}), g.Alias, current, schema.Correction, schema.Owner)
	m.DecoyClause = fmt.Sprintf(" Its %s is %s.", schema.Owner, decoy)
	m.Kind = protocol.AnswerValue
	m.Expected, m.AcceptAny = answer, []string{answer}
	resolve := V10QueryNode{Op: "resolve_entity", Field: schema.Alias}
	latest := V10QueryNode{Op: "latest", Field: schema.Owner, Children: []V10QueryNode{resolve}}
	if x.AskContact {
		m.Question = v13Question(seed, group, variant, []string{
			"which contact reaches whoever currently holds the %[2]s role on %[1]s?",
			"after every recorded reassignment, what contact should I use to reach the %[2]s for %[1]s?",
			"for %[1]s, give the contact of the person who stands as %[2]s today.",
		}, []string{"Just the contact, exactly as listed.", "Give the contact exactly as the directory lists it.", "Answer with the contact only."}, subject, schema.Owner)
		m.Distractors = []string{handles[4], handles[0], handles[3]}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindValue, answer, []string{answer}, 1)}
		m.Program = V10QueryNode{Op: "join_handle", Field: "directory", Children: []V10QueryNode{latest}}
	} else {
		m.Question = v13Question(seed, group, variant, []string{
			"who currently holds the %[2]s role on %[1]s? Give the person's name, not their contact.",
			"after every recorded reassignment, which person is the %[2]s for %[1]s? Name them rather than giving a contact.",
			"for %[1]s, who stands as %[2]s today? I need the person's name, not a contact.",
		}, []string{"Just the name is fine.", "Give the person's name.", "Answer with the name only."}, subject, schema.Owner)
		m.Distractors = []string{names[4], names[0], names[3]}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindPerson, answer, []string{answer}, 1)}
		m.Program = V10QueryNode{Op: "join_person", Field: "directory", Children: []V10QueryNode{latest}}
	}
	m.Protected = append(m.Protected, schema.Owner)
	m.Protected = append(m.Protected, names[:]...)
	m.Protected = append(m.Protected, x.Handles[:]...)
	m.Facts = []string{"per-run schema glossary", "entity alias", "initial owner assignment", "superseding reassignment", "contact directory"}
	m.Constraints = []string{g.Alias, schema.Owner, schema.Correction, g.Purpose}
	m.Operations = []string{"induce schema", "resolve subject by remit", "select latest owner assignment", "join the owner to the directory", "return the joined side"}
}

// v14Mix finalises the FNV draw hash with splitmix64. FNV-1a's low bit is the
// parity of its input bytes, so a bare v10Seed%2 ties every binary draw of a
// group to the same digit parity; the v14 families draw from the mixed value.
func v14Mix(seed int64, salt string) uint64 {
	z := uint64(v10Seed(seed, "v14:"+salt)) + 0x9e3779b97f4a7c15
	z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9
	z = (z ^ (z >> 27)) * 0x94d049bb133111eb
	return z ^ (z >> 31)
}

func v14Index(seed int64, salt string, n int) int { return int(v14Mix(seed, salt) % uint64(n)) }

func v14Pick(seed int64, salt string, bank []string) string {
	return bank[v14Index(seed, salt, len(bank))]
}

// v14Perm returns a seeded Fisher-Yates permutation of [0,n).
func v14Perm(seed int64, salt string, n int) []int {
	idx := make([]int, n)
	for i := range idx {
		idx[i] = i
	}
	for i := n - 1; i > 0; i-- {
		j := v14Index(seed, fmt.Sprintf("%s:%d", salt, i), i+1)
		idx[i], idx[j] = idx[j], idx[i]
	}
	return idx
}
