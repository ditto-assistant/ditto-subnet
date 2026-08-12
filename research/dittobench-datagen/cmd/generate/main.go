// Command generate produces a DittoBench dataset from a seed, deterministically.
//
// The generator is fully non-LLM, so a given (seed, bench_version) always yields
// the identical artifact bytes. This binary is the public, auditable entry point:
// given the seed the platform derived on-chain for a scored submission (published
// on the leaderboard), anyone can regenerate the exact dataset that submission was
// graded against and independently re-grade it.
//
// Usage:
//
//	generate -bench-version 3 -seed 123456789 -run-size full
//	generate -bench-version 2 -seed 123456789 -run-size full -out d.json
//	generate -bench-version 3 -seed 123456789 -sha
//	generate -bench-version 3 -run-size small # random seed (prints it)
//
// The SHA-256 printed on stderr is the same dataset_sha256 the platform pins and a
// validator re-derives. If two runs of the same seed print a different hash, the
// generator is not deterministic. That invariant is guarded by the CI determinism
// test.
package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func main() {
	var (
		seed    int64
		hasSeed bool
		runSize string
		outPath string
		shaOnly bool
		version int
	)
	flag.IntVar(&version, "bench-version", 0, "required benchmark generation version (2 through 10)")
	flag.Int64Var(&seed, "seed", 0, "dataset seed (omit for a fresh random seed)")
	flag.StringVar(&runSize, "run-size", "full", "profile: small | medium | full")
	flag.StringVar(&outPath, "out", "", "write canonical JSON here (default: stdout)")
	flag.BoolVar(&shaOnly, "sha", false, "print only the SHA-256 of the artifact, no JSON")
	flag.Parse()
	if !protocol.SupportedBenchVersion(version) {
		fmt.Fprintln(os.Stderr, "-bench-version is required and must be 2 through 10")
		os.Exit(2)
	}

	// Detect whether -seed was passed so an omitted seed can default to random.
	flag.Visit(func(f *flag.Flag) {
		if f.Name == "seed" {
			hasSeed = true
		}
	})
	if !hasSeed {
		seed = gen.FreshSeed()
	}

	prof, ok := gen.ProfileForVersion(runSize, version)
	if !ok {
		fmt.Fprintf(os.Stderr, "unknown run-size %q; valid: small, medium, full\n", runSize)
		os.Exit(2)
	}

	art, err := gen.GenerateDataset(seed, prof, version)
	if err != nil {
		fmt.Fprintf(os.Stderr, "generate artifact: %v\n", err)
		os.Exit(1)
	}
	sha, canonical, err := art.SHA256Hex()
	if err != nil {
		fmt.Fprintf(os.Stderr, "hash artifact: %v\n", err)
		os.Exit(1)
	}

	// Provenance to stderr so stdout stays clean for piping the artifact.
	fmt.Fprintf(os.Stderr, "seed=%d run_size=%s bench_version=%d dataset_sha256=%s\n",
		seed, runSize, art.BenchVersion, sha)

	if shaOnly {
		fmt.Println(sha)
		return
	}

	if outPath == "" {
		if _, err := os.Stdout.Write(canonical); err != nil {
			fmt.Fprintf(os.Stderr, "write stdout: %v\n", err)
			os.Exit(1)
		}
		fmt.Println()
		return
	}
	if err := os.WriteFile(outPath, canonical, 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "write %s: %v\n", outPath, err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "wrote %d bytes to %s\n", len(canonical), outPath)
}
