// Command rerank-pools generates reranking pools for the SN118 Season 2
// reranker competition (docs/reranker-season.md).
//
// It runs the ordinary deterministic dataset generator for each seed and
// converts every memory case with planted evidence into a pool (package
// rerankpool). The output is one JSON bundle the dittobench-reranker scorer
// reads.
//
// Usage:
//
//	rerank-pools -seed 123456789 -count 8 -out pools.json
//	rerank-pools -seed 123456789 -count 8 -surface-salt 7 -sha
//
// Seeds are seed, seed+1, ..., seed+count-1. Scored seeds are derived by
// Platform after a submission is committed; any seed a miner picks locally is
// practice. -surface-salt re-renders bench_version >= 13 wording under a held
// salt, so scored pools differ in surface form from anything public.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-reranker/poolgen/rerankpool"
)

// Bundle is the file format: every set produced in one invocation.
type Bundle struct {
	Schema       string           `json:"schema"`
	BenchVersion int              `json:"bench_version"`
	RunSize      string           `json:"run_size"`
	PoolSize     int              `json:"pool_size"`
	Sets         []rerankpool.Set `json:"sets"`
}

func main() {
	var (
		seed     int64
		count    int
		version  int
		runSize  string
		salt     uint64
		poolSize int
		outPath  string
		shaOnly  bool
	)
	flag.Int64Var(&seed, "seed", 0, "first dataset seed (required)")
	flag.IntVar(&count, "count", 1, "number of consecutive seeds")
	flag.IntVar(&version, "bench-version", protocol.BenchVersionV13, "dataset generation version ("+protocol.SupportedBenchVersionList()+")")
	flag.StringVar(&runSize, "run-size", "full", "dataset profile: small | medium | full")
	flag.Uint64Var(&salt, "surface-salt", 0, "bench_version >= 13 surface salt (0 = public rehearsal default)")
	flag.IntVar(&poolSize, "pool-size", rerankpool.DefaultPoolSize, "candidates per pool")
	flag.StringVar(&outPath, "out", "", "write the bundle here (default: stdout)")
	flag.BoolVar(&shaOnly, "sha", false, "print only the bundle SHA-256")
	flag.Parse()

	seedSet := false
	flag.Visit(func(f *flag.Flag) {
		if f.Name == "seed" {
			seedSet = true
		}
	})
	if !seedSet {
		fail(2, "-seed is required")
	}
	if count < 1 || count > 10_000 {
		fail(2, "-count must be between 1 and 10000")
	}
	if !protocol.SupportedBenchVersion(version) {
		fail(2, "-bench-version must be one of "+protocol.SupportedBenchVersionList())
	}
	if salt != 0 && version < protocol.BenchVersionV13 {
		fail(2, "-surface-salt applies to bench_version 13 and later only")
	}
	prof, ok := gen.ProfileForVersion(runSize, version)
	if !ok {
		fail(2, "unknown -run-size "+runSize)
	}

	bundle := Bundle{
		Schema:       rerankpool.SchemaVersion,
		BenchVersion: version,
		RunSize:      runSize,
		PoolSize:     poolSize,
		Sets:         make([]rerankpool.Set, 0, count),
	}
	kept := 0
	for i := 0; i < count; i++ {
		s := seed + int64(i)
		artifact, err := gen.GenerateDatasetWithSurface(s, prof, version, gen.SurfaceOptions{Salt: salt})
		if err != nil {
			fail(1, fmt.Sprintf("generate seed %d: %v", s, err))
		}
		set, err := rerankpool.Build(artifact, rerankpool.Options{PoolSize: poolSize})
		if err != nil {
			fail(1, fmt.Sprintf("pools for seed %d: %v", s, err))
		}
		kept += len(set.Pools)
		bundle.Sets = append(bundle.Sets, set)
	}
	if kept == 0 {
		fail(1, rerankpool.ErrNoPools.Error())
	}

	raw, err := json.Marshal(bundle)
	if err != nil {
		fail(1, err.Error())
	}
	sum := sha256.Sum256(raw)
	digest := hex.EncodeToString(sum[:])
	fmt.Fprintf(os.Stderr, "pools=%d sets=%d sha256=%s\n", kept, len(bundle.Sets), digest)
	if shaOnly {
		fmt.Println(digest)
		return
	}
	if outPath == "" {
		os.Stdout.Write(raw)
		return
	}
	if err := os.WriteFile(outPath, raw, 0o644); err != nil {
		fail(1, err.Error())
	}
}

func fail(code int, msg string) {
	fmt.Fprintln(os.Stderr, "rerank-pools: "+msg)
	os.Exit(code)
}
