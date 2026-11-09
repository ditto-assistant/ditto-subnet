package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/challengerun"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-api/internal/store"
	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

var updateDuelingGolden = flag.Bool("update-dueling-golden", false, "rewrite testdata/dueling_report_golden.json")

const (
	sliceTestHotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
	sliceTestSeason = "0b6f1c1e-6a52-4d8e-9a57-3f0c2d1e4b5a"
	// sliceTestSeasonSeed is any 64-hex season seed; slice seeds derive from it.
	sliceTestSeasonSeed = "4f7c2a1e9b3d5f6081a2c4e6f8091b2d3c4e5f60718293a4b5c6d7e8f9012345"
	sliceTestOwner      = "coldkey:owner-c2"
)

// sliceTestPolicy is the newest pack policy a slice at the dueling floor
// accepts: the policy a v16 season would pin by default.
func sliceTestPolicy(t *testing.T) int {
	t.Helper()
	accepted := challenge.GradingBenchVersionsThrough(protocol.DuelingMinBenchVersion)
	if len(accepted) == 0 {
		t.Fatalf("a v%d slice accepts no pack policy", protocol.DuelingMinBenchVersion)
	}
	return accepted[len(accepted)-1]
}

// sliceTestPack is a valid DuelingSliceCasesPerColumn-case pack of update
// chains (the reference generator's shape). variant makes every case's
// question and answer distinct from another variant's, so two columns with the
// same variant are exact duplicates for challenge.SliceExecutionCases.
func sliceTestPack(t *testing.T, variant int, sliceSeed string, policy int) json.RawMessage {
	t.Helper()
	waves := make([]protocol.SeedRequest, 3)
	for i := range waves {
		waves[i].Wave = i
	}
	pairs := 0
	add := func(wave int, prompt, response string) string {
		pairs++
		id := fmt.Sprintf("p%04d", pairs)
		waves[wave].Pairs = append(waves[wave].Pairs, protocol.MemoryPair{
			PairID: id, SessionID: fmt.Sprintf("s%02d", wave*10+pairs%7),
			Timestamp: time.Date(2026, time.January, 5, 9, 0, 0, 0, time.UTC).Add(time.Duration(pairs) * 37 * time.Minute).Format(time.RFC3339),
			Prompt:    prompt, Response: response,
		})
		return id
	}
	cases := make([]challenge.Case, 0, protocol.DuelingSliceCasesPerColumn)
	for i := 0; i < protocol.DuelingSliceCasesPerColumn; i++ {
		base := 1000 + variant*500 + i*3
		old, decoy, fresh := fmt.Sprint(base), fmt.Sprint(base+1), fmt.Sprint(base+2)
		thing := fmt.Sprintf("locker %d-%d code", variant, i)
		decoyThing := fmt.Sprintf("garage keypad %d-%d", variant, i)
		add(0, fmt.Sprintf("Quick note so I don't forget: my %s is %s.", thing, old), fmt.Sprintf("Noted, your %s is %s.", thing, old))
		add(1, fmt.Sprintf("Also, the %s is %s.", decoyThing, decoy), fmt.Sprintf("Got it, %s is %s.", decoyThing, decoy))
		ev := add(2, fmt.Sprintf("They reset my %s this morning. The new one is %s; %s no longer works.", thing, fresh, old), fmt.Sprintf("Updated: your %s is now %s.", thing, fresh))
		cases = append(cases, challenge.Case{
			MemoryCase: protocol.MemoryCase{
				ID: fmt.Sprintf("code-update-%d", i), QuestionType: "knowledge-update",
				Question:       fmt.Sprintf("What is my %s right now?", thing),
				ExpectedAnswer: fresh, AnswerKind: protocol.AnswerNumber,
				DistractorAnswers: []string{old, decoy},
			},
			RunAfterWave: 2, EvidencePairIDs: []string{ev},
		})
	}
	raw, err := json.Marshal(challenge.Pack{
		Contract: challenge.ContractV1, GradingBenchVersion: policy, MinerHotkey: sliceTestHotkey,
		Generator:   challenge.Generator{Name: "slice-test", Version: "1", Seed: sliceSeed},
		MemoryWaves: waves, Cases: cases,
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

type sliceTestKey struct {
	runSeed  int64
	variants [5]int
}

type sliceTestEncoded struct {
	raw []byte
	sum string
}

var (
	sliceTestMu      sync.Mutex
	sliceTestEncodes = map[sliceTestKey]sliceTestEncoded{}
	sliceTestDecodes = map[sliceTestKey]*duelingSlice{}
)

// sliceTestDataset encodes a full slice for runSeed. variants[i] is column
// ci's pack variant; c2 is owned by sliceTestOwner, c4 is a public default
// fill and c3 a substituted column.
//
// Pack validation (ValidateGenerated) costs about a second per 35-case pack,
// so the canonical bytes are assembled directly (compact packs plus their
// digests, exactly what EncodeSliceDataset emits) and memoized; decoding
// still validates every pack.
func sliceTestDataset(t *testing.T, runSeed int64, variants [5]int) ([]byte, string) {
	t.Helper()
	key := sliceTestKey{runSeed, variants}
	sliceTestMu.Lock()
	defer sliceTestMu.Unlock()
	if enc, ok := sliceTestEncodes[key]; ok {
		return enc.raw, enc.sum
	}
	policy := sliceTestPolicy(t)
	ds := challenge.SliceDataset{
		Contract: challenge.SliceContractV1, BenchVersion: protocol.DuelingMinBenchVersion,
		SeasonID: sliceTestSeason, SeasonIndex: 26119, RunSeed: runSeed,
	}
	for i := 0; i < protocol.DuelingSliceColumns; i++ {
		id := challenge.SliceColumnID(i)
		seed, err := challenge.SliceSeed(sliceTestSeasonSeed, runSeed, id)
		if err != nil {
			t.Fatal(err)
		}
		col := challenge.SliceDatasetColumn{
			ColumnID: id, SubmissionID: "sub-" + id, OwnerKey: "coldkey:owner-" + id,
			MinerHotkey: sliceTestHotkey, TreeSHA256: challenge.Digest([]byte("tree:" + id)),
			GradingBenchVersion: policy, SliceSeed: seed,
			Pack: sliceTestPack(t, variants[i], seed, policy),
		}
		switch i {
		case 3:
			col.SubmissionID, col.OwnerKey, col.PublicDefault, col.Substituted = "default", "", true, true
		case 4:
			col.SubmissionID, col.OwnerKey, col.PublicDefault = "default", "", true
		}
		compact, packSum, err := challenge.CompactPack(col.Pack)
		if err != nil {
			t.Fatal(err)
		}
		col.Pack, col.PackSHA256 = compact, packSum
		ds.Columns = append(ds.Columns, col)
	}
	raw, err := json.Marshal(ds)
	if err != nil {
		t.Fatal(err)
	}
	sum := challenge.Digest(raw)
	sliceTestEncodes[key] = sliceTestEncoded{raw, sum}
	return raw, sum
}

// decodedTestSlice is the decoded slice of duelingRequest(runSeed, variants),
// memoized: it is read-only once decoded.
func decodedTestSlice(t *testing.T, runSeed int64, variants [5]int) *duelingSlice {
	t.Helper()
	req := duelingRequest(t, runSeed, variants)
	key := sliceTestKey{runSeed, variants}
	sliceTestMu.Lock()
	defer sliceTestMu.Unlock()
	if slice, ok := sliceTestDecodes[key]; ok {
		return slice
	}
	slice, err := decodeDuelingSlice(req, runSeed)
	if err != nil {
		t.Fatal(err)
	}
	sliceTestDecodes[key] = slice
	return slice
}

func ownerKey(s string) *string { return &s }

func duelingRequest(t *testing.T, runSeed int64, variants [5]int) submitRequest {
	t.Helper()
	raw, sum := sliceTestDataset(t, runSeed, variants)
	return submitRequest{
		BenchVersion: protocol.DuelingMinBenchVersion, Seed: runSeed,
		DuelingSliceMode: duelingSliceMode, DuelingSliceBytes: raw,
		ExpectedSliceSHA256: sum, DuelingOwnerKey: ownerKey(sliceTestOwner),
	}
}

// --- capability ----------------------------------------------------------

func TestDuelingSliceFeaturesDeriveFromTheAdvertisedVersions(t *testing.T) {
	if got := duelingSliceFeatures([]int{8, 9, 13, 14, 15}); got != nil {
		t.Fatalf("a scorer that runs no dueling version advertised %v", got)
	}
	got := duelingSliceFeatures([]int{14, 15, 16})
	if want := []string{"dueling-slice-v1", "dueling-slice-bench-16"}; !slices.Equal(got, want) {
		t.Fatalf("features with 16 advertised = %v, want %v", got, want)
	}
	got = duelingSliceFeatures([]int{15, 16, 17})
	if want := []string{"dueling-slice-v1", "dueling-slice-bench-16", "dueling-slice-bench-17"}; !slices.Equal(got, want) {
		t.Fatalf("every dueling version must be advertised: %v", got)
	}
}

// datasetFeatures follows supportedBenchVersions, so the flip that raises
// advertisedMaxBenchVersion to 16 advertises the slice mode with no other
// change, and a build that does not run 16 never claims it.
func TestDatasetFeaturesAdvertiseDuelingExactlyForRunVersions(t *testing.T) {
	s := &server{allowDuelingSlices: true}
	features := s.datasetFeatures()
	runsDueling := slices.ContainsFunc(supportedBenchVersions(), protocol.DuelingBenchVersion)
	if !runsDueling || !slices.Contains(features, "dueling-slice-bench-16") {
		t.Fatalf("the flip advertises bench 16 with its slice mode: %v / %v", supportedBenchVersions(), features)
	}
	if slices.Contains(features, duelingSliceMode) != runsDueling {
		t.Fatalf("dueling-slice-v1 advertised=%v, but the scorer runs a dueling version=%v (%v)",
			slices.Contains(features, duelingSliceMode), runsDueling, supportedBenchVersions())
	}
	for _, v := range supportedBenchVersions() {
		want := protocol.DuelingBenchVersion(v)
		if got := slices.Contains(features, fmt.Sprintf("dueling-slice-bench-%d", v)); got != want {
			t.Fatalf("dueling-slice-bench-%d advertised=%v want %v", v, got, want)
		}
	}
	if off := (&server{}).datasetFeatures(); slices.ContainsFunc(off, func(f string) bool { return strings.HasPrefix(f, "dueling-slice") }) {
		t.Fatalf("a scorer outside screened-image mode advertised a slice feature: %v", off)
	}
	if (&server{allowDuelingSlices: true}).privateBodyBytes() < challenge.MaxRoundDatasetBytes {
		t.Fatal("a slice-capable scorer must accept a MaxRoundDatasetBytes slice body")
	}
}

// --- request admission -----------------------------------------------------

func TestValidateDuelingSliceRequest(t *testing.T) {
	sha := strings.Repeat("a", 64)
	full := func(v int) submitRequest {
		return submitRequest{
			BenchVersion: v, DuelingSliceMode: duelingSliceMode, DuelingSliceBytes: []byte("{}"),
			ExpectedSliceSHA256: sha, DuelingOwnerKey: ownerKey("coldkey:x"),
		}
	}
	for _, tc := range []struct {
		name    string
		req     submitRequest
		enabled bool
		want    string
	}{
		{"v16 with slice", full(16), true, ""},
		{"v17 with slice (a floor)", full(17), true, ""},
		{"v16 with an empty owner", func() submitRequest { r := full(16); r.DuelingOwnerKey = ownerKey(""); return r }(), true, ""},
		{"v15 without slice", submitRequest{BenchVersion: 15}, true, ""},
		{"v16 without slice", submitRequest{BenchVersion: 16}, true, "dueling slice required"},
		{"v16 without owner", func() submitRequest { r := full(16); r.DuelingOwnerKey = nil; return r }(), true, "dueling slice required"},
		{"v16 wrong mode", func() submitRequest { r := full(16); r.DuelingSliceMode = "generator-round-v1"; return r }(), true, "dueling slice required"},
		{"v16 non-canonical digest", func() submitRequest { r := full(16); r.ExpectedSliceSHA256 = strings.ToUpper(sha); return r }(), true, "dueling slice required"},
		{"v16 oversized", func() submitRequest {
			r := full(16)
			r.DuelingSliceBytes = make([]byte, challenge.MaxRoundDatasetBytes+1)
			return r
		}(), true, "dueling slice required"},
		{"v16 overlong owner", func() submitRequest {
			r := full(16)
			r.DuelingOwnerKey = ownerKey(strings.Repeat("k", maxDuelingOwnerKeyBytes+1))
			return r
		}(), true, "too long"},
		{"v16 disabled", full(16), false, "unavailable"},
		{"v15 with slice", full(15), true, "only on a canonical"},
		{"v15 with only an owner", submitRequest{BenchVersion: 15, DuelingOwnerKey: ownerKey("")}, true, "only on a canonical"},
		{"v16 generator round", submitRequest{BenchVersion: 16, PrivateDatasetMode: generatorRoundMode}, true, ""},
		{"v16 generator round with slice", func() submitRequest { r := full(16); r.PrivateDatasetMode = generatorRoundMode; return r }(), true, "only on a canonical"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := validateDuelingSliceRequest(tc.req, tc.enabled)
			switch {
			case tc.want == "" && err != nil:
				t.Fatalf("refused: %v", err)
			case tc.want != "" && (err == nil || !strings.Contains(err.Error(), tc.want)):
				t.Fatalf("err = %v, want %q", err, tc.want)
			}
		})
	}
}

// The handler refuses slice fields below the dueling floor before anything
// runs, and practice never accepts them.
func TestScoreAndPracticeRefuseMisplacedDuelingSlices(t *testing.T) {
	sha := strings.Repeat("b", 64)
	body := `{"bench_version":15,"run_size":"small","seed":7,"dataset_sha256":"` + sha +
		`","harness_url":"http://127.0.0.1:9","inference_session_id":"session-1234567890",` +
		`"dueling_slice_mode":"dueling-slice-v1","dueling_slice_bytes":"e30=","expected_slice_sha256":"` + sha + `","dueling_owner_key":""}`
	rr := httptest.NewRecorder()
	s := newScoreTestServer()
	s.allowDuelingSlices = true
	s.handleVersionedScore(rr, httptest.NewRequest(http.MethodPost, "/v2/score", strings.NewReader(body)))
	if rr.Code != http.StatusBadRequest || !strings.Contains(rr.Body.String(), "dueling slice") {
		t.Fatalf("v15 /v2/score with a slice = %d %s", rr.Code, rr.Body.String())
	}
	rr = submitOn(newPracticeTestServer(), `{"bench_version":15,"run_size":"small","harness_url":"http://127.0.0.1:9","dueling_owner_key":""}`)
	if rr.Code != http.StatusBadRequest || !strings.Contains(rr.Body.String(), "dueling slices are not accepted on practice") {
		t.Fatalf("practice with a slice field = %d %s", rr.Code, rr.Body.String())
	}
}

// Only a local private-harness scorer (the miner kit's slice rehearsal) takes
// a slice on practice, complete and bound to an explicit seed; hosted
// practice refuses one at any version.
func TestPracticeAdmitsALocalDuelingSliceOnly(t *testing.T) {
	sha := strings.Repeat("b", 64)
	full := `"dueling_slice_mode":"dueling-slice-v1","dueling_slice_bytes":"e30=","expected_slice_sha256":"` + sha + `","dueling_owner_key":""`
	for _, tc := range []struct {
		name  string
		local bool
		body  string
		want  string
	}{
		{"hosted v16", false, `{"bench_version":16,"seed":7,` + full + `}`, "dueling slices are not accepted on practice"},
		{"local v16 complete", true, `{"bench_version":16,"seed":7,` + full + `}`, ""},
		{"local v16 without a seed", true, `{"bench_version":16,` + full + `}`, "explicit seed"},
		{"local v16 partial", true, `{"bench_version":16,"seed":7,"dueling_owner_key":""}`, "dueling slice required"},
		{"local v15 with a slice", true, `{"bench_version":15,"seed":7,` + full + `}`, "only on a canonical"},
		{"local v16 without a slice", true, `{"bench_version":16,"seed":7}`, ""},
		{"local v16 scored", true, `{"bench_version":16,"seed":7,"dataset_sha256":"` + sha + `",` + full + `}`, "cannot be a scored run"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var req submitRequest
			if err := json.Unmarshal([]byte(tc.body), &req); err != nil {
				t.Fatal(err)
			}
			got := validatePracticeDuelingSlice(req, tc.local)
			if (tc.want == "") != (got == "") || !strings.Contains(got, tc.want) {
				t.Fatalf("validatePracticeDuelingSlice = %q, want %q", got, tc.want)
			}
		})
	}
	if (&server{}).practiceBodyBytes() != maxSubmitBody {
		t.Fatal("hosted practice keeps the tiny body cap")
	}
	if (&server{allowPrivate: true}).practiceBodyBytes() < int64(challenge.MaxRoundDatasetBytes) {
		t.Fatal("a local scorer must accept a full base64 slice body")
	}
}

func TestDecodeDuelingSliceBindsVersionSeedAndDigest(t *testing.T) {
	req := duelingRequest(t, 777, [5]int{0, 1, 2, 3, 4})
	slice := decodedTestSlice(t, 777, [5]int{0, 1, 2, 3, 4})
	if slice.ownerKey != sliceTestOwner || slice.sha256 != req.ExpectedSliceSHA256 || len(slice.columns) != protocol.DuelingSliceColumns ||
		len(slice.cases) != protocol.DuelingSliceColumns*protocol.DuelingSliceCasesPerColumn {
		t.Fatalf("decoded slice = owner %q sha %s columns %d cases %d", slice.ownerKey, slice.sha256, len(slice.columns), len(slice.cases))
	}
	if _, err := decodeDuelingSlice(req, 778); err == nil {
		t.Fatal("a slice built for another run seed was accepted: one seed carries exactly one slice")
	}
	other := req
	other.BenchVersion = protocol.DuelingMinBenchVersion + 1
	if _, err := decodeDuelingSlice(other, 777); err == nil {
		t.Fatal("a slice for another bench version was accepted")
	}
	tampered := req
	tampered.ExpectedSliceSHA256 = strings.Repeat("0", 64)
	if _, err := decodeDuelingSlice(tampered, 777); err == nil {
		t.Fatal("a slice that does not hash to its pin was accepted")
	}
}

// --- execution -------------------------------------------------------------

// sliceHarness answers each question by a policy and records the wire.
type sliceHarness struct {
	mu     sync.Mutex
	seeds  []protocol.SeedRequest
	runs   []protocol.RunRequest
	answer func(question string) (string, int)
}

func (h *sliceHarness) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/health":
		w.WriteHeader(http.StatusOK)
	case "/seed":
		var req protocol.SeedRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		h.mu.Lock()
		h.seeds = append(h.seeds, req)
		h.mu.Unlock()
		_ = json.NewEncoder(w).Encode(protocol.SeedResponse{Pairs: len(req.Pairs)})
	case "/run":
		var req protocol.RunRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		h.mu.Lock()
		h.runs = append(h.runs, req)
		h.mu.Unlock()
		text, status := "", http.StatusOK
		if h.answer != nil {
			text, status = h.answer(req.UserInput)
		}
		if status != http.StatusOK {
			http.Error(w, "fail", status)
			return
		}
		_ = json.NewEncoder(w).Encode(protocol.RunResponse{Answer: text, FinalText: text})
	default:
		http.NotFound(w, r)
	}
}

// sliceAnswers answers column ci by policy[ci]: "gold", "wrong", or
// "gold-but-one-fault" (the first case's /run fails).
func sliceAnswers(t *testing.T, slice *duelingSlice, policy map[string]string) func(string) (string, int) {
	t.Helper()
	type answer struct {
		text   string
		status int
	}
	m := map[string]answer{}
	for _, col := range slice.columns {
		for i, c := range col.Pack.Cases {
			switch policy[col.Column.ColumnID] {
			case "gold":
				m[c.Question] = answer{c.ExpectedAnswer, http.StatusOK}
			case "gold-but-one-fault":
				if i == 0 {
					m[c.Question] = answer{"", http.StatusInternalServerError}
				} else {
					m[c.Question] = answer{c.ExpectedAnswer, http.StatusOK}
				}
			default:
				m[c.Question] = answer{c.DistractorAnswers[0], http.StatusOK}
			}
		}
	}
	return func(q string) (string, int) {
		a, ok := m[q]
		if !ok {
			return "", http.StatusOK
		}
		return a.text, a.status
	}
}

func runSliceOnce(t *testing.T, slice *duelingSlice, policy map[string]string) (duelingSliceResult, *sliceHarness) {
	t.Helper()
	h := &sliceHarness{answer: sliceAnswers(t, slice, policy)}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	result, err := executeDuelingSlice(context.Background(), slice, duelingSliceEnv{
		harnessURL: srv.URL, benchVersion: protocol.DuelingMinBenchVersion, seed: slice.dataset.RunSeed,
		toolSrv: toolexec.NewServer(), concurrency: 4,
	})
	if err != nil {
		t.Fatal(err)
	}
	return result, h
}

func TestExecuteDuelingSliceAggregatesColumnsWithoutCaseText(t *testing.T) {
	runner.Configure(true)
	t.Cleanup(func() { runner.Configure(false) })
	// c1 repeats c0's pack exactly, so every c1 case is claimed by c0.
	slice := decodedTestSlice(t, 99, [5]int{0, 0, 2, 3, 4})
	policy := map[string]string{"c0": "gold", "c1": "gold", "c2": "wrong", "c3": "gold-but-one-fault", "c4": "gold"}
	result, h := runSliceOnce(t, slice, policy)

	n := protocol.DuelingSliceCasesPerColumn
	want := map[string]struct {
		n   int
		sum float64
	}{"c0": {n, float64(n)}, "c1": {0, 0}, "c2": {n, 0}, "c3": {n, float64(n - 1)}, "c4": {n, float64(n)}}
	if len(result.columns) != protocol.DuelingSliceColumns {
		t.Fatalf("got %d columns", len(result.columns))
	}
	for i, col := range result.columns {
		w := want[col.ColumnID]
		if col.ColumnID != challenge.SliceColumnID(i) || col.N != w.n || col.Sum != w.sum || col.SumSq != w.sum {
			t.Errorf("column %+v, want n=%d sum=%v", col, w.n, w.sum)
		}
		if col.GradingBenchVersion != sliceTestPolicy(t) || col.SubmissionID == "" {
			t.Errorf("column %s lost its pin: %+v", col.ColumnID, col)
		}
	}
	if !result.columns[3].Substituted || result.columns[3].OwnerKey != "" || result.columns[2].OwnerKey != sliceTestOwner {
		t.Fatalf("column ownership not carried: %+v", result.columns)
	}
	if err := challenge.CheckDuelingColumns(duelingAggregates(result.columns)); err != nil {
		t.Fatalf("reported aggregates fail the report contract: %v", err)
	}
	// Rows: aggregates-only shape, column-prefixed case IDs, no text.
	if len(result.rows) != 4*n {
		t.Fatalf("got %d slice rows, want %d (c1 deduped away)", len(result.rows), 4*n)
	}
	for _, row := range result.rows {
		col, caseID, ok := strings.Cut(row.CaseID, "/")
		if !ok || !strings.HasPrefix(col, "c") || !strings.HasPrefix(caseID, "code-update-") ||
			row.Category != "dueling_slice" || row.Kind != protocol.KindMemory {
			t.Fatalf("slice row %+v", row)
		}
		if len(row.Notes) != 0 || len(row.Called) != 0 || len(row.Expected) != 0 || row.Relation != "" {
			t.Fatalf("slice row leaks grader detail: %+v", row)
		}
	}
	// The harness never sees a pack identity, and every column answers from
	// its own memory graph.
	usersByColumn := map[string]bool{}
	for _, run := range h.runs {
		if strings.Contains(run.CaseID, "code-update") || run.UserID == "" {
			t.Fatalf("pack identity crossed the wire: case=%q user=%q", run.CaseID, run.UserID)
		}
		usersByColumn[run.UserID] = true
	}
	if len(usersByColumn) != 4 {
		t.Fatalf("got %d distinct opaque users across 4 executed columns, want one graph per column", len(usersByColumn))
	}
	// Every wave of every executed column is seeded; a column the dedupe
	// emptied runs nothing, so its haystack is never sent.
	if len(h.seeds) != 3*(protocol.DuelingSliceColumns-1) {
		t.Fatalf("seeded %d waves, want %d", len(h.seeds), 3*(protocol.DuelingSliceColumns-1))
	}

	// The projection nonce derives from slice_seed: a rerun of the same slice
	// sends byte-identical case and user IDs.
	_, again := runSliceOnce(t, slice, policy)
	ids := func(h *sliceHarness) []string {
		var out []string
		for _, r := range h.runs {
			out = append(out, r.CaseID+"|"+r.UserID)
		}
		slices.Sort(out)
		return out
	}
	if !slices.Equal(ids(h), ids(again)) {
		t.Fatal("slice wire IDs are not deterministic across reruns")
	}
}

// A slice that starts after the bench v18 budget is spent seeds nothing and
// runs nothing: every wanted case is a budget-refused 0 inside its column.
func TestExecuteDuelingSliceSkipsSeedingOnceTheBudgetIsSpent(t *testing.T) {
	runner.Configure(true)
	t.Cleanup(func() { runner.Configure(false) })
	slice := decodedTestSlice(t, 99, [5]int{0, 1, 2, 3, 4})
	policy := map[string]string{"c0": "gold", "c1": "gold", "c2": "gold", "c3": "gold", "c4": "gold"}
	h := &sliceHarness{answer: sliceAnswers(t, slice, policy)}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	budget := &runBudget{benchVersion: protocol.BenchVersionV18, cases: 350, exhausted: true}
	result, err := executeDuelingSlice(context.Background(), slice, duelingSliceEnv{
		harnessURL: srv.URL, benchVersion: protocol.DuelingMinBenchVersion, seed: slice.dataset.RunSeed,
		toolSrv: toolexec.NewServer(), concurrency: 4, budget: budget,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(h.seeds) != 0 || len(h.runs) != 0 {
		t.Fatalf("a spent budget seeded %d waves and ran %d cases", len(h.seeds), len(h.runs))
	}
	n := protocol.DuelingSliceCasesPerColumn
	for _, col := range result.columns {
		if col.N != n || col.BudgetExhausted != n || col.Sum != 0 {
			t.Fatalf("column %+v, want %d refused zeros", col, n)
		}
	}
	if len(result.rows) != protocol.DuelingSliceColumns*n {
		t.Fatalf("got %d rows, want %d", len(result.rows), protocol.DuelingSliceColumns*n)
	}
	for _, row := range result.rows {
		if !row.BudgetExhausted || row.Score != 0 || len(row.Notes) != 1 || !strings.HasPrefix(row.Notes[0], protocol.RunBudgetExhaustedReason) {
			t.Fatalf("refused slice row %+v", row)
		}
	}
	if got := budget.summary(); got.UnrunSliceCases != protocol.DuelingSliceColumns*n {
		t.Fatalf("summary %+v", got)
	}
}

func TestExecuteDuelingSliceSeedFailureIsHarnessUnavailable(t *testing.T) {
	runner.Configure(true)
	t.Cleanup(func() { runner.Configure(false) })
	slice := decodedTestSlice(t, 777, [5]int{0, 1, 2, 3, 4})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/seed" {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	_, err := executeDuelingSlice(context.Background(), slice, duelingSliceEnv{
		harnessURL: srv.URL, benchVersion: protocol.DuelingMinBenchVersion, seed: 777, toolSrv: toolexec.NewServer(), concurrency: 2,
	})
	if !errors.Is(err, challengerun.ErrHarnessUnavailable) {
		t.Fatalf("seed failure = %v, want ErrHarnessUnavailable", err)
	}
}

// --- composite and report ----------------------------------------------------

func duelingFixtureColumns() []protocol.DuelingColumnDetails {
	return []protocol.DuelingColumnDetails{
		{ColumnID: "c0", OwnerKey: "coldkey:owner-c0", SubmissionID: "sub-c0", GradingBenchVersion: 15, N: 35, Sum: 30, SumSq: 29.5},
		{ColumnID: "c1", OwnerKey: "coldkey:owner-c1", SubmissionID: "sub-c1", GradingBenchVersion: 15, N: 34, Sum: 21.5, SumSq: 18.25},
		{ColumnID: "c2", OwnerKey: sliceTestOwner, SubmissionID: "sub-c2", GradingBenchVersion: 16, N: 35, Sum: 35, SumSq: 35},
		{ColumnID: "c3", OwnerKey: "", SubmissionID: "default", GradingBenchVersion: 15, N: 35, Sum: 10, SumSq: 7, Substituted: true},
		{ColumnID: "c4", OwnerKey: "", SubmissionID: "default", GradingBenchVersion: 15, N: 0},
	}
}

func TestDuelingCompositeMasksOnlyTheOwnerAndKeepsGatesOnDatagen(t *testing.T) {
	half := scorer.DatagenHalf{Raw: 0.82, RawStderr: 0.021, Gate: 0.9}
	cols := duelingFixtureColumns()
	composite, stderr, err := duelingOrdinaryScore(half, cols, sliceTestOwner)
	if err != nil {
		t.Fatal(err)
	}
	aggs := duelingAggregates(cols)
	self := challenge.DuelingComposite(half.Raw, half.Gate, aggs, map[string]bool{sliceTestOwner: true})
	if composite != math.Round(self*1e6)/1e6 {
		t.Fatalf("ordinary composite %v != self-masked %v", composite, self)
	}
	// The owner's column (a perfect 35/35) must not lift the owner's agent.
	unmasked := challenge.DuelingComposite(half.Raw, half.Gate, aggs, nil)
	if !(unmasked > self) {
		t.Fatalf("masking the owner's perfect column should lower the composite: masked %v unmasked %v", self, unmasked)
	}
	// An empty owner masks nothing: the public default columns always count.
	if got, _, _ := duelingOrdinaryScore(half, cols, ""); got != math.Round(unmasked*1e6)/1e6 {
		t.Fatalf("empty owner masked a column: %v vs %v", got, unmasked)
	}
	if stderr <= 0 {
		t.Fatalf("stderr = %v", stderr)
	}
	// Every gate multiplies the whole composite; the v9 applied factor joins
	// the datagen gate in the report so the fold reproduces the composite.
	slice := &duelingSlice{dataset: challenge.SliceDataset{SeasonID: sliceTestSeason, SeasonIndex: 3}, sha256: strings.Repeat("c", 64), ownerKey: sliceTestOwner}
	v9 := &protocol.V9BaseDetails{AppliedGateFactorBPS: 5000}
	details := duelingReportDetails(slice, half, nil, v9, cols)
	if details.DatagenGate != half.Gate*0.5 || details.DatagenRaw != half.Raw || details.ColumnWeight != protocol.DuelingColumnWeight {
		t.Fatalf("details = %+v", details)
	}
	effective := float64((int64(math.Round(composite*1e6))*5000+5000)/10000) / 1e6
	refold := challenge.DuelingComposite(details.DatagenRaw, details.DatagenGate, aggs, map[string]bool{details.OwnerKey: true})
	if math.Abs(refold-effective) > 1e-6 {
		t.Fatalf("fold recomputes %v, report carries %v", refold, effective)
	}
	if math.Abs(details.CompositeStderr-stderr*0.5) > 1e-6 {
		t.Fatalf("details stderr %v vs reported %v", details.CompositeStderr, stderr*0.5)
	}
	// A token-efficiency multiplier joins the gate the same way.
	scaled := duelingReportDetails(slice, half, &protocol.TokenEfficiency{Multiplier: 0.8}, v9, cols)
	if scaled.DatagenGate != half.Gate*0.8*0.5 {
		t.Fatalf("token-efficiency multiplier not carried: %v", scaled.DatagenGate)
	}
	if _, _, err := duelingOrdinaryScore(half, cols[:4], sliceTestOwner); err == nil {
		t.Fatal("a report with four columns was scored")
	}
}

// The details.dueling wire shape is pinned by a golden that the Python mirror
// (packages/ditto-screening-protocol tests/test_dueling.py) parses with
// DuelingReport and refolds bit for bit.
func TestDuelingReportGolden(t *testing.T) {
	half := scorer.DatagenHalf{Raw: 0.8123456789, RawStderr: 0.0173, Gate: 0.96}
	cols := duelingFixtureColumns()
	slice := &duelingSlice{
		dataset: challenge.SliceDataset{SeasonID: sliceTestSeason, SeasonIndex: 26119},
		sha256:  "3b1f4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f70819", ownerKey: sliceTestOwner,
	}
	details := duelingReportDetails(slice, half, &protocol.TokenEfficiency{Multiplier: 1}, &protocol.V9BaseDetails{AppliedGateFactorBPS: 10000}, cols)
	aggs := duelingAggregates(cols)
	golden := struct {
		Details         *protocol.DuelingDetails `json:"details"`
		SelfComposite   float64                  `json:"self_composite"`
		PairComposite   float64                  `json:"pair_composite"`
		PairMask        []string                 `json:"pair_mask"`
		DatagenStderr   float64                  `json:"datagen_stderr"`
		CompositeStderr float64                  `json:"composite_stderr"`
	}{
		Details:         details,
		SelfComposite:   challenge.DuelingComposite(details.DatagenRaw, details.DatagenGate, aggs, map[string]bool{sliceTestOwner: true}),
		PairComposite:   challenge.DuelingComposite(details.DatagenRaw, details.DatagenGate, aggs, map[string]bool{sliceTestOwner: true, "coldkey:owner-c0": true}),
		PairMask:        []string{sliceTestOwner, "coldkey:owner-c0"},
		DatagenStderr:   half.RawStderr,
		CompositeStderr: details.CompositeStderr,
	}
	got, err := json.MarshalIndent(golden, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	path := filepath.Join("testdata", "dueling_report_golden.json")
	if *updateDuelingGolden {
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (regenerate with go test ./cmd/dittobench-api -run TestDuelingReportGolden -update-dueling-golden)", err)
	}
	if string(got) != string(want) {
		t.Fatalf("details.dueling golden moved; regenerate with -update-dueling-golden and update the Python mirror test\n%s", got)
	}
	// The block carries exactly the spec 7.5 keys.
	var keys map[string]json.RawMessage
	if err := json.Unmarshal(mustJSON(t, details), &keys); err != nil {
		t.Fatal(err)
	}
	wantKeys := []string{"column_weight", "columns", "composite_stderr", "datagen_gate", "datagen_raw", "owner_key", "season_id", "season_index", "slice_sha256"}
	gotKeys := make([]string, 0, len(keys))
	for k := range keys {
		gotKeys = append(gotKeys, k)
	}
	slices.Sort(gotKeys)
	if !slices.Equal(gotKeys, wantKeys) {
		t.Fatalf("details.dueling keys = %v, want %v", gotKeys, wantKeys)
	}
	var colKeys []map[string]json.RawMessage
	if err := json.Unmarshal(keys["columns"], &colKeys); err != nil {
		t.Fatal(err)
	}
	wantCol := []string{"column_id", "grading_bench_version", "n", "owner_key", "submission_id", "substituted", "sum", "sum_sq"}
	for _, c := range colKeys {
		var ks []string
		for k := range c {
			ks = append(ks, k)
		}
		slices.Sort(ks)
		if !slices.Equal(ks, wantCol) {
			t.Fatalf("column keys = %v, want %v", ks, wantCol)
		}
	}
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// --- end to end ---------------------------------------------------------------

// runSizeJob at bench 16 runs every datagen case first, then the slice, on the
// same harness, and reports the dueling composite with the slice as
// aggregates only. Practice scope (a direct loopback harness) keeps the test
// free of the relay and broker; the canonical path differs only in accounting.
func TestRunSizeJobRunsTheSliceAfterTheDatagenHalf(t *testing.T) {
	runner.Configure(true)
	t.Cleanup(func() { runner.Configure(false) })
	t.Setenv("DITTOBENCH_PRIVATE_ARTIFACT_DIR", filepath.Join(t.TempDir(), "private"))
	const seed = 777
	req := duelingRequest(t, seed, [5]int{0, 1, 2, 3, 4})
	slice := decodedTestSlice(t, seed, [5]int{0, 1, 2, 3, 4})
	sliceQuestions := map[string]bool{}
	for _, c := range slice.cases {
		sliceQuestions[c.Case.Question] = true
	}
	h := &sliceHarness{answer: sliceAnswers(t, slice, map[string]string{"c0": "gold", "c1": "gold", "c2": "gold", "c3": "wrong", "c4": "gold"})}
	srv := httptest.NewServer(h)
	defer srv.Close()

	req.HarnessURL = srv.URL
	req.RunSize = "small"
	prof, ok := gen.ProfileForVersion(req.RunSize, req.BenchVersion)
	if !ok {
		t.Fatal("no small profile at the dueling floor")
	}
	s := &server{store: store.New(), runSlots: make(chan struct{}, 1), allowPrivate: true, allowDuelingSlices: true}
	s.runSlots <- struct{}{}
	runID := "6f1d2c3b-4a5e-4f60-8172-93a4b5c6d7e8"
	s.store.Create(runID, "run_size", store.StatusQueued, seed, prof.Tools+prof.Mem)
	s.runSizeJob(context.Background(), runID, req, prof, seed)

	job, ok := s.store.Get(runID)
	if !ok || job.Status != store.StatusDone || job.Report == nil {
		t.Fatalf("job = %+v", job)
	}
	report := job.Report
	// Ordering: every datagen /run precedes the first slice /run.
	firstSlice, lastDatagen := -1, -1
	for i, run := range h.runs {
		if sliceQuestions[run.UserInput] {
			if firstSlice < 0 {
				firstSlice = i
			}
		} else {
			lastDatagen = i
		}
	}
	if firstSlice < 0 || lastDatagen < 0 || lastDatagen > firstSlice {
		t.Fatalf("slice /run did not follow the datagen half: first slice %d, last datagen %d", firstSlice, lastDatagen)
	}
	d := report.Details.Dueling
	if d == nil {
		t.Fatal("bench 16 report has no details.dueling")
	}
	if d.SliceSHA256 != req.ExpectedSliceSHA256 || d.OwnerKey != sliceTestOwner || d.SeasonID != sliceTestSeason || d.SeasonIndex != 26119 {
		t.Fatalf("details.dueling identity = %+v", d)
	}
	// The datagen half is exactly the ordinary aggregate of the non-slice rows.
	var datagen []protocol.CaseScore
	sliceRows := 0
	for _, cs := range report.PerCase {
		if cs.Category == challenge.SliceCaseCategory {
			sliceRows++
			continue
		}
		datagen = append(datagen, cs)
	}
	half := scorer.DatagenHalfForVersion(datagen, req.BenchVersion)
	if d.DatagenRaw != half.Raw || d.DatagenGate != half.Gate {
		t.Fatalf("datagen half %+v, report raw=%v gate=%v", half, d.DatagenRaw, d.DatagenGate)
	}
	if sliceRows != len(slice.cases) || report.N != len(report.PerCase) {
		t.Fatalf("slice rows %d (want %d), n=%d per_case=%d", sliceRows, len(slice.cases), report.N, len(report.PerCase))
	}
	// The priced cost covers the whole run (both halves). This practice run
	// meters no inference, so it reports insufficient evidence and the gate
	// above stays exactly the datagen composite gate.
	if pc := report.Details.PricedCost; pc == nil || pc.Window != "whole_run" || pc.Cases != report.N ||
		pc.Result != "insufficient_evidence" || pc.AppliedFactorBPS != 10000 {
		t.Fatalf("details.priced_cost = %+v", report.Details.PricedCost)
	}
	aggs := duelingAggregates(d.Columns)
	self := challenge.DuelingComposite(d.DatagenRaw, d.DatagenGate, aggs, map[string]bool{d.OwnerKey: true})
	if math.Abs(report.Composite-self) > 1e-6 {
		t.Fatalf("composite %v is not the self-masked dueling composite %v", report.Composite, self)
	}
	// c2 (the owner's, all gold) is masked; c3 (wrong) counts against it.
	if d.Columns[2].Sum != float64(protocol.DuelingSliceCasesPerColumn) || d.Columns[3].Sum != 0 {
		t.Fatalf("columns = %+v", d.Columns)
	}
	// The datagen means the report publishes ignore the slice.
	if report.ToolMean != half.ToolMean || report.MemoryMean != half.MemoryMean {
		t.Fatalf("report means moved by the slice: tool %v/%v memory %v/%v", report.ToolMean, half.ToolMean, report.MemoryMean, half.MemoryMean)
	}
	for _, cat := range report.PerCategory {
		if cat.Category == challenge.SliceCaseCategory {
			t.Fatal("per_category carries the slice")
		}
	}
	// No slice case reaches the transcript.
	if job.Transcript != nil && strings.Contains(string(job.Transcript), "code-update-") {
		t.Fatal("a slice case reached the transcript artifact")
	}
}

// The datagen gates' session totals (catalog_gate, catalog_suppression_rate,
// the tool-provenance summary) are captured once at the datagen/slice
// boundary. Slice completions booked afterwards never reach them, and a
// boundary capture that found nothing stays unavailable instead of falling
// back to a post-slice read.
func TestDuelingSessionTotalsNeverSeeSliceCompletions(t *testing.T) {
	broker, sessionID, stop := newCatalogCaptureBroker(t, protocol.DuelingMinBenchVersion, emitSetThemeCompletion)
	defer stop()
	s := &server{broker: broker}
	v := protocol.DuelingMinBenchVersion

	postCatalogChat(t, broker, openAICatalogRequest) // a datagen completion
	boundary := s.captureDatagenSessionTotals(v, sessionID)
	if boundary.catalog == nil || boundary.catalog.Completions != 1 || boundary.toolProvenance == nil {
		t.Fatalf("boundary totals = %+v", boundary)
	}
	postCatalogChat(t, broker, openAICatalogRequest) // slice completions
	postCatalogChat(t, broker, openAICatalogRequest)

	got := s.datagenSessionTotalsAtScoring(true, boundary, v, sessionID)
	if got.catalog.Completions != 1 || *got.toolProvenance != *boundary.toolProvenance ||
		!reflect.DeepEqual(got.unattributedCost, boundary.unattributedCost) {
		t.Fatalf("dueling gates read post-slice totals: %+v / %+v / %+v", *got.catalog, *got.toolProvenance, got.unattributedCost)
	}
	if live := s.captureDatagenSessionTotals(v, sessionID); reflect.DeepEqual(live.unattributedCost, boundary.unattributedCost) {
		t.Fatalf("the slice completions booked no unattributed cost, so the test proves nothing: %+v", live.unattributedCost)
	}
	// A failed boundary capture (here: an unknown session) is not repaired by
	// a post-slice read of the live session.
	failed := s.captureDatagenSessionTotals(v, "no-such-session")
	if failed.catalog != nil || failed.toolProvenance != nil {
		t.Fatalf("unknown session produced totals: %+v", failed)
	}
	if got := s.datagenSessionTotalsAtScoring(true, failed, v, sessionID); got.catalog != nil || got.toolProvenance != nil {
		t.Fatalf("a failed boundary capture fell back to post-slice totals: %+v", got)
	}
	// Without a slice the read happens at scoring, exactly as before.
	if got := s.datagenSessionTotalsAtScoring(false, datagenSessionTotals{}, v, sessionID); got.catalog == nil || got.catalog.Completions != 3 {
		t.Fatalf("non-dueling totals = %+v", got.catalog)
	}
}
