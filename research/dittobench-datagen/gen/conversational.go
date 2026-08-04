package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Conversational-sanity and ordinary-declarative-write cases (bench_version 5;
// v5 plan sections 4.1 and 4.2).
//
// v4 rewarded a phrase-list router that recognizes memory writes only through a
// closed cue family and, on any turn it does not match, force-routes to a recall
// pipeline that dumps a retrieved memory. On a plain "hi" that pipeline surfaced a
// stray seeded value ("Aurora-9"); v4 scored it near the top because no case
// graded conversational relevance and no case tested a plain declarative
// statement as a durable write. These cases close both holes deterministically,
// judge-free, reusing the existing forbidden/distractor/dump machinery:
//
//   - Greeting non-leak (QTChitchat): a small-talk turn seeds a high-entropy,
//     off-topic sentinel into the haystack. The reply must surface neither the
//     sentinel nor a broad slice of off-topic self values. This is the canary
//     check inverted and directly catches the "Aurora-9" leak.
//   - Declarative acknowledgement (QTDeclarativeAck): the user states a coined
//     value THIS turn with no save verb; the reply must echo it (acknowledgement)
//     and must not leak the sentinel. A router that ignores the statement and
//     dumps memory fails to echo, or leaks, and scores 0.
//   - Abstention over confabulation (QTAbstainConfab): a coined value is seeded on
//     a semantically ADJACENT possession, then a never-seeded neighbor is asked
//     ("named my kayak X" seeded, "what did I name my canoe" asked). Correct
//     behavior is to abstain; a retriever that reports the nearest neighbor
//     surfaces the seeded value and scores 0. This is the general form of the
//     Aurora-9 bug.
//   - Ordinary declarative write chain (QTDeclarativeWrite / QTDeclarativeRead /
//     QTDeclarativeBehavior): the user states a durable preference conversationally
//     with NO imperative save verb (never "remember"/"save"/"update"); a later-wave
//     read proves the write persisted, and a later-wave behavior-change case
//     requires APPLYING it. The honored values are coined and appear nowhere but
//     the stating /run turn, so a phrase-list classifier that only fires on
//     imperative verbs never captures them and fails both later cases.
//
// The two conjunction slices the first-class conversational-sanity metric is built
// from (declarative echo, behavior-change application) are here alongside the
// greeting slice, so a canned "Got it!" bot that clears the greeting non-leak
// still fails the metric on the other two (v5 plan 4.1 interlock).
const (
	// QTChitchat is a greeting / small-talk turn with no memory question; graded
	// on the non-leak floor only (AnswerChitchat). Part of the first-class
	// conversational-sanity metric (greeting slice).
	QTChitchat = "conversational-chitchat"
	// QTDeclarativeAck is a same-turn declarative statement the reply must
	// acknowledge by echoing the stated value without leaking a sentinel. Part of
	// the conversational-sanity metric (declarative-echo slice).
	QTDeclarativeAck = "conversational-declarative"
	// QTAbstainConfab is the abstention-over-confabulation probe: a never-seeded
	// neighbor of a seeded coined value; correct behavior is to decline.
	QTAbstainConfab = "conversational-abstention"
	// QTDeclarativeWrite is a no-save-verb declarative write turn (wave 0); graded
	// on the non-leak floor (the persistence proof lives in the read case).
	QTDeclarativeWrite = "declarative-write"
	// QTDeclarativeRead is the later-wave persistence proof: the coined value is
	// answerable only if the declarative write actually persisted.
	QTDeclarativeRead = "declarative-write-read"
	// QTDeclarativeBehavior is the later-wave behavior-change case: an application
	// question whose answer is the honored coined value, same-attribute rejected
	// value as distractor. Part of the conversational-sanity metric (behavior slice).
	QTDeclarativeBehavior = "declarative-behavior"
)

// conversationalSuite is the generated v5 material: staged cases plus the wave-0
// haystack pairs the greeting sentinel and the abstention neighbor rely on.
type conversationalSuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

// conversationalEnabled reports whether the conversational-sanity and
// declarative-write case classes apply to a bench version (v5+).
func conversationalEnabled(benchVersion int) bool {
	return benchVersion >= protocol.BenchVersionV5
}

// declarativeChainsFor is the seed-independent declarative-write chain quota. Each
// chain yields one persistence-read and one behavior-change case, which are two of
// the three conversational-sanity slices (the metric is a weakest-link min over the
// slices). Full runs carry FOUR chains so the behavior/read slices have enough N
// that the min-over-slices metric is not a per-case coin flip (the plan's 4.10
// statistical-power guard); a single hard-domain miss no longer halves a champion's
// composite. A chain needs a later wave, so single-wave runs carry none.
func declarativeChainsFor(n, nWaves int) int {
	switch {
	case nWaves < 2:
		return 0
	case n >= 70:
		return 4
	case n >= 40:
		return 3
	case n >= 20:
		return 1
	default:
		return 0
	}
}

// convToken coins a high-entropy token in the run's shape family (shared with the
// canary nonce and lifecycle values, the v3 grammar-collision property), so a
// shape-keyed output scrub that deletes payload-shaped tokens deletes these too.
func convToken(seed int64, salt string) string {
	return persona.CoinShaped(seed, "cv|"+salt)
}

// greetingPrompts are pure small-talk turns: no fact is asked, so the only
// correct-behavior signal is the non-leak floor.
var greetingPrompts = []string{
	"hey!",
	"hi there, how are you today?",
	"good morning!",
	"hey, how's it going?",
	"morning! hope you're well.",
	"yo, what's up?",
	"hello! long time no chat.",
	"hiya :)",
	"evening! how are things?",
	"hey friend, how've you been?",
	"good to see you again!",
	"sup, how's your day going?",
}

// declarativeAckSpecs are same-turn declarative statements carrying a coined
// value the reply must echo. The %s is filled with the coined value AFTER phrasing
// selection so the token is never rewritten.
var declarativeAckSpecs = []string{
	"By the way, the codeword for our chats is %s — use it when you check in with me.",
	"Quick note: you can file me under the handle %s from now on.",
	"Oh, my call sign for our sessions is %s, in case it comes up.",
	"Heads up, the tag I go by around here is %s.",
	"For the record, my membership number here is %s.",
	"Just so you have it, my locker tag is %s.",
	"FYI, the reference I use for us is %s.",
	"Let's use %s as my nickname going forward.",
}

var v8DeclarativeAckSpecs = []string{
	"By the way, the codeword for our chats is %s — use it when you check in with me.",
	"Quick note: you can file this project under the handle %s from now on.",
	"The call sign for these sessions is %s, in case it comes up.",
	"The tag I use for this work is %s.",
	"My membership number here is %s.",
	"Just so you have it, my locker tag is %s.",
	"The reference code for this thread is %s.",
	"Use %s as the project reference going forward.",
}

// declarativeAckForms are acknowledgment surfaces that also count as a correct
// response to a plain declarative statement (bench_version 6). The old grading
// required the harness to ECHO the coined token verbatim, so a perfectly grounded
// reply like "Got it, noted." scored zero. That was a false negative against the
// honest, grounded harness the conversational-sanity gate exists to reward. Accept
// a clear acknowledgment as well: the non-leak guards (ForbiddenAnswer + DumpGuard)
// still zero a memory dump, and durable capture is proven unfakeably by the separate
// declarative-write-read case, so a generic "got it" cannot game the gate (a harness
// that acknowledges but never captures still fails the write-read and behavior
// slices). This only removes the false negative.
var declarativeAckForms = []string{
	"got it", "noted", "understood", "will do", "makes sense", "sounds good",
	"good to know", "i'll remember", "i've noted", "duly noted", "acknowledged",
	"of course", "no problem", "gotcha", "sure thing", "consider it noted",
}

// confabSpec is one abstention-over-confabulation probe: a coined value seeded on
// seededNoun, then askedNoun (never seeded, semantically adjacent) is queried. A
// confabulator reports the seeded neighbor; the correct behavior is to decline.
type confabSpec struct {
	seededNoun string
	askedNoun  string
}

// declPrefDomain is one behavior-change preference domain. write takes two %s
// fills (preferred, rejected); read and behavior take none (the value is proven
// by recall, not restated). Distinct domains keep multiple chains in one run from
// colliding on the same "which service" question.
type declPrefDomain struct {
	write    []string
	read     []string
	behavior []string
}

var declPrefDomains = []declPrefDomain{
	{
		write: []string{
			"Going forward, always arrange my travel through %s — never %s, they lost a booking of mine once.",
			"For anything travel-related, I only ever use %s. I don't touch %s after last time.",
			"Just so it's on record how I like things: book my trips through %s, and steer clear of %s.",
		},
		read: []string{
			"Which travel service do I want you to use?",
			"Who did I say to book my travel through?",
			"What's my preferred travel-booking service again?",
		},
		behavior: []string{
			"Plan me a weekend trip next month — who are you booking the travel through?",
			"Set up my travel for the conference; which service are you going with?",
			"Sort out a flight for me next week — what service are you booking it on?",
		},
	},
	{
		write: []string{
			"For food, I always order from %s these days — never %s, they messed up my last three orders.",
			"When it comes to takeout, %s is my go-to. I've sworn off %s entirely.",
			"Just noting my standing preference: get my meals from %s, and never from %s.",
		},
		read: []string{
			"Which food-delivery service do I want you to use?",
			"Who did I say to order my meals through?",
			"What's my preferred takeout service again?",
		},
		behavior: []string{
			"Order me some dinner tonight — which service are you putting it through?",
			"Grab me lunch for the meeting; what app are you ordering from?",
			"Set up a food order for the party — which service are you using?",
		},
	},
	{
		write: []string{
			"For rides, I only ever call %s now — never %s, their drivers kept cancelling on me.",
			"When I need a car, book %s. I've stopped using %s entirely.",
			"Standing rule for getting around: use %s, and never %s.",
		},
		read: []string{
			"Which rideshare service do I want you to use?",
			"Who did I say to book my rides through?",
			"What's my preferred car service again?",
		},
		behavior: []string{
			"Get me a ride to the airport tomorrow — which service are you booking?",
			"Call me a car for 6pm; what app are you using?",
			"Arrange a ride to the venue — which service are you going with?",
		},
	},
	{
		write: []string{
			"For music I've moved everything to %s — never %s again, the recommendations were awful.",
			"I stream all my music on %s now. I cancelled %s for good.",
			"Standing preference: play my music through %s, not %s.",
		},
		read:     []string{"Which music service do I use?", "Where do I stream my music these days?", "What's my music app of choice again?"},
		behavior: []string{"Put on my morning playlist — which service are you using?", "Queue up some focus music; what app are you opening?", "Start my workout mix — where are you playing it from?"},
	},
	{
		write: []string{
			"For groceries I order from %s exclusively now — never %s, their produce was always off.",
			"I get all my groceries via %s. I'm done with %s.",
			"Note my standing choice: groceries come from %s, never %s.",
		},
		read:     []string{"Which grocery service do I want?", "Where do I order groceries from now?", "What's my go-to grocery app again?"},
		behavior: []string{"Restock my fridge for the week — which service are you ordering from?", "Order ingredients for dinner; what app are you using?", "Set up a grocery delivery — where are you placing it?"},
	},
	{
		write: []string{
			"For banking I've switched to %s — never %s, their fees were ridiculous.",
			"I do all my banking with %s now. I closed my %s account.",
			"For the record, my bank is %s these days, not %s.",
		},
		read:     []string{"Which bank do I use now?", "Where do I do my banking these days?", "What's my current bank again?"},
		behavior: []string{"Help me set up a transfer — which bank are we working with?", "I need to check my balance; what bank app are you opening?", "Draft a note to my bank — which one is it?"},
	},
	{
		write: []string{
			"I read my news on %s now — never %s, too much clickbait.",
			"For news I've moved to %s. I unsubscribed from %s.",
			"Standing preference: get my news from %s, not %s.",
		},
		read:     []string{"Which news source do I prefer?", "Where do I read my news now?", "What's my go-to news outlet again?"},
		behavior: []string{"Give me this morning's headlines — which source are you pulling from?", "Summarize today's news; what outlet are you using?", "Find me a story on that — where are you looking?"},
	},
	{
		write: []string{
			"For flights I only book %s now — never %s, they cancelled on me twice.",
			"When I fly, it's %s. I avoid %s entirely.",
			"Note my airline preference: %s, never %s.",
		},
		read:     []string{"Which airline do I prefer?", "Who do I fly with these days?", "What's my airline of choice again?"},
		behavior: []string{"Book me a flight to Chicago — which airline are you using?", "Find me a red-eye next week; what carrier are you booking?", "Sort out my flights for the trip — which airline?"},
	},
	{
		write: []string{
			"For fitness I use %s now — never %s, the app kept crashing.",
			"I track my workouts on %s. I ditched %s.",
			"Standing choice: my fitness app is %s, not %s.",
		},
		read:     []string{"Which fitness app do I use?", "Where do I track my workouts now?", "What's my go-to fitness app again?"},
		behavior: []string{"Log my run from this morning — which app are you using?", "Set up a workout plan; what app are you opening?", "Check my step count — where are you pulling it from?"},
	},
	{
		write: []string{
			"For shipping I use %s now — never %s, they lost two packages.",
			"I send everything via %s. I've stopped using %s.",
			"Note my courier preference: %s, never %s.",
		},
		read:     []string{"Which courier do I prefer?", "Who do I ship with these days?", "What's my go-to shipping service again?"},
		behavior: []string{"Send this package for me — which courier are you using?", "Arrange a return pickup; what service are you booking?", "Ship a gift to my sister — which carrier?"},
	},
}

var confabSpecs = []confabSpec{
	{seededNoun: "kayak", askedNoun: "canoe"},
	{seededNoun: "sailboat", askedNoun: "motorbike"},
	{seededNoun: "houseplant on the balcony", askedNoun: "houseplant in the kitchen"},
	{seededNoun: "guitar", askedNoun: "ukulele"},
	{seededNoun: "road bike", askedNoun: "mountain bike"},
	{seededNoun: "espresso machine", askedNoun: "kettle"},
	{seededNoun: "orange tabby", askedNoun: "goldfish"},
	{seededNoun: "vegetable patch", askedNoun: "flower bed"},
	{seededNoun: "campervan", askedNoun: "rowboat"},
	{seededNoun: "acoustic guitar", askedNoun: "banjo"},
	{seededNoun: "rooftop garden", askedNoun: "window box"},
	{seededNoun: "vintage scooter", askedNoun: "skateboard"},
	{seededNoun: "parrot", askedNoun: "hamster"},
	{seededNoun: "chess set", askedNoun: "dartboard"},
	{seededNoun: "pizza oven", askedNoun: "smoker"},
	{seededNoun: "record player", askedNoun: "cassette deck"},
	{seededNoun: "fishing rod", askedNoun: "tennis racket"},
	{seededNoun: "greenhouse", askedNoun: "compost bin"},
}

// buildConversational generates the v5 conversational-sanity and declarative-write
// cases. Deterministic per (seed, draw position): all picks draw from the shared
// suite rng at a fixed point in GenerateMemorySuite, gated so a pre-v5 contract's
// draw sequence is untouched.
func buildConversational(r *rand.Rand, seed int64, plan *persona.Plan, n, nWaves, benchVersion int) conversationalSuite {
	// v6 softens declarative acknowledgement: a clear acknowledgment counts, not only
	// a verbatim echo of the coined value (v5 kept the strict echo, so v5's bytes and
	// its frozen vector are untouched).
	var ackAccept []string
	if benchVersion >= protocol.BenchVersionV6 {
		ackAccept = declarativeAckForms
	}
	var out conversationalSuite
	pick := func(opts []string) string { return opts[r.Intn(len(opts))] }
	ackSpecs := declarativeAckSpecs
	if benchVersion >= protocol.BenchVersionV8 {
		ackSpecs = v8DeclarativeAckSpecs
	}
	offTopic := offTopicSelfValues(plan, 8)

	ordinal := 0
	addCase := func(mc protocol.MemoryCase, wave int) {
		mc.ID = protocol.OpaqueCaseID(seed, "memcv", ordinal)
		out.Cases = append(out.Cases, StagedCase{Case: mc, RunAfterWave: wave})
		ordinal++
	}

	// --- 4.1 greeting non-leak (two greetings, each with its own sentinel) ---
	for i := 0; i < 2; i++ {
		sentinel := convToken(seed, fmt.Sprintf("greet-sentinel-%d", i))
		out.Pairs = append(out.Pairs, convPair(plan, ordinal,
			pick([]string{
				"Random aside — the raffle ticket I bought is number " + sentinel + ". Just noting it here.",
				"Off topic, but my locker slip at the pool says " + sentinel + ". Filing it away.",
				"Unrelated: the claim tag from the dry cleaner is " + sentinel + ".",
			}),
			"Noted.",
		))
		addCase(protocol.MemoryCase{
			QuestionType:    QTChitchat,
			Question:        pick(greetingPrompts),
			AnswerKind:      protocol.AnswerChitchat,
			ForbiddenAnswer: sentinel,
			DumpGuard:       offTopic,
		}, 0)
	}

	// --- 4.1 declarative acknowledgement (two, coined value stated in-turn) ---
	for i := 0; i < 2; i++ {
		val := convToken(seed, fmt.Sprintf("decl-ack-%d", i))
		sentinel := convToken(seed, fmt.Sprintf("decl-sentinel-%d", i))
		out.Pairs = append(out.Pairs, convPair(plan, ordinal,
			"Side note: my old confirmation code was "+sentinel+", not that it matters now.",
			"Understood.",
		))
		addCase(protocol.MemoryCase{
			QuestionType:    QTDeclarativeAck,
			Question:        fmt.Sprintf(pick(ackSpecs), val),
			ExpectedAnswer:  val,
			AnswerKind:      protocol.AnswerValue,
			AcceptAny:       ackAccept, // v6: a clear acknowledgment also passes
			ForbiddenAnswer: sentinel,
			DumpGuard:       offTopic,
		}, 0)
	}

	// --- 4.1 abstention over confabulation ---
	{
		spec := confabSpecs[r.Intn(len(confabSpecs))]
		neighborVal := convToken(seed, "confab-neighbor")
		out.Pairs = append(out.Pairs, convPair(plan, ordinal,
			"I finally named my "+spec.seededNoun+" — I'm calling it "+neighborVal+".",
			"Nice, "+neighborVal+" it is.",
		))
		addCase(protocol.MemoryCase{
			QuestionType: QTAbstainConfab,
			Question: pick([]string{
				"What did I say I named my " + spec.askedNoun + "?",
				"Remind me — what's the name of my " + spec.askedNoun + "?",
				"What's my " + spec.askedNoun + " called again?",
			}),
			ExpectedAnswer:    abstentionExpectedAnswer,
			AnswerKind:        protocol.AnswerDecline,
			DistractorAnswers: []string{neighborVal},
		}, 0)
	}

	// --- 4.2 ordinary declarative write chains (need a later wave) ---
	// Pre-coin every chain's preferred value so the read/behavior distractors are
	// the OTHER chains' preferred values (the lifecycle cross-chain anti-shotgun
	// pattern). The same chain's REJECTED value is deliberately NOT a distractor:
	// it is the user's OWN stated non-preference, so a reply that names the
	// preferred value while contrasting it with the rejected one ("book through X,
	// not Y as you asked") is correct application, not a wrong-fact surface --
	// exactly the grader's rule that a superseded value of the user's own chain is
	// not a distractor. On-model precheck evidence: the honest reference reader
	// routinely says "X, avoiding Y", which the rejected-as-distractor rule zeroed.
	nChains := declarativeChainsFor(n, nWaves)
	if benchVersion >= protocol.BenchVersionV8 {
		nChains = 0
	}
	// Draw DISTINCT random preference domains per seed (not chain%len, which would
	// only ever use the first few domains and make the write family enumerable).
	domPerm := r.Perm(len(declPrefDomains))
	preferredVals := make([]string, nChains)
	for chain := 0; chain < nChains; chain++ {
		preferredVals[chain] = convToken(seed, fmt.Sprintf("decl-vendor-pref-%d", chain))
	}
	otherPreferred := func(chain int) []string {
		var out []string
		for j, v := range preferredVals {
			if j != chain {
				out = append(out, v)
			}
		}
		return out
	}
	readWave := nWaves - 1
	for chain := 0; chain < nChains; chain++ {
		preferred := preferredVals[chain]
		rejected := convToken(seed, fmt.Sprintf("decl-vendor-rej-%d", chain))
		// Write turn (wave 0): a durable preference stated conversationally with NO
		// imperative save verb. Graded on the non-leak floor; the unfakeable signal
		// is the later read and behavior cases, whose values appear only here.
		dom := declPrefDomains[domPerm[chain%len(domPerm)]]
		addCase(protocol.MemoryCase{
			QuestionType: QTDeclarativeWrite,
			Question:     fmt.Sprintf(pick(dom.write), preferred, rejected),
			AnswerKind:   protocol.AnswerChitchat,
			DumpGuard:    offTopic,
		}, 0)
		// Persistence read (last wave): direct recall of the stored preference.
		addCase(protocol.MemoryCase{
			QuestionType:      QTDeclarativeRead,
			Question:          pick(dom.read),
			ExpectedAnswer:    preferred,
			AnswerKind:        protocol.AnswerValue,
			DistractorAnswers: otherPreferred(chain),
		}, readWave)
		// Behavior change (last wave): an application question whose correct answer
		// requires APPLYING the stored preference. A wrong answer names a DIFFERENT
		// chain's preferred value (cross-chain shotgun); naming the preferred value
		// while contrasting the same chain's rejected one stays correct.
		addCase(protocol.MemoryCase{
			QuestionType:      QTDeclarativeBehavior,
			Question:          pick(dom.behavior),
			ExpectedAnswer:    preferred,
			AnswerKind:        protocol.AnswerValue,
			DistractorAnswers: otherPreferred(chain),
		}, readWave)
	}

	return out
}

// offTopicSelfValues returns up to max of the user's current scalar self values,
// used as the dump-guard set for greeting and declarative turns: surfacing a broad
// slice of them on a turn that asks for none is the answer-dump signature. Mirrors
// persona.currentSelfScalarValues (unexported there) over the plan facts.
func offTopicSelfValues(plan *persona.Plan, max int) []string {
	var out []string
	seen := map[string]bool{}
	for _, f := range plan.Facts {
		if f.Kind != persona.KindScalar || f.Entity != "self" || !f.Current || f.Value == "" {
			continue
		}
		if seen[f.Value] {
			continue
		}
		seen[f.Value] = true
		out = append(out, f.Value)
		if len(out) >= max {
			break
		}
	}
	return out
}

// convPair renders one wave-0 haystack pair for a conversational sentinel or
// abstention neighbor. Timestamps use the same DatasetEpoch-anchored scheme as the
// persona renderer, offset into day 0 so they never collide with a session beat or
// a lifecycle pair.
func convPair(plan *persona.Plan, idx int, prompt, response string) protocol.MemoryPair {
	ts := persona.TimeAnchor(plan).Add(5*time.Hour + time.Duration(idx*beatSpacingMinutes)*time.Minute)
	return protocol.MemoryPair{
		PairID:    fmt.Sprintf("p-cv-%d", idx),
		SessionID: "sess-cv",
		Timestamp: ts.Format(time.RFC3339),
		Prompt:    prompt,
		Response:  response,
	}
}
