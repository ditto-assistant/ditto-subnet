package gen

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Bench v13 grounded abstention (issue #1530).
//
// A representative v12 artifact carries zero AnswerDecline cases: the pre-v8
// abstention families (gen/abstention.go, gen/nearmiss.go, gen/lifecycle.go)
// are unreachable at v8+, and nothing in the world suite tests whether an agent
// invents a memory answer when no supporting memory exists. v13 ports the six
// absence-proof families onto the shared world and universe.QuestionPlan
// (universe/v13_absence.go):
//
//  1. pure absence        - the asked person exists in no record at all (a
//     coined name asked at a real contact's employer and event);
//  2. near miss           - a sibling at the same employer has the address, the
//     asked colleague is mentioned only in an unrelated context;
//  3. stale / removed     - only a withdrawn handle supports the tempting value;
//  4. false premise       - the trip exists, the asked leg never did;
//  5. cross-user only     - the person lives only in the other user's graph;
//  6. insufficient        - the approved total is known, the payment amount is
//     composition          not, so the balance cannot be derived.
//
// Every unanswerable case grades as AnswerAbsence (grade/v13.go): a decline
// that cites a GroundingTokens value actually present in the records searched
// (never a tempting value) scores 1; the tempting value scores 0 when ASSERTED
// as the answer — offered, or not rejected right after it is cited — and keeps
// credit when cited as insufficient evidence; a generic refusal scores 0. Every
// unanswerable case is paired with a distributionally matched answerable twin
// (same family and oracle, different surface draw) under TwinRelationDecision,
// so wording cannot reveal whether to answer or abstain, and on the twin a
// prose decline with an empty answer slot is an abstention, so the never-decide
// hedge fails both halves. The pair is placed at least twenty cases apart and
// never adjacent (placeV13TwinPairs). Gated on bench_version >= 13, so v12 and
// earlier regenerate byte-identically.

// QTAbsence prefixes the unanswerable question types ("absence-pure", ...);
// QTAbsenceTwin prefixes their answerable twins ("absence-twin-pure-absence",
// ...). Both are validator-internal and free of the injection/canary/isolation
// substrings the grader special-cases.
const (
	QTAbsence     = "absence-"
	QTAbsenceTwin = "absence-twin-"
)

// v13AbstentionCaseCount is the fixed unanswerable case budget for a run size;
// the same number of answerable twins is carved out alongside it.
func v13AbstentionCaseCount(n int) int {
	scale, _ := v8WorldProfile(n)
	return universe.V13AbsenceCaseCount(scale)
}

// v13IsoCasesForScale is the multi-graph isolation quota the cross-user family
// assumes for a world scale: the same numbers profilesV13 pins for medium
// (scale 2) and full (scale 3), derived from the scale so an analysis run at a
// non-public memory count (vstudy/gstudy) builds the same family. The
// isolation projection is deterministic per anchor, so a projected person is
// the same whether or not the run's own isolation suite seeds that many.
func v13IsoCasesForScale(scale int) int {
	switch {
	case scale >= 3:
		return 9
	case scale == 2:
		return 5
	default:
		return 0
	}
}

// v13IsoCasesForMem is v13IsoCasesForScale over the world scale a memory-case
// count selects.
func v13IsoCasesForMem(n int) int {
	scale, _ := v8WorldProfile(n)
	return v13IsoCasesForScale(scale)
}

// v13CrossUserFacts derives the other-graph contacts the cross-user family asks
// about from the same isolation projection generateV8WorldIsolation seeds. Even
// anchors are preferred (the isolation suite asks the primary graph about odd
// anchors), and only anchors whose given name is unique in the primary world
// qualify, so the in-graph near miss resolves to exactly one person.
func v13CrossUserFacts(seed int64, n int, a universe.V13Allocation, benchVersion int) ([]universe.V13CrossUserFact, error) {
	isoCases := v13IsoCasesForMem(n)
	want := len(a.CrossPeople)
	if isoCases == 0 || want == 0 {
		return nil, nil
	}
	primary, secondary, _, err := v8IsolationProjection(seed, n, isoCases, benchVersion)
	if err != nil {
		return nil, err
	}
	given := map[string]int{}
	for _, p := range primary.People {
		given[strings.Fields(p.Name)[0]]++
	}
	out := make([]universe.V13CrossUserFact, 0, want)
	// Even anchors first (their primary contact is not otherwise asked), then
	// odd ones as a fallback when the shared given name is not unique enough.
	order := make([]int, 0, isoCases)
	for i := 0; i < isoCases; i += 2 {
		order = append(order, i)
	}
	for i := 1; i < isoCases; i += 2 {
		order = append(order, i)
	}
	for _, i := range order {
		if len(out) == want {
			break
		}
		anchor := primary.People[i]
		if given[strings.Fields(anchor.Name)[0]] != 1 {
			continue
		}
		other := secondary.People[i]
		if other.Name == anchor.Name || other.Email == anchor.Email {
			continue
		}
		out = append(out, universe.V13CrossUserFact{
			Anchor: i, Name: other.Name, Employer: other.Employer, Context: other.Context, Email: other.Email,
		})
	}
	if len(out) < want {
		return nil, fmt.Errorf("v13 cross-user family found %d distinct anchors, need %d", len(out), want)
	}
	return out, nil
}

// buildV13Abstention renders the decision pairs of a v13 world into staged
// cases, [unanswerable, answerable] per pair, in family order.
func buildV13Abstention(seed int64, n int, world universe.World, a universe.V13Allocation, benchVersion int) ([][2]StagedCase, error) {
	crossUser, err := v13CrossUserFacts(seed, n, a, benchVersion)
	if err != nil {
		return nil, fmt.Errorf("v13 abstention: %w", err)
	}
	pairs, err := world.V13DecisionPairs(a, crossUser)
	if err != nil {
		return nil, fmt.Errorf("v13 abstention: %w", err)
	}
	out := make([][2]StagedCase, 0, len(pairs))
	for k, pair := range pairs {
		pairID := protocol.OpaqueCaseID(seed, "v13-decision-twin", k)
		unanswerable, answerable := pair.Unanswerable, pair.Answerable
		unanswerable.Case.QuestionType = QTAbsence + pair.Family
		answerable.Case.QuestionType = QTAbsenceTwin + pair.Family
		// The twin is an ordinary world program re-issued under its own id, so it
		// never collides with the same fact if the ordinary pool also drew it.
		answerable.Case.ID = protocol.OpaqueCaseID(seed, "v13-decision-twin-answerable", k)
		answerable.Case.QuestionID = answerable.Case.ID
		var staged [2]StagedCase
		for half, plan := range []universe.QuestionPlan{unanswerable, answerable} {
			plan.Case.BenchVersion = benchVersion
			plan.Case.TwinRelation = protocol.TwinRelationDecision
			plan.Case.TwinPairID = pairID
			protected := append([]string(nil), plan.Constraints...)
			protected = append(protected, plan.Case.GroundingTokens...)
			plan.Case.WritingProtected = protected
			staged[half] = StagedCase{Case: plan.Case, RunAfterWave: 0, RequiredPairIDs: append([]string(nil), plan.RequiredPairIDs...)}
		}
		out = append(out, staged)
	}
	return out, nil
}

// placeV13TwinPairs lays the wave-0 case list out so the two members of every
// relation pair sit in opposite halves of the run: first members spread through
// the first half, second members through the second, a seed-keyed coin deciding
// which member goes first. The pair distance is therefore about half the run
// (>= 20 on every scored profile) and never adjacent, while the relative order
// of every other case is preserved.
func placeV13TwinPairs(seed int64, others []StagedCase, pairs [][2]StagedCase) []StagedCase {
	if len(pairs) == 0 {
		return others
	}
	n := len(others) + 2*len(pairs)
	half := n / 2
	firsts := make([]StagedCase, len(pairs))
	seconds := make([]StagedCase, len(pairs))
	for k, pair := range pairs {
		if protocol.OpaqueCaseID(seed, "v13-twin-order", k)[1]%2 == 0 {
			firsts[k], seconds[k] = pair[0], pair[1]
		} else {
			firsts[k], seconds[k] = pair[1], pair[0]
		}
	}
	slot := func(k int) int { return (2*k + 1) * half / (2 * len(pairs)) }
	reserved := map[int]StagedCase{}
	for k := range pairs {
		reserved[slot(k)] = firsts[k]
		reserved[half+slot(k)] = seconds[k]
	}
	out := make([]StagedCase, 0, n)
	next := 0
	for i := 0; i < n; i++ {
		if c, ok := reserved[i]; ok {
			out = append(out, c)
			continue
		}
		if next < len(others) {
			out = append(out, others[next])
			next++
		}
	}
	// Reserved slots can collide only when half < 2*len(pairs); every scored
	// profile is far above that, but append any displaced member rather than
	// drop it so the count is always exact.
	if len(out) < n {
		seen := map[string]bool{}
		for _, c := range out {
			seen[c.Case.ID] = true
		}
		for k := range pairs {
			for _, c := range []StagedCase{firsts[k], seconds[k]} {
				if !seen[c.Case.ID] {
					out = append(out, c)
					seen[c.Case.ID] = true
				}
			}
		}
		for ; next < len(others); next++ {
			out = append(out, others[next])
		}
	}
	return out
}
