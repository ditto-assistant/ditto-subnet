package universe

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v14 multi-step process family (SN-356).
//
// V13's next-action-responsible draws one action, states it in the first
// record, and has the superseding record say outright that "the step itself is
// unchanged". Only the owner half needs state tracking, so copying the action
// from record 0 earns half credit on every case. Generator competition rounds
// measured the opposite shape as discriminating: a plan of several steps whose
// states change across records, asking for the first step still open
// (contributed by a generator-competition miner, generator D, rounds
// 25955-25963; a single stated next step was trivial, generator G).
//
// From v14 the family renders an ordered process of three to five steps, each
// with its responsible person, stated once in order. Later records close steps
// (done or dropped), may finish a later step early, may reopen a closed step,
// and reassign one step's owner. The question asks for the first step still
// open and who owns it now. The first step is never the answer, so the plan
// record alone never answers. The counterfactual flips one step's completion
// in one record, so the answer moves to a neighbouring step:
//
//   - untouched mode: steps before the answer are closed and the answer step
//     is never mentioned again. The counterfactual reports the step before it
//     as started but still open.
//   - reopened mode: the answer step is reported done and later reopened. The
//     counterfactual reports it checked again and still closed, so the next
//     step answers.
//
// The reassignment targets the base answer's step or the counterfactual
// answer's step, so in one member the owner comes from the plan and in the
// other from the reassignment.

// v14ProcessStep is one step class: an imperative canonical form with its
// accepted equivalents, and the past-tense clause records use when it is done.
// Accept forms are verb phrases only, so a reply narrating a finished step in
// the past tense ("the proposal was drafted") never asserts that step.
type v14ProcessStep struct {
	V13TermClass
	Done string
}

// V14ProcessStepClasses is the v14 step bank, sized to the pool floor (#492).
// Accept clusters are disjoint: no term of one class is a bounded substring of
// a term of another.
var V14ProcessStepClasses = []v14ProcessStep{
	{V13TermClass{Canonical: "draft the proposal", Accept: []string{"draft the proposal", "write the proposal", "prepare the proposal"}}, "drafted the proposal"},
	{V13TermClass{Canonical: "collect the signatures", Accept: []string{"collect the signatures", "gather the signatures", "get the signatures"}}, "collected the signatures"},
	{V13TermClass{Canonical: "book the venue", Accept: []string{"book the venue", "reserve the venue"}}, "booked the venue"},
	{V13TermClass{Canonical: "run the security scan", Accept: []string{"run the security scan", "do the security scan"}}, "ran the security scan"},
	{V13TermClass{Canonical: "order the hardware", Accept: []string{"order the hardware", "buy the hardware"}}, "ordered the hardware"},
	{V13TermClass{Canonical: "send the invoice", Accept: []string{"send the invoice", "issue the invoice"}}, "sent the invoice"},
	{V13TermClass{Canonical: "schedule the demo", Accept: []string{"schedule the demo", "set up the demo"}}, "scheduled the demo"},
	{V13TermClass{Canonical: "update the price list", Accept: []string{"update the price list", "revise the price list"}}, "updated the price list"},
	{V13TermClass{Canonical: "file the permit", Accept: []string{"file the permit", "submit the permit"}}, "filed the permit"},
	{V13TermClass{Canonical: "brief the sales team", Accept: []string{"brief the sales team"}}, "briefed the sales team"},
	{V13TermClass{Canonical: "migrate the accounts", Accept: []string{"migrate the accounts", "move the accounts over"}}, "migrated the accounts"},
	{V13TermClass{Canonical: "publish the release notes", Accept: []string{"publish the release notes", "post the release notes"}}, "published the release notes"},
	{V13TermClass{Canonical: "train the support staff", Accept: []string{"train the support staff"}}, "trained the support staff"},
	{V13TermClass{Canonical: "renew the insurance", Accept: []string{"renew the insurance"}}, "renewed the insurance"},
	{V13TermClass{Canonical: "archive the old records", Accept: []string{"archive the old records"}}, "archived the old records"},
	{V13TermClass{Canonical: "test the backup restore", Accept: []string{"test the backup restore", "run the restore test"}}, "tested the backup restore"},
	{V13TermClass{Canonical: "confirm the shipping date", Accept: []string{"confirm the shipping date"}}, "confirmed the shipping date"},
	{V13TermClass{Canonical: "review the contract terms", Accept: []string{"review the contract terms", "go over the contract terms"}}, "reviewed the contract terms"},
	{V13TermClass{Canonical: "create the test accounts", Accept: []string{"create the test accounts", "open the test accounts"}}, "created the test accounts"},
	{V13TermClass{Canonical: "print the welcome packs", Accept: []string{"print the welcome packs"}}, "printed the welcome packs"},
	{V13TermClass{Canonical: "call the references", Accept: []string{"call the references", "phone the references"}}, "called the references"},
	{V13TermClass{Canonical: "approve the budget line", Accept: []string{"approve the budget line", "sign off the budget line"}}, "approved the budget line"},
	{V13TermClass{Canonical: "hire the contractor", Accept: []string{"hire the contractor", "engage the contractor"}}, "hired the contractor"},
	{V13TermClass{Canonical: "close the old account", Accept: []string{"close the old account", "shut the old account"}}, "closed the old account"},
}

// Process modes.
const (
	v14ProcessUntouched = "untouched"
	v14ProcessReopened  = "reopened"
)

// Process event kinds, in the order records report them.
const (
	v14EvPlan     = "plan"
	v14EvDone     = "done"
	v14EvDropped  = "dropped"
	v14EvEarly    = "early"
	v14EvReopen   = "reopen"
	v14EvReassign = "reassign"
	v14EvNeutral  = "neutral"
)

type v14ProcessEvent struct {
	Kind string
	Step int
}

// v14Coin is a fair seeded coin. A bare v13Index(..., 2) reads the low bit of
// an FNV-1a hash, which is a fixed function of the salt's bytes, so two such
// coins drawn for one group would always agree or always differ.
func v14Coin(seed int64, salt string) bool {
	return v13Index(seed, salt, 1000) < 500
}

// v14Process is one multi-step process draw.
type v14Process struct {
	Steps      []int    // step class per position (V14ProcessStepClasses)
	Owners     []string // planned owner per position
	Decoy      int      // decoy workstream's step class
	Mode       string
	Answer     int // first open step, base reading
	Counter    int // first open step, counterfactual reading
	Flip       int // index into Log of the one record the counterfactual changes
	Reassign   int // step whose owner is reassigned
	ReassignTo string
	Log        []v14ProcessEvent // story order
}

// Owner returns the current owner of step i.
func (p *v14Process) Owner(i int) string {
	if i == p.Reassign {
		return p.ReassignTo
	}
	return p.Owners[i]
}

func (d *v13Draws) drawV14Process(group int, g *v13Group) *v14Process {
	seed := d.seed
	key := func(name string) string { return fmt.Sprintf("v14-proc-%s-%d", name, group) }
	n := 3 + v13Index(seed, key("n"), 3)
	p := &v14Process{}
	classes := v13Perm(seed, key("steps"), len(V14ProcessStepClasses))
	p.Steps = append([]int(nil), classes[:n]...)
	p.Decoy = classes[n]
	// The group's first two people stay the plan's first two owners; later
	// owners and the reassignment target are drawn only at v14.
	p.Owners = []string{g.People[0], g.People[1]}
	for len(p.Owners) < n {
		p.Owners = append(p.Owners, d.name())
	}
	p.ReassignTo = g.People[2]
	closed := 0 // steps 0..closed-1 are closed in the base reading
	early := -1
	if v14Coin(seed, key("mode")) {
		p.Mode = v14ProcessUntouched
		p.Answer = 2 + v13Index(seed, key("answer"), n-2)
		p.Counter = p.Answer - 1
		closed = p.Answer
		if p.Answer+1 < n && v14Coin(seed, key("early")) {
			early = p.Answer + 1 + v13Index(seed, key("early-step"), n-p.Answer-1)
		}
	} else {
		p.Mode = v14ProcessReopened
		p.Answer = 1 + v13Index(seed, key("answer"), n-2)
		p.Counter = p.Answer + 1
		closed = p.Answer + 1
		if p.Answer+2 < n && v14Coin(seed, key("early")) {
			early = p.Answer + 2 + v13Index(seed, key("early-step"), n-p.Answer-2)
		}
	}
	// One step before the counterfactual answer and the base answer is
	// dropped rather than done.
	dropped := -1
	if limit := min(p.Answer, p.Counter); limit >= 1 {
		dropped = v13Index(seed, key("dropped"), limit)
	}
	p.Reassign = p.Answer
	if v14Coin(seed, key("reassign")) {
		p.Reassign = p.Counter
	}
	// Story order: the plan, then the closures in step order with the early
	// finish, the reopen (after the answer's closure), the reassignment and the
	// neutral record inserted at seeded positions.
	log := []v14ProcessEvent{}
	for i := 0; i < closed; i++ {
		kind := v14EvDone
		if i == dropped {
			kind = v14EvDropped
		}
		log = append(log, v14ProcessEvent{Kind: kind, Step: i})
	}
	insert := func(ev v14ProcessEvent, from int, salt string) {
		at := from + v13Index(seed, key(salt), len(log)-from+1)
		log = append(log[:at], append([]v14ProcessEvent{ev}, log[at:]...)...)
	}
	if early >= 0 {
		insert(v14ProcessEvent{Kind: v14EvEarly, Step: early}, 0, "at-early")
	}
	if p.Mode == v14ProcessReopened {
		after := 0
		for i, ev := range log {
			if ev.Kind == v14EvDone && ev.Step == p.Answer {
				after = i + 1
			}
		}
		insert(v14ProcessEvent{Kind: v14EvReopen, Step: p.Answer}, after, "at-reopen")
	}
	insert(v14ProcessEvent{Kind: v14EvReassign, Step: p.Reassign}, 0, "at-reassign")
	insert(v14ProcessEvent{Kind: v14EvNeutral, Step: -1}, 0, "at-neutral")
	p.Log = append([]v14ProcessEvent{{Kind: v14EvPlan, Step: -1}}, log...)
	for i, ev := range p.Log {
		switch {
		case p.Mode == v14ProcessUntouched && ev.Kind == v14EvDone && ev.Step == p.Answer-1:
			p.Flip = i
		case p.Mode == v14ProcessReopened && ev.Kind == v14EvReopen:
			p.Flip = i
		}
	}
	return p
}

// renderV14Process renders the SN-356 multi-step process family.
func renderV14Process(d *v13Draws, schema v13Schema, group, variant int, g v13Group, counter bool, subject string, m *v13Member) {
	seed := d.seed
	p := g.T14.Proc
	neutral := m.Records[2]
	step := func(i int) v14ProcessStep { return V14ProcessStepClasses[p.Steps[i]] }
	pick := func(salt string, forms []string) string {
		return v13Pick(seed, fmt.Sprintf("v14-proc-%s-%d", salt, group), forms)
	}
	var records []string
	for i, ev := range p.Log {
		flipped := counter && i == p.Flip
		switch ev.Kind {
		case v14EvPlan:
			parts := make([]string, len(p.Steps))
			for k := range p.Steps {
				parts[k] = fmt.Sprintf("(%d) %s, %s %s", k+1, step(k).Canonical, schema.Responsible, p.Owners[k])
			}
			records = append(records, fmt.Sprintf(pick("plan", []string{
				"The %[2]s sequence on %[1]s runs strictly in this order: %[3]s.",
				"%[1]s's %[2]s steps, in the order they must happen: %[3]s.",
				"For %[1]s the %[2]s plan is, in order, %[3]s.",
			}), g.Alias, schema.Action, strings.Join(parts, "; ")))
		case v14EvDone, v14EvEarly:
			s := step(ev.Step)
			if flipped {
				records = append(records, fmt.Sprintf(pick("open", []string{
					"%[2]s has started to %[3]s on %[1]s but has not finished; that step is still open.",
					"On %[1]s the step to %[3]s is under way with %[2]s and not yet complete.",
				}), g.Alias, p.Owner(ev.Step), s.Canonical))
				continue
			}
			forms := []string{
				"Update on %[1]s: %[2]s %[3]s, so that step is closed.",
				"%[2]s %[3]s for %[1]s; that step is complete.",
				"On %[1]s, %[2]s has now %[3]s.",
			}
			if ev.Kind == v14EvEarly {
				forms = []string{
					"Ahead of the plan, %[2]s already %[3]s for %[1]s.",
					"%[2]s %[3]s for %[1]s early, out of turn; that step is complete.",
				}
			}
			records = append(records, fmt.Sprintf(pick(fmt.Sprintf("%s-%d", ev.Kind, ev.Step), forms), g.Alias, p.Owner(ev.Step), s.Done))
		case v14EvDropped:
			records = append(records, fmt.Sprintf(pick("dropped", []string{
				"The step to %[2]s on %[1]s has been dropped: it is no longer needed and counts as closed.",
				"We are skipping the step to %[2]s on %[1]s altogether; treat it as closed.",
			}), g.Alias, step(ev.Step).Canonical))
		case v14EvReopen:
			if flipped {
				records = append(records, fmt.Sprintf(pick("stays-closed", []string{
					"The step to %[2]s on %[1]s was checked again and stays closed.",
					"A second look at the step to %[2]s on %[1]s confirmed it is finished; it stays closed.",
				}), g.Alias, step(ev.Step).Canonical))
				continue
			}
			records = append(records, fmt.Sprintf(pick("reopen", []string{
				"The step to %[2]s on %[1]s has been reopened: it has to be done again.",
				"On %[1]s the step to %[2]s failed its check and is open again.",
			}), g.Alias, step(ev.Step).Canonical))
		case v14EvReassign:
			records = append(records, fmt.Sprintf(pick("reassign", []string{
				"Under this batch's %[5]s, the %[4]s for the step to %[2]s on %[1]s is now %[3]s.",
				"%[5]s: %[3]s takes over as %[4]s for the step to %[2]s on %[1]s.",
			}), g.Alias, step(ev.Step).Canonical, p.ReassignTo, schema.Responsible, schema.Correction))
		case v14EvNeutral:
			records = append(records, neutral)
		}
	}
	v14SetRecords(m, records)
	decoy := V14ProcessStepClasses[p.Decoy]
	m.DecoyClause = fmt.Sprintf(" Its %s is to %s, owned by %s.", schema.Action, decoy.Canonical, g.People[3])
	m.Question = v13Question(seed, group, variant, []string{
		"following the recorded order of its %[2]s steps, which step on %[1]s is the first one still open, and who is its %[3]s now?",
		"for %[1]s, name the earliest %[2]s step in the planned order that is not yet closed, and the person currently named %[3]s for it.",
		"which %[2]s step on %[1]s comes first in the plan among those still open, and who owns it after every recorded change?",
	}, []string{"Give both the step and the name.", "Two parts: the step, then the person.", "Answer with the action and the owner."}, subject, schema.Action, schema.Responsible)
	answer := p.Answer
	if counter {
		answer = p.Counter
	}
	want, owner := step(answer), p.Owner(answer)
	m.Kind = protocol.AnswerList
	m.Expected = want.Canonical + "; " + owner
	m.Items = []string{want.Canonical, owner}
	m.ItemKinds = []string{"", ""}
	m.ItemAccept = [][]string{append([]string(nil), want.Accept...), {owner}}
	// Every other step of the process is a wrong same-attribute action, so a
	// reply that lists the plan instead of naming one step asserts them.
	for i := range p.Steps {
		if i != answer {
			m.Distractors = append(m.Distractors, step(i).Canonical)
		}
	}
	m.Distractors = append(m.Distractors, g.People[3], decoy.Canonical)
	m.Claims = []protocol.Claim{
		v13Claim(protocol.ClaimKindAction, want.Canonical, want.Accept, 0.5),
		v13Claim(protocol.ClaimKindPerson, owner, []string{owner}, 0.5),
	}
	resolve := V10QueryNode{Op: "resolve_entity", Field: schema.Alias}
	m.Program = V10QueryNode{Op: "first_open_step", Field: schema.Action, Children: []V10QueryNode{
		resolve, {Op: "fold_step_states", Field: schema.Action}, {Op: "latest", Field: schema.Responsible, Children: []V10QueryNode{resolve}},
	}}
	m.Protected = append(m.Protected, schema.Action, schema.Responsible, p.ReassignTo, g.People[3], decoy.Canonical)
	for i := range p.Steps {
		m.Protected = append(m.Protected, step(i).Canonical, step(i).Done, p.Owners[i])
	}
	m.Facts = []string{"per-run schema glossary", "entity alias", fmt.Sprintf("ordered plan of %d steps", len(p.Steps)), "step state changes", "responsibility reassignment"}
	m.Constraints = []string{g.Alias, schema.Action, schema.Responsible, schema.Correction, g.Purpose}
	m.Operations = []string{"induce schema", "resolve subject by remit", "fold each step's state across the records", "select the first open step in plan order", "select its current owner", "return both"}
}
