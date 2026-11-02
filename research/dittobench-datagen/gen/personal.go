package gen

import (
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Bench v13 personal-life programs (household, appointments and medication,
// school logistics, family milestones, subscriptions and renewals, travel
// logistics, hobbies and clubs). The event schemas and renderers live in
// universe/personal.go; this file is the suite-side entry point the v13
// envelope calls, alongside V13BusinessProgramCases. Zero monetary groups.

// v13PersonalProgramCaseCount is the bounded share of the v13 memory mix spent
// on personal-life programs: six metamorphic groups of four on the full
// profile, a multiple of four on every profile.
func v13PersonalProgramCaseCount(n int) int {
	switch {
	case n >= 100:
		return 24
	case n >= 40:
		return 12
	default:
		return 4
	}
}

// GenerateV13PersonalPrograms generates the v13 personal-life programs for a
// seed and stages them for the memory suite (StagedCase per member with its
// provenance; every evidence record in the returned wave-0 pairs).
func GenerateV13PersonalPrograms(seed int64, count int) ([]StagedCase, []protocol.MemoryPair, error) {
	generated, err := universe.GenerateV13PersonalPrograms(seed, count)
	if err != nil {
		return nil, nil, fmt.Errorf("v13 personal programs: %w", err)
	}
	cases, pairs := stageV13Programs(generated)
	return cases, pairs, nil
}
