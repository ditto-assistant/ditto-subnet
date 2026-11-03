package challenge

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strconv"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// SliceContractV1 is the dueling slice dataset contract
// (docs/dueling-bench.md section 7.2).
//
// From protocol.DuelingMinBenchVersion a canonical agent run is a datagen half
// plus a generator slice: protocol.DuelingSliceColumns columns of
// protocol.DuelingSliceCasesPerColumn cases, one per pinned season generator,
// each generated on its own SliceSeed. Platform's trusted scorer encodes the
// slice once per (season, run seed); the validator receives the bytes with the
// lease's digest and the scorer decodes them before any case runs.
const SliceContractV1 = "dittobench-dueling-slice/v1"

// SliceCaseCategory labels slice cases in a score report. Slice case IDs are
// RoundCaseID(column_id, case_id).
const SliceCaseCategory = "dueling_slice"

var sliceIdentPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)

// SliceDataset is the encoded generator slice of one agent run.
type SliceDataset struct {
	Contract     string `json:"contract"`
	BenchVersion int    `json:"bench_version"`
	SeasonID     string `json:"season_id"`
	// SeasonIndex is the generator round index the season opened on.
	SeasonIndex int64 `json:"season_index"`
	// RunSeed is the ticket's datagen seed every column's SliceSeed binds.
	RunSeed int64                `json:"run_seed"`
	Columns []SliceDatasetColumn `json:"columns"`
}

// SliceDatasetColumn is one pinned generator's pack for the run.
//
// A column the pinned generator failed on is Substituted: it carries the
// public default generator's pack on the same SliceSeed, with PublicDefault
// set and an empty OwnerKey. A public default column is owned by nobody and is
// never masked; every other column names its owner so self- and pair-masks
// can drop it.
type SliceDatasetColumn struct {
	ColumnID     string `json:"column_id"`
	SubmissionID string `json:"submission_id"`
	OwnerKey     string `json:"owner_key"`
	MinerHotkey  string `json:"miner_hotkey"`
	TreeSHA256   string `json:"tree_sha256"`
	// GradingBenchVersion is the pack policy the season pinned for the
	// column; the pack must declare exactly it.
	GradingBenchVersion int    `json:"grading_bench_version"`
	SliceSeed           string `json:"slice_seed"`
	PublicDefault       bool   `json:"public_default"`
	Substituted         bool   `json:"substituted"`
	// PackSHA256 is the SHA-256 of the compact pack exactly as encoded.
	PackSHA256 string          `json:"pack_sha256"`
	Pack       json.RawMessage `json:"pack"`
}

// SliceColumnPack is a decoded, validated slice column.
type SliceColumnPack struct {
	Column SliceDatasetColumn
	Pack   Pack
}

// SliceColumnID is the id of the i-th slice column: c0, c1, ...
func SliceColumnID(i int) string { return "c" + strconv.Itoa(i) }

// SliceGenerateRequest is the request a slice column's pack must satisfy: its
// slice seed, its pinned generator hotkey, exactly
// protocol.DuelingSliceCasesPerColumn cases, and exactly its pinned policy.
func SliceGenerateRequest(col SliceDatasetColumn) GenerateRequest {
	req := NewGenerateRequest(col.SliceSeed, col.MinerHotkey, protocol.DuelingSliceCasesPerColumn)
	req.GradingBenchVersion = col.GradingBenchVersion
	return req
}

func checkSliceHeader(ds SliceDataset) error {
	switch {
	case ds.Contract != SliceContractV1:
		return fmt.Errorf("contract must be %q", SliceContractV1)
	case !protocol.DuelingBenchVersion(ds.BenchVersion):
		return fmt.Errorf("bench_version %d carries no dueling slice (needs >= %d)", ds.BenchVersion, protocol.DuelingMinBenchVersion)
	case !sliceIdentPattern.MatchString(ds.SeasonID):
		return errors.New("season_id is invalid")
	case ds.SeasonIndex < 0:
		return errors.New("season_index must be non-negative")
	case ds.RunSeed < 0:
		return errors.New("run_seed must be non-negative")
	case len(ds.Columns) != protocol.DuelingSliceColumns:
		return fmt.Errorf("slice needs exactly %d columns, got %d", protocol.DuelingSliceColumns, len(ds.Columns))
	}
	return nil
}

// checkSliceColumn checks a column's pin against its pack. raw must already
// be the compact pack bytes.
func checkSliceColumn(benchVersion int, col SliceDatasetColumn, raw []byte) (Pack, error) {
	switch {
	case !sliceIdentPattern.MatchString(col.SubmissionID):
		return Pack{}, errors.New("submission_id is invalid")
	case !ValidHotkey(col.MinerHotkey):
		return Pack{}, errors.New("miner_hotkey must be an SS58 hotkey")
	case !shaPattern.MatchString(col.TreeSHA256):
		return Pack{}, errors.New("tree_sha256 must be 64 lowercase hex characters")
	case !shaPattern.MatchString(col.SliceSeed):
		return Pack{}, errors.New("slice_seed must be 64 lowercase hex characters")
	case col.Substituted && !col.PublicDefault:
		return Pack{}, errors.New("a substituted column must carry the public default generator")
	case col.PublicDefault && col.OwnerKey != "":
		return Pack{}, errors.New("a public default column must have an empty owner_key")
	case !col.PublicDefault && col.OwnerKey == "":
		return Pack{}, errors.New("a miner generator column must name its owner_key")
	case len(col.OwnerKey) > 256:
		return Pack{}, errors.New("owner_key is too long")
	}
	accepted := false
	for _, v := range GradingBenchVersionsThrough(benchVersion) {
		accepted = accepted || v == col.GradingBenchVersion
	}
	if !accepted {
		return Pack{}, fmt.Errorf("pinned grading_bench_version %d is not one a v%d season accepts %v",
			col.GradingBenchVersion, benchVersion, GradingBenchVersionsThrough(benchVersion))
	}
	p, err := Decode(raw)
	if err != nil {
		return Pack{}, errors.New("pack does not decode")
	}
	switch {
	case p.Generator.Seed != col.SliceSeed:
		return Pack{}, errors.New("pack was not generated for its slice seed")
	case p.MinerHotkey != col.MinerHotkey:
		return Pack{}, errors.New("pack does not echo its pinned miner_hotkey")
	case p.GradingBenchVersion != col.GradingBenchVersion:
		return Pack{}, fmt.Errorf("pack declares grading_bench_version %d, the season pinned %d", p.GradingBenchVersion, col.GradingBenchVersion)
	}
	if r := ValidateGenerated(raw, SliceGenerateRequest(col)); !r.Valid {
		return Pack{}, fmt.Errorf("pack is invalid (%d issues, first %s)", len(r.Issues), r.Issues[0].Rule)
	}
	return p, nil
}

// EncodeSliceDataset encodes a slice into canonical bytes and returns them
// with their SHA-256. Each column's Pack holds the generator's raw output;
// PackSHA256 is ignored and recomputed. Columns are sorted by column ID and
// must be exactly c0..c(DuelingSliceColumns-1); every pack is compacted and
// must pass its column's binding (SliceGenerateRequest) and the pack contract,
// so the same packs always encode to the same bytes and digest.
func EncodeSliceDataset(ds SliceDataset) ([]byte, string, error) {
	fail := func(err error) ([]byte, string, error) {
		return nil, "", fmt.Errorf("slice dataset: %w", err)
	}
	if err := checkSliceHeader(ds); err != nil {
		return fail(err)
	}
	cols := append([]SliceDatasetColumn(nil), ds.Columns...)
	sort.SliceStable(cols, func(i, j int) bool { return cols[i].ColumnID < cols[j].ColumnID })
	for i := range cols {
		col := &cols[i]
		if col.ColumnID != SliceColumnID(i) {
			return fail(fmt.Errorf("columns must be exactly c0..c%d, got %q", protocol.DuelingSliceColumns-1, col.ColumnID))
		}
		compact, sum, err := CompactPack(col.Pack)
		if err != nil {
			return fail(fmt.Errorf("column %s: %w", col.ColumnID, err))
		}
		if _, err := checkSliceColumn(ds.BenchVersion, *col, compact); err != nil {
			return fail(fmt.Errorf("column %s: %w", col.ColumnID, err))
		}
		col.Pack = compact
		col.PackSHA256 = sum
	}
	ds.Columns = cols
	raw, err := json.Marshal(ds)
	if err != nil {
		return fail(err)
	}
	if len(raw) > MaxRoundDatasetBytes {
		return fail(fmt.Errorf("exceeds %d bytes", MaxRoundDatasetBytes))
	}
	return raw, Digest(raw), nil
}

// DecodeSliceDataset verifies slice bytes against the digest a lease pinned
// and returns every column's validated pack, in column order.
//
// It refuses bytes that do not hash to expectedSHA, unknown fields or trailing
// data, a column set other than c0..c(DuelingSliceColumns-1), a pack whose
// recorded digest or compact form does not match, a pack that does not echo
// its column's slice_seed and miner_hotkey, a pack declaring a policy other
// than the column's pinned grading_bench_version (which itself must be one the
// slice's bench version accepts), and a pack failing ValidateGenerated.
func DecodeSliceDataset(raw []byte, expectedSHA string) (SliceDataset, []SliceColumnPack, error) {
	fail := func(reason string) (SliceDataset, []SliceColumnPack, error) {
		return SliceDataset{}, nil, errors.New("slice dataset: " + reason)
	}
	if len(raw) == 0 || len(raw) > MaxRoundDatasetBytes {
		return fail("invalid size")
	}
	sum := sha256.Sum256(raw)
	if !shaPattern.MatchString(expectedSHA) || hex.EncodeToString(sum[:]) != expectedSHA {
		return fail("lease digest mismatch")
	}
	var ds SliceDataset
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&ds); err != nil {
		return fail("invalid JSON")
	}
	var extra json.RawMessage
	if err := dec.Decode(&extra); err != io.EOF {
		return fail("trailing data after the dataset")
	}
	if err := checkSliceHeader(ds); err != nil {
		return fail(err.Error())
	}
	out := make([]SliceColumnPack, 0, len(ds.Columns))
	for i, col := range ds.Columns {
		if col.ColumnID != SliceColumnID(i) {
			return fail("columns must be exactly c0..c" + strconv.Itoa(protocol.DuelingSliceColumns-1) + " in order")
		}
		compact, err := checkSliceColumnPackBytes(col)
		if err != nil {
			return fail("column " + col.ColumnID + " " + err.Error())
		}
		p, err := checkSliceColumn(ds.BenchVersion, col, compact)
		if err != nil {
			return fail("column " + col.ColumnID + ": " + err.Error())
		}
		out = append(out, SliceColumnPack{Column: col, Pack: p})
	}
	return ds, out, nil
}

// checkSliceColumnPackBytes binds a column's pack bytes to its recorded
// digest. Each failure has its own reason, so a pack that cannot be compacted
// or is not in compact form is never reported as a digest mismatch: only a
// compact pack whose SHA-256 differs from pack_sha256 is a mismatch.
func checkSliceColumnPackBytes(col SliceDatasetColumn) ([]byte, error) {
	compact, digest, err := CompactPack(col.Pack)
	if err != nil {
		return nil, fmt.Errorf("pack compaction failed: %w", err)
	}
	if !bytes.Equal(compact, col.Pack) {
		return nil, errors.New("pack is not in compact form")
	}
	if digest != col.PackSHA256 {
		return nil, errors.New("pack digest mismatch")
	}
	return compact, nil
}

// VerifySliceSeeds checks every column's slice_seed against the revealed
// season seed: SliceSeed(seasonSeed, ds.RunSeed, column_id). Before reveal a
// validator cannot run it (the season seed is secret) and relies on the lease
// digest; after reveal anyone can.
func VerifySliceSeeds(ds SliceDataset, seasonSeed string) error {
	for _, col := range ds.Columns {
		want, err := SliceSeed(seasonSeed, ds.RunSeed, col.ColumnID)
		if err != nil {
			return fmt.Errorf("slice dataset: %w", err)
		}
		if col.SliceSeed != want {
			return fmt.Errorf("slice dataset: column %s slice_seed does not derive from the season seed", col.ColumnID)
		}
	}
	return nil
}

// SliceExecutionCase is one slice case as a run executes it.
type SliceExecutionCase struct {
	ColumnID string
	// CaseID is RoundCaseID(column_id, case id), the report identity.
	CaseID      string
	Fingerprint string
	Case        Case
}

// SliceExecutionCases returns the cases a run executes, in column then pack
// order. A case whose Fingerprint repeats a case of an earlier column is
// dropped from the later column (the round claim rule), so every consumer
// dedupes identically and a column that copies another earns nothing for the
// copy. Column means keep equal column weights whatever a column loses.
func SliceExecutionCases(columns []SliceColumnPack) []SliceExecutionCase {
	claimed := map[string]bool{}
	var out []SliceExecutionCase
	for _, col := range columns {
		id := col.Column.ColumnID
		var fps []string
		for _, c := range col.Pack.Cases {
			fp := Fingerprint(c)
			fps = append(fps, fp)
			if claimed[fp] {
				continue
			}
			out = append(out, SliceExecutionCase{ColumnID: id, CaseID: RoundCaseID(id, c.ID), Fingerprint: fp, Case: c})
		}
		for _, fp := range fps {
			claimed[fp] = true
		}
	}
	return out
}
