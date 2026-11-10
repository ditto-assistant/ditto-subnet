package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"math"

	"github.com/ditto-assistant/dittobench-api/internal/challengerun"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// duelingSliceMode carries a bench >= 16 run's generator slice on /v2/score
// (docs/dueling-bench.md section 7.5). The datagen half is never sent: the
// scorer regenerates it from the public seed and checks dataset_sha256 as for
// every canonical run. The slice is Platform-pinned bytes with their SHA-256.
const duelingSliceMode = "dueling-slice-v1"

// duelingSliceBenchFeaturePrefix names one bench version this scorer runs with
// a generator slice: dueling-slice-bench-<n>. The validator forwards the set as
// scorer_benchmarks.dueling_slice_bench_versions, and Platform leases a >= 16
// ticket only to a validator whose scorer advertises its version.
const duelingSliceBenchFeaturePrefix = "dueling-slice-bench-"

// maxDuelingOwnerKeyBytes bounds dueling_owner_key, the scored agent's owner
// (the challenge.SliceDatasetColumn owner_key bound).
const maxDuelingOwnerKeyBytes = 256

// duelingSliceProjectionDomain keys a slice column's opaque ID projection. The
// nonce derives from the column's slice_seed, never from fresh randomness, so a
// rerun of the same (season, run seed) sends the harness byte-identical
// requests, and each column gets its own opaque users and memory graph.
const duelingSliceProjectionDomain = "dittobench-dueling-slice-projection/v1"

// duelingSliceFeatures advertises dueling-slice-v1 and one
// dueling-slice-bench-<n> for every version in benchVersions that carries a
// slice. It is empty when none does, so a scorer that runs no dueling version
// claims no slice capability at all.
func duelingSliceFeatures(benchVersions []int) []string {
	var out []string
	for _, v := range benchVersions {
		if protocol.DuelingBenchVersion(v) {
			out = append(out, fmt.Sprintf("%s%d", duelingSliceBenchFeaturePrefix, v))
		}
	}
	if len(out) == 0 {
		return nil
	}
	return append([]string{duelingSliceMode}, out...)
}

// hasDuelingSliceFields reports whether a request carries any slice field.
func hasDuelingSliceFields(req submitRequest) bool {
	return req.DuelingSliceMode != "" || len(req.DuelingSliceBytes) != 0 ||
		req.ExpectedSliceSHA256 != "" || req.DuelingOwnerKey != nil
}

// requiresDuelingSlice reports whether a request is a canonical dueling run:
// bench >= 16 and not a generator round (a round at a dueling version is a
// private pack dataset and never carries a slice).
func requiresDuelingSlice(req submitRequest) bool {
	return protocol.DuelingBenchVersion(req.BenchVersion) && !isGeneratorRoundMode(req.PrivateDatasetMode)
}

// validateDuelingSliceRequest admits the slice fields exactly on a canonical
// bench >= 16 run, and requires all of them there. A pre-16 request or a
// generator round with any slice field is refused rather than silently
// scoring without it.
func validateDuelingSliceRequest(req submitRequest, enabled bool) error {
	has := hasDuelingSliceFields(req)
	if !requiresDuelingSlice(req) {
		if has {
			return fmt.Errorf("dueling slice is accepted only on a canonical bench_version >= %d run", protocol.DuelingMinBenchVersion)
		}
		return nil
	}
	if !enabled {
		return errors.New("dueling slice mode unavailable")
	}
	if req.DuelingSliceMode != duelingSliceMode || len(req.DuelingSliceBytes) == 0 ||
		len(req.DuelingSliceBytes) > challenge.MaxRoundDatasetBytes ||
		!canonicalSHA256(req.ExpectedSliceSHA256) || req.DuelingOwnerKey == nil {
		return errors.New("dueling slice required (dueling_slice_mode, dueling_slice_bytes, expected_slice_sha256, dueling_owner_key)")
	}
	if len(*req.DuelingOwnerKey) > maxDuelingOwnerKeyBytes {
		return errors.New("dueling_owner_key is too long")
	}
	return nil
}

// validatePracticeDuelingSlice admits a slice on practice only for the miner
// kit's LOCAL slice rehearsal (scripts/local-rehearsal.py at bench >= 16): a
// scorer started with DITTOBENCH_ALLOW_PRIVATE_HARNESS, which can never also
// load screened images (validatePrivateHarnessPosture), so neither hosted
// practice nor a validator stack takes one here. The slice must be complete
// and bound to an explicit seed, because decodeDuelingSlice requires
// run_seed == seed and a practice seed is otherwise fresh. A practice run at
// >= 16 without slice fields still scores the datagen half only. Returns ""
// when admitted.
func validatePracticeDuelingSlice(req submitRequest, localScorer bool) string {
	if !hasDuelingSliceFields(req) {
		return ""
	}
	if !localScorer {
		return "dueling slices are not accepted on practice"
	}
	// A practice slice is never a scored run: a scored /v1/submit (an expected
	// dataset digest) with a slice would skip /v2/score's ticket and inference
	// checks, so refuse it here.
	if runScope(req) == scorer.ScopeScored {
		return "a local dueling slice rehearsal cannot be a scored run (omit dataset_sha256)"
	}
	if req.Seed == 0 {
		return "a local dueling slice rehearsal requires an explicit seed (the slice is built for it)"
	}
	if err := validateDuelingSliceRequest(req, true); err != nil {
		return err.Error()
	}
	return ""
}

// practiceBodyBytes is the /v1/submit body cap: tiny on hosted practice, and
// large enough for a base64 slice on a local private-harness scorer.
func (s *server) practiceBodyBytes() int64 {
	limit := int64(maxSubmitBody)
	if s.allowPrivate {
		limit += int64((challenge.MaxRoundDatasetBytes + 2) / 3 * 4)
	}
	return limit
}

// duelingSlice is a decoded, verified slice ready to execute.
type duelingSlice struct {
	dataset  challenge.SliceDataset
	columns  []challenge.SliceColumnPack
	sha256   string
	ownerKey string
	// cases is challenge.SliceExecutionCases: the deduped run order every
	// consumer agrees on.
	cases []challenge.SliceExecutionCase
}

// decodeDuelingSlice verifies the request's slice against its pin and binds it
// to this run: the slice must be for the request's bench version and for the
// run's own seed, so a run (canonical, retest or confirmation seed) carries
// exactly one seed with exactly one slice.
func decodeDuelingSlice(req submitRequest, seed int64) (*duelingSlice, error) {
	if req.DuelingOwnerKey == nil {
		return nil, errors.New("dueling slice required")
	}
	// Bind the pinned bytes to this run before the full decode: pack
	// validation costs about a second per column, and a slice for another run
	// or version should fail fast. DecodeSliceDataset re-checks the digest.
	if challenge.Digest(req.DuelingSliceBytes) != req.ExpectedSliceSHA256 {
		return nil, errors.New("slice dataset: lease digest mismatch")
	}
	var header struct {
		BenchVersion int   `json:"bench_version"`
		RunSeed      int64 `json:"run_seed"`
	}
	if err := json.Unmarshal(req.DuelingSliceBytes, &header); err != nil {
		return nil, errors.New("slice dataset: invalid JSON")
	}
	if header.BenchVersion != req.BenchVersion {
		return nil, fmt.Errorf("dueling slice is for bench_version %d, the run is %d", header.BenchVersion, req.BenchVersion)
	}
	if header.RunSeed != seed {
		return nil, errors.New("dueling slice was not built for this run's seed")
	}
	ds, columns, err := challenge.DecodeSliceDataset(req.DuelingSliceBytes, req.ExpectedSliceSHA256)
	if err != nil {
		return nil, err
	}
	if ds.BenchVersion != req.BenchVersion || ds.RunSeed != seed {
		return nil, errors.New("dueling slice does not bind this run")
	}
	return &duelingSlice{
		dataset: ds, columns: columns, sha256: req.ExpectedSliceSHA256,
		ownerKey: *req.DuelingOwnerKey, cases: challenge.SliceExecutionCases(columns),
	}, nil
}

// duelingSliceProjectionNonce is the opaque ID projection key of one column.
func duelingSliceProjectionNonce(sliceSeed string) []byte {
	sum := sha256.Sum256([]byte(duelingSliceProjectionDomain + "\x00" + sliceSeed))
	return sum[:]
}

// duelingSliceEnv is the live run a slice executes inside: the same harness,
// ticket inference gateway, tool catalog and observed tool endpoint the
// datagen half used.
type duelingSliceEnv struct {
	harnessURL   string
	benchVersion int
	seed         int64
	tools        []protocol.ToolDefinition
	toolSrv      *toolexec.Server
	toolEndpoint observedToolEndpoint
	gateway      string
	concurrency  int
	// progress, when set, reports cumulative executed slice cases.
	progress func(done int)
	// budget, when set, is the run's bench v18 cost budget; a slice case it
	// refuses never runs and scores 0 (run_budget.go).
	budget *runBudget
}

// duelingSliceResult is everything about a slice run that leaves the scorer:
// per-column aggregates and score-only per-case rows. Case text, transcripts
// and grader notes never do (open question 6).
type duelingSliceResult struct {
	columns []protocol.DuelingColumnDetails
	rows    []protocol.CaseScore
}

// executeDuelingSlice runs every slice column after the datagen waves, column
// by column, through challengerun: each column's waves are seeded into its own
// opaque memory graph, its cases run with the run's case concurrency, and each
// case is graded by scorer.GradeMemory at the column's pinned
// grading_bench_version (the generator-round grader). A case another column
// already claimed is not run (challenge.SliceExecutionCases). A case whose
// /run failed scores 0 and counts toward its column. A seeding failure aborts
// with challengerun.ErrHarnessUnavailable.
func executeDuelingSlice(ctx context.Context, slice *duelingSlice, env duelingSliceEnv) (duelingSliceResult, error) {
	include := map[string]map[string]bool{}
	for _, c := range slice.cases {
		if include[c.ColumnID] == nil {
			include[c.ColumnID] = map[string]bool{}
		}
		include[c.ColumnID][c.Case.ID] = true
	}
	prepareCase := func(packCaseID, opaqueCaseID, opaqueUserID string) string {
		env.toolSrv.Register(opaqueCaseID, toolexec.BuildFixtureForVersion(env.seed, protocol.ToolCase{ID: packCaseID}, env.benchVersion))
		return env.toolEndpoint.forCase(opaqueCaseID, opaqueUserID)
	}
	result := duelingSliceResult{
		columns: make([]protocol.DuelingColumnDetails, 0, len(slice.columns)),
		rows:    make([]protocol.CaseScore, 0, len(slice.cases)),
	}
	for _, col := range slice.columns {
		id := col.Column.ColumnID
		agg := protocol.DuelingColumnDetails{
			ColumnID: id, OwnerKey: col.Column.OwnerKey, SubmissionID: col.Column.SubmissionID,
			GradingBenchVersion: col.Column.GradingBenchVersion, Substituted: col.Column.Substituted,
		}
		wanted := include[id]
		if len(wanted) > 0 {
			outcomes, err := challengerun.Execute(ctx, env.harnessURL, col.Pack, challengerun.Options{
				Concurrency: env.concurrency,
				// The agent runs at the run's bench version; each case is graded at
				// its column's pinned policy (DecodeSliceDataset checked the pack
				// declares it).
				BenchVersion: env.benchVersion,
				Nonce:        duelingSliceProjectionNonce(col.Column.SliceSeed),
				InferenceBaseURL: func(opaqueCaseID string) string {
					return v13CaseInferenceBaseURL(env.benchVersion, env.gateway, opaqueCaseID)
				},
				Tools:         env.tools,
				ToolEndpoint:  prepareCase,
				ObservedCalls: env.toolSrv.Observed,
				Include:       func(packCaseID string) bool { return wanted[packCaseID] },
				Admit: func() bool {
					if env.budget.admit() {
						return true
					}
					env.budget.refuse("slice")
					return false
				},
				RefusalNote: env.budget.refusalNote,
				Exhausted:   env.budget.isExhausted,
			})
			if err != nil {
				return duelingSliceResult{}, fmt.Errorf("column %s: %w", id, err)
			}
			for _, o := range outcomes {
				// A failed /run is a 0, exactly as a datagen case that never
				// answered: the column mean must not reward silence.
				score := o.Score
				if o.Fault || math.IsNaN(score) {
					score = 0
				}
				if o.BudgetExhausted {
					// Bench v18: never run; scores 0 inside the column, like a
					// datagen case the budget refused.
					score = 0
					agg.BudgetExhausted++
				}
				agg.N++
				agg.Sum += score
				agg.SumSq += score * score
				row := protocol.CaseScore{
					CaseID:          challenge.RoundCaseID(id, o.CaseID),
					Category:        challenge.SliceCaseCategory,
					Kind:            protocol.KindMemory,
					Score:           score,
					BudgetExhausted: o.BudgetExhausted,
				}
				if o.BudgetExhausted {
					// The refusal's specific reason (spent budget or lost meter).
					row.Notes = append([]string(nil), o.Notes...)
				}
				result.rows = append(result.rows, row)
			}
			if env.progress != nil {
				env.progress(len(result.rows))
			}
		}
		result.columns = append(result.columns, agg)
	}
	return result, nil
}

// duelingAggregates projects the reported columns onto the composite inputs.
func duelingAggregates(columns []protocol.DuelingColumnDetails) []challenge.DuelingColumnAggregate {
	out := make([]challenge.DuelingColumnAggregate, len(columns))
	for i, c := range columns {
		out[i] = challenge.DuelingColumnAggregate{
			ColumnID: c.ColumnID, OwnerKey: c.OwnerKey, N: c.N, Sum: c.Sum, SumSq: c.SumSq, Substituted: c.Substituted,
		}
	}
	return out
}

// duelingSelfMask is the reported composite's mask: the scored agent's owner.
// An empty owner masks nothing (DuelingComposite never masks "").
func duelingSelfMask(ownerKey string) map[string]bool {
	return map[string]bool{ownerKey: true}
}

// duelingOrdinaryScore is the composite the datagen-only gate stack (v9 score
// gates) then scales: gate_datagen x ((1 - w x |U|) x datagen + w x sum col_c)
// with the composite gate alone, quantized to micros like every ordinary
// composite. Every gate input is a datagen case: the generator slice lowers a
// score through its column means, never through a gate.
//
// The stderr is deliberately at the composite gate only, like the composite:
// applyV9BaseEvidence then scales both by the applied v9 factor
// (scoregates.ApplyForVersion), which keeps composite_stderr equal to the
// signed effective_stderr_micros. DuelingCompositeStderr is linear in the
// gate, so the result matches details.dueling.composite_stderr (computed with
// the post-v9 gate) to within micro rounding.
func duelingOrdinaryScore(half scorer.DatagenHalf, columns []protocol.DuelingColumnDetails, ownerKey string) (composite, stderr float64, err error) {
	aggs := duelingAggregates(columns)
	if err := challenge.CheckDuelingColumns(aggs); err != nil {
		return 0, 0, err
	}
	mask := duelingSelfMask(ownerKey)
	composite = challenge.DuelingComposite(half.Raw, half.Gate, aggs, mask)
	stderr = challenge.DuelingCompositeStderr(half.RawStderr, half.Gate, aggs, mask)
	return math.Round(composite*1e6) / 1e6, math.Round(stderr*1e6) / 1e6, nil
}

// duelingReportDetails is the details.dueling block. datagenGate is the
// product of every factor applied to the run's composite: the composite gate
// (from bench 16 already carrying the applied priced-cost factor,
// applyPricedCostToHalf),
// the token-efficiency multiplier (always 1 under the v7+ quality-only
// contract, carried so the identity holds by construction) and, from v9, the
// applied score-gate factor of the signed base evidence. With it
// the validator fold recomputes the composite under any owner mask; under the
// self mask it reproduces the reported composite to within its micro rounding.
func duelingReportDetails(slice *duelingSlice, half scorer.DatagenHalf, efficiency *protocol.TokenEfficiency, v9 *protocol.V9BaseDetails, columns []protocol.DuelingColumnDetails) *protocol.DuelingDetails {
	gate := half.Gate
	if efficiency != nil {
		gate = gate * efficiency.Multiplier
	}
	if v9 != nil {
		gate = gate * (float64(v9.AppliedGateFactorBPS) / float64(scoregates.BasisPointScale))
	}
	aggs := duelingAggregates(columns)
	return &protocol.DuelingDetails{
		SeasonID:        slice.dataset.SeasonID,
		SeasonIndex:     slice.dataset.SeasonIndex,
		SliceSHA256:     slice.sha256,
		OwnerKey:        slice.ownerKey,
		DatagenRaw:      half.Raw,
		DatagenGate:     gate,
		ColumnWeight:    protocol.DuelingColumnWeight,
		Columns:         columns,
		CompositeStderr: challenge.DuelingCompositeStderr(half.RawStderr, gate, aggs, duelingSelfMask(slice.ownerKey)),
	}
}

// relayDatagenInterval is a dueling run's relay accounting up to the
// datagen/slice boundary.
type relayDatagenInterval struct {
	usage     protocol.TokenUsage
	execution relayExecutionSummary
}

// datagenSessionTotals are the session-wide totals the datagen half's gates
// and shadow summaries read: v10+ tool provenance, v13+ catalog capture and
// the v13+ unattributed inference cost (the inference_cost summary).
// nil means unavailable, which each consumer already treats as "no totals"
// (never as zero). A dueling run reads them exactly once, at the datagen/slice
// boundary, so no slice completion can reach catalog_gate,
// catalog_suppression_rate or the tool-provenance summary.
type datagenSessionTotals struct {
	toolProvenance *sessionToolProvenanceTotals
	catalog        *sessionCatalogTotals
	// unattributedCost is the session's completions no case claimed; the zero
	// record when there is no session, as summarizeV13InferenceCost reads it.
	unattributedCost scoregates.InferenceCostRecord
}

// captureDatagenSessionTotals reads both totals from the ticket session.
func (s *server) captureDatagenSessionTotals(benchVersion int, sessionID string) datagenSessionTotals {
	var totals datagenSessionTotals
	if sessionID == "" || s.broker == nil {
		return totals
	}
	if benchVersion >= protocol.BenchVersionV10 {
		if t, ok := s.broker.sessionToolProvenanceTotals(sessionID); ok {
			totals.toolProvenance = &t
		}
	}
	if benchVersion >= scorer.CatalogGateBenchVersion {
		if t, ok := s.broker.sessionCatalogTotals(sessionID); ok {
			totals.catalog = &t
		}
	}
	if benchVersion >= protocol.BenchVersionV13 {
		totals.unattributedCost = s.broker.sessionUnattributedInferenceCost(sessionID)
	}
	return totals
}

// datagenSessionTotalsAtScoring is what the datagen gates read at scoring
// time. A run without a slice reads the session now, exactly as before. A
// dueling run returns its boundary capture unchanged, including a capture
// that found nothing: it never falls back to a post-slice read.
func (s *server) datagenSessionTotalsAtScoring(dueling bool, boundary datagenSessionTotals, benchVersion int, sessionID string) datagenSessionTotals {
	if dueling {
		return boundary
	}
	return s.captureDatagenSessionTotals(benchVersion, sessionID)
}
