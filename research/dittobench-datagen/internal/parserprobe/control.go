package parserprobe

import (
	"fmt"
	"math"
)

// PublicControlFailures checks the necessary public baseline for a private
// surface comparison. Passing is not private qualification: honest-agent,
// N14, runtime tool/control and private-artifact evidence remain required.
func PublicControlFailures(r Report) []string {
	var failures []string
	if r.BenchVersion != 13 || r.RunSize != "full" || r.Source != "generated" {
		failures = append(failures, "control requires generated public V13 full-profile artifacts")
	}
	seeds := make(map[int64]bool)
	for _, seed := range r.Seeds {
		if seed.BenchVersion != 13 || seeds[seed.Seed] {
			failures = append(failures, "control seeds must be unique V13 artifacts")
			break
		}
		seeds[seed.Seed] = true
	}
	if len(seeds) < 40 {
		failures = append(failures, "control requires at least 40 unique seeds")
	}
	if len(r.Unclassified) != 0 {
		failures = append(failures, "control contains unclassified families")
	}
	if r.GIH == nil {
		return append(failures, "control is missing GIH results")
	}
	for _, slice := range []string{"story", "programs", "personal", "quantity", "tool-prompts"} {
		stats, ok := r.GIH.Slices[slice]
		if !ok || stats.Cases <= 0 || math.IsNaN(stats.Mean) || math.IsInf(stats.Mean, 0) || stats.Mean < 0.90 || stats.Mean > 1 {
			failures = append(failures, fmt.Sprintf("control slice %s requires mean >= 0.90 with nonzero coverage", slice))
		}
	}
	return failures
}
