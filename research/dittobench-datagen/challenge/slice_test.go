package challenge

import (
	"encoding/json"
	"os"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// SliceVectors pins the canonical slice encoding: the same packs and pins
// must always encode to the same bytes and digest.
type SliceVectors struct {
	Slices []SliceVector `json:"slices"`
}

type SliceVector struct {
	Name        string            `json:"name"`
	SeasonIndex int64             `json:"season_index"`
	SeasonSeed  string            `json:"season_seed"`
	RunSeed     int64             `json:"run_seed"`
	Bytes       int               `json:"bytes"`
	SliceSHA256 string            `json:"slice_sha256"`
	PackSHA256  map[string]string `json:"pack_sha256"`
}

// testSliceBenchVersion is a bench version that carries a slice. It is the
// floor, so the test keeps working when later versions ship.
const testSliceBenchVersion = protocol.DuelingMinBenchVersion

// testSlicePolicy is the pack policy the slice vectors were recorded at:
// policy 15, which a slice at testSliceBenchVersion accepts. It is pinned
// rather than read off the window so widening the v16 window to
// {14, 15, 16} (GradingPolicyWindowV16) does not move the vectors;
// TestSliceAcceptsEveryPolicyInItsWindow covers the window's ends.
func testSlicePolicy(t *testing.T) int {
	t.Helper()
	policy := GradingBenchVersionV15
	accepted := GradingBenchVersionsThrough(testSliceBenchVersion)
	for _, v := range accepted {
		if v == policy {
			return policy
		}
	}
	t.Fatalf("a v%d slice accepts %v, which no longer holds the vectors' policy %d", testSliceBenchVersion, accepted, policy)
	return 0
}

// slicePack is a valid DuelingSliceCasesPerColumn-case pack generated for a
// slice seed at a policy.
func slicePack(t *testing.T, packSeed int64, sliceSeed string, policy int) json.RawMessage {
	t.Helper()
	p := widePack(t, packSeed, protocol.DuelingSliceCasesPerColumn)
	p.Generator.Seed = sliceSeed
	p.GradingBenchVersion = policy
	raw, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// testSlice builds a full slice: c0..c2 miner columns, c3 a substituted
// column, c4 a public default fill.
func testSlice(t *testing.T, seasonSeed string, runSeed int64) SliceDataset {
	t.Helper()
	policy := testSlicePolicy(t)
	ds := SliceDataset{
		Contract: SliceContractV1, BenchVersion: testSliceBenchVersion,
		SeasonID: "0b6f1c1e-6a52-4d8e-9a57-3f0c2d1e4b5a", SeasonIndex: 26119, RunSeed: runSeed,
	}
	// Columns are given out of order: the encoding sorts them.
	for _, i := range []int{4, 2, 0, 3, 1} {
		id := SliceColumnID(i)
		seed, err := SliceSeed(seasonSeed, runSeed, id)
		if err != nil {
			t.Fatal(err)
		}
		col := SliceDatasetColumn{
			ColumnID: id, SubmissionID: "sub-" + id, OwnerKey: "coldkey:owner-" + id,
			MinerHotkey: testHotkey, TreeSHA256: Digest([]byte("tree:" + id)),
			GradingBenchVersion: policy, SliceSeed: seed,
			Pack: slicePack(t, int64(100+i), seed, policy),
		}
		switch i {
		case 3:
			col.PublicDefault, col.Substituted, col.OwnerKey = true, true, ""
		case 4:
			col.SubmissionID, col.PublicDefault, col.OwnerKey = "default", true, ""
		}
		ds.Columns = append(ds.Columns, col)
	}
	return ds
}

func testSliceSeasonSeed(t *testing.T) string {
	t.Helper()
	seed, err := SeasonSeed(26119, testSeasonSecret("slice"), "0x"+strings.Repeat("5a", 32))
	if err != nil {
		t.Fatal(err)
	}
	return seed
}

// Validating a full slice validates five 35-case packs (most of a second
// each), so the encoded test slices are built once per run seed and shared.
var (
	encodedSlicesMu sync.Mutex
	encodedSlices   = map[int64][2]string{}
)

func encodedTestSlice(t *testing.T, runSeed int64) ([]byte, string) {
	t.Helper()
	encodedSlicesMu.Lock()
	defer encodedSlicesMu.Unlock()
	if e, ok := encodedSlices[runSeed]; ok {
		return []byte(e[0]), e[1]
	}
	raw, sum, err := EncodeSliceDataset(testSlice(t, testSliceSeasonSeed(t), runSeed))
	if err != nil {
		t.Fatal(err)
	}
	encodedSlices[runSeed] = [2]string{string(raw), sum}
	return raw, sum
}

func buildSliceVectors(t *testing.T) SliceVectors {
	t.Helper()
	seasonSeed := testSliceSeasonSeed(t)
	var out SliceVectors
	for _, v := range []struct {
		name string
		run  int64
	}{{"run-123", 123}, {"run-0", 0}} {
		raw, sum := encodedTestSlice(t, v.run)
		var ds SliceDataset
		if err := json.Unmarshal(raw, &ds); err != nil {
			t.Fatal(err)
		}
		packs := map[string]string{}
		for _, c := range ds.Columns {
			packs[c.ColumnID] = c.PackSHA256
		}
		out.Slices = append(out.Slices, SliceVector{
			Name: v.name, SeasonIndex: ds.SeasonIndex, SeasonSeed: seasonSeed, RunSeed: ds.RunSeed,
			Bytes: len(raw), SliceSHA256: sum, PackSHA256: packs,
		})
	}
	return out
}

func TestSliceVectorsGolden(t *testing.T) {
	t.Parallel()
	got := buildSliceVectors(t)
	path := "testdata/slice_vectors.json"
	if *update {
		raw, err := json.MarshalIndent(got, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(raw, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run go test ./challenge -run TestSliceVectorsGolden -update)", err)
	}
	var want SliceVectors
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatal(err)
	}
	gotRaw, _ := json.Marshal(got)
	wantRaw, _ := json.Marshal(want)
	if string(gotRaw) != string(wantRaw) {
		t.Fatal("slice vectors drifted; re-run with -update and review the diff")
	}
}

func TestSliceDatasetRoundTrip(t *testing.T) {
	t.Parallel()
	seasonSeed := testSliceSeasonSeed(t)
	raw, sum := encodedTestSlice(t, 123)
	if sum != Digest(raw) || len(raw) > MaxRoundDatasetBytes {
		t.Fatal("the digest must be the SHA-256 of the encoded bytes")
	}
	ds, cols, err := DecodeSliceDataset(raw, sum)
	if err != nil {
		t.Fatal(err)
	}
	// Re-encoding the decoded slice (packs already compact) is canonical.
	again, sum2, err := EncodeSliceDataset(ds)
	if err != nil || sum2 != sum || string(again) != string(raw) {
		t.Fatal("encoding must be canonical")
	}
	if ds.Contract != SliceContractV1 || ds.RunSeed != 123 || len(cols) != protocol.DuelingSliceColumns {
		t.Fatalf("unexpected decode: %+v", ds)
	}
	for i, c := range cols {
		if c.Column.ColumnID != SliceColumnID(i) || len(c.Pack.Cases) != protocol.DuelingSliceCasesPerColumn {
			t.Fatalf("column %d decoded as %s with %d cases", i, c.Column.ColumnID, len(c.Pack.Cases))
		}
	}
	if !cols[3].Column.Substituted || cols[3].Column.OwnerKey != "" || !cols[4].Column.PublicDefault {
		t.Fatal("column pins must round-trip")
	}
	if err := VerifySliceSeeds(ds, seasonSeed); err != nil {
		t.Fatal(err)
	}
	if err := VerifySliceSeeds(ds, testSeasonSeedOther(t)); err == nil {
		t.Fatal("slice seeds must be checked against the season seed")
	}
	if _, _, err := DecodeSliceDataset(raw, strings.Repeat("0", 64)); err == nil {
		t.Fatal("a digest mismatch must be refused")
	}
	trailing := append(append([]byte(nil), raw...), []byte(" {}")...)
	if _, _, err := DecodeSliceDataset(trailing, Digest(trailing)); err == nil {
		t.Fatal("trailing data must be refused")
	}
}

func testSeasonSeedOther(t *testing.T) string {
	t.Helper()
	seed, err := SeasonSeed(26119, testSeasonSecret("other"), "0x"+strings.Repeat("5a", 32))
	if err != nil {
		t.Fatal(err)
	}
	return seed
}

// reencode rewrites an encoded slice's JSON without re-validating, as a
// tampering scorer or transport would, and returns bytes and digest.
func reencode(t *testing.T, raw []byte, mutate func(*SliceDataset)) ([]byte, string) {
	t.Helper()
	var ds SliceDataset
	if err := json.Unmarshal(raw, &ds); err != nil {
		t.Fatal(err)
	}
	mutate(&ds)
	out, err := json.Marshal(ds)
	if err != nil {
		t.Fatal(err)
	}
	return out, Digest(out)
}

// repack rewrites one column's pack and its recorded digest.
func repack(t *testing.T, col *SliceDatasetColumn, mutate func(*Pack)) {
	t.Helper()
	p, err := Decode(col.Pack)
	if err != nil {
		t.Fatal(err)
	}
	mutate(&p)
	b, _ := json.Marshal(p)
	compact, sum, err := CompactPack(b)
	if err != nil {
		t.Fatal(err)
	}
	col.Pack, col.PackSHA256 = compact, sum
}

// TestSliceAcceptsEveryPolicyInItsWindow decodes a slice at the dueling floor
// whose columns declare each end of the floor's window (14 and 16 at v16,
// GradingPolicyWindowV16), and refuses one that declares a policy outside it.
func TestSliceAcceptsEveryPolicyInItsWindow(t *testing.T) {
	t.Parallel()
	accepted := GradingBenchVersionsThrough(testSliceBenchVersion)
	if want := []int{GradingBenchVersionV14, GradingBenchVersionV15, GradingBenchVersionV16}; !reflect.DeepEqual(accepted, want) {
		t.Fatalf("a v%d slice accepts %v, want %v", testSliceBenchVersion, accepted, want)
	}
	seasonSeed := testSliceSeasonSeed(t)
	for _, policy := range []int{accepted[0], accepted[len(accepted)-1]} {
		ds := testSlice(t, seasonSeed, 7)
		for i := range ds.Columns {
			ds.Columns[i].GradingBenchVersion = policy
			ds.Columns[i].Pack = slicePack(t, int64(200+i), ds.Columns[i].SliceSeed, policy)
		}
		raw, sum, err := EncodeSliceDataset(ds)
		if err != nil {
			t.Fatal(err)
		}
		if _, cols, err := DecodeSliceDataset(raw, sum); err != nil || len(cols) != protocol.DuelingSliceColumns {
			t.Fatalf("a slice at policy %d must decode: %v", policy, err)
		}
	}
	ds := testSlice(t, seasonSeed, 7)
	old := GradingBenchVersion
	ds.Columns[0].GradingBenchVersion = old
	ds.Columns[0].Pack = slicePack(t, 300, ds.Columns[0].SliceSeed, old)
	if _, _, err := EncodeSliceDataset(ds); err == nil {
		t.Fatalf("the encoder accepted a v%d slice column at policy %d", testSliceBenchVersion, old)
	}
	// The decoder must refuse it too, on bytes the validating encoder never saw.
	sort.Slice(ds.Columns, func(i, j int) bool { return ds.Columns[i].ColumnID < ds.Columns[j].ColumnID })
	for i := range ds.Columns {
		compact, packSum, err := CompactPack(ds.Columns[i].Pack)
		if err != nil {
			t.Fatal(err)
		}
		ds.Columns[i].Pack, ds.Columns[i].PackSHA256 = compact, packSum
	}
	forged, err := json.Marshal(ds)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := DecodeSliceDataset(forged, Digest(forged)); err == nil || !strings.Contains(err.Error(), "grading_bench_version") {
		t.Fatalf("a v%d slice must refuse a column at policy %d, got %v", testSliceBenchVersion, old, err)
	}
}

func TestDecodeSliceDatasetRefusesBrokenBindings(t *testing.T) {
	t.Parallel()
	raw, _ := encodedTestSlice(t, 123)
	cases := []struct {
		name   string
		want   string
		mutate func(*SliceDataset)
	}{
		{"swapped packs break the seed echo", "slice seed", func(ds *SliceDataset) {
			a, b := &ds.Columns[0], &ds.Columns[1]
			a.Pack, b.Pack = b.Pack, a.Pack
			a.PackSHA256, b.PackSHA256 = b.PackSHA256, a.PackSHA256
		}},
		{"slice seed rewritten without the pack", "slice seed", func(ds *SliceDataset) {
			ds.Columns[2].SliceSeed = strings.Repeat("1", 64)
		}},
		{"pack hotkey differs from the pin", "miner_hotkey", func(ds *SliceDataset) {
			repack(t, &ds.Columns[0], func(p *Pack) { p.MinerHotkey = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty" })
		}},
		{"pack policy differs from the pin", "grading_bench_version", func(ds *SliceDataset) {
			repack(t, &ds.Columns[1], func(p *Pack) { p.GradingBenchVersion = GradingBenchVersion })
		}},
		{"pinned policy outside the season window", "grading_bench_version", func(ds *SliceDataset) {
			ds.Columns[1].GradingBenchVersion = GradingBenchVersion
			repack(t, &ds.Columns[1], func(p *Pack) { p.GradingBenchVersion = GradingBenchVersion })
		}},
		{"pack fails the contract", "invalid", func(ds *SliceDataset) {
			repack(t, &ds.Columns[2], func(p *Pack) { p.Cases[0].EvidencePairIDs = []string{"no-such-pair"} })
		}},
		{"pack with the wrong case count", "invalid", func(ds *SliceDataset) {
			repack(t, &ds.Columns[2], func(p *Pack) { p.Cases = p.Cases[:MinCases] })
		}},
		{"pack digest not recorded", "digest", func(ds *SliceDataset) {
			ds.Columns[0].PackSHA256 = strings.Repeat("0", 64)
		}},
		{"substituted column keeps an owner", "public default", func(ds *SliceDataset) {
			ds.Columns[3].OwnerKey = "coldkey:x"
		}},
		{"substituted column not marked public default", "substituted", func(ds *SliceDataset) {
			ds.Columns[3].PublicDefault = false
		}},
		{"miner column without an owner", "owner_key", func(ds *SliceDataset) {
			ds.Columns[0].OwnerKey = ""
		}},
		{"a missing column", "columns", func(ds *SliceDataset) {
			ds.Columns = ds.Columns[:4]
		}},
		{"columns out of order", "columns", func(ds *SliceDataset) {
			ds.Columns[0], ds.Columns[1] = ds.Columns[1], ds.Columns[0]
		}},
		{"bench version below the dueling floor", "bench_version", func(ds *SliceDataset) {
			ds.BenchVersion = protocol.DuelingMinBenchVersion - 1
		}},
		{"wrong contract", "contract", func(ds *SliceDataset) {
			ds.Contract = RoundContractV1
		}},
		{"negative run seed", "run_seed", func(ds *SliceDataset) {
			ds.RunSeed = -1
		}},
	}
	for _, tc := range cases {
		bad, sum := reencode(t, raw, tc.mutate)
		_, _, err := DecodeSliceDataset(bad, sum)
		if err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: want an error mentioning %q, got %v", tc.name, tc.want, err)
		}
	}
	// Unknown fields are refused, not dropped.
	extra := strings.Replace(string(raw), `"contract":`, `"extra":1,"contract":`, 1)
	if _, _, err := DecodeSliceDataset([]byte(extra), Digest([]byte(extra))); err == nil {
		t.Fatal("an unknown field must be refused")
	}
}

func TestEncodeSliceDatasetRefusesBadColumns(t *testing.T) {
	t.Parallel()
	seasonSeed := testSliceSeasonSeed(t)
	// testSlice gives c0 at index 2; breaking it fails before any pack is
	// validated.
	ds := testSlice(t, seasonSeed, 9)
	ds.Columns[2].ColumnID = "c7"
	if _, _, err := EncodeSliceDataset(ds); err == nil || !strings.Contains(err.Error(), "c0..c4") {
		t.Fatalf("a column id outside c0..c4 must be refused, got %v", err)
	}
	ds = testSlice(t, seasonSeed, 9)
	ds.Columns[2].Pack = slicePack(t, 1, strings.Repeat("2", 64), testSlicePolicy(t))
	if _, _, err := EncodeSliceDataset(ds); err == nil || !strings.Contains(err.Error(), "slice seed") {
		t.Fatalf("a pack generated for another seed must be refused, got %v", err)
	}
	ds = testSlice(t, seasonSeed, 9)
	ds.Columns = append(ds.Columns, ds.Columns[0])
	if _, _, err := EncodeSliceDataset(ds); err == nil {
		t.Fatal("a sixth column must be refused")
	}
}

func TestSliceExecutionCasesDropsRepeatsOfEarlierColumns(t *testing.T) {
	t.Parallel()
	seasonSeed := testSliceSeasonSeed(t)
	ds := testSlice(t, seasonSeed, 11)
	// c3 (given at index 3) re-emits c0's pack content on its own seed: every
	// case repeats an earlier column's fingerprint.
	for i := range ds.Columns {
		if ds.Columns[i].ColumnID == "c3" {
			ds.Columns[i].Pack = slicePack(t, 100, ds.Columns[i].SliceSeed, testSlicePolicy(t))
		}
	}
	// SliceExecutionCases is a pure function of the decoded packs; skip the
	// (slow) full validation the round-trip tests already cover.
	cols := make([]SliceColumnPack, len(ds.Columns))
	for _, c := range ds.Columns {
		p, err := Decode(c.Pack)
		if err != nil {
			t.Fatal(err)
		}
		i, _ := strconv.Atoi(strings.TrimPrefix(c.ColumnID, "c"))
		cols[i] = SliceColumnPack{Column: c, Pack: p}
	}
	got := SliceExecutionCases(cols)
	perColumn := map[string]int{}
	seen := map[string]bool{}
	for _, c := range got {
		perColumn[c.ColumnID]++
		if !strings.HasPrefix(c.CaseID, c.ColumnID+"/") {
			t.Fatalf("slice case ids must carry the column: %q", c.CaseID)
		}
		if seen[c.Fingerprint] {
			t.Fatalf("fingerprint %s executed twice", c.Fingerprint)
		}
		seen[c.Fingerprint] = true
	}
	if perColumn["c3"] != 0 || perColumn["c0"] != protocol.DuelingSliceCasesPerColumn {
		t.Fatalf("the later column must lose the repeats, not the earlier one: %v", perColumn)
	}
	if len(got) != 4*protocol.DuelingSliceCasesPerColumn {
		t.Fatalf("executed %d cases", len(got))
	}
	if got[0].ColumnID != "c0" || got[len(got)-1].ColumnID != "c4" {
		t.Fatal("cases run in column order")
	}
}

// A pack that cannot be compacted, a pack not in compact form, and a compact
// pack with the wrong recorded digest each fail with their own reason; only
// the last is a digest mismatch.
func TestSliceColumnPackFailuresAreDistinct(t *testing.T) {
	t.Parallel()
	raw, _ := encodedTestSlice(t, 123)
	var ds SliceDataset
	if err := json.Unmarshal(raw, &ds); err != nil {
		t.Fatal(err)
	}
	good := ds.Columns[0]
	if _, err := checkSliceColumnPackBytes(good); err != nil {
		t.Fatalf("a well-formed column must pass: %v", err)
	}

	broken := good
	broken.Pack = json.RawMessage(`{"cases":`)
	if _, err := checkSliceColumnPackBytes(broken); err == nil || !strings.Contains(err.Error(), "compaction failed") || strings.Contains(err.Error(), "digest") {
		t.Fatalf("an uncompactable pack must report a compaction error, got %v", err)
	}

	spaced := good
	spaced.Pack = append(json.RawMessage(" "), good.Pack...)
	if _, err := checkSliceColumnPackBytes(spaced); err == nil || !strings.Contains(err.Error(), "compact form") || strings.Contains(err.Error(), "digest") {
		t.Fatalf("a non-compact pack must report its form, got %v", err)
	}

	wrong := good
	wrong.PackSHA256 = strings.Repeat("0", 64)
	if _, err := checkSliceColumnPackBytes(wrong); err == nil || !strings.Contains(err.Error(), "digest mismatch") {
		t.Fatalf("a wrong recorded digest must report a mismatch, got %v", err)
	}

	// Through the decoder: whitespace inside a pack is a form error, not a
	// digest mismatch.
	needle := `"pack":{`
	if !strings.Contains(string(raw), needle) {
		t.Fatal("encoded slice has no compact pack to perturb")
	}
	bad := []byte(strings.Replace(string(raw), needle, `"pack":{ `, 1))
	if _, _, err := DecodeSliceDataset(bad, Digest(bad)); err == nil || !strings.Contains(err.Error(), "compact form") {
		t.Fatalf("decoder must report a non-compact pack, got %v", err)
	}
}
