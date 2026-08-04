package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/internal/toolprobe"
)

func main() {
	version := flag.Int("bench-version", 8, "benchmark contract version")
	runSize := flag.String("run-size", "full", "small, medium, or full")
	train := flag.Int("train-seeds", 30, "number of training seeds")
	heldOut := flag.Int("held-out-seeds", 10, "number of held-out seeds")
	start := flag.Int64("seed-start", 1, "first training seed")
	flag.Parse()

	result, err := toolprobe.Run(*version, *runSize, *start, *train, *heldOut)
	if err != nil {
		fmt.Fprintln(os.Stderr, "toolprobe:", err)
		os.Exit(1)
	}
	fmt.Printf("bench v%d %s: %d/%d = %.4f complete tool-outcome accuracy\n", *version, *runSize, result.Correct, result.Total, result.Accuracy())
	for _, family := range toolprobe.SortedFamilies(result) {
		f := result.Families[family]
		fmt.Printf("%-40s %4d/%-4d %.4f\n", family, f.Correct, f.Total, f.Accuracy())
	}
}
