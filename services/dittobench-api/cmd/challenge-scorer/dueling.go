package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Dueling work (bench v16+, research/dittobench-datagen/docs/dueling-bench.md
// sections 2 and 7.4). A dueling season pins up to five generators whose
// packs fill the generator half of every canonical agent run. The scorer does
// the two CPU-only pieces Platform cannot:
//
//   - battery: the season qualification battery. Each candidate generator is
//     built once and run on every secret battery seed; every pack must pass
//     the pack contract, all of them must declare the same grading policy
//     inside the season's window, and seed 0 must regenerate byte for byte.
//   - slice: one agent run's generator slice. Each pinned column runs on its
//     slice seed (challenge.SliceGenerateRequest); a pinned generator that
//     fails on the seed is replaced by the public default generator on the
//     same seed and marked substituted. The columns are encoded with
//     challenge.EncodeSliceDataset and posted with the slice digest.
//
// Builds are cached by source digest across leases (buildCache), so a season's
// binaries are built once. Like round work, a request this build cannot serve
// or a sandbox failure is reported as a fault (work "dueling") and never as a
// generator's failure. Mirrors the dueling models in
// apps/platform/ditto/api_models/generator.py.

// duelingWorkKinds is advertised on every work poll as dueling_work_kinds.
const duelingWorkKinds = "slice,battery"

// maxBatterySeeds bounds one battery lease. The season battery is 32 seeds.
const maxBatterySeeds = 64

// maxDuelingError bounds a posted column or candidate error.
const maxDuelingError = 1000

const duelingWorkPath = "/api/v1/generator-scorer/dueling-work"

type duelingWorkResponse struct {
	Work *duelingWork `json:"work"`
}

type duelingWork struct {
	Kind         string `json:"kind"` // "battery" | "slice"
	LeaseID      string `json:"lease_id"`
	SeasonID     string `json:"season_id"`
	BenchVersion int    `json:"bench_version"`

	// battery
	Cases      int                `json:"cases,omitempty"`
	Seeds      []string           `json:"seeds,omitempty"`
	Candidates []duelingCandidate `json:"candidates,omitempty"`

	// slice
	PreparationID  string               `json:"preparation_id,omitempty"`
	SeasonIndex    int64                `json:"season_index,omitempty"`
	RunSeed        int64                `json:"run_seed,omitempty"`
	CasesPerColumn int                  `json:"cases_per_column,omitempty"`
	Columns        []duelingSliceColumn `json:"columns,omitempty"`
	Fallback       *duelingSource       `json:"fallback,omitempty"`
}

// duelingSource is one generator's exact source archive and identity.
type duelingSource struct {
	SubmissionID string `json:"submission_id"`
	MinerHotkey  string `json:"miner_hotkey"`
	SourceSHA256 string `json:"source_sha256"`
	SourceBase64 string `json:"source_base64"`
	// TreeSHA256, when Platform sends it, must equal the archive's tree
	// digest; the slice records the digest of the bytes actually built.
	TreeSHA256 string `json:"tree_sha256,omitempty"`
}

type duelingCandidate struct {
	duelingSource
	OwnerKey string `json:"owner_key"`
}

type duelingSliceColumn struct {
	duelingSource
	ColumnID            string `json:"column_id"`
	OwnerKey            string `json:"owner_key"`
	GradingBenchVersion int    `json:"grading_bench_version"`
	SliceSeed           string `json:"slice_seed"`
	PublicDefault       bool   `json:"public_default"`
	// Substituted is set only by reproduce-slice, from a revealed pin: serve
	// decides substitution itself and never takes it from Platform.
	Substituted bool `json:"-"`
}

type batteryCandidateResult struct {
	SubmissionID string `json:"submission_id"`
	OK           bool   `json:"ok"`
	// GradingBenchVersion is the policy every battery pack declared; absent
	// unless OK.
	GradingBenchVersion *int   `json:"grading_bench_version,omitempty"`
	Error               string `json:"error,omitempty"`
}

type batteryResult struct {
	LeaseID       string                   `json:"lease_id"`
	SeasonID      string                   `json:"season_id"`
	ScorerVersion string                   `json:"scorer_version"`
	Candidates    []batteryCandidateResult `json:"candidates"`
}

// sliceColumnResult is one column's outcome. OK means the slice holds a valid
// pack for the column, the pinned generator's or (Substituted) the public
// default generator's. Error is the pinned generator's failure on the seed,
// set whenever the column was substituted or failed, so Platform can exclude
// the generator from the next season.
type sliceColumnResult struct {
	ColumnID    string `json:"column_id"`
	OK          bool   `json:"ok"`
	Substituted bool   `json:"substituted"`
	Error       string `json:"error,omitempty"`
}

// sliceResult carries the encoded slice only when every column is OK.
type sliceResult struct {
	LeaseID       string              `json:"lease_id"`
	PreparationID string              `json:"preparation_id"`
	ScorerVersion string              `json:"scorer_version"`
	Columns       []sliceColumnResult `json:"columns"`
	SliceSHA256   string              `json:"slice_sha256,omitempty"`
	SliceBase64   string              `json:"slice_base64,omitempty"`
}

// boundError cuts s to maxDuelingError bytes without splitting a UTF-8
// character.
func boundError(s string) string {
	if len(s) <= maxDuelingError {
		return s
	}
	end := maxDuelingError
	for end > 0 && !utf8.RuneStart(s[end]) {
		end--
	}
	return s[:end]
}

// unsupported marks a lease this build cannot serve (reported as
// unsupported_request, never as a generator failure).
func unsupported(format string, args ...any) error {
	return fmt.Errorf("%w: %s", genrun.ErrInvalidRequest, fmt.Sprintf(format, args...))
}

// decodeSource returns a source's archive bytes. Bytes that do not hash to the
// pin are a Platform inconsistency: never run, and the whole lease is
// reported as an unsupported request so Platform stops re-leasing it.
func decodeSource(label string, src duelingSource) ([]byte, error) {
	raw, err := base64.StdEncoding.DecodeString(src.SourceBase64)
	if err != nil {
		return nil, unsupported("%s: source_base64 is not base64", label)
	}
	if got := challenge.Digest(raw); got != src.SourceSHA256 {
		return nil, unsupported("%s: served source hashes to %s, lease pins %s", label, got, src.SourceSHA256)
	}
	return raw, nil
}

// buildSource builds a source through the cache and checks a pinned tree
// digest. A pinned tree that differs from the archive's is a Platform
// inconsistency, like a source digest mismatch.
func buildSource(ctx context.Context, cache *buildCache, label string, src duelingSource) (*builtGenerator, error) {
	raw, err := decodeSource(label, src)
	if err != nil {
		return nil, err
	}
	b, err := cache.get(ctx, raw)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", label, err)
	}
	if src.TreeSHA256 != "" && b.TreeSHA256 != "" && b.TreeSHA256 != src.TreeSHA256 {
		return nil, unsupported("%s: archive tree is %s, lease pins %s", label, b.TreeSHA256, src.TreeSHA256)
	}
	return b, nil
}

// processDueling leases and completes one dueling work item. It reports
// whether work was leased. A Platform that predates dueling work answers 404:
// that is idle, not an error.
func processDueling(ctx context.Context, client *platformClient, cache *buildCache) (bool, error) {
	var resp duelingWorkResponse
	if err := client.do(ctx, http.MethodGet, client.pollPath(duelingWorkPath), nil, &resp); err != nil {
		var status *httpStatusError
		if errors.As(err, &status) && status.Status == http.StatusNotFound {
			return false, nil
		}
		return false, err
	}
	w := resp.Work
	if w == nil {
		return false, nil
	}
	fault := faultReport{Work: "dueling", LeaseID: w.LeaseID, SeasonID: uuidOrEmpty(w.SeasonID), PreparationID: uuidOrEmpty(w.PreparationID)}
	switch w.Kind {
	case "battery":
		result, err := runBattery(ctx, cache, w)
		if err != nil {
			return true, reportFault(ctx, client, fault, fmt.Errorf("dueling battery %s: %w", w.LeaseID, err))
		}
		return true, client.do(ctx, http.MethodPost, "/api/v1/generator-scorer/dueling-battery", result, nil)
	case "slice":
		result, err := runSlice(ctx, cache, w)
		if err != nil {
			return true, reportFault(ctx, client, fault, fmt.Errorf("dueling slice %s: %w", w.PreparationID, err))
		}
		return true, client.do(ctx, http.MethodPost, "/api/v1/generator-scorer/dueling-slice", result, nil)
	}
	return true, reportFault(ctx, client, fault, unsupported("unknown dueling work kind %q", w.Kind))
}

// uuidPattern is Platform's UUID form. GeneratorScorerFault types season_id
// and preparation_id as optional UUIDs, so a malformed id (the reason a lease
// is refused) is omitted rather than turning the fault itself into a 422.
var uuidPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

func uuidOrEmpty(s string) string {
	if uuidPattern.MatchString(s) {
		return s
	}
	return ""
}

// seasonIDPattern mirrors the season_id rule challenge.EncodeSliceDataset
// enforces, so a malformed header is refused before anything is built.
var seasonIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)

// seasonPolicies is the pack grading window a season at benchVersion accepts.
func seasonPolicies(benchVersion int) ([]int, error) {
	if !protocol.DuelingBenchVersion(benchVersion) {
		return nil, unsupported("bench_version %d carries no dueling season (needs >= %d)", benchVersion, protocol.DuelingMinBenchVersion)
	}
	window := challenge.GradingBenchVersionsThrough(benchVersion)
	if len(window) == 0 {
		return nil, unsupported("this build grades no pack policy a v%d season accepts (supports %v)", benchVersion, challenge.SupportedGradingBenchVersions())
	}
	return window, nil
}

func containsInt(xs []int, v int) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

// runBattery runs the season qualification battery on every candidate.
func runBattery(ctx context.Context, cache *buildCache, w *duelingWork) (batteryResult, error) {
	out := batteryResult{LeaseID: w.LeaseID, SeasonID: w.SeasonID, ScorerVersion: ScorerVersion, Candidates: []batteryCandidateResult{}}
	window, err := seasonPolicies(w.BenchVersion)
	if err != nil {
		return out, err
	}
	if len(w.Seeds) == 0 || len(w.Seeds) > maxBatterySeeds {
		return out, unsupported("battery needs 1..%d seeds, got %d", maxBatterySeeds, len(w.Seeds))
	}
	// Every request is checked before anything is built, so a lease outside
	// this build's pack contract is a fault, never a candidate's failure.
	for _, c := range w.Candidates {
		for _, seed := range w.Seeds {
			if err := challenge.NewGenerateRequestFor(seed, c.MinerHotkey, w.Cases, w.BenchVersion).Validate(); err != nil {
				return out, unsupported("candidate %s: %v", c.SubmissionID, err)
			}
		}
	}
	for _, c := range w.Candidates {
		res, err := batteryCandidate(ctx, cache, w, window, c)
		if err != nil {
			return out, err
		}
		log.Printf("dueling battery %s candidate %s: ok=%t %s", w.LeaseID, c.SubmissionID, res.OK, res.Error)
		out.Candidates = append(out.Candidates, res)
	}
	return out, nil
}

func batteryCandidate(ctx context.Context, cache *buildCache, w *duelingWork, window []int, c duelingCandidate) (batteryCandidateResult, error) {
	res := batteryCandidateResult{SubmissionID: c.SubmissionID}
	b, err := buildSource(ctx, cache, "candidate "+c.SubmissionID, c.duelingSource)
	if err != nil {
		return res, err
	}
	if b.Failure != "" {
		res.Error = boundError("build: " + b.Failure)
		return res, nil
	}
	request := func(seed string) challenge.GenerateRequest {
		return challenge.NewGenerateRequestFor(seed, c.MinerHotkey, w.Cases, w.BenchVersion)
	}
	policy := 0
	var first []byte
	for i, seed := range w.Seeds {
		raw, failure, err := generateValidated(ctx, b.Prog, request(seed))
		if err != nil {
			return res, fmt.Errorf("candidate %s seed %d: %w", c.SubmissionID, i, err)
		}
		if failure != "" {
			res.Error = boundError(fmt.Sprintf("seed %d: %s", i, failure))
			return res, nil
		}
		p, err := challenge.Decode(raw)
		if err != nil {
			res.Error = boundError(fmt.Sprintf("seed %d: pack does not decode", i))
			return res, nil
		}
		switch {
		case !containsInt(window, p.GradingBenchVersion):
			res.Error = fmt.Sprintf("seed %d: pack declares grading_bench_version %d, outside the season window %v", i, p.GradingBenchVersion, window)
			return res, nil
		case i == 0:
			policy, first = p.GradingBenchVersion, raw
		case p.GradingBenchVersion != policy:
			res.Error = fmt.Sprintf("seed %d: pack declares grading_bench_version %d, seed 0 declared %d", i, p.GradingBenchVersion, policy)
			return res, nil
		}
	}
	again, failure, err := generateValidated(ctx, b.Prog, request(w.Seeds[0]))
	if err != nil {
		return res, fmt.Errorf("candidate %s determinism: %w", c.SubmissionID, err)
	}
	if failure != "" {
		res.Error = boundError("determinism: seed 0 rerun: " + failure)
		return res, nil
	}
	if !bytes.Equal(again, first) {
		res.Error = "determinism: seed 0 regenerated different pack bytes"
		return res, nil
	}
	res.OK, res.GradingBenchVersion = true, &policy
	return res, nil
}

// sliceSpec is one slice to generate: its header and its columns' pins. The
// serve path and reproduce-slice share it.
type sliceSpec struct {
	Header   challenge.SliceDataset // Columns unused
	Columns  []duelingSliceColumn
	Fallback *duelingSource
}

// checkSliceSpec refuses a slice this build cannot produce before anything is
// built: a bench version without a slice, a column set other than c0..c4, a
// pinned policy outside the window, or a request the pack contract refuses.
func checkSliceSpec(spec *sliceSpec, casesPerColumn int) error {
	window, err := seasonPolicies(spec.Header.BenchVersion)
	if err != nil {
		return err
	}
	if casesPerColumn != 0 && casesPerColumn != protocol.DuelingSliceCasesPerColumn {
		return unsupported("cases_per_column %d; the v%d slice contract is %d", casesPerColumn, spec.Header.BenchVersion, protocol.DuelingSliceCasesPerColumn)
	}
	if spec.Header.RunSeed < 0 || spec.Header.SeasonIndex < 0 {
		return unsupported("run_seed and season_index must be non-negative")
	}
	if !seasonIDPattern.MatchString(spec.Header.SeasonID) {
		return unsupported("season_id is invalid")
	}
	if len(spec.Columns) != protocol.DuelingSliceColumns {
		return unsupported("slice needs exactly %d columns, got %d", protocol.DuelingSliceColumns, len(spec.Columns))
	}
	sort.SliceStable(spec.Columns, func(i, j int) bool { return spec.Columns[i].ColumnID < spec.Columns[j].ColumnID })
	for i, col := range spec.Columns {
		if col.ColumnID != challenge.SliceColumnID(i) {
			return unsupported("columns must be exactly c0..c%d, got %q", protocol.DuelingSliceColumns-1, col.ColumnID)
		}
		if !containsInt(window, col.GradingBenchVersion) {
			return unsupported("column %s pins grading_bench_version %d, outside the v%d window %v", col.ColumnID, col.GradingBenchVersion, spec.Header.BenchVersion, window)
		}
		if col.PublicDefault == (col.OwnerKey != "") {
			return unsupported("column %s: a public default column has no owner_key and a miner column names one", col.ColumnID)
		}
		if err := challenge.SliceGenerateRequest(sliceDatasetColumn(col)).Validate(); err != nil {
			return unsupported("column %s: %v", col.ColumnID, err)
		}
		if spec.Fallback != nil && !col.PublicDefault {
			sub := substitute(sliceDatasetColumn(col), *spec.Fallback)
			if err := challenge.SliceGenerateRequest(sub).Validate(); err != nil {
				return unsupported("fallback for column %s: %v", col.ColumnID, err)
			}
		}
	}
	return nil
}

// sliceDatasetColumn is a column's pin as the slice records it, before its
// tree digest and pack are known.
func sliceDatasetColumn(col duelingSliceColumn) challenge.SliceDatasetColumn {
	return challenge.SliceDatasetColumn{
		ColumnID: col.ColumnID, SubmissionID: col.SubmissionID, OwnerKey: col.OwnerKey,
		MinerHotkey: col.MinerHotkey, GradingBenchVersion: col.GradingBenchVersion,
		SliceSeed: col.SliceSeed, PublicDefault: col.PublicDefault, Substituted: col.Substituted,
	}
}

// substitute is a column the public default generator fills on the pinned
// column's seed and policy.
func substitute(col challenge.SliceDatasetColumn, fb duelingSource) challenge.SliceDatasetColumn {
	col.SubmissionID, col.MinerHotkey, col.OwnerKey = fb.SubmissionID, fb.MinerHotkey, ""
	col.PublicDefault, col.Substituted = true, true
	col.TreeSHA256 = ""
	return col
}

// generateColumn builds src and generates col's pack on its slice seed.
func generateColumn(ctx context.Context, cache *buildCache, src duelingSource, col *challenge.SliceDatasetColumn) (failure string, err error) {
	b, err := buildSource(ctx, cache, "column "+col.ColumnID, src)
	if err != nil {
		return "", err
	}
	col.TreeSHA256 = b.TreeSHA256
	if b.Failure != "" {
		return "build: " + b.Failure, nil
	}
	raw, failure, err := generateValidated(ctx, b.Prog, challenge.SliceGenerateRequest(*col))
	if err != nil {
		return "", fmt.Errorf("column %s: %w", col.ColumnID, err)
	}
	col.Pack = raw
	return failure, nil
}

// buildSlice generates every column, substitutes the fallback for a failed
// pinned generator, and encodes the slice when every column holds a pack.
func buildSlice(ctx context.Context, cache *buildCache, spec sliceSpec) ([]sliceColumnResult, []byte, string, error) {
	var results []sliceColumnResult
	ds := spec.Header
	ds.Contract = challenge.SliceContractV1
	ds.Columns = nil
	complete := true
	for _, pin := range spec.Columns {
		res := sliceColumnResult{ColumnID: pin.ColumnID}
		col := sliceDatasetColumn(pin)
		failure, err := generateColumn(ctx, cache, pin.duelingSource, &col)
		if err != nil {
			return nil, nil, "", err
		}
		if failure == "" {
			res.OK = true
		} else {
			res.Error = boundError(failure)
			if !pin.PublicDefault && spec.Fallback != nil {
				sub := substitute(sliceDatasetColumn(pin), *spec.Fallback)
				fbFailure, err := generateColumn(ctx, cache, *spec.Fallback, &sub)
				if err != nil {
					return nil, nil, "", fmt.Errorf("fallback: %w", err)
				}
				if fbFailure == "" {
					res.OK, res.Substituted, col = true, true, sub
				} else {
					res.Error = boundError(failure + "; fallback: " + fbFailure)
				}
			}
		}
		complete = complete && res.OK
		results = append(results, res)
		ds.Columns = append(ds.Columns, col)
	}
	if !complete {
		return results, nil, "", nil
	}
	raw, sum, err := challenge.EncodeSliceDataset(ds)
	if err != nil {
		// Every pack already validated against its request; an encoding
		// failure (size, a pin the contract refuses) is posted with no slice
		// so Platform fails the preparation with the reason.
		for i := range results {
			results[i].OK = false
			results[i].Error = boundError(strings.TrimSpace(results[i].Error + " slice encoding failed: " + err.Error()))
		}
		return results, nil, "", nil
	}
	return results, raw, sum, nil
}

// runSlice generates one agent run's slice.
func runSlice(ctx context.Context, cache *buildCache, w *duelingWork) (sliceResult, error) {
	out := sliceResult{LeaseID: w.LeaseID, PreparationID: w.PreparationID, ScorerVersion: ScorerVersion}
	spec := sliceSpec{
		Header: challenge.SliceDataset{
			BenchVersion: w.BenchVersion, SeasonID: w.SeasonID, SeasonIndex: w.SeasonIndex, RunSeed: w.RunSeed,
		},
		Columns:  append([]duelingSliceColumn(nil), w.Columns...),
		Fallback: w.Fallback,
	}
	if err := checkSliceSpec(&spec, w.CasesPerColumn); err != nil {
		return out, err
	}
	results, raw, sum, err := buildSlice(ctx, cache, spec)
	if err != nil {
		return out, err
	}
	out.Columns = results
	for _, r := range results {
		log.Printf("dueling slice %s column %s: ok=%t substituted=%t %s", w.PreparationID, r.ColumnID, r.OK, r.Substituted, r.Error)
	}
	if raw != nil {
		out.SliceSHA256 = sum
		out.SliceBase64 = base64.StdEncoding.EncodeToString(raw)
	}
	return out, nil
}
