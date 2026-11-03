package universe

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v14 supersession chains (SN-353) with cue placement (SN-354).
//
// Every v13 correction family has one superseding record, and that record
// announces itself ("the newest entry governs", "replacing whoever held it
// before"). With v14's story-order stamps, the latest timestamp and the cue
// sentence point to the same row. A model-free inverse can therefore answer by
// finding the cue sentence or reading record[1], without tracking state.
//
// From v14 the owner, standing-status and current-channel business families
// and six personal domains render a short chain instead. After the initial
// record come 2 or 3 more state records (capped at 3 hops for the first
// qualification), and every chain has at least one of:
//
//   - a revert to an earlier value (A, then B, then A again);
//   - a proposed change that was not adopted;
//   - an entry logged in error, so the value before it stands.
//
// The causal counterfactual flips exactly one link of the same chain: whether
// a proposal was adopted, whether an entry was confirmed or logged in error,
// or the value of the last change. Only the governing value moves.
//
// Cue placement is seeded per group. Half of the groups announce every adopted
// change as v13 did. A quarter state every change plainly, with no change
// vocabulary at all, so precedence comes only from the case file's story-order
// filing dates. The last quarter misplace the cues: the change vocabulary sits
// on records that do not govern (a superseded change, a proposal that was
// later turned down), while the governing record is plain. Fresh wording alone
// failed qualification (#1994); this changes where a cue sits relative to the
// answer, not which words are used.
//
// The oracle folds the chain (v14ChainFold), and a test proves that the
// position, latest-filing and cue-sentence heuristics no longer recover the
// answer.

// Chain step operations.
const (
	v14OpSet      = 'S' // an adopted change to Val
	v14OpPropose  = 'P' // a proposed change to Val, not yet decided
	v14OpWithdraw = 'W' // the pending proposal was not adopted
	v14OpAdopt    = 'A' // the pending proposal was adopted
	v14OpError    = 'E' // the last adopted change was logged in error
	v14OpConfirm  = 'C' // the last adopted change was checked and stands
)

// v14ChainStep is one state record after the initial value. Val indexes the
// group's chain values; for W, A, E and C it names the value the record refers
// to, so the record is self-contained.
type v14ChainStep struct {
	Op  byte
	Val int
}

// v14ChainTemplate is a chain shape with the counterfactual it flips. Values
// counts the distinct chain values used across base and counterfactual (value
// 0 is the initial value).
type v14ChainTemplate struct {
	Name    string
	Kind    string // revert, withdrawn, or error
	Steps   []v14ChainStep
	Counter []v14ChainStep
	Values  int
}

// v14ChainTemplates. Each has 2 or 3 steps after the initial record, and its
// counterfactual differs from it in exactly one step.
var v14ChainTemplates = []v14ChainTemplate{
	{Name: "revert", Kind: "revert", Values: 4,
		Steps:   []v14ChainStep{{v14OpSet, 1}, {v14OpSet, 0}},
		Counter: []v14ChainStep{{v14OpSet, 1}, {v14OpSet, 3}}},
	{Name: "revert-to-middle", Kind: "revert", Values: 4,
		Steps:   []v14ChainStep{{v14OpSet, 1}, {v14OpSet, 2}, {v14OpSet, 1}},
		Counter: []v14ChainStep{{v14OpSet, 1}, {v14OpSet, 2}, {v14OpSet, 3}}},
	{Name: "change-then-declined-proposal", Kind: "withdrawn", Values: 3,
		Steps:   []v14ChainStep{{v14OpSet, 1}, {v14OpPropose, 2}, {v14OpWithdraw, 2}},
		Counter: []v14ChainStep{{v14OpSet, 1}, {v14OpPropose, 2}, {v14OpAdopt, 2}}},
	{Name: "declined-proposal", Kind: "withdrawn", Values: 2,
		Steps:   []v14ChainStep{{v14OpPropose, 1}, {v14OpWithdraw, 1}},
		Counter: []v14ChainStep{{v14OpPropose, 1}, {v14OpAdopt, 1}}},
	{Name: "change-logged-in-error", Kind: "error", Values: 2,
		Steps:   []v14ChainStep{{v14OpSet, 1}, {v14OpError, 1}},
		Counter: []v14ChainStep{{v14OpSet, 1}, {v14OpConfirm, 1}}},
	{Name: "second-change-logged-in-error", Kind: "error", Values: 3,
		Steps:   []v14ChainStep{{v14OpSet, 1}, {v14OpSet, 2}, {v14OpError, 2}},
		Counter: []v14ChainStep{{v14OpSet, 1}, {v14OpSet, 2}, {v14OpConfirm, 2}}},
}

// v14ChainFold returns the value that stands after the chain, starting from
// value 0, and the index of the record that establishes it (-1 for the
// initial record; otherwise a step index).
func v14ChainFold(steps []v14ChainStep) (value, governing int) {
	type state struct{ value, record int }
	history := []state{{0, -1}}
	pending, pendingAt := -1, -1
	for i, s := range steps {
		switch s.Op {
		case v14OpSet:
			history = append(history, state{s.Val, i})
		case v14OpPropose:
			pending, pendingAt = s.Val, i
		case v14OpAdopt:
			if pending >= 0 {
				history = append(history, state{pending, pendingAt})
			}
			pending = -1
		case v14OpWithdraw:
			pending = -1
		case v14OpError:
			if len(history) > 1 {
				history = history[:len(history)-1]
			}
		case v14OpConfirm:
		}
	}
	top := history[len(history)-1]
	return top.value, top.record
}

// Cue modes.
const (
	v14CueAnnounced = "announced"
	v14CueFree      = "cue-free"
	v14CueMisplaced = "misplaced"
)

// v14ChainPlan is a group's chain: the template and the seeded cue mode.
type v14ChainPlan struct {
	Template v14ChainTemplate
	Cue      string
	// UnnamedRevert renders a revert as "back to the original / previous
	// entry" without restating the value, so the newest record that names a
	// value is not the answer. The idea is ported from a generator-competition
	// miner's revert family (generator D; docs/v14-generator-credits.md).
	UnnamedRevert bool
}

// drawV14ChainPlan picks a template that fits the number of distinct values
// the family can supply and the group's cue mode. Deterministic in (seed,
// domain, group).
func drawV14ChainPlan(seed int64, domain string, group, maxValues int) v14ChainPlan {
	var fit []v14ChainTemplate
	for _, t := range v14ChainTemplates {
		if t.Values <= maxValues {
			fit = append(fit, t)
		}
	}
	t := fit[v13Index(seed, fmt.Sprintf("v14-chain-%s-%d", domain, group), len(fit))]
	cue := v14CueAnnounced
	switch v13Index(seed, fmt.Sprintf("v14-cue-%s-%d", domain, group), 4) {
	case 2:
		cue = v14CueFree
	case 3:
		cue = v14CueMisplaced
	}
	unnamed := v13Index(seed, fmt.Sprintf("v14-revert-unnamed-%s-%d", domain, group), 2) == 1
	return v14ChainPlan{Template: t, Cue: cue, UnnamedRevert: unnamed}
}

// v14ChainVoice is how one family talks about its chained attribute.
type v14ChainVoice struct {
	// Subject is the subject-attribute phrase, e.g. "the owner on <alias>".
	Subject string
	// Copula joins Subject to a value: "is", or "is to" for a plan.
	Copula string
	// Value renders chain value i for record salt.
	Value func(i int, salt string) string
}

var (
	v14PlainForms = []string{
		"%[1]s %[3]s %[2]s.",
		"For the record, %[1]s %[3]s %[2]s.",
		"Noting that %[1]s %[3]s %[2]s.",
	}
	v14AnnouncedForms = []string{
		"Update: %[1]s %[3]s now %[2]s, replacing the earlier entry.",
		"From now on %[1]s %[3]s %[2]s instead; the newest entry governs.",
		"Change logged: %[1]s %[3]s now %[2]s, and the previous entry no longer stands.",
	}
	v14ProposeForms = []string{
		"There is a proposal to change %[1]s to %[2]s.",
		"Someone suggested changing %[1]s to %[2]s.",
		"A request came in to switch %[1]s to %[2]s.",
	}
	v14ProposeCuedForms = []string{
		"Draft update for sign-off: %[1]s %[3]s now %[2]s, replacing the current entry.",
		"Proposed change, pending agreement: from now on %[1]s would be %[2]s instead.",
		"Update requested: %[1]s to become %[2]s, superseding what is on file.",
	}
	v14WithdrawForms = []string{
		"The proposal to change %[1]s to %[2]s was turned down.",
		"We did not go ahead with changing %[1]s to %[2]s.",
		"The request to switch %[1]s to %[2]s was dropped.",
	}
	v14AdoptForms = []string{
		"The proposal to change %[1]s to %[2]s was accepted.",
		"We went ahead with changing %[1]s to %[2]s.",
		"The request to switch %[1]s to %[2]s was approved.",
	}
	v14ErrorForms = []string{
		"The note that %[1]s %[3]s %[2]s was logged in error and never took effect.",
		"Disregard the entry saying %[1]s %[3]s %[2]s; it was a mistake.",
		"That entry about %[1]s being %[2]s was wrong and has been struck.",
	}
	// Unnamed reverts: the subject goes back to the original (first) entry
	// or to the entry before the last change, and the value is not restated.
	v14RevertOriginalForms = []string{
		"%[1]s went back to what it was originally.",
		"%[1]s is back to the original entry.",
		"%[1]s returned to the first arrangement on file.",
	}
	v14RevertOriginalCuedForms = []string{
		"Update: %[1]s goes back to the original entry, replacing the change.",
		"From now on %[1]s reverts to the original entry instead; the newest entry governs.",
		"Change logged: %[1]s is back to the original, and the later change no longer stands.",
	}
	v14RevertPreviousForms = []string{
		"%[1]s went back to the entry before the last change.",
		"%[1]s is back to what it was before the most recent change.",
		"%[1]s returned to the previous arrangement.",
	}
	v14RevertPreviousCuedForms = []string{
		"Update: %[1]s goes back to the previous entry, replacing the last change.",
		"From now on %[1]s reverts to the previous entry instead; the newest entry governs.",
		"Change logged: %[1]s is back to the previous entry, and the last change no longer stands.",
	}
	v14ConfirmForms = []string{
		"The note that %[1]s %[3]s %[2]s was checked and stands.",
		"Double-checked the entry saying %[1]s %[3]s %[2]s; it is right.",
		"That entry about %[1]s being %[2]s was verified and holds.",
	}
)

// v14ChainRow is one rendered state record and the chain value it mentions.
// Cued reports whether it carries change vocabulary. Tests use the rows to
// prove that no surface heuristic recovers the answer.
type v14ChainRow struct {
	Text  string
	Value int
	Cued  bool
	// Named is false for an unnamed revert: the record states no value.
	Named bool
}

// renderV14Chain renders the initial record plus one record per step. The
// initial record is supplied by the family (its v13 first-record wording);
// salt keys every seeded choice so base and counterfactual readings of a
// group pick identical wording for every step they share.
func renderV14Chain(seed int64, salt string, plan v14ChainPlan, steps []v14ChainStep, voice v14ChainVoice, initial string) []v14ChainRow {
	// Cue placement follows the BASE chain's governing record, so flipping
	// one link in the counterfactual never moves a cue onto another record.
	_, governing := v14ChainFold(plan.Template.Steps)
	rows := []v14ChainRow{{Text: initial, Value: 0, Named: true}}
	adopted := []int{0} // values in force, newest last
	pending := -1
	for i, s := range steps {
		forms, cued, named := v14PlainForms, false, true
		switch s.Op {
		case v14OpSet:
			announce := plan.Cue == v14CueAnnounced || (plan.Cue == v14CueMisplaced && i != governing)
			if announce {
				forms, cued = v14AnnouncedForms, true
			}
			if plan.UnnamedRevert && len(adopted) > 1 {
				switch {
				case s.Val == adopted[0]:
					forms, named = v14RevertOriginalForms, false
					if announce {
						forms = v14RevertOriginalCuedForms
					}
				case s.Val == adopted[len(adopted)-2]:
					forms, named = v14RevertPreviousForms, false
					if announce {
						forms = v14RevertPreviousCuedForms
					}
				}
			}
		case v14OpPropose:
			forms = v14ProposeForms
			if plan.Cue == v14CueMisplaced {
				forms, cued = v14ProposeCuedForms, true
			}
		case v14OpWithdraw:
			forms = v14WithdrawForms
		case v14OpAdopt:
			forms = v14AdoptForms
		case v14OpError:
			forms = v14ErrorForms
		case v14OpConfirm:
			forms = v14ConfirmForms
		}
		// The wording index depends on the step position and op, never on the
		// counterfactual flag, so a flipped step is the only record that moves.
		key := fmt.Sprintf("%s-step-%d-%c", salt, i, s.Op)
		form := v13Pick(seed, key, forms)
		var text string
		if named {
			text = fmt.Sprintf(form, voice.Subject, voice.Value(s.Val, key), voice.Copula)
		} else {
			text = fmt.Sprintf(form, voice.Subject)
		}
		rows = append(rows, v14ChainRow{Text: v13Capitalize(text), Value: s.Val, Cued: cued, Named: named})
		switch s.Op {
		case v14OpSet:
			adopted = append(adopted, s.Val)
		case v14OpPropose:
			pending = s.Val
		case v14OpAdopt:
			if pending >= 0 {
				adopted = append(adopted, pending)
			}
			pending = -1
		case v14OpWithdraw:
			pending = -1
		case v14OpError:
			if len(adopted) > 1 {
				adopted = adopted[:len(adopted)-1]
			}
		}
	}
	return rows
}

// v14ChainOperations describes the fold in provenance.
func v14ChainOperations(plan v14ChainPlan) []string {
	return []string{"resolve the subject", "order the records by filing date", "fold the " + plan.Template.Kind + " chain", "return the value that stands"}
}

// v14ChainProgram is the provenance program of a chained member.
func v14ChainProgram(field string, resolve V10QueryNode) V10QueryNode {
	return V10QueryNode{Op: "fold_supersession", Field: field, Children: []V10QueryNode{resolve}}
}

// v14ChainUsesChains reports whether a contract renders supersession chains.
func v14ChainUsesChains(benchVersion int) bool {
	return benchVersion >= protocol.BenchVersionV14
}

// v14ChainValues returns the distinct chain values a template uses, in order.
func v14ChainValues(steps []v14ChainStep) []int {
	seen := map[int]bool{0: true}
	out := []int{0}
	for _, s := range steps {
		if !seen[s.Val] {
			seen[s.Val] = true
			out = append(out, s.Val)
		}
	}
	return out
}

// v14ChainApply replaces a member's state records with the rendered chain:
// Records[0] and Records[1] carry the first two rows, ChainExtra the rest.
// Records[2] keeps the neutral record.
func (m *v13Member) v14ChainApply(rows []v14ChainRow) {
	m.Records[0] = rows[0].Text
	m.Records[1] = rows[1].Text
	m.ChainExtra = nil
	for _, r := range rows[2:] {
		m.ChainExtra = append(m.ChainExtra, r.Text)
	}
	m.chain = rows
}

// v14ChainFacts names the evidence a chained member needs.
func v14ChainFacts(plan v14ChainPlan, subject string) []string {
	return []string{subject, "initial record", strings.ReplaceAll(plan.Template.Name, "-", " ") + " chain", "story-order filing dates"}
}
