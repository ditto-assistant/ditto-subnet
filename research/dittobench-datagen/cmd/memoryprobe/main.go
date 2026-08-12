package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func main() {
	seed := flag.Int64("seed", 1, "dataset seed")
	runSize := flag.String("run-size", "full", "small, medium, or full")
	flag.Parse()

	profile, ok := gen.ProfileForVersion(*runSize, protocol.BenchVersionV10)
	if !ok {
		fmt.Fprintf(os.Stderr, "unsupported run size %q\n", *runSize)
		os.Exit(1)
	}
	artifact, err := gen.GenerateDataset(*seed, profile, protocol.BenchVersionV10)
	if err != nil {
		fmt.Fprintln(os.Stderr, "memoryprobe:", err)
		os.Exit(1)
	}
	result, err := gen.AuditV10MemoryExposure(artifact)
	if err != nil {
		fmt.Fprintln(os.Stderr, "memoryprobe:", err)
		os.Exit(1)
	}
	fmt.Printf("bench v10 %s seed %d: transformed %d/%d = %.4f; verbatim %d/%d = %.4f\n",
		*runSize, *seed, result.Transformed, result.Eligible, result.TransformedShare(), result.Verbatim, result.Eligible, result.VerbatimShare())
}
