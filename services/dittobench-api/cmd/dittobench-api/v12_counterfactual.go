package main

// Bench v12 causal model-dependence counterfactual runtime.
//
// This is the relay side that FEEDS the already-built, already-tested v12
// dependence gate (cmd/dittobench-api/v9_base.go v9DependenceTelemetryForVersion
// -> internal/v9base BuildGateEvidence -> internal/scoregates). Its whole job is
// to settle the two trusted per-case fields the gate reads
// (runner.CaseExecution.ModelCounterfactualObserved / ModelCounterfactualDependent):
//
//   - After the normal (clean) scored pass has produced every case's answer and
//     score, a SHADOW counterfactual pass re-runs each model-reached, non-excluded
//     scored case under a LaneInference FULL-ABLATION responder that serves a
//     completion with NO usable content (internal/ablation.Responder.AblatedChat).
//     The agent gets no model output to reason from on the ablated slice.
//   - The verdict is CORRECTNESS-BASED, not answer-equality. For each case the
//     clean run graded CORRECT, the ablated response is graded against the SAME
//     expected answer with the SAME deterministic grader (spec.grade):
//       - DEPENDENT (good): clean correct AND ablated INCORRECT — the agent needed
//         the model; its correctness collapsed under ablation.
//       - INDEPENDENT (bad): clean correct AND ablated STILL correct — the agent
//         recovered the answer WITHOUT a working model, so it has a non-model path
//         (re-insertion / deterministic solver) and is a launderer.
//     A case whose clean run was already incorrect is not evidence either way and
//     is excluded from the dependent/independent tally (kept out of the
//     denominator). An ablated run that errors / returns nothing counts as
//     ablated-incorrect -> dependent. A genuine agent floods DependentCases (gate
//     passes); a compute-then-launder harness stays correct under ablation, so
//     IndependentCases dominate and the dependent share falls below threshold ->
//     the gate floors the composite.
//
// Shadow-pass isolation: this pass NEVER writes back to perCase, the report,
// transcript Response bytes, or the token/relay accounting that feed the
// composite. It writes ONLY the two CaseExecution telemetry fields on the
// aligned transcript slice. The clean response used for the comparison is read
// out of the already-committed transcript, not recomputed.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"log"
	"sort"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// counterfactualMaxCases bounds how many model-reached cases the counterfactual
// pass will re-run in one scored run. Each administered case is exactly ONE
// additional case-scoped inference lease (one extra /run over the same harness),
// and the broker caps a run at 8,192 provider reservations (inference_broker.go
// ticket budget). Real runs reach the model on at most a few hundred cases, so
// the whole pass stays far under that ceiling; this cap is only a hard ticket-
// budget guard. If a run somehow exceeds it, the overflow cases are left with no
// verdict, SliceAttributionComplete stays false, and the gate fails OPEN — a
// detection gate must never zero an honest run for a ticket-budget gap.
const counterfactualMaxCases = 1024

// counterfactualChatBudget is the per-case synthetic ledger ceiling for the
// substituting responder. A single case may make several model calls (multi-hop
// tool trajectories); this bounds the synthetic completions served for one case
// well above any honest per-case call count while staying finite.
var counterfactualChatBudget = ablation.Budget{
	MaxChatRequests:   256,
	MaxChatInputBytes: 8 << 20,
}

// counterfactualCaseSpec is the clean-pass data needed to re-run one case under
// the ablation responder. It is captured during the clean pass (where the wire
// case id, prompt, tools, per-case tool endpoint, and the case's deterministic
// grader are in scope) and is aligned index-for-index with the transcript slice.
type counterfactualCaseSpec struct {
	caseID       string // wire case id the harness knows (pre-projection)
	prompt       string
	tools        []protocol.ToolDefinition
	toolEndpoint string
	userID       string
	// grade re-scores a RunResponse for THIS case against its expected answer with
	// the same deterministic scorer/grader the clean pass used (scope-practice, so
	// the self-report path is graded rather than the scored unobserved cap). The
	// counterfactual verdict is correctness-based: the pass grades both the clean
	// response and the ablated response through this closure and compares whether
	// correctness was PRESERVED under full model ablation. nil leaves the case
	// ungradeable, so it stays pending and the gate fails open.
	grade func(protocol.RunResponse) float64
}

func (s counterfactualCaseSpec) populated() bool { return s.caseID != "" && s.grade != nil }

// counterfactualCorrectThreshold is the case-score at or above which a graded
// response counts as CORRECT for the counterfactual verdict. It matches the
// established competent-answer cut used elsewhere in the scorer (effGateScore /
// canaryPassThreshold), so "correct" here means the same thing it does there.
const counterfactualCorrectThreshold = 0.5

// counterfactualCaseRunner re-runs one case under a LaneInference full-ablation
// responder and reports the harness's RunResponse produced with NO usable model
// output. ok=false leaves the case's verdict unsettled (fail open).
//
// The interface is the seam that keeps the deterministic slice/verdict logic
// unit-testable: production wires the live broker lease (brokerCounterfactualRunner),
// tests model the two harness archetypes.
type counterfactualCaseRunner interface {
	CounterfactualResponse(ctx context.Context, spec counterfactualCaseSpec, responder *ablation.Responder) (resp protocol.RunResponse, ok bool)
}

// counterfactualDomain domain-separates the two keyed derivations (which cases,
// in what order, and each case's perturbation seed) from every other digest.
const counterfactualDomain = "dittobench-v12-counterfactual"

// counterfactualRank is a deterministic per-case rank keyed by the run seed and
// bench version. It fixes the administration ORDER independently of case or
// completion order, mirroring the coordinator's keyed selection style.
func counterfactualRank(seed int64, benchVersion int, caseID, subdomain string) [sha256.Size]byte {
	h := sha256.New()
	_, _ = h.Write([]byte(counterfactualDomain))
	_, _ = h.Write([]byte{0})
	_, _ = h.Write([]byte(subdomain))
	_, _ = h.Write([]byte{0})
	var s [8]byte
	binary.BigEndian.PutUint64(s[:], uint64(seed))
	_, _ = h.Write(s[:])
	_, _ = h.Write([]byte{0})
	var v [8]byte
	binary.BigEndian.PutUint64(v[:], uint64(benchVersion))
	_, _ = h.Write(v[:])
	_, _ = h.Write([]byte{0})
	_, _ = h.Write([]byte(caseID))
	var out [sha256.Size]byte
	copy(out[:], h.Sum(nil))
	return out
}

// counterfactualResponderSeed derives the deterministic perturbation seed for
// one case from (run seed, bench version, case id). Same run -> same perturbed
// completion -> same verdict.
func counterfactualResponderSeed(seed int64, benchVersion int, caseID string) []byte {
	digest := counterfactualRank(seed, benchVersion, caseID, "perturbation")
	return digest[:]
}

// counterfactualSummary is returned for logging/reporting only; it never feeds a
// score. Administered = cases the ablated re-run settled (whether or not they
// enter the tally). Dependent/Independent are the clean-correct tally; Excluded
// counts administered cases whose clean run was already incorrect (kept out of
// the denominator).
type counterfactualSummary struct {
	Eligible     int
	Administered int
	Dependent    int
	Independent  int
	Excluded     int
	Overflow     int
}

// populateV12Counterfactual runs the shadow counterfactual pass and writes the
// two per-case telemetry fields the v12 dependence gate reads.
//
// It administers the counterfactual to EVERY eligible model-reached case (in a
// deterministic seed-keyed order): the gate reader treats any model-reached case
// WITHOUT a settled verdict as pending, and SliceAttributionComplete requires
// zero pending. So the "slice" is the full model-reached population, which is
// itself a bounded fraction of the scored cases and therefore inside the ticket
// budget; the seed governs the deterministic per-case perturbation, not a
// sub-sample.
func populateV12Counterfactual(
	ctx context.Context,
	runID string,
	seed int64,
	benchVersion int,
	perCase []protocol.CaseScore,
	transcripts []transcriptCase,
	specs []counterfactualCaseSpec,
	run counterfactualCaseRunner,
) counterfactualSummary {
	var summary counterfactualSummary
	if benchVersion < protocol.BenchVersionV12 || run == nil {
		return summary
	}
	// Misaligned inputs cannot be trusted to attribute counterfactuals; leaving
	// the fields unset makes the reader fail OPEN (insufficient_evidence).
	if len(perCase) != len(transcripts) || len(specs) != len(transcripts) {
		log.Printf("run %s: v12 counterfactual skipped: misaligned per-case inputs", runID)
		return summary
	}

	type eligibleCase struct {
		index  int
		caseID string
		rank   [sha256.Size]byte
	}
	eligible := make([]eligibleCase, 0, len(perCase))
	for i, score := range perCase {
		if v9base.ExcludedFromModelPopulation(score) {
			continue
		}
		if !transcripts[i].Execution.ModelInferenceObserved {
			// Never reached the model in the clean pass: cannot depend on it, and
			// the reader excludes it from the slice.
			continue
		}
		if !specs[i].populated() {
			// No re-run spec captured for this model-reached case: cannot administer
			// its counterfactual, so it stays pending and the gate fails open.
			continue
		}
		eligible = append(eligible, eligibleCase{index: i, caseID: score.CaseID, rank: counterfactualRank(seed, benchVersion, score.CaseID, "selection")})
	}
	summary.Eligible = len(eligible)
	if len(eligible) == 0 {
		return summary
	}
	sort.Slice(eligible, func(a, b int) bool {
		if c := bytes.Compare(eligible[a].rank[:], eligible[b].rank[:]); c != 0 {
			return c < 0
		}
		return eligible[a].caseID < eligible[b].caseID
	})

	for rank, candidate := range eligible {
		if ctx.Err() != nil {
			break
		}
		if rank >= counterfactualMaxCases {
			// Beyond the ticket-budget guard: leave the rest pending (fail open).
			summary.Overflow = len(eligible) - rank
			log.Printf("run %s: v12 counterfactual over budget guard (%d eligible > %d); gate will fail open",
				runID, len(eligible), counterfactualMaxCases)
			break
		}
		responder, err := ablation.NewSeededResponder(
			ablation.InterventionInference,
			counterfactualChatBudget,
			counterfactualResponderSeed(seed, benchVersion, candidate.caseID),
		)
		if err != nil {
			continue
		}
		spec := specs[candidate.index]
		ablated, ok := run.CounterfactualResponse(ctx, spec, responder)
		if !ok {
			// Unsettled: leave the case pending. The reader keeps
			// SliceAttributionComplete false until every eligible case settles.
			continue
		}
		// The re-run settled: the counterfactual is ADMINISTERED for this case, so it
		// counts toward slice-attribution completeness regardless of the tally below.
		transcripts[candidate.index].Execution.ModelCounterfactualObserved = true
		summary.Administered++

		// Grade the clean and ablated responses through the case's own deterministic
		// grader. A case whose CLEAN response is already incorrect is not evidence of
		// dependence either way: it is administered but excluded from the tally
		// (ModelCounterfactualDependent stays nil, which the reader treats as
		// out-of-denominator, NOT pending).
		cleanCorrect := spec.grade(transcripts[candidate.index].Response) >= counterfactualCorrectThreshold
		if !cleanCorrect {
			summary.Excluded++
			continue
		}
		// Correctness preserved under FULL ablation == the agent has a non-model path
		// (launderer) -> INDEPENDENT. Correctness collapsed -> the agent needed the
		// model -> DEPENDENT.
		ablatedCorrect := spec.grade(ablated) >= counterfactualCorrectThreshold
		dependent := !ablatedCorrect
		transcripts[candidate.index].Execution.ModelCounterfactualDependent = &dependent
		if dependent {
			summary.Dependent++
		} else {
			summary.Independent++
		}
	}
	log.Printf("run %s: v12 counterfactual: eligible=%d administered=%d dependent=%d independent=%d excluded=%d overflow=%d",
		runID, summary.Eligible, summary.Administered, summary.Dependent, summary.Independent, summary.Excluded, summary.Overflow)
	return summary
}
