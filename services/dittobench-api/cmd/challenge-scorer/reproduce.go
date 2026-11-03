package main

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// reproduce-slice rebuilds a revealed dueling slice byte for byte
// (dueling-bench.md section 2, "Delayed reproducibility"). After a season's
// reveal, its secret and anchor block are public, and each pinned
// generator's exact source is published once its embargo ends. With the run's
// public seed they determine every column's slice seed and pack, so the
// encoded slice's SHA-256 must equal the slice_sha256 on the score row:
//
//	challenge-scorer reproduce-slice -season-secret <hex> -season-index 26119 \
//	    -anchor-block-hash 0x... -run-seed 123 -bench-version 16 -pins pins.json \
//	    -source c0=gen0.tar.gz -source c1=./gen1 ... -expect-sha256 <hex>
//
// pins.json names the season and each column's pin as the season and score
// row published them: {"season_id": "...", "columns": [{"column_id": "c0",
// "submission_id": "...", "owner_key": "...", "miner_hotkey": "5...",
// "tree_sha256": "...", "grading_bench_version": 16, "public_default": false,
// "substituted": false}, ...]}. A revealed slice file has that shape too and
// works as pins (its packs are ignored). A substituted column's -source is the
// public default generator's. A -source directory is packed like an upload;
// its tree digest must match the pin's tree_sha256 when the pin names one.

type slicePins struct {
	SeasonID string     `json:"season_id"`
	Columns  []slicePin `json:"columns"`
}

type slicePin struct {
	ColumnID            string `json:"column_id"`
	SubmissionID        string `json:"submission_id"`
	OwnerKey            string `json:"owner_key"`
	MinerHotkey         string `json:"miner_hotkey"`
	TreeSHA256          string `json:"tree_sha256"`
	GradingBenchVersion int    `json:"grading_bench_version"`
	SliceSeed           string `json:"slice_seed"`
	PublicDefault       bool   `json:"public_default"`
	Substituted         bool   `json:"substituted"`
}

type sourceFlags map[string]string

func (s sourceFlags) String() string { return fmt.Sprint(len(s)) }

func (s sourceFlags) Set(v string) error {
	id, path, ok := strings.Cut(v, "=")
	if !ok || id == "" || path == "" {
		return fmt.Errorf("source %q must be column_id=path", v)
	}
	if _, dup := s[id]; dup {
		return fmt.Errorf("duplicate -source for column %s", id)
	}
	s[id] = path
	return nil
}

func readSource(ctx context.Context, path string) ([]byte, error) {
	st, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if st.IsDir() {
		raw, _, err := genrun.PackDir(ctx, path)
		return raw, err
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, int64(challenge.MaxArchiveBytes)+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > challenge.MaxArchiveBytes {
		return nil, fmt.Errorf("source archive exceeds %d bytes", challenge.MaxArchiveBytes)
	}
	return raw, nil
}

func reproduceSlice(ctx context.Context, args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("reproduce-slice", flag.ContinueOnError)
	secretHex := fs.String("season-secret", "", "revealed season secret (64 hex characters)")
	seasonIndex := fs.Int64("season-index", -1, "season index (the generator round the season opened on)")
	anchor := fs.String("anchor-block-hash", "", "season anchor block hash (32 bytes of hex)")
	runSeed := fs.Int64("run-seed", -1, "the run's public datagen seed")
	benchVersion := fs.Int("bench-version", protocol.DuelingMinBenchVersion, "the run's bench version")
	pinsPath := fs.String("pins", "", "season and column pins (JSON)")
	seasonID := fs.String("season-id", "", "season id (overrides the pins file)")
	sandbox := fs.String("sandbox", "docker", "docker (isolated) or local (runs generator code on this host)")
	expect := fs.String("expect-sha256", "", "fail unless the slice hashes to this digest")
	out := fs.String("out", "", "write the slice bytes here")
	sources := sourceFlags{}
	fs.Var(sources, "source", "column_id=archive-or-directory (repeatable)")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	secret, err := hex.DecodeString(strings.TrimPrefix(strings.TrimSpace(*secretHex), "0x"))
	switch {
	case err != nil || len(secret) != challenge.SeasonSecretBytes:
		return errors.New("reproduce-slice: -season-secret must be 32 bytes of hex")
	case *seasonIndex < 0 || *runSeed < 0:
		return errors.New("reproduce-slice: -season-index and -run-seed are required (non-negative)")
	case *pinsPath == "":
		return errors.New("reproduce-slice: -pins is required")
	}
	rawPins, err := os.ReadFile(*pinsPath)
	if err != nil {
		return err
	}
	var pins slicePins
	if err := json.Unmarshal(rawPins, &pins); err != nil {
		return fmt.Errorf("reproduce-slice: pins: %w", err)
	}
	if *seasonID != "" {
		pins.SeasonID = *seasonID
	}
	commitment, err := challenge.SeasonCommitment(uint64(*seasonIndex), secret)
	if err != nil {
		return err
	}
	seasonSeed, err := challenge.SeasonSeed(uint64(*seasonIndex), secret, *anchor)
	if err != nil {
		return fmt.Errorf("reproduce-slice: %w", err)
	}
	log.Printf("season %d: commitment %s (compare with the published commitment)", *seasonIndex, commitment)
	sb, err := sandboxFor(*sandbox)
	if err != nil {
		return err
	}
	spec := sliceSpec{Header: challenge.SliceDataset{
		BenchVersion: *benchVersion, SeasonID: pins.SeasonID, SeasonIndex: *seasonIndex, RunSeed: *runSeed,
	}}
	for _, pin := range pins.Columns {
		seed, err := challenge.SliceSeed(seasonSeed, *runSeed, pin.ColumnID)
		if err != nil {
			return fmt.Errorf("reproduce-slice: column %s: %w", pin.ColumnID, err)
		}
		if pin.SliceSeed != "" && pin.SliceSeed != seed {
			return fmt.Errorf("reproduce-slice: column %s: pinned slice_seed %s does not derive from the season seed (%s)", pin.ColumnID, pin.SliceSeed, seed)
		}
		path, ok := sources[pin.ColumnID]
		if !ok {
			return fmt.Errorf("reproduce-slice: no -source for column %s", pin.ColumnID)
		}
		raw, err := readSource(ctx, path)
		if err != nil {
			return fmt.Errorf("reproduce-slice: column %s source: %w", pin.ColumnID, err)
		}
		spec.Columns = append(spec.Columns, duelingSliceColumn{
			duelingSource: duelingSource{
				SubmissionID: pin.SubmissionID, MinerHotkey: pin.MinerHotkey, TreeSHA256: pin.TreeSHA256,
				SourceSHA256: challenge.Digest(raw), SourceBase64: base64.StdEncoding.EncodeToString(raw),
			},
			ColumnID: pin.ColumnID, OwnerKey: pin.OwnerKey, GradingBenchVersion: pin.GradingBenchVersion,
			SliceSeed: seed, PublicDefault: pin.PublicDefault, Substituted: pin.Substituted,
		})
	}
	if len(sources) != len(spec.Columns) {
		return fmt.Errorf("reproduce-slice: %d -source flags for %d pinned columns", len(sources), len(spec.Columns))
	}
	if err := checkSliceSpec(&spec, 0); err != nil {
		return fmt.Errorf("reproduce-slice: %w", err)
	}
	cache := newBuildCache(sb, buildCacheSize)
	defer cache.Close()
	results, raw, sum, err := buildSlice(ctx, cache, spec)
	if err != nil {
		return fmt.Errorf("reproduce-slice: %w", err)
	}
	for _, r := range results {
		if !r.OK {
			return fmt.Errorf("reproduce-slice: column %s did not reproduce: %s", r.ColumnID, r.Error)
		}
	}
	// A mismatch must not overwrite a previously verified -out.
	if *expect != "" && !strings.EqualFold(strings.TrimSpace(*expect), sum) {
		return fmt.Errorf("reproduce-slice: slice hashes to %s, expected %s", sum, *expect)
	}
	if *out != "" {
		if err := os.WriteFile(*out, raw, 0o644); err != nil {
			return err
		}
	}
	_, err = fmt.Fprintln(stdout, sum)
	return err
}
