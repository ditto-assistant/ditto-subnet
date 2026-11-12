package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// testDuelingBench is a bench version that carries a slice: the floor, so the
// tests keep working when later versions ship.
const testDuelingBench = protocol.DuelingMinBenchVersion

func testSeasonPolicy(t *testing.T) int {
	t.Helper()
	window := challenge.GradingBenchVersionsThrough(testDuelingBench)
	if len(window) == 0 {
		t.Fatalf("a v%d season accepts no pack policy", testDuelingBench)
	}
	return window[len(window)-1]
}

// Dueling generators are the starter kit's families, which fill any case
// count, behind a mode switch:
//
//   - ok: a deterministic, version-aware generator;
//   - crash: exits non-zero on every request;
//   - flaky: stamps a per-binary run counter into its provenance, so the same
//     request yields different bytes (Local runs share the binary's TMPDIR).
var (
	duelingArchivesMu sync.Mutex
	duelingArchives   = map[string][]byte{}
)

func duelingArchive(t *testing.T, mode string) []byte {
	t.Helper()
	duelingArchivesMu.Lock()
	defer duelingArchivesMu.Unlock()
	if raw, ok := duelingArchives[mode]; ok {
		return raw
	}
	datagen, err := filepath.Abs("../../../../research/dittobench-datagen")
	if err != nil {
		t.Fatal(err)
	}
	kit, err := filepath.Abs("../../../../miners/dittobench-generator-starter-kit")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	body := `package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-generator-starter-kit/families"
)

const mode = "MODE"

func main() {
	var req challenge.GenerateRequest
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil || mode == "crash" {
		os.Exit(3)
	}
	version := "1"
	if mode == "flaky" {
		counter := filepath.Join(os.TempDir(), "runs")
		raw, _ := os.ReadFile(counter)
		n, _ := strconv.Atoi(string(raw))
		_ = os.WriteFile(counter, []byte(strconv.Itoa(n+1)), 0o644)
		version = strconv.Itoa(n + 1)
	}
	b := families.NewBuilder(req)
	for i := 0; b.Len() < req.Cases; i++ {
		round, family := i/len(families.Default), families.Default[i%len(families.Default)]
		b.Add(family(b, round))
		if i%len(families.Default) == len(families.Default)-1 && b.Len()+2 <= req.Cases {
			families.Filler(b)
		}
	}
	p := b.Pack(req, "dueling-test", version)
	p.GradingBenchVersion = req.NewestAccepted(nil)
	_ = json.NewEncoder(os.Stdout).Encode(p)
}
`
	body = strings.Replace(body, "MODE", mode, 1)
	gomod := "module example.com/dueling\n\ngo 1.27\n\nrequire (\n\tgithub.com/ditto-assistant/dittobench-datagen v0.0.0\n\tgithub.com/ditto-assistant/dittobench-generator-starter-kit v0.0.0\n)\n\n" +
		"replace github.com/ditto-assistant/dittobench-datagen => " + datagen + "\n\n" +
		"replace github.com/ditto-assistant/dittobench-generator-starter-kit => " + kit + "\n"
	for name, data := range map[string]string{"main.go": body, "go.mod": gomod} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(data), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	raw, _, err := genrun.PackDir(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	duelingArchives[mode] = raw
	return raw
}

// countingSandbox counts builds, so tests can see the build cache work.
type countingSandbox struct {
	genrun.Sandbox
	mu     sync.Mutex
	builds int
}

func (c *countingSandbox) Build(ctx context.Context, dir string) (genrun.Program, error) {
	c.mu.Lock()
	c.builds++
	c.mu.Unlock()
	return c.Sandbox.Build(ctx, dir)
}

func (c *countingSandbox) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.builds
}

type fakeDuelingPlatform struct {
	*httptest.Server
	status    int // non-zero: every poll answers this status
	work      []*duelingWork
	polls     []string
	batteries []batteryResult
	slices    []sliceResult
	faults    []faultReport
}

func newFakeDuelingPlatform(t *testing.T, work ...*duelingWork) *fakeDuelingPlatform {
	t.Helper()
	fp := &fakeDuelingPlatform{work: work}
	fp.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer secret-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case duelingWorkPath:
			fp.polls = append(fp.polls, r.URL.RawQuery)
			if fp.status != 0 {
				http.Error(w, "no", fp.status)
				return
			}
			var next *duelingWork
			if len(fp.work) > 0 {
				next, fp.work = fp.work[0], fp.work[1:]
			}
			_ = json.NewEncoder(w).Encode(duelingWorkResponse{Work: next})
		case "/api/v1/generator-scorer/dueling-battery":
			var res batteryResult
			_ = json.NewDecoder(r.Body).Decode(&res)
			fp.batteries = append(fp.batteries, res)
		case "/api/v1/generator-scorer/dueling-slice":
			var res sliceResult
			_ = json.NewDecoder(r.Body).Decode(&res)
			fp.slices = append(fp.slices, res)
		case "/api/v1/generator-scorer/fault":
			var f faultReport
			_ = json.NewDecoder(r.Body).Decode(&f)
			fp.faults = append(fp.faults, f)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(fp.Close)
	return fp
}

func (fp *fakeDuelingPlatform) client() *platformClient {
	return &platformClient{base: fp.URL, token: "secret-token", http: fp.Client()}
}

func source(id string, raw []byte) duelingSource {
	return duelingSource{
		SubmissionID: id, MinerHotkey: testHotkey,
		SourceSHA256: challenge.Digest(raw), SourceBase64: base64.StdEncoding.EncodeToString(raw),
	}
}

func testCache(t *testing.T) (*buildCache, *countingSandbox) {
	t.Helper()
	sb := &countingSandbox{Sandbox: genrun.Local{}}
	cache := newBuildCache(sb, buildCacheSize)
	t.Cleanup(cache.Close)
	return cache, sb
}

// An idle Platform answers {"work": null}; one that predates dueling work
// answers 404, which is idle too. Any other failure is an error.
func TestProcessDuelingIdleAndOlderPlatforms(t *testing.T) {
	cache, _ := testCache(t)
	fp := newFakeDuelingPlatform(t)
	for _, status := range []int{0, http.StatusNotFound} {
		fp.status = status
		if busy, err := processDueling(context.Background(), fp.client(), cache); busy || err != nil {
			t.Fatalf("status %d: busy=%v err=%v", status, busy, err)
		}
	}
	fp.status = http.StatusInternalServerError
	if _, err := processDueling(context.Background(), fp.client(), cache); err == nil {
		t.Fatal("a 500 must be a failed cycle")
	}
}

func batterySeeds(n int) []string {
	seeds := make([]string, n)
	for i := range seeds {
		seeds[i] = challenge.SeedFromInt64(int64(900 + i))
	}
	return seeds
}

func TestBatteryQualifiesOnlyValidDeterministicGenerators(t *testing.T) {
	ok, crash, flaky := duelingArchive(t, "ok"), duelingArchive(t, "crash"), duelingArchive(t, "flaky")
	fp := newFakeDuelingPlatform(t, &duelingWork{
		Kind: "battery", LeaseID: "lease-b", SeasonID: testSeasonID, BenchVersion: testDuelingBench,
		Cases: protocol.DuelingSliceCasesPerColumn, Seeds: batterySeeds(3),
		Candidates: []duelingCandidate{
			{duelingSource: source("sub-ok", ok), OwnerKey: "coldkey:a"},
			{duelingSource: source("sub-crash", crash), OwnerKey: "coldkey:b"},
			{duelingSource: source("sub-flaky", flaky), OwnerKey: "coldkey:c"},
			{duelingSource: source("sub-ok-again", ok), OwnerKey: "coldkey:d"},
		},
	})
	cache, sb := testCache(t)
	if busy, err := processDueling(context.Background(), fp.client(), cache); !busy || err != nil {
		t.Fatalf("busy=%v err=%v", busy, err)
	}
	if len(fp.batteries) != 1 || len(fp.faults) != 0 {
		t.Fatalf("batteries=%d faults=%+v", len(fp.batteries), fp.faults)
	}
	got := fp.batteries[0]
	if got.LeaseID != "lease-b" || got.SeasonID != testSeasonID || got.ScorerVersion != ScorerVersion || len(got.Candidates) != 4 {
		t.Fatalf("identity: %+v", got)
	}
	policy := testSeasonPolicy(t)
	byID := map[string]batteryCandidateResult{}
	for _, c := range got.Candidates {
		byID[c.SubmissionID] = c
	}
	for _, id := range []string{"sub-ok", "sub-ok-again"} {
		if c := byID[id]; !c.OK || c.GradingBenchVersion == nil || *c.GradingBenchVersion != policy || c.Error != "" {
			t.Fatalf("%s: %+v", id, c)
		}
	}
	if c := byID["sub-crash"]; c.OK || c.GradingBenchVersion != nil || !strings.HasPrefix(c.Error, "seed 0:") {
		t.Fatalf("crash: %+v", c)
	}
	if c := byID["sub-flaky"]; c.OK || !strings.HasPrefix(c.Error, "determinism:") {
		t.Fatalf("flaky: %+v", c)
	}
	// Four candidates, three distinct archives: three builds.
	if sb.count() != 3 {
		t.Fatalf("builds = %d, want 3 (cached by source digest)", sb.count())
	}
}

func TestBatteryRefusesUnsupportedLeasesAsFaults(t *testing.T) {
	ok := duelingArchive(t, "ok")
	for name, w := range map[string]*duelingWork{
		"pre-dueling bench": {BenchVersion: protocol.DuelingMinBenchVersion - 1, Cases: 35, Seeds: batterySeeds(1)},
		"no seeds":          {BenchVersion: testDuelingBench, Cases: 35},
		"too many cases":    {BenchVersion: testDuelingBench, Cases: challenge.MaxCases + 1, Seeds: batterySeeds(1)},
	} {
		w.Kind, w.LeaseID, w.SeasonID = "battery", "lease-u", testSeasonID
		w.Candidates = []duelingCandidate{{duelingSource: source("sub-ok", ok), OwnerKey: "coldkey:a"}}
		fp := newFakeDuelingPlatform(t, w)
		cache, sb := testCache(t)
		_, err := processDueling(context.Background(), fp.client(), cache)
		if !errors.Is(err, genrun.ErrInvalidRequest) || len(fp.batteries) != 0 || sb.count() != 0 {
			t.Fatalf("%s: err=%v posted=%d builds=%d", name, err, len(fp.batteries), sb.count())
		}
		if f := fp.faults; len(f) != 1 || f[0].Work != "dueling" || f[0].ErrorKind != "unsupported_request" ||
			f[0].LeaseID != "lease-u" || f[0].SeasonID != testSeasonID || f[0].ScorerVersion != ScorerVersion {
			t.Fatalf("%s: fault %+v", name, f)
		}
	}
}

const (
	testSeasonIndex = 26119
	testRunSeed     = 4242
	testSeasonID    = "0b6f1c1e-6a52-4d8e-9a57-3f0c2d1e4b5a"
	testPrepID      = "7c1d2e3f-4a5b-4c6d-8e7f-901a2b3c4d5e"
)

var testAnchor = "0x" + strings.Repeat("5a", 32)

func testSecret() []byte { return bytes.Repeat([]byte{0x42}, challenge.SeasonSecretBytes) }

func testSeasonSeed(t *testing.T) string {
	t.Helper()
	seed, err := challenge.SeasonSeed(testSeasonIndex, testSecret(), testAnchor)
	if err != nil {
		t.Fatal(err)
	}
	return seed
}

// sliceWork pins c0 and c2 to the same miner generator (one build), c1 to a
// generator that crashes (substituted by the fallback), c3 to the public
// default generator as a fill, and c4 to a second miner generator.
func sliceWork(t *testing.T, withFallback bool) *duelingWork {
	t.Helper()
	ok, crash, def := duelingArchive(t, "ok"), duelingArchive(t, "crash"), duelingArchive(t, "ok")
	seasonSeed := testSeasonSeed(t)
	policy := testSeasonPolicy(t)
	w := &duelingWork{
		Kind: "slice", LeaseID: "lease-s", PreparationID: testPrepID, SeasonID: testSeasonID,
		SeasonIndex: testSeasonIndex, BenchVersion: testDuelingBench, RunSeed: testRunSeed,
		CasesPerColumn: protocol.DuelingSliceCasesPerColumn,
	}
	srcs := map[string]struct {
		raw   []byte
		owner string
	}{
		"c0": {ok, "coldkey:a"}, "c1": {crash, "coldkey:b"}, "c2": {ok, "coldkey:c"},
		"c3": {def, ""}, "c4": {ok, "coldkey:d"},
	}
	// Columns arrive out of order: the scorer sorts them.
	for _, id := range []string{"c4", "c1", "c3", "c0", "c2"} {
		seed, err := challenge.SliceSeed(seasonSeed, testRunSeed, id)
		if err != nil {
			t.Fatal(err)
		}
		s := srcs[id]
		col := duelingSliceColumn{
			duelingSource: source("sub-"+id, s.raw), ColumnID: id, OwnerKey: s.owner,
			GradingBenchVersion: policy, SliceSeed: seed, PublicDefault: s.owner == "",
		}
		if col.PublicDefault {
			col.SubmissionID = "default"
		}
		w.Columns = append(w.Columns, col)
	}
	if withFallback {
		fb := source("default", def)
		w.Fallback = &fb
	}
	return w
}

// The served slice is then rebuilt byte for byte by reproduce-slice from the
// revealed secret, the anchor block, the run seed, the pins and the published
// sources, on a fresh build cache.
func TestSliceGeneratesSubstitutesEncodesAndReproduces(t *testing.T) {
	fp := newFakeDuelingPlatform(t, sliceWork(t, true))
	cache, sb := testCache(t)
	if busy, err := processDueling(context.Background(), fp.client(), cache); !busy || err != nil {
		t.Fatalf("busy=%v err=%v", busy, err)
	}
	if len(fp.slices) != 1 || len(fp.faults) != 0 {
		t.Fatalf("slices=%d faults=%+v", len(fp.slices), fp.faults)
	}
	got := fp.slices[0]
	if got.LeaseID != "lease-s" || got.PreparationID != testPrepID || got.ScorerVersion != ScorerVersion || len(got.Columns) != 5 {
		t.Fatalf("identity: %+v", got)
	}
	for i, c := range got.Columns {
		if c.ColumnID != challenge.SliceColumnID(i) || !c.OK {
			t.Fatalf("column %d: %+v", i, c)
		}
		if wantSub := c.ColumnID == "c1"; c.Substituted != wantSub || (c.Error != "") != wantSub {
			t.Fatalf("column %s substitution: %+v", c.ColumnID, c)
		}
	}
	raw, err := base64.StdEncoding.DecodeString(got.SliceBase64)
	if err != nil {
		t.Fatal(err)
	}
	ds, cols, err := challenge.DecodeSliceDataset(raw, got.SliceSHA256)
	if err != nil {
		t.Fatal(err)
	}
	if ds.SeasonID != testSeasonID || ds.SeasonIndex != testSeasonIndex || ds.RunSeed != testRunSeed || ds.BenchVersion != testDuelingBench {
		t.Fatalf("header: %+v", ds)
	}
	if err := challenge.VerifySliceSeeds(ds, testSeasonSeed(t)); err != nil {
		t.Fatal(err)
	}
	okTree := cols[0].Column.TreeSHA256
	for _, c := range cols {
		col := c.Column
		switch col.ColumnID {
		case "c1":
			if !col.Substituted || !col.PublicDefault || col.OwnerKey != "" || col.SubmissionID != "default" {
				t.Fatalf("substituted column: %+v", col)
			}
		case "c3":
			if col.Substituted || !col.PublicDefault || col.OwnerKey != "" {
				t.Fatalf("default fill column: %+v", col)
			}
		default:
			if col.Substituted || col.PublicDefault || col.OwnerKey == "" || col.SubmissionID != "sub-"+col.ColumnID {
				t.Fatalf("miner column: %+v", col)
			}
		}
		if col.TreeSHA256 == "" || len(c.Pack.Cases) != protocol.DuelingSliceCasesPerColumn {
			t.Fatalf("column %s: tree %q cases %d", col.ColumnID, col.TreeSHA256, len(c.Pack.Cases))
		}
		if col.ColumnID != "c1" && col.TreeSHA256 != okTree {
			t.Fatalf("column %s built another tree", col.ColumnID)
		}
	}
	// Two distinct archives: the ok generator (every miner column, the
	// default and the fallback) and the crashing one.
	if sb.count() != 2 {
		t.Fatalf("builds = %d, want 2", sb.count())
	}
	checkReproduceSlice(t, got, raw)
}

// Without a fallback a failed pinned generator leaves the slice incomplete:
// the column results are posted with no slice, so Platform fails the
// preparation instead of serving a short slice.
func TestSliceWithoutFallbackPostsNoSlice(t *testing.T) {
	fp := newFakeDuelingPlatform(t, sliceWork(t, false))
	cache, _ := testCache(t)
	if _, err := processDueling(context.Background(), fp.client(), cache); err != nil {
		t.Fatal(err)
	}
	got := fp.slices[0]
	if got.SliceSHA256 != "" || got.SliceBase64 != "" {
		t.Fatal("an incomplete slice was posted")
	}
	for _, c := range got.Columns {
		if bad := c.ColumnID == "c1"; c.OK == bad || c.Substituted || (c.Error != "") != bad {
			t.Fatalf("column %s: %+v", c.ColumnID, c)
		}
	}
}

func TestSliceRefusesUnsupportedLeasesAsFaults(t *testing.T) {
	for name, mutate := range map[string]func(*duelingWork){
		"pre-dueling bench": func(w *duelingWork) { w.BenchVersion = protocol.DuelingMinBenchVersion - 1 },
		"four columns":      func(w *duelingWork) { w.Columns = w.Columns[:4] },
		"other case count":  func(w *duelingWork) { w.CasesPerColumn = 70 },
		"policy outside":    func(w *duelingWork) { w.Columns[0].GradingBenchVersion = challenge.GradingBenchVersion },
		"duplicate column":  func(w *duelingWork) { w.Columns[0].ColumnID = w.Columns[1].ColumnID },
		"ownerless miner":   func(w *duelingWork) { w.Columns[0].OwnerKey = ""; w.Columns[0].PublicDefault = false },
		"negative index":    func(w *duelingWork) { w.SeasonIndex = -1 },
		"bad season id":     func(w *duelingWork) { w.SeasonID = "../season" },
	} {
		w := sliceWork(t, true)
		mutate(w)
		fp := newFakeDuelingPlatform(t, w)
		cache, sb := testCache(t)
		_, err := processDueling(context.Background(), fp.client(), cache)
		if !errors.Is(err, genrun.ErrInvalidRequest) || len(fp.slices) != 0 || sb.count() != 0 {
			t.Fatalf("%s: err=%v posted=%d builds=%d", name, err, len(fp.slices), sb.count())
		}
		if f := fp.faults; len(f) != 1 || f[0].Work != "dueling" || f[0].ErrorKind != "unsupported_request" ||
			f[0].PreparationID != testPrepID || f[0].SeasonID != uuidOrEmpty(w.SeasonID) {
			t.Fatalf("%s: fault %+v", name, f)
		}
	}
}

func TestSliceRefusesMismatchedPins(t *testing.T) {
	// Columns[3] is c0, the first column generated.
	for name, mutate := range map[string]func(*duelingWork){
		"source digest": func(w *duelingWork) { w.Columns[3].SourceSHA256 = strings.Repeat("0", 64) },
		"tree digest":   func(w *duelingWork) { w.Columns[3].TreeSHA256 = strings.Repeat("0", 64) },
	} {
		w := sliceWork(t, true)
		mutate(w)
		fp := newFakeDuelingPlatform(t, w)
		cache, _ := testCache(t)
		_, err := processDueling(context.Background(), fp.client(), cache)
		if !errors.Is(err, genrun.ErrInvalidRequest) || len(fp.slices) != 0 {
			t.Fatalf("%s: err=%v posted=%d", name, err, len(fp.slices))
		}
		if f := fp.faults; len(f) != 1 || f[0].ErrorKind != "unsupported_request" || f[0].PreparationID != testPrepID {
			t.Fatalf("%s: fault %+v", name, f)
		}
	}
}

func TestBoundErrorKeepsUTF8(t *testing.T) {
	s := strings.Repeat("a", maxDuelingError-1) + "é tail"
	got := boundError(s)
	if !utf8.ValidString(got) || len(got) != maxDuelingError-1 {
		t.Fatalf("len=%d valid=%v", len(got), utf8.ValidString(got))
	}
	if boundError("short") != "short" {
		t.Fatal("short error changed")
	}
}

func checkReproduceSlice(t *testing.T, served sliceResult, raw []byte) {
	t.Helper()
	dir := t.TempDir()
	// The revealed slice itself has the pins shape.
	pins := filepath.Join(dir, "pins.json")
	if err := os.WriteFile(pins, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	ok, def := duelingArchive(t, "ok"), duelingArchive(t, "ok")
	write := func(name string, data []byte) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, data, 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}
	okPath, defPath := write("ok.tar.gz", ok), write("default.tar.gz", def)
	out := filepath.Join(dir, "slice.json")
	args := []string{
		"-season-secret", hex.EncodeToString(testSecret()), "-season-index", strconv.Itoa(testSeasonIndex),
		"-anchor-block-hash", testAnchor, "-run-seed", strconv.Itoa(testRunSeed),
		"-bench-version", strconv.Itoa(testDuelingBench), "-pins", pins, "-sandbox", "local",
		"-source", "c0=" + okPath, "-source", "c1=" + defPath, "-source", "c2=" + okPath,
		"-source", "c3=" + defPath, "-source", "c4=" + okPath,
		"-expect-sha256", served.SliceSHA256, "-out", out,
	}
	var stdout bytes.Buffer
	if err := reproduceSlice(context.Background(), args, &stdout); err != nil {
		t.Fatal(err)
	}
	if got := strings.TrimSpace(stdout.String()); got != served.SliceSHA256 {
		t.Fatalf("reproduced %s, served %s", got, served.SliceSHA256)
	}
	if again, _ := os.ReadFile(out); !bytes.Equal(again, raw) {
		t.Fatal("reproduced bytes differ")
	}
	// A wrong secret derives other slice seeds than the pins carry.
	bad := append([]string(nil), args...)
	bad[1] = strings.Repeat("00", challenge.SeasonSecretBytes)
	if err := reproduceSlice(context.Background(), bad, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "does not derive") {
		t.Fatalf("wrong secret: %v", err)
	}
}

// timeoutSandbox times out every build, as a loaded host would.
type timeoutSandbox struct{}

func (timeoutSandbox) Name() string { return "timeout" }

func (timeoutSandbox) Build(context.Context, string) (genrun.Program, error) {
	return nil, &genrun.GeneratorError{Phase: "build", Reason: "timeout"}
}

// A build timeout is host load, never the generator's failure: the battery
// and the slice report a sandbox fault instead of failing or substituting.
func TestBuildTimeoutsAreSandboxFaults(t *testing.T) {
	ok := duelingArchive(t, "ok")
	battery := &duelingWork{
		Kind: "battery", LeaseID: "lease-t", SeasonID: testSeasonID, BenchVersion: testDuelingBench,
		Cases: protocol.DuelingSliceCasesPerColumn, Seeds: batterySeeds(1),
		Candidates: []duelingCandidate{{duelingSource: source("sub-ok", ok), OwnerKey: "coldkey:a"}},
	}
	for _, w := range []*duelingWork{battery, sliceWork(t, true)} {
		fp := newFakeDuelingPlatform(t, w)
		cache := newBuildCache(timeoutSandbox{}, buildCacheSize)
		t.Cleanup(cache.Close)
		_, err := processDueling(context.Background(), fp.client(), cache)
		if !errors.Is(err, genrun.ErrSandbox) || len(fp.batteries)+len(fp.slices) != 0 {
			t.Fatalf("%s: err=%v posted=%d", w.Kind, err, len(fp.batteries)+len(fp.slices))
		}
		if f := fp.faults; len(f) != 1 || f[0].ErrorKind != "sandbox" || f[0].Work != "dueling" {
			t.Fatalf("%s: fault %+v", w.Kind, f)
		}
	}
}

// A dueling battery (10 miner candidates and the default generator) or a slice
// (five columns and the fallback) at the archive bound must fit one Platform
// answer, base64-expanded; the old 2x-archive cap truncated it into a JSON
// error.
func TestPlatformResponseCapFitsTheLargestDuelingLease(t *testing.T) {
	encoded := (challenge.MaxArchiveBytes + 2) / 3 * 4
	for name, archives := range map[string]int{"battery": 2*protocol.DuelingSliceColumns + 1, "slice": protocol.DuelingSliceColumns + 1} {
		if need := archives * encoded; need >= maxPlatformResponseBytes {
			t.Fatalf("%s lease needs %d bytes, cap is %d", name, need, maxPlatformResponseBytes)
		}
	}
}
