package main

// Tests for the Bench v12 Class-D answer-stuffing detection: the passive
// provenance pass that settles the two per-case telemetry fields the gate reads,
// plus the value-normalization and value-provenance rule that back it. The gate
// arithmetic itself is proven in internal/scoregates/answer_stuffing_test.go;
// here we prove the driver over the harness archetypes:
//
//   (a) an answer-stuffing agent (finished computed answer placed in the model
//       input before any completion) -> FLAGGED and gated to 0.00 at v12;
//   (b) a legit computing agent (operands in input, answer only in completion) ->
//       NOT flagged;
//   (c) a verbatim-recall RAG case (answer in seeded memory, in the retrieved
//       input) -> NOT flagged (excluded as non-computed) -- the RAG guard;
//   (d) legit multi-turn (answer first produced in an earlier completion, reused
//       in a later input) -> NOT flagged;
//   plus fail-open on missing/truncated capture, inertness before v12, and the
//   run-level share protecting an honest run with a rare numeric coincidence.

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// modeledIOSource returns a fixed per-case recorded I/O log, modeling what the
// broker captured during the clean pass. A missing key models a case whose I/O
// the broker could not supply (pending -> fail open).
type modeledIOSource map[string]caseModelIOLog

func (m modeledIOSource) CaseModelIO(caseID string) (caseModelIOLog, bool) {
	io, ok := m[caseID]
	return io, ok
}

// tokenSet models a captured per-side value-token hash set the way the broker
// records it: each surface value is hashed with the SAME hashToken the production
// extractor uses, so a modeled call and a real capture are byte-for-byte
// comparable.
func tokenSet(vals ...string) map[uint64]struct{} {
	s := make(map[uint64]struct{}, len(vals))
	for _, v := range vals {
		s[hashToken(v)] = struct{}{}
	}
	return s
}

// manyDistinctTokens returns a hash set of n distinct filler value tokens (none of
// which collide with the "100"/"37" operands or the per-case answers), modeling a
// large full-context prompt. It never marks truncation: a set well under the
// per-side ceiling is fully captured.
func manyDistinctTokens(n int, extra ...string) map[uint64]struct{} {
	s := make(map[uint64]struct{}, n+len(extra))
	for i := 0; i < n; i++ {
		s[hashToken("filler"+itoa(i))] = struct{}{}
	}
	for _, v := range extra {
		s[hashToken(v)] = struct{}{}
	}
	return s
}

func ioCall(in, out map[uint64]struct{}) caseModelCall {
	return caseModelCall{inputTokens: in, completionTokens: out}
}

// stuffCase builds one model-reached, dependence-passing scored case. Setting the
// counterfactual verdict to dependent keeps the co-resident model_dependence gate
// passing so the answer-stuffing gate is the ONLY gate that can zero the run.
func stuffCase(caseID string, computed bool, answer string) (protocol.CaseScore, transcriptCase, answerStuffingCaseSpec) {
	// A PROVABLY-computed case is also loose-computed (provable is a subset of loose),
	// so keep computedLoose aligned with computed for these archetypes; the pure
	// coinciding-value (loose-but-not-provable) archetype is built by looseCase.
	return caseWithSpec(caseID, answerStuffingCaseSpec{caseID: caseID, computed: computed, computedLoose: computed, answerCandidates: []string{answer}})
}

// looseCase builds a case that is COMPUTED in the LOOSE sense (not verbatim-recall)
// but NOT provable: its answer value ALSO appears somewhere in seeded memory, so it
// is excluded from the provable auto-gate slice (computed=false) yet counted in the
// loose systematic-review signal (computedLoose=true). This models a lets_5.0-style
// coinciding-value stuffer.
func looseCase(caseID, answer string) (protocol.CaseScore, transcriptCase, answerStuffingCaseSpec) {
	return caseWithSpec(caseID, answerStuffingCaseSpec{caseID: caseID, computed: false, computedLoose: true, answerCandidates: []string{answer}})
}

func caseWithSpec(caseID string, spec answerStuffingCaseSpec) (protocol.CaseScore, transcriptCase, answerStuffingCaseSpec) {
	dep := true
	return protocol.CaseScore{CaseID: caseID},
		transcriptCase{
			CaseID: caseID,
			Execution: runner.CaseExecution{
				ModelInferenceObserved:       true,
				ModelAttributionComplete:     true,
				ModelCounterfactualObserved:  true,
				ModelCounterfactualDependent: &dep,
			},
		},
		spec
}

// assembleAnswerStuffing drives the REAL production gate stack over the
// (now answer-stuffing-populated) per-case telemetry, mirroring
// applyV9BaseEvidence: dependence + answer-stuffing gates folded into the signed
// v9 base root. Dependence is fed from the transcripts (kept passing) so only the
// answer-stuffing gate governs the outcome.
func assembleAnswerStuffing(t *testing.T, perCase []protocol.CaseScore, transcripts []transcriptCase, cfg v9base.AnswerStuffingGateConfig) (protocol.V9BaseDetails, scoregates.Score) {
	t.Helper()
	dep := v9DependenceTelemetryForVersion(protocol.BenchVersionV12, perCase, transcripts)
	gates, err := v9base.BuildGateEvidence(protocol.BenchVersionV12, perCase, passingModelTelemetry(len(perCase)), true, dep...)
	if err != nil {
		t.Fatalf("BuildGateEvidence(v12) error = %v", err)
	}
	stuffing := v12AnswerStuffingTelemetry(protocol.BenchVersionV12, perCase, transcripts)
	gates, err = v9base.AttachAnswerStuffingGate(protocol.BenchVersionV12, gates, stuffing, cfg)
	if err != nil {
		t.Fatalf("AttachAnswerStuffingGate(v12) error = %v", err)
	}
	details, _, effective, err := v9base.Build(v9base.Inputs{
		RunID: "v12-as-run", BenchVersion: protocol.BenchVersionV12,
		ArtifactSHA256:   "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		DatasetSHA256:    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		TranscriptSHA256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		Ordinary:         scoregates.Score{Composite: 0.997012, CompositeStderr: 0.01},
		Gates:            gates,
	})
	if err != nil {
		t.Fatalf("v9base.Build(v12) error = %v", err)
	}
	if err := v9base.Validate(details); err != nil {
		t.Fatalf("v9base.Validate(v12) error = %v", err)
	}
	return details, effective
}

// (a) An answer-stuffing agent: every computed case's finished answer sits in the
// model input (and the model parrots it in the completion). The pass flags every
// case and the enforce-posture gate zeroes the composite to 0.00.

// enforceStuffingConfig is the default gate config forced to ENFORCE posture, for
// the tests that assert the hard zero-path (the default posture is penalize).
func enforceStuffingConfig() v9base.AnswerStuffingGateConfig {
	c := v9base.DefaultAnswerStuffingGateConfig()
	c.Posture = scoregates.AnswerStuffingEnforce
	return c
}

func TestV12AnswerStuffingStufferGated(t *testing.T) {
	const n = 10
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]answerStuffingCaseSpec, 0, n)
	source := modeledIOSource{}
	for i := 0; i < n; i++ {
		id := caseName(i)
		answer := answerFor(i)
		cs, tr, spec := stuffCase(id, true, answer)
		perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
		// Finished answer in the INPUT (harness-authored) before the model echoes it.
		source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet(answer), tokenSet(answer))}}
	}
	summary := populateV12AnswerStuffing("run-stuffer", protocol.BenchVersionV12, perCase, transcripts, specs, source)
	if summary.Eligible != n || summary.Stuffed != n || summary.Clean != 0 || summary.Excluded != 0 || summary.Pending != 0 {
		t.Fatalf("stuffer summary = %+v, want eligible=stuffed=%d", summary, n)
	}
	tel := v12AnswerStuffingTelemetry(protocol.BenchVersionV12, perCase, transcripts)
	if !tel.AttributionComplete || tel.EligibleCases != n || tel.StuffedCases != n {
		t.Fatalf("stuffer telemetry = %+v", tel)
	}
	details, effective := assembleAnswerStuffing(t, perCase, transcripts, enforceStuffingConfig())
	gate := details.ScoreGates.AnswerStuffing
	if gate.Result != string(scoregates.ResultAnswerStuffed) || gate.FactorBPS != 0 {
		t.Fatalf("stuffer gate not flagged/zeroed: %+v", gate)
	}
	if details.SemanticGateFactorBPS != 0 || effective != (scoregates.Score{}) {
		t.Fatalf("stuffer composite not zeroed: semantic=%d effective=%+v", details.SemanticGateFactorBPS, effective)
	}
}

// (b) A legit computing agent: operands in the input, the answer only ever in the
// completion (the model derived it). No case is flagged; the gate passes and the
// composite is preserved.
func TestV12AnswerStuffingLegitComputeClean(t *testing.T) {
	const n = 10
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]answerStuffingCaseSpec, 0, n)
	source := modeledIOSource{}
	for i := 0; i < n; i++ {
		id := caseName(i)
		answer := answerFor(i)
		cs, tr, spec := stuffCase(id, true, answer)
		perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
		// Only operands in the input; the answer appears only in the completion.
		source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet("100", "37"), tokenSet(answer))}}
	}
	summary := populateV12AnswerStuffing("run-legit", protocol.BenchVersionV12, perCase, transcripts, specs, source)
	if summary.Eligible != n || summary.Clean != n || summary.Stuffed != 0 {
		t.Fatalf("legit summary = %+v, want eligible=clean=%d stuffed=0", summary, n)
	}
	details, effective := assembleAnswerStuffing(t, perCase, transcripts, v9base.DefaultAnswerStuffingGateConfig())
	if details.ScoreGates.AnswerStuffing.Result != string(scoregates.ResultPassed) {
		t.Fatalf("legit gate not passed: %+v", details.ScoreGates.AnswerStuffing)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("legit composite not preserved: %v", effective.Composite)
	}
}

// (c) A verbatim-recall RAG case: the answer is in the seeded memory the agent
// legitimately retrieved into the model input -- but the case is non-computed, so
// it is EXCLUDED from the slice before any I/O is inspected. Even with the answer
// sitting in the input (which would flag a computed case), the gate is
// not_applicable and the composite is preserved. This is the primary RAG guard.
func TestV12AnswerStuffingVerbatimRecallExcluded(t *testing.T) {
	const n = 8
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]answerStuffingCaseSpec, 0, n)
	source := modeledIOSource{}
	for i := 0; i < n; i++ {
		id := caseName(i)
		answer := answerFor(i)
		cs, tr, spec := stuffCase(id, false, answer) // computed=false -> verbatim recall
		perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
		// The retrieved memory verbatim contains the recall answer in the input.
		source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet(answer), tokenSet(answer))}}
	}
	summary := populateV12AnswerStuffing("run-recall", protocol.BenchVersionV12, perCase, transcripts, specs, source)
	if summary.Administered != n || summary.Excluded != n || summary.Eligible != 0 || summary.Stuffed != 0 {
		t.Fatalf("recall summary = %+v, want administered=excluded=%d eligible=stuffed=0", summary, n)
	}
	for i := range transcripts {
		if !transcripts[i].Execution.AnswerStuffObserved || transcripts[i].Execution.AnswerStuffed != nil {
			t.Fatalf("verbatim case %d must be observed with a nil verdict: %+v", i, transcripts[i].Execution)
		}
	}
	tel := v12AnswerStuffingTelemetry(protocol.BenchVersionV12, perCase, transcripts)
	if !tel.AttributionComplete || tel.EligibleCases != 0 {
		t.Fatalf("recall telemetry = %+v, want eligible=0 attribution complete", tel)
	}
	details, effective := assembleAnswerStuffing(t, perCase, transcripts, v9base.DefaultAnswerStuffingGateConfig())
	if details.ScoreGates.AnswerStuffing.Result != string(scoregates.ResultNotApplicable) {
		t.Fatalf("recall gate not not_applicable: %+v", details.ScoreGates.AnswerStuffing)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("recall composite not preserved: %v", effective.Composite)
	}
}

// (d) Legit multi-turn: the model computes the value in an earlier completion and
// the agent reuses it in a later input. The value's earliest occurrence is a
// completion (call 0), before any input occurrence (call 1) -> clean.
func TestV12AnswerStuffingMultiTurnClean(t *testing.T) {
	const n = 6
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]answerStuffingCaseSpec, 0, n)
	source := modeledIOSource{}
	for i := 0; i < n; i++ {
		id := caseName(i)
		answer := answerFor(i)
		cs, tr, spec := stuffCase(id, true, answer)
		perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
		source[id] = caseModelIOLog{calls: []caseModelCall{
			ioCall(tokenSet("100", "37"), tokenSet(answer)), // model produces the answer
			ioCall(tokenSet(answer), tokenSet("done")),      // agent reuses it next turn
		}}
	}
	summary := populateV12AnswerStuffing("run-multiturn", protocol.BenchVersionV12, perCase, transcripts, specs, source)
	if summary.Eligible != n || summary.Clean != n || summary.Stuffed != 0 {
		t.Fatalf("multi-turn summary = %+v, want eligible=clean=%d stuffed=0", summary, n)
	}
	details, _ := assembleAnswerStuffing(t, perCase, transcripts, v9base.DefaultAnswerStuffingGateConfig())
	if details.ScoreGates.AnswerStuffing.Result != string(scoregates.ResultPassed) {
		t.Fatalf("multi-turn gate not passed: %+v", details.ScoreGates.AnswerStuffing)
	}
}

// buildAnswerStuffingRun drives the populate + gate stack over n stuffCase cases
// whose per-case I/O comes from withLog; a nil return models a case whose capture
// log is unavailable.
func buildAnswerStuffingRun(t *testing.T, n int, withLog func(id, answer string) (caseModelIOLog, bool)) (protocol.V9AnswerStuffingGateEvidence, scoregates.Score, answerStuffingSummary) {
	t.Helper()
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]answerStuffingCaseSpec, 0, n)
	source := modeledIOSource{}
	for i := 0; i < n; i++ {
		id := caseName(i)
		answer := answerFor(i)
		cs, tr, spec := stuffCase(id, true, answer)
		perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
		if log, ok := withLog(id, answer); ok {
			source[id] = log
		}
	}
	summary := populateV12AnswerStuffing("run-open", protocol.BenchVersionV12, perCase, transcripts, specs, source)
	details, effective := assembleAnswerStuffing(t, perCase, transcripts, v9base.DefaultAnswerStuffingGateConfig())
	return details.ScoreGates.AnswerStuffing, effective, summary
}

// Missing capture for an eligible computed case (the broker supplied no log at
// all -- a genuine capture gap the agent cannot cause) leaves it pending, so
// attribution is incomplete and the gate fails OPEN (never penalizes an honest run
// for a capture gap).
func TestV12AnswerStuffingMissingCaptureFailsOpen(t *testing.T) {
	// One case with no captured I/O -> pending -> fail open.
	missing := func(id, answer string) (caseModelIOLog, bool) {
		if id == caseName(2) {
			return caseModelIOLog{}, false
		}
		return caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet(answer), tokenSet(answer))}}, true
	}
	gate, effective, _ := buildAnswerStuffingRun(t, 6, missing)
	if gate.Result != string(scoregates.ResultInsufficientEvidence) || gate.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("missing-capture gate not fail-open: %+v", gate)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("missing-capture composite not preserved: %v", effective.Composite)
	}
}

// (c) A COMPUTED case whose capture overflowed the per-side ceiling (a
// pathological prompt beyond the model's full context window, modeled by a
// truncated log) routes the whole run to REVIEW: a distinct result that is NEITHER
// a pass NOR an auto-zero, with the composite preserved for a human to adjudicate.
// This is the safe last-resort net -- it must never fail-open (the old truncation
// bug) and never auto-zero (which would risk an honest giant-context agent).
func TestV12AnswerStuffingResidualTruncationRoutesReview(t *testing.T) {
	// One over-ceiling (truncated) computed log; every other case looks stuffed.
	truncated := func(id, answer string) (caseModelIOLog, bool) {
		if id == caseName(2) {
			return caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet(answer), tokenSet(answer))}, truncated: true}, true
		}
		return caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet(answer), tokenSet(answer))}}, true
	}
	gate, effective, summary := buildAnswerStuffingRun(t, 6, truncated)
	if summary.Review != 1 {
		t.Fatalf("expected exactly one review case, got summary %+v", summary)
	}
	if gate.Result != string(scoregates.ResultReviewRequired) || gate.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("residual-truncation gate not routed to review with full factor: %+v", gate)
	}
	if !gate.ReviewRequired {
		t.Fatalf("gate.ReviewRequired not set: %+v", gate)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("review-routed composite not preserved (must not auto-zero): %v", effective.Composite)
	}
}

// (a) The seam regression: a stuffer whose prompt is FULL-CONTEXT-sized -- far
// beyond the old 512-token bound, with tens of thousands of distinct value tokens
// -- is now FULLY captured (no truncation) because the answer sits among them, so
// it is flagged and gated to 0.00. Under the old bound this exact run truncated ->
// pending -> insufficient_evidence -> fail-open, and the stuffer escaped. Size is
// no longer a hiding place.
func TestV12AnswerStuffingLargeContextStufferGated(t *testing.T) {
	// 30000 distinct filler value tokens (>> the old 512 bound, well under the
	// 262144 ceiling) PLUS the finished answer in the input -> not truncated.
	stuffer := func(id, answer string) (caseModelIOLog, bool) {
		return caseModelIOLog{calls: []caseModelCall{
			ioCall(manyDistinctTokens(30000, answer), tokenSet(answer)),
		}}, true
	}
	gate, effective, summary := buildAnswerStuffingRun(t, 8, stuffer)
	if summary.Review != 0 || summary.Pending != 0 || summary.Stuffed != 8 || summary.Eligible != 8 {
		t.Fatalf("large-context stuffer not fully captured & flagged: %+v", summary)
	}
	// Under the default PENALIZE posture, a fully-captured stuffer takes the capped
	// penalty and is NEVER zeroed: 8/8 stuffed -> share 10000 -> factor = cap floor.
	wantFactor := scoregates.BasisPointScale - scoregates.AnswerStuffingMaxPenaltyBPS
	if gate.Result != string(scoregates.ResultAnswerStuffed) || gate.FactorBPS != wantFactor {
		t.Fatalf("large-context stuffer not penalized to capped factor %d: %+v", wantFactor, gate)
	}
	if effective == (scoregates.Score{}) {
		t.Fatalf("large-context stuffer composite zeroed under penalize (should be reduced, not zero): %+v", effective)
	}
}

// (b) An honest deep-RAG agent on a COMPUTED case retrieves a FULL-CONTEXT-sized
// prompt (tens of thousands of distinct value tokens: operands and unrelated
// evidence) but NOT the finished answer, which the model derives only in the
// completion. It is fully captured (no truncation, no review) and cleared. Size is
// never a signal: the same-sized prompt that flags a stuffer clears the honest
// agent because provenance -- not size -- is what the gate reads.
func TestV12AnswerStuffingLargeContextHonestRAGClean(t *testing.T) {
	honest := func(id, answer string) (caseModelIOLog, bool) {
		// Huge retrieved context with operands, but the answer appears only in the
		// model's completion.
		return caseModelIOLog{calls: []caseModelCall{
			ioCall(manyDistinctTokens(30000, "100", "37"), tokenSet(answer)),
		}}, true
	}
	gate, effective, summary := buildAnswerStuffingRun(t, 8, honest)
	if summary.Review != 0 || summary.Pending != 0 || summary.Clean != 8 || summary.Stuffed != 0 {
		t.Fatalf("large-context honest RAG not fully captured & clean: %+v", summary)
	}
	if gate.Result != string(scoregates.ResultPassed) || gate.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("large-context honest RAG not passed: %+v", gate)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("large-context honest RAG composite not preserved: %v", effective.Composite)
	}
}

// The capture primitive itself: a full-context-window-sized blob of distinct value
// tokens (far beyond the old 512 bound) is captured WITHOUT truncation and the
// answer token is present; only a pathological input that exceeds the per-side
// ceiling (above the model's entire context window) trips truncation. This is what
// makes size a non-signal end to end.
func TestValueTokenSetGenerousBound(t *testing.T) {
	// A realistic full-context prompt: 40000 distinct numbers plus the answer.
	var b strings.Builder
	for i := 0; i < 40000; i++ {
		b.WriteString(itoa(2_000_000 + i))
		b.WriteByte(' ')
	}
	b.WriteString("987654321") // the answer value
	tokens, truncated := valueTokenSet(b.String())
	if truncated {
		t.Fatalf("a full-context-sized prompt (40000 distinct value tokens) tripped truncation; ceiling is %d", answerIOMaxTokensPerSide)
	}
	if _, ok := tokens[hashToken("987654321")]; !ok {
		t.Fatal("answer value token not captured from the large prompt")
	}
	if len(tokens) != 40001 {
		t.Fatalf("expected 40001 distinct value-token hashes, got %d", len(tokens))
	}

	// Only a pathological input beyond the entire context window trips the net.
	var big strings.Builder
	for i := 0; i < answerIOMaxTokensPerSide+50; i++ {
		big.WriteString(itoa(3_000_000 + i))
		big.WriteByte(' ')
	}
	if _, over := valueTokenSet(big.String()); !over {
		t.Fatalf("an input beyond the %d ceiling did not trip truncation", answerIOMaxTokensPerSide)
	}
}

// lets_5.0-style MINORITY stuffer: a SINGLE provable stuffed case among many clean
// computed ones now GATES the run to 0.00 under the default MinCases=1. Each case
// here is genuinely computed (answer modeled as nowhere in seeded memory), so the
// lone stuffed case is a provable injection -- and the old majority-share rule,
// which passed a 1/5 minority, missed exactly this real exploit.
func TestV12AnswerStuffingMinorityStufferGated(t *testing.T) {
	const n = 5
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]answerStuffingCaseSpec, 0, n)
	source := modeledIOSource{}
	for i := 0; i < n; i++ {
		id := caseName(i)
		answer := answerFor(i)
		cs, tr, spec := stuffCase(id, true, answer)
		perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
		if i == 0 {
			// The lone stuffer: finished answer in the INPUT before the model echoes it.
			source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet(answer), tokenSet(answer))}}
		} else {
			// Clean computed cases: operands in the input, answer only in the completion.
			source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet("100", "37"), tokenSet(answer))}}
		}
	}
	summary := populateV12AnswerStuffing("run-minority", protocol.BenchVersionV12, perCase, transcripts, specs, source)
	if summary.Stuffed != 1 || summary.Clean != n-1 || summary.Eligible != n {
		t.Fatalf("minority summary = %+v, want stuffed=1 clean=%d eligible=%d", summary, n-1, n)
	}
	details, effective := assembleAnswerStuffing(t, perCase, transcripts, enforceStuffingConfig())
	gate := details.ScoreGates.AnswerStuffing
	if gate.Result != string(scoregates.ResultAnswerStuffed) || gate.FactorBPS != 0 {
		t.Fatalf("minority stuffer not gated: %+v", gate)
	}
	if details.SemanticGateFactorBPS != 0 || effective != (scoregates.Score{}) {
		t.Fatalf("minority stuffer composite not zeroed: semantic=%d effective=%+v", details.SemanticGateFactorBPS, effective)
	}
}

// Change 1 (provability): the operand-equals-answer coincidence is excluded at the
// SPEC level, not by a share threshold. A computed answer whose value appears
// ANYWHERE in the run's seeded memory (here it numerically equals an unrelated
// operand in a different pair) is non-computed for the slice and never flagged; a
// genuinely computed answer that appears nowhere in seeded memory stays eligible.
func TestV12AnswerStuffingOperandCoincidenceExcluded(t *testing.T) {
	// Full seeded memory: an unrelated legacy pair happens to carry the value 300.
	seeded := map[string]string{
		"p1": "the draft was approved for 500 dollars",
		"p2": "then 200 dollars was spent on supplies",
		"p3": "an unrelated legacy invoice recorded 300 dollars",
	}
	memValues := buildSeededMemoryValueSet(seeded)

	// 300 (= 500-200) is NOT verbatim in the case's own evidence (p1,p2), so the old
	// per-case rule alone would call it computed...
	coincidence := protocol.MemoryCase{ID: "coincidence", ExpectedAnswer: "300"}
	if !memoryAnswerIsComputed(coincidence, []string{"p1", "p2"}, seeded) {
		t.Fatal("precondition: coincidence answer must be non-verbatim in its own evidence")
	}
	// ...but Change 1 excludes it because 300 appears elsewhere in seeded memory.
	if memoryAnswerIsProvablyComputed(coincidence, []string{"p1", "p2"}, seeded, memValues, answerStuffingCandidates(coincidence)) {
		t.Fatal("operand-coincidence answer (value present elsewhere in memory) was not excluded by Change 1")
	}

	// A genuinely computed answer absent from all seeded memory stays in the slice.
	genuine := protocol.MemoryCase{ID: "genuine", ExpectedAnswer: "4242"}
	if !memoryAnswerIsProvablyComputed(genuine, []string{"p1", "p2"}, seeded, memValues, answerStuffingCandidates(genuine)) {
		t.Fatal("a genuinely computed answer absent from seeded memory must remain in the flagged slice")
	}
}

// Pre-v12 runs must not administer the answer-stuffing pass at all.
func TestV12AnswerStuffingInertBeforeV12(t *testing.T) {
	const n = 4
	for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV10, protocol.BenchVersionV11} {
		perCase := make([]protocol.CaseScore, 0, n)
		transcripts := make([]transcriptCase, 0, n)
		specs := make([]answerStuffingCaseSpec, 0, n)
		source := modeledIOSource{}
		for i := 0; i < n; i++ {
			id := caseName(i)
			answer := answerFor(i)
			cs, tr, spec := stuffCase(id, true, answer)
			perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
			source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet(answer), tokenSet(answer))}}
		}
		summary := populateV12AnswerStuffing("run-pre", version, perCase, transcripts, specs, source)
		if summary != (answerStuffingSummary{}) {
			t.Fatalf("v%d administered answer-stuffing: %+v", version, summary)
		}
		for i := range transcripts {
			if transcripts[i].Execution.AnswerStuffObserved || transcripts[i].Execution.AnswerStuffed != nil {
				t.Fatalf("pre-v12 run wrote answer-stuffing fields on case %d", i)
			}
		}
	}
}

// Fix B (a): a SYSTEMATIC loose stuffer. Every case injects a computed answer
// whose value ALSO appears in seeded memory, so none is provable (the auto-gate
// stays clean: eligible=0), but a systematic share feeds the finished answer into
// the model input. The loose systematic-review signal routes the whole run to
// REVIEW -- never a pass (which would silently let the launderer through) and
// never an auto-zero (which the unprovable-vs-RAG signal must never do). Composite
// is preserved for a human to adjudicate.
func TestV12AnswerStuffingLooseSystematicRoutesReview(t *testing.T) {
	const n = 6
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]answerStuffingCaseSpec, 0, n)
	source := modeledIOSource{}
	for i := 0; i < n; i++ {
		id := caseName(i)
		answer := answerFor(i)
		cs, tr, spec := looseCase(id, answer)
		perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
		// Finished answer fed into the INPUT before the model echoes it.
		source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet(answer), tokenSet(answer))}}
	}
	summary := populateV12AnswerStuffing("run-loose", protocol.BenchVersionV12, perCase, transcripts, specs, source)
	if summary.Eligible != 0 || summary.Stuffed != 0 || summary.LooseEligible != n || summary.LooseStuffed != n {
		t.Fatalf("loose summary = %+v, want eligible=stuffed=0 loose_eligible=loose_stuffed=%d", summary, n)
	}
	tel := v12AnswerStuffingTelemetry(protocol.BenchVersionV12, perCase, transcripts)
	if !tel.AttributionComplete || tel.EligibleCases != 0 || tel.LooseEligibleCases != n || tel.LooseStuffedCases != n {
		t.Fatalf("loose telemetry = %+v", tel)
	}
	details, effective := assembleAnswerStuffing(t, perCase, transcripts, v9base.DefaultAnswerStuffingGateConfig())
	gate := details.ScoreGates.AnswerStuffing
	if gate.Result != string(scoregates.ResultReviewRequired) || gate.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("systematic loose stuffer not routed to review with full factor: %+v", gate)
	}
	if gate.LooseStuffedCases != n || gate.LooseEligibleCases != n || gate.LooseStuffedBPS != scoregates.BasisPointScale {
		t.Fatalf("loose evidence not emitted: %+v", gate)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("loose-review composite not preserved (must not auto-zero): %v", effective.Composite)
	}
}

// Fix B (b): an ISOLATED coincidence. Among several loose-computed cases exactly
// ONE happens to carry its answer value in an input -- a single numeric
// coincidence, not a systematic pattern. It is below the review floor (needs >= 2
// systematic cases), so the run does NOT route to review: it is clean (provable
// eligible=0 -> not_applicable), composite preserved.
func TestV12AnswerStuffingLooseIsolatedCoincidenceClean(t *testing.T) {
	const n = 6
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]answerStuffingCaseSpec, 0, n)
	source := modeledIOSource{}
	for i := 0; i < n; i++ {
		id := caseName(i)
		answer := answerFor(i)
		cs, tr, spec := looseCase(id, answer)
		perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
		if i == 0 {
			// The lone coincidence: answer value in the input.
			source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet(answer), tokenSet(answer))}}
		} else {
			// Clean: operands in the input, answer only in the completion.
			source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet("100", "37"), tokenSet(answer))}}
		}
	}
	summary := populateV12AnswerStuffing("run-isolated", protocol.BenchVersionV12, perCase, transcripts, specs, source)
	if summary.LooseStuffed != 1 || summary.LooseEligible != n {
		t.Fatalf("isolated summary = %+v, want loose_stuffed=1 loose_eligible=%d", summary, n)
	}
	details, effective := assembleAnswerStuffing(t, perCase, transcripts, v9base.DefaultAnswerStuffingGateConfig())
	gate := details.ScoreGates.AnswerStuffing
	if gate.Result == string(scoregates.ResultReviewRequired) {
		t.Fatalf("an isolated single-case coincidence must NOT route to review: %+v", gate)
	}
	if gate.Result != string(scoregates.ResultNotApplicable) || gate.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("isolated-coincidence gate not clean: %+v", gate)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("isolated-coincidence composite not preserved: %v", effective.Composite)
	}
}

// Fix B (c): a PROVABLE stuffer is still AUTO-GATED to 0.00. The provable auto-gate
// is the only zeroing path and takes precedence over the loose review signal even
// when both fire (every case here is provable AND loose-stuffed).
func TestV12AnswerStuffingProvableStillAutoGatedWithLoose(t *testing.T) {
	const n = 5
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]answerStuffingCaseSpec, 0, n)
	source := modeledIOSource{}
	for i := 0; i < n; i++ {
		id := caseName(i)
		answer := answerFor(i)
		cs, tr, spec := stuffCase(id, true, answer) // provable (and thus loose) computed
		perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
		source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet(answer), tokenSet(answer))}}
	}
	summary := populateV12AnswerStuffing("run-provable", protocol.BenchVersionV12, perCase, transcripts, specs, source)
	if summary.Stuffed != n || summary.LooseStuffed != n {
		t.Fatalf("provable summary = %+v, want stuffed=loose_stuffed=%d", summary, n)
	}
	details, effective := assembleAnswerStuffing(t, perCase, transcripts, enforceStuffingConfig())
	gate := details.ScoreGates.AnswerStuffing
	if gate.Result != string(scoregates.ResultAnswerStuffed) || gate.FactorBPS != 0 {
		t.Fatalf("provable stuffer not auto-gated (review must not override the zero): %+v", gate)
	}
	if effective != (scoregates.Score{}) {
		t.Fatalf("provable stuffer composite not zeroed: %+v", effective)
	}
}

// Fix B (d): an HONEST agent whose computed answers appear only in COMPLETIONS (the
// model derived them) has a zero loose share, so it never routes to review and is
// clean -- even though its answers coincide with seeded-memory values (loose slice).
func TestV12AnswerStuffingLooseHonestClean(t *testing.T) {
	const n = 6
	perCase := make([]protocol.CaseScore, 0, n)
	transcripts := make([]transcriptCase, 0, n)
	specs := make([]answerStuffingCaseSpec, 0, n)
	source := modeledIOSource{}
	for i := 0; i < n; i++ {
		id := caseName(i)
		answer := answerFor(i)
		cs, tr, spec := looseCase(id, answer)
		perCase, transcripts, specs = append(perCase, cs), append(transcripts, tr), append(specs, spec)
		// Operands in the input; the answer appears only in the completion.
		source[id] = caseModelIOLog{calls: []caseModelCall{ioCall(tokenSet("100", "37"), tokenSet(answer))}}
	}
	summary := populateV12AnswerStuffing("run-honest-loose", protocol.BenchVersionV12, perCase, transcripts, specs, source)
	if summary.LooseStuffed != 0 || summary.LooseEligible != n {
		t.Fatalf("honest-loose summary = %+v, want loose_stuffed=0 loose_eligible=%d", summary, n)
	}
	details, effective := assembleAnswerStuffing(t, perCase, transcripts, v9base.DefaultAnswerStuffingGateConfig())
	gate := details.ScoreGates.AnswerStuffing
	if gate.Result == string(scoregates.ResultReviewRequired) || gate.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("honest agent must not route to review: %+v", gate)
	}
	if effective.Composite != 0.997012 {
		t.Fatalf("honest-loose composite not preserved: %v", effective.Composite)
	}
}

func caseName(i int) string { return "case-" + string(rune('a'+i)) }

// answerFor returns a distinctive computed value per case, unlikely to collide
// with the "100"/"37" operands used in the clean archetypes.
func answerFor(i int) string { return canonicalNumber(itoa(1000 + i*7)) }
