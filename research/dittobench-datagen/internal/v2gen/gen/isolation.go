package gen

import (
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/internal/v2gen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// User graph identifiers for multi-graph isolation. The
// primary persona is seeded under PrimaryUser (default "miner");
// a second persona is seeded under SecondaryUser. Isolation cases then query one
// user while the OTHER user's graph holds a conflicting value for the same
// attribute. A harness that respects the user_id scoping answers correctly; one
// that leaks across graphs returns the other user's value and is wrong.
const (
	PrimaryUser   = "miner"
	SecondaryUser = "colleague"
)

// isolationSalt perturbs the master seed to draw a DISTINCT second persona
// (different names/values) from the same pools, deterministically.
const isolationSalt = 0x5ec0_11ab_0000_0001

// IsolationSuite is the secondary user graph plus the cross-user isolation cases
// (both A-scoped and B-scoped). It is layered on top of the primary MemorySuite
// by the pipeline: the secondary graph is seeded once under SecondaryUser, and
// the cases are run interleaved with the primary cases (each carrying the user_id
// it must be answered under).
type IsolationSuite struct {
	SecondaryWave protocol.SeedRequest // B's haystack, seeded under SecondaryUser
	Cases         []StagedCase         // isolation cases, each with StagedCase.UserID set
}

// isolationOpts keeps the secondary (contamination) persona small: it exists to
// hold conflicting values, not to be the primary test surface.
func isolationOpts() persona.Opts {
	return persona.Opts{Sessions: 4, Projects: 3, Trips: 2, Pets: 1, UpdateChains: 1, Reversals: 1, DecoyPeople: 3, DomainItems: 3, LongChain: 0}
}

// GenerateIsolation builds the multi-graph isolation layer. It is a pure
// function of (seed, primaryN, nWaves, isoCases): it rebuilds the primary plan
// from the seed (BuildPlan is pure) to align isolation cases with the primary
// seeding waves, draws a distinct secondary persona, and emits up to isoCases
// isolation cases split between A-scoped (query PrimaryUser, the conflicting
// value lives in B) and B-scoped (query SecondaryUser, the conflict lives in A).
// The secondary haystack is TEMPLATE-rendered (no LLM): it is contamination, so
// it adds zero generator token cost. isoCases<=0 returns an empty suite.
func GenerateIsolation(seed int64, primaryN, nWaves, isoCases int) IsolationSuite {
	suite, _ := GenerateIsolationForVersion(seed, primaryN, nWaves, isoCases, protocol.BenchVersionV2)
	return suite
}

// GenerateIsolationForVersion builds the isolation layer for an explicit
// benchmark contract.
func GenerateIsolationForVersion(seed int64, primaryN, nWaves, isoCases, benchVersion int) (IsolationSuite, error) {
	if isoCases <= 0 {
		return IsolationSuite{}, nil
	}
	if nWaves < 1 {
		nWaves = 1
	}
	pPlan, err := persona.BuildPlanForVersion(seed, personaOptsFor(primaryN), benchVersion)
	if err != nil {
		return IsolationSuite{}, err
	}
	sPlan, err := persona.BuildPlanForVersion(seed^isolationSalt, isolationOpts(), benchVersion)
	if err != nil {
		return IsolationSuite{}, err
	}

	// Secondary haystack: template-rendered (non-LLM), fully Tier-A (prepared
	// subjects) so it is retrievable: a cross-graph leak actually surfaces it.
	sPairs, sEvidence := RenderHaystack(sPlan)
	sSubjects, sLinks := synthesizeSubjects(sPlan, sEvidence, nil)
	secondary := protocol.SeedRequest{
		UserID:   SecondaryUser,
		Pairs:    sPairs,
		Subjects: sSubjects,
		Links:    sLinks,
	}

	pCur, sCur := currentScalars(pPlan), currentScalars(sPlan)
	pRecall, sRecall := recallByAttr(pPlan), recallByAttr(sPlan)
	pFW := factWaves(pPlan, nWaves)

	// Attributes both personas hold as a current scalar self-fact with DIFFERENT
	// values: the conflict axis. Sorted for determinism.
	var attrs []string
	for a, v := range pCur {
		if sv, ok := sCur[a]; ok && sv != "" && sv != v {
			attrs = append(attrs, a)
		}
	}
	sort.Strings(attrs)

	cases := make([]StagedCase, 0, isoCases)
	i := 0
	for _, a := range attrs {
		if len(cases) >= isoCases {
			break
		}
		// Alternate the query direction so a harness cannot pass by always trusting
		// one graph: even index → A-scoped, odd → B-scoped.
		if len(cases)%2 == 0 {
			q, ok := pRecall[a]
			if !ok {
				continue
			}
			cases = append(cases, StagedCase{
				Case: protocol.MemoryCase{
					ID:              protocol.OpaqueCaseID(seed, "iso-a", i),
					QuestionID:      "iso-a-" + a,
					QuestionType:    "isolation",
					Question:        q.Text,
					ExpectedAnswer:  q.Answer,
					ForbiddenAnswer: sCur[a], // the value B's graph holds; leaking it is wrong
				},
				RunAfterWave: caseUnlockWave(q, pFW),
				UserID:       PrimaryUser,
			})
		} else {
			q, ok := sRecall[a]
			if !ok {
				continue
			}
			cases = append(cases, StagedCase{
				Case: protocol.MemoryCase{
					ID:              protocol.OpaqueCaseID(seed, "iso-b", i),
					QuestionID:      "iso-b-" + a,
					QuestionType:    "isolation",
					Question:        q.Text,
					ExpectedAnswer:  q.Answer,
					ForbiddenAnswer: pCur[a], // the value A's graph holds; leaking it is wrong
				},
				RunAfterWave: 0, // B is seeded once, up front
				UserID:       SecondaryUser,
			})
		}
		i++
	}

	return IsolationSuite{SecondaryWave: secondary, Cases: cases}, nil
}

// currentScalars maps each current (latest-value-wins) scalar self attribute to
// its value for a plan.
func currentScalars(plan *persona.Plan) map[string]string {
	out := map[string]string{}
	for _, f := range plan.Facts {
		if f.Kind == persona.KindScalar && f.Entity == "self" && f.Current {
			out[f.Attribute] = f.Value
		}
	}
	return out
}

// recallByAttr maps each scalar attribute to a single-session-recall question for
// it (the first one derived). Used to phrase an isolation question in the
// persona's own recall wording.
func recallByAttr(plan *persona.Plan) map[string]persona.Question {
	factAttr := map[string]string{}
	for _, f := range plan.Facts {
		factAttr[f.ID] = f.Attribute
	}
	out := map[string]persona.Question{}
	for _, q := range persona.DeriveQuestions(plan) {
		if q.Abstain || q.Type != persona.QTSingleSession || len(q.Evidence) == 0 {
			continue
		}
		attr := factAttr[q.Evidence[0]]
		if attr == "" {
			continue
		}
		if _, seen := out[attr]; !seen {
			out[attr] = q
		}
	}
	return out
}
