package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func main() {
	// The probe defaults to the newest version this module can reproduce so a
	// bench bump never silently re-pins the exposure audit to an older contract.
	version := flag.Int("bench-version", protocol.NewestSupportedBenchVersion(), "benchmark contract version (v10 or later)")
	seed := flag.Int64("seed", 1, "dataset seed")
	runSize := flag.String("run-size", "full", "small, medium, or full")
	flag.Parse()

	profile, ok := gen.ProfileForVersion(*runSize, *version)
	if !ok {
		fmt.Fprintf(os.Stderr, "unsupported run size %q for bench version %d\n", *runSize, *version)
		os.Exit(1)
	}
	artifact, err := gen.GenerateDataset(*seed, profile, *version)
	if err != nil {
		fmt.Fprintln(os.Stderr, "memoryprobe:", err)
		os.Exit(1)
	}
	result, err := gen.AuditMemoryExposureForVersion(artifact, *version)
	if err != nil {
		fmt.Fprintln(os.Stderr, "memoryprobe:", err)
		os.Exit(1)
	}
	fmt.Printf("bench v%d %s seed %d: transformed %d/%d = %.4f; verbatim %d/%d = %.4f; computed-by-v13-rule %d (strict transformed %d/%d = %.4f)\n",
		*version, *runSize, *seed, result.Transformed, result.Eligible, result.TransformedShare(), result.Verbatim, result.Eligible, result.VerbatimShare(),
		result.ComputedByRule, result.Transformed-result.ComputedByRule, result.Eligible, result.StrictTransformedShare())
}
