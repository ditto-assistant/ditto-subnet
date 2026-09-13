package universe

import (
	"fmt"
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 world surfaces. Every version through 12 rendered the project and
// person identity records from one fixed frame each, and every ordinary and
// story question from four fixed phrasings selected by index. A parser holding
// the repository recovered role bindings from those frames without reading. v13
// renders each of them from a persona.Grammar drawn from a per-seed bank:
//
//   - the project identity record permutes its role clauses (alias/formal
//     equivalence, internal owner, vendor, AP record), may omit the vendor
//     clause (the vendor is restated in the business paste and never graded
//     from this record), and may state the alias and formal name as
//     interchangeable rather than as "when I say X I mean Y";
//   - the person identity record varies how the nickname is attributed;
//   - every question frame keeps the declared constraints (the entities the
//     validator proves resolve to one subject) while the framing varies.
//
// People, projects, trips, stories, money, pair identities, oracles, and every
// graded value are untouched; validatePlan still proves each rendered question
// answerable, so a frame that leaks or shortcuts is rejected at generation.

func (w World) v13Surface() bool { return w.BenchVersion >= protocol.BenchVersionV13 }

func (w World) v13Rand(parts ...string) *rand.Rand {
	return persona.HashRand(w.Seed, append([]string{"v13-world-surface"}, parts...)...)
}

func (w World) v13Bank(surface string, g persona.Grammar) persona.Grammar {
	return persona.SeedBank(w.Seed, "world:"+surface, g)
}

var v13PersonIdentityGrammar = persona.Grammar{
	"root": {
		"#lead##name# is my #relation#. Everyone there calls them “#nickname#.”",
		"My #relation#, #name#, goes by “#nickname#” with everyone there.",
		"“#nickname#” is what everyone calls #name#, my #relation#.",
		"#lead##name# — my #relation# — is “#nickname#” to everyone who knows them.",
		"#lead#you'll hear #name# called “#nickname#” by everyone; they're my #relation#.",
		"#lead#for the record, #name# is my #relation#, and the whole crowd calls them “#nickname#.”",
	},
	"lead": {"", "", "Oh, ", "So — ", "Quick one: ", "By the way, ", "Right, "},
}

// v13ProjectClauseGrammar holds the four project-identity role clauses. The
// renderer permutes clause order per record and drops the vendor clause on a
// share of records.
var v13ProjectClauseGrammar = persona.Grammar{
	"clause-equivalence": {
		"When I say #aliasq# I mean #name# for #client#, not the similarly named client work.",
		"#aliasq# is just my shorthand for #name#, the #client# engagement — don't confuse it with the lookalike client job.",
		"#name# and #aliasq# are the same project, the one for #client#; the similarly named client work is separate.",
		"Internally we call #name# #aliasq#. It's the #client# work, not the similarly named job for another client.",
		"Treat #aliasq# and #name# as one and the same — our project for #client# — and keep the near-namesake client work apart.",
	},
	"clause-owner": {
		"#lead# owns it internally.",
		"The internal owner is #lead#.",
		"#lead# runs it on our side.",
		"Internally it sits with #lead#.",
	},
	"clause-vendor": {
		"#vendor# is the vendor.",
		"The vendor on it is #vendor#.",
		"Vendor-side it's #vendor#.",
	},
	"clause-record": {
		"The AP record is #record#.",
		"Its accounts-payable record is #record#.",
		"AP filed it under #record#.",
		"The payables reference is #record#.",
	},
}

// v13VendorOmitPercent is the share of project identity records that leave the
// vendor clause out; the business paste restates every project's vendor line.
const v13VendorOmitPercent = 40

func (w World) v13PersonIdentity(p Person, index int) string {
	r := w.v13Rand("person-identity", fmt.Sprint(index))
	return persona.ExpandSlots(r, w.v13Bank("person-identity", v13PersonIdentityGrammar), "root", map[string]string{
		"name": p.Name, "relation": p.Relation, "nickname": p.Nickname,
	})
}

func (w World) v13ProjectIdentity(p Project, lead Person, index int) string {
	r := w.v13Rand("project-identity", fmt.Sprint(index))
	g := persona.WithSlots(w.v13Bank("project-identity", v13ProjectClauseGrammar), map[string]string{
		"aliasq": "“" + p.Alias + "”", "name": p.Name, "client": p.Client,
		"lead": lead.Name, "vendor": p.Vendor, "record": p.RecordID,
	})
	// Clause symbols are namespaced so a slot (vendor, record, lead) never
	// shadows the clause that renders it.
	clauses := []string{"clause-equivalence", "clause-owner", "clause-vendor", "clause-record"}
	if r.Intn(100) < v13VendorOmitPercent {
		clauses = []string{"clause-equivalence", "clause-owner", "clause-record"}
	}
	order := r.Perm(len(clauses))
	parts := make([]string, 0, len(clauses))
	for _, i := range order {
		parts = append(parts, persona.Expand(r, g, clauses[i]))
	}
	return strings.Join(parts, " ")
}

// ── Question frames ─────────────────────────────────────────────────────────

var v13QuestionGrammars = map[string]persona.Grammar{
	oracleContactCurrent: {
		"root": {
			"For the #context# follow-up, what email should I actually use now for #name# at #employer#? #verify#",
			"I need to reach #name# at #employer# about the #context#. Which email is current? #verify#",
			"Which up-to-date email belongs to #name# at #employer#, the person from the #context#? #verify#",
			"What is the corrected email for #name# at #employer#? This is for the #context# follow-up. #verify#",
			"Before I send the #context# note: what's the right, current email for #name# over at #employer#? #verify#",
			"#name# at #employer# — the #context# contact — which email reaches them these days? #verify#",
		},
		"verify": {
			"I want to avoid sending the note to an inbox nobody checks anymore, so please double-check before I hit send.",
			"I remember we had to replace an older one, and I would rather verify than have this disappear.",
			"I am pulling together the final details before I send anything and want to make sure it reaches them.",
			"I do not want the message disappearing into their old workplace. Please check the latest one.",
			"Confirm it against the correction we saved rather than the first address I had.",
		},
	},
	oracleContactPrevious: {
		"root": {
			"Before #nickname# changed addresses, which email had I saved for my #relation# in #city# from the #context#?",
			"What was the earlier email for my #relation# in #city# I call #nickname# from the #context#, before the contact correction?",
			"I need the pre-correction email for #nickname# — my #relation# who handled the #context# in #city#. What was it?",
			"Looking back before the update, which email did I first have for #nickname#, my #relation# from the #context# who lives in #city#?",
			"For #nickname#, my #relation# from the #context#: what was the old email, the one from before they moved? They're in #city#.",
			"Which address did I originally store for #nickname# — the #relation# in #city# from the #context# — prior to the fix?",
		},
	},
	oracleProjectOutstanding: {
		"root": {
			"For #aliasq#, the #purpose# work for #client#, what is still owed to #vendor# once the approved correction and the payment already sent are reconciled?",
			"AP needs the remaining balance for #vendor#'s invoice on #aliasq# for #client#. Use the corrected total, not the draft, and account for our payment.",
			"What remains on the corrected #vendor# bill tied to #aliasq#, the #purpose# project for #client#, after what we already paid?",
			"Reconcile #aliasq# for #client#: after replacing the original #vendor# invoice figure with the approved one and subtracting the partial payment, what balance remains?",
			"How much do we still owe #vendor# on #aliasq# (#client#, #purpose#)? Corrected invoice minus what's been paid, please.",
			"On the #client# project we call #aliasq#, what's left to pay #vendor# after the approval correction and our partial payment?",
		},
	},
	oracleProjectLeadCurrent: {
		"root": {
			"Who should get the #aliasq# handoff internally, and what current email should I use? I mean the #purpose# work for #client#, not an outside recipient.",
			"For #client#'s #purpose# project that we call #aliasq#, give me the corrected email for its internal owner.",
			"What up-to-date email belongs to the person running #aliasq# on our side — #client#'s #purpose# engagement?",
			"I am sending the #aliasq# update. Resolve the internal owner from the #purpose# work for #client#, then use their current rather than original email.",
			"Who owns #aliasq# internally — the #purpose# work for #client# — and what's their present email, post-correction?",
			"Current email for whoever leads #aliasq# on our side, please; that's the #client# #purpose# project.",
		},
	},
	oracleProjectLeadPrevious: {
		"root": {
			"Before the address correction, what email did I have for the internal lead on #aliasq#, the #purpose# work for #client#?",
			"Find the earlier email for whoever owns #aliasq#, our #purpose# project for #client# — not their current one.",
			"What was the original email for the internal owner of the #purpose# project for #client# that we call #aliasq#?",
			"Looking back before the update, which email was saved for #aliasq#'s internal owner on the #purpose# work for #client#?",
			"For #aliasq# — #client#, #purpose# — what old address did I have for its internal lead before they moved?",
			"Which pre-correction email belonged to the person who runs #aliasq#, the #purpose# engagement for #client#?",
		},
	},
	oracleTripCurrent: {
		"root": {
			"How many days is #alias# now — the #purpose# trip we took in #when# through #c0#, #c1#, and #c2# — after the change?",
			"We changed the #changed# part of #alias#, our #purpose# trip from #when#. How many days is the whole trip now?",
			"Can you piece together the updated stays for #alias#, our #purpose# trip from #when#? How long is the trip altogether now?",
			"Remind me how long #alias# is now — the #purpose# trip from #when# — after we changed the #changed# stay.",
			"With the revised #changed# leg included, what's the full length of #alias#, the #purpose# trip from #when#?",
			"Add up the current legs of #alias# (#purpose#, #when#): how many days in total after the update?",
		},
	},
	oracleTripChangedLegPrevious: {
		"root": {
			"Before we changed one of the stays on #alias#, our #purpose# trip from #when#, how many days had we planned for that stay?",
			"Thinking back to the first version of #alias# — the #purpose# trip from #when# — how long was the stay we later changed?",
			"How many days had we originally planned for the stay we later revised on #alias#, our #purpose# trip from #when#?",
			"In our first plan for #alias#, the #purpose# trip from #when#, how long was the stay that eventually changed?",
			"On #alias#, the #purpose# trip from #when#, one leg got revised later. What was its original length in days?",
			"For the leg of #alias# we ended up changing — #purpose# trip, #when# — how many days did the first plan give it?",
		},
	},
	oracleTripChangedLegCurrent: {
		"root": {
			"On #alias#, the #purpose# trip from #when#, how many days are we spending in #country# after the change?",
			"How long is the updated stay in #country# for #alias#, the #purpose# trip from #when#?",
			"For #alias#, our #purpose# trip from #when#, how many days is the changed #country# stay now?",
			"After changing the #country# part of #alias#, our #purpose# trip from #when#, how many days are we spending there?",
			"What's the current length of the #country# leg on #alias# (#purpose#, #when#) now that it's been revised?",
			"Post-revision, how many days does #alias# — the #purpose# trip from #when# — give us in #country#?",
		},
	},
	oracleTripLongestCurrent: {
		"root": {
			"After changing the #changed# stay, what is the longest amount of time we spend in any one country on #alias#, our #purpose# trip from #when#?",
			"Looking across the updated plan for #alias#, the #purpose# trip from #when#, how many days is our longest stay?",
			"Once the #changed# change is included, what is the longest stay on #alias#, our #purpose# trip from #when#?",
			"For #alias# in #when#, our #purpose# trip with the changed #changed# stay, how many days is the longest stop?",
			"With the #changed# revision applied, which stop on #alias# — the #purpose# trip from #when# — is longest, in days?",
			"On the current version of #alias# (#purpose#, #when#), how many days is the single longest stay after the #changed# change?",
		},
	},
}

// v13Question renders one ordinary world question frame for an oracle. The
// draw is a per-(seed, oracle, index) hash so adding or reordering candidates
// never shifts another question's frame.
func (w World) v13Question(kind string, index int, slots map[string]string) string {
	g, ok := v13QuestionGrammars[kind]
	if !ok {
		panic("v13 question grammar missing for " + kind)
	}
	r := w.v13Rand("question", kind, fmt.Sprint(index))
	return persona.ExpandSlots(r, w.v13Bank("question:"+kind, g), "root", slots)
}

// ── Story anchor and task frames ────────────────────────────────────────────

var v13StoryAnchorGrammar = persona.Grammar{
	"root": {
		"Think back to the #alias# with #nickname#.",
		"Go back to when #nickname# and I were talking about the #alias#.",
		"This is about the #alias#, the one I planned with #nickname#.",
		"Remember my conversation with #nickname# about the #alias#?",
		"I mean the thread with #nickname# that began around the #alias#.",
		"You know the #alias# — the one #nickname# and I put together?",
		"Back to the #alias# and everything that started with #nickname#.",
		"Take the #alias# I did with #nickname# and the mess that followed.",
		"About the #alias#: the chapter #nickname# and I kicked off.",
		"Cast your mind back to #nickname#, the #alias#, and what came after.",
		"Same story as the #alias# — the one with #nickname#.",
		"Pick up the #alias# thread, the one that ran through #nickname#.",
	},
}

var v13StoryTaskGrammars = map[string]persona.Grammar{
	oracleStoryBalanceCurrent: {
		"root": {
			"Where did the available budget land after everything?",
			"What is the actual amount we have left now?",
			"After all the changes and payments, what remains?",
			"Can you work out the final available balance for me?",
			"Once #everything# is counted, what's left in the budget?",
			"Net of #everything#, how much do we actually have available now?",
			"Reconcile #everything# — what's the balance we ended up with?",
			"What's the bottom line on the budget after #everything#?",
		},
		"everything": {"every change and payment", "the correction, the payment, the extra charge, and the credit", "all the later movements", "the whole sequence of adjustments"},
	},
	oracleStoryBudgetDelta: {
		"root": {
			"How much was the budget correction itself?",
			"What was the size of the later budget change on its own?",
			"How much did the approval correction add or remove?",
			"What amount did finance change the budget by?",
			"Just the correction: by how much did finance move the envelope?",
			"Setting aside payments and costs, what was the budget adjustment worth?",
			"What was the magnitude of the approval change to the budget, on its own?",
		},
	},
	oracleStoryPostApproval: {
		"root": {
			"What balance did we have after the corrected approval and first payment, before the later charges?",
			"How much was left after the approved change and payment, but before the extra cost and credit?",
			"What was available at the middle point, right after the revised approval and first payment?",
			"Before the last expense and credit arrived, what balance were we working with?",
			"Take the corrected budget, subtract the first payment, and stop there — what's that figure?",
			"Where did the balance stand just after the approval correction and the initial payment, ignoring what came later?",
		},
	},
	oracleStoryLaterNetChange: {
		"root": {
			"Taken together, did the later correction, cost, and credit raise or lower the balance, and by how much?",
			"What was the net effect of the later budget change, expense, and credit? Give me the direction and amount.",
			"Across those final three changes, did we end up gaining or losing money, and how much?",
			"Did the follow-up changes move the balance up or down overall, and by how much?",
			"Combine the correction, the surprise charge, and the credit: which way did the balance go, and by what amount?",
			"Net out the three later movements for me — direction first, then the amount.",
		},
	},
	oracleStoryContactCurrent: {
		"root": {
			"What email should I use now?",
			"Which email is the right one now?",
			"Where should I send the note?",
			"What email did we end up using?",
			"Which review address is current after the switch?",
			"If I write today, what address does the note go to?",
			"What's the replacement review inbox we were told to use?",
		},
	},
	oracleStoryLesson: {
		"root": {
			"What advice did I take from the whole mess?",
			"What did I say I would do differently next time?",
			"What practical lesson did I learn from this?",
			"What was the rule I wanted to remember afterward?",
			"What takeaway did I write down once it was over?",
			"Which piece of advice stuck with me from that episode?",
			"What did I resolve to do next time, in my own words?",
		},
	},
	oracleStoryOutcomeSummary: {
		"root": {
			"Can you give me the current reviewer email, final available balance, and the advice I wrote down?",
			"Remind me of the final reviewer email, the balance we landed on, and the lesson from it.",
			"Where did this leave us: which email, how much money, and what rule for next time?",
			"Pull together the current contact, the final amount, and what I learned from the experience.",
			"Three things: the review address we use now, the money left, and the takeaway.",
			"Sum it up — the current inbox, the closing balance, and the lesson I noted.",
		},
	},
}

func (w World) v13StoryAnchor(r *rand.Rand, person Person, trip Trip) (string, []string) {
	text := persona.ExpandSlots(r, w.v13Bank("story-anchor", v13StoryAnchorGrammar), "root", map[string]string{
		"alias": trip.Alias, "nickname": person.Nickname,
	})
	return text, []string{trip.Alias, person.Nickname}
}

func (w World) v13StoryTask(r *rand.Rand, kind string) string {
	g, ok := v13StoryTaskGrammars[kind]
	if !ok {
		panic("v13 story task grammar missing for " + kind)
	}
	return persona.Expand(r, w.v13Bank("story-task:"+kind, g), "root")
}
