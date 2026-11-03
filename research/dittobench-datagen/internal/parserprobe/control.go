package parserprobe

import (
	"fmt"
	"math"
)

// PublicControlFailures checks the necessary public baseline for a private
// surface comparison. Passing is not private qualification: honest-agent,
// N14, runtime tool/control and private-artifact evidence remain required.
// This legacy entry point remains bound to v13.
func PublicControlFailures(r Report) []string {
	return PublicControlFailuresForVersion(r, 13)
}

// PublicControlFailuresForVersion checks one explicitly selected public
// benchmark control. A v13 control cannot qualify a v14 comparison, even when
// their public surfaces share implementation. Future contracts need review
// before inheriting these coverage requirements.
func PublicControlFailuresForVersion(r Report, benchVersion int) []string {
	if benchVersion != 13 && benchVersion != 14 {
		return []string{fmt.Sprintf("public control requirements are not defined for bench v%d", benchVersion)}
	}
	var failures []string
	if r.BenchVersion != benchVersion || r.RunSize != "full" || r.Source != "generated" {
		failures = append(failures, fmt.Sprintf("control requires generated public V%d full-profile artifacts", benchVersion))
	}
	seeds := make(map[int64]bool)
	for _, seed := range r.Seeds {
		if seed.BenchVersion != benchVersion || seeds[seed.Seed] {
			failures = append(failures, fmt.Sprintf("control seeds must be unique V%d artifacts", benchVersion))
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
