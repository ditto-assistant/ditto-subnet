package universe

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v14 membership fold (SN-357).
//
// V13's only set-valued family is the school packing list: four items, one
// drop and one add, so the current set is the first list with one swap. From
// v14 the school list runs three to five membership events across records:
//
//   - a removal of an item on the list, followed later by its re-add;
//   - a replacement ("swap the B for a Y");
//   - optionally a plain addition;
//   - optionally a no-op removal of an item that was never on the list.
//
// The current set is the fold of every event over the first list, four or
// five items. Grading keeps one set-member claim per item at weight 1/n, so a
// reply that loses a member keeps the members it got; the replaced item, the
// no-op item and the decoy list's item are slot-scoped distractors. The
// counterfactual changes only the replacement record, swapping a different
// original item, so exactly one record moves and two members change.
//
// The event shape comes from the generator competition (contributed by a
// generator-competition miner, generator D, rounds 25955-25963, whose
// membership-after-joins-and-leaves cases kept panel means below 1.0 because
// agents lost individual members; generator C's set-intersection was
// discriminating in round 25955). The public default kit's scattered list was
// trivial in every round.

// v14Membership is one membership-fold draw over the school list.
type v14Membership struct {
	Removed  int // original index removed, then re-added
	Replaced int // original index replaced in the base reading
	CFRepl   int // original index replaced in the counterfactual reading
	Swap     string
	Added    string // "" when the plain addition is absent
	NoOp     string // "" when the no-op removal is absent
	// Log is the story order of the event records after the first list.
	Log []string
}

// Membership event kinds.
const (
	v14SetRemove  = "remove"
	v14SetReadd   = "readd"
	v14SetReplace = "replace"
	v14SetAdd     = "add"
	v14SetNoOp    = "noop"
	v14SetNeutral = "neutral"
)

func (d *v13Draws) drawV14Membership(group int, g *v13PersonalGroup) *v14Membership {
	seed := d.seed
	key := func(name string) string { return fmt.Sprintf("v14-set-%s-%d", name, group) }
	perm := v13Perm(seed, fmt.Sprintf("p-items-%d", group), len(v13SchoolItems))
	// perm[0..3] are the first list, perm[4] the v13 added item and perm[5]
	// the decoy item (drawPersonalGroup). The v14 events draw from the rest.
	roles := v13Perm(seed, key("roles"), 4)
	s := &v14Membership{Removed: roles[0], Replaced: roles[1], CFRepl: roles[2]}
	s.Swap = v13SchoolItems[perm[4]]
	if v14Coin(seed, key("add")) {
		s.Added = v13SchoolItems[perm[6]]
	}
	if v14Coin(seed, key("noop")) {
		s.NoOp = v13SchoolItems[perm[7]]
	}
	events := []string{v14SetRemove, v14SetReplace}
	if s.Added != "" {
		events = append(events, v14SetAdd)
	}
	if s.NoOp != "" {
		events = append(events, v14SetNoOp)
	}
	order := v13Perm(seed, key("order"), len(events))
	for _, i := range order {
		s.Log = append(s.Log, events[i])
	}
	// The re-add follows its removal at a seeded later position.
	at := 0
	for i, ev := range s.Log {
		if ev == v14SetRemove {
			at = i + 1
		}
	}
	at += v13Index(seed, key("readd"), len(s.Log)-at+1)
	s.Log = append(s.Log[:at], append([]string{v14SetReadd}, s.Log[at:]...)...)
	at = v13Index(seed, key("neutral"), len(s.Log)+1)
	s.Log = append(s.Log[:at], append([]string{v14SetNeutral}, s.Log[at:]...)...)
	return s
}

// renderV14Membership renders the SN-357 school membership fold.
func renderV14Membership(d *v13Draws, group, variant int, g v13PersonalGroup, counter bool, m *v13Member) {
	seed := d.seed
	s := g.T14Set
	relation := g.Relation
	neutral := m.Records[2]
	replaced := s.Replaced
	if counter {
		replaced = s.CFRepl
	}
	pick := func(salt string, forms []string) string {
		return v13Pick(seed, fmt.Sprintf("v14-set-%s-%d", salt, group), forms)
	}
	records := []string{fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-school-a-%d", group), []string{
		"%[1]s's %[2]s list from school: %[3]s.",
		"For %[1]s's %[2]s the school wants: %[3]s.",
		"Packing list for %[1]s's %[2]s — %[3]s.",
	}), relation, g.Subject, strings.Join(g.Items[:4], ", "))}
	for _, ev := range s.Log {
		switch ev {
		case v14SetRemove:
			records = append(records, fmt.Sprintf(pick("remove", []string{
				"The school took the %[2]s off the %[1]s list.",
				"Update for the %[1]s: no %[2]s needed any more.",
			}), g.Subject, g.Items[s.Removed]))
		case v14SetReadd:
			records = append(records, fmt.Sprintf(pick("readd", []string{
				"Scratch the earlier change for the %[1]s: the %[2]s is back on the list.",
				"The school changed its mind again — the %[1]s does need the %[2]s after all.",
			}), g.Subject, g.Items[s.Removed]))
		case v14SetReplace:
			records = append(records, fmt.Sprintf(pick("replace", []string{
				"For the %[1]s, swap the %[2]s for a %[3]s.",
				"The %[1]s list now has a %[3]s instead of the %[2]s.",
			}), g.Subject, g.Items[replaced], s.Swap))
		case v14SetAdd:
			records = append(records, fmt.Sprintf(pick("add", []string{
				"One more for the %[1]s: they want a %[2]s too.",
				"The school added a %[2]s to the %[1]s list.",
			}), g.Subject, s.Added))
		case v14SetNoOp:
			records = append(records, fmt.Sprintf(pick("noop", []string{
				"The school says no %[2]s for the %[1]s; it was never on our list anyway, so nothing changes.",
				"They mentioned a %[2]s is not needed for the %[1]s, but we never had one on the list.",
			}), g.Subject, s.NoOp))
		case v14SetNeutral:
			records = append(records, neutral)
		}
	}
	v14SetRecords(m, records)
	m.Items = nil
	for i, item := range g.Items[:4] {
		if i != replaced {
			m.Items = append(m.Items, item)
		}
	}
	m.Items = append(m.Items, s.Swap)
	if s.Added != "" {
		m.Items = append(m.Items, s.Added)
	}
	m.ItemKinds = make([]string, len(m.Items))
	m.Expected = strings.Join(m.Items, "; ")
	m.Distractors = []string{g.Items[replaced], g.DecoyItm}
	if s.NoOp != "" {
		m.Distractors = append(m.Distractors, s.NoOp)
	}
	m.Claims = nil
	for _, item := range m.Items {
		m.Claims = append(m.Claims, v13Claim(protocol.ClaimKindSetMember, item, []string{item}, 1/float64(len(m.Items))))
	}
	m.Program = V10QueryNode{Op: "set_fold", Field: "packing_list", Children: []V10QueryNode{{Op: "resolve_entity", Field: "school_event"}}}
	m.Protected = append([]string{relation, g.Subject, g.Decoy, s.Swap, g.DecoyItm, g.People[3]}, g.Items...)
	if s.Added != "" {
		m.Protected = append(m.Protected, s.Added)
	}
	if s.NoOp != "" {
		m.Protected = append(m.Protected, s.NoOp)
	}
	m.Facts = []string{"original list", "removal and re-add", "replacement", "membership events"}
	m.Operations = []string{"resolve the event", "fold every membership event in order", "return the current set"}
}
