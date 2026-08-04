package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// bench_version 7 subscribed-graph attribution.
//
// Production models memory across two namespaces the agent sees as ONE flat
// search result list: the user's own KG and any graphs they subscribe to
// (backend pkg/tools/memory/tools.go SearchMemories fans out over own KG +
// subscribed graphs; "Subscribed workspaces are included in memory search and
// fetch", ditto-app SubscribePanel). Subscribed hits are distinguished ONLY by a
// rewritten provenance prefix: annotateSubscribedSlimMemory rewrites Source to
// "subscribed_graph:<orig>" and SourceContext to "@<username> / <orig>"
// (backend tools.go:458). The documented failure (backend failure-mode #7,
// docs/CROSSUSER_EXAMPLES.md; ditto-app graph-scoping fixes 8668a17b/37cce44d/
// c10335a3) is an agent that ignores the prefix and presents another user's fact
// as the user's own, or answers about the friend with the user's own value.
//
// A unit seeds the SAME attribute in both namespaces with different values and
// asks it two ways:
//
//   - OWN: "what's MY <attr>?" -> the user's own value; the friend's value is a
//     cross-graph leak (ForbiddenAnswer), scored 0 exactly like the isolation
//     probe. A retriever that returns the highest-similarity hit regardless of
//     ownership leaks the friend's value.
//   - ATTRIBUTED: "what did @<friend> say their <attr> is?" -> the friend's
//     value; returning the user's own value (a distractor) is the mirror error.
//
// Faithful to the product because the friend's memory is genuinely IN the search
// result set (subscribed graphs are searched), attributed by an "@<friend>"
// provenance prefix, exactly as the backend surfaces it. Deterministic per
// (seed, draw position); v7-gated so v6's bytes are untouched.
const (
	QTSubscriptionOwn    = "subscription-own"
	QTSubscriptionAttrib = "subscription-attributed"
)

type subscriptionSuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

func subscriptionEnabled(benchVersion int) bool {
	return benchVersion >= protocol.BenchVersionV7
}

// subscriptionUnitsFor is the seed-independent unit quota. Each unit seeds two
// pairs (own + subscribed-friend) and emits two cases (own + attributed).
func subscriptionUnitsFor(n, nWaves int) int {
	switch {
	case nWaves < 2:
		return 0
	case n >= 100:
		return 8
	case n >= 40:
		return 2
	default:
		return 0
	}
}

// subFriends are @-handles for the subscribed-graph owner. Disjoint from every
// persona name so a handle never collides with a decoy person in the haystack.
var subFriends = []string{
	"dana_lin", "marco_vd", "priya_r", "kwame_o", "lena_h", "yuki_t",
	"amara_e", "theo_b", "noor_s", "cillian_m",
}

// subAttrs are shareable personal attributes a user and a subscribed friend can
// both hold. Distinct from every persona attribute, lifecycle noun, and
// composed/stored-instruction label so no distractor pool moves.
var subAttrs = []struct {
	noun string
	ask  string // "my <ask>" / "@friend's <ask>"
}{
	{"guest wifi network name", "guest wifi network name"},
	{"preferred meeting-room booking code", "meeting-room booking code"},
	{"go-to potluck dish", "go-to potluck dish"},
	{"book-club current pick", "book-club pick"},
	{"community-garden plot number", "garden plot number"},
	{"carpool pickup spot", "carpool pickup spot"},
	{"gym class time slot", "gym class time slot"},
	{"shared-calendar color", "shared-calendar color"},
	{"neighborhood group chat name", "group chat name"},
	{"volunteer shift day", "volunteer shift day"},
	{"favorite trailhead", "favorite trailhead"},
	{"preferred courier drop-off", "courier drop-off"},
	{"study-group meeting night", "study-group meeting night"},
	{"shared streaming profile name", "streaming profile name"},
	{"office plant-watering day", "plant-watering day"},
	{"weekend farmers-market stall", "farmers-market stall"},
}

// buildSubscription generates the v7 subscribed-graph attribution suite.
func buildSubscription(r *rand.Rand, seed int64, plan *persona.Plan, n, nWaves int) subscriptionSuite {
	var out subscriptionSuite
	nUnits := subscriptionUnitsFor(n, nWaves)
	if nUnits == 0 {
		return out
	}
	pairIdx := 0
	ordinal := 0
	seedPair := func(session int, prompt, response string) {
		ts := persona.TimeAnchor(plan).Add(time.Duration(6+session)*time.Hour + time.Duration(pairIdx*beatSpacingMinutes)*time.Minute)
		out.Pairs = append(out.Pairs, protocol.MemoryPair{
			PairID:    fmt.Sprintf("p-sub-%d", pairIdx),
			SessionID: fmt.Sprintf("sess-sub-%d", session),
			Timestamp: ts.Format(time.RFC3339),
			Prompt:    prompt,
			Response:  response,
		})
		pairIdx++
	}

	attrPerm := r.Perm(len(subAttrs))
	friendPerm := r.Perm(len(subFriends))
	for u := 0; u < nUnits; u++ {
		attr := subAttrs[attrPerm[u%len(attrPerm)]]
		ownVal := persona.CoinShaped(seed, fmt.Sprintf("sub|own|%d", u))

		// v7 round-2 deepening: seed the SAME attribute across the user's own KG and
		// THREE subscribed friends' graphs, each with a distinct coined value, all
		// surfacing in one flat search result list distinguished only by their
		// "@<friend>" provenance prefix (backend annotateSubscribedSlimMemory fans
		// out over own + every subscribed graph). At depth-1 the round-1 single-friend
		// version was saturated (all harnesses ~1.0); with several conflicting values
		// the agent must disambiguate BY provenance, not just return the one hit — the
		// production failure documented in docs/CROSSUSER_EXAMPLES.md.
		friends := make([]string, 3)
		friendVals := make([]string, 3)
		for i := range friends {
			friends[i] = subFriends[friendPerm[(u*3+i)%len(friendPerm)]]
			friendVals[i] = persona.CoinShaped(seed, fmt.Sprintf("sub|friend|%d|%d", u, i))
		}

		// The user's own value, stated first-person.
		seedPair(4*u, fmt.Sprintf("My %s is %s.", attr.noun, ownVal), "Got it.")
		// Each subscribed friend's value, carrying its "@<friend>" provenance.
		for i, fr := range friends {
			seedPair(4*u+1+i,
				fmt.Sprintf("[from @%s's shared memories] @%s's %s is %s.", fr, fr, attr.noun, friendVals[i]),
				"Noted from the subscribed graph.")
		}

		// OWN: the user's own value is correct; EVERY friend's value is a cross-graph
		// leak. ForbiddenAnswer carries the first (the dual-path zero fires on any
		// forbidden hit); the rest ride as distractors so surfacing any friend's
		// value on "my X" is zeroed.
		out.Cases = append(out.Cases, StagedCase{Case: protocol.MemoryCase{
			ID:                protocol.OpaqueCaseID(seed, "memsub", ordinal),
			QuestionType:      QTSubscriptionOwn,
			Question:          fmt.Sprintf("What's my %s?", attr.ask),
			ExpectedAnswer:    ownVal,
			AnswerKind:        protocol.AnswerValue,
			ForbiddenAnswer:   friendVals[0],
			DistractorAnswers: []string{friendVals[1], friendVals[2]},
		}, RunAfterWave: nWaves - 1})
		ordinal++

		// ATTRIBUTED: ask about ONE specific friend among the three; that friend's
		// value is correct, and the OTHER two friends' values plus the user's own are
		// scored distractors, so the agent must attribute to the exact @handle asked.
		target := r.Intn(3)
		dis := []string{ownVal}
		for i := range friends {
			if i != target {
				dis = append(dis, friendVals[i])
			}
		}
		out.Cases = append(out.Cases, StagedCase{Case: protocol.MemoryCase{
			ID:                protocol.OpaqueCaseID(seed, "memsub", ordinal),
			QuestionType:      QTSubscriptionAttrib,
			Question:          fmt.Sprintf("What did @%s say their %s is?", friends[target], attr.ask),
			ExpectedAnswer:    friendVals[target],
			AnswerKind:        protocol.AnswerValue,
			DistractorAnswers: dis,
		}, RunAfterWave: nWaves - 1})
		ordinal++
	}
	return out
}
