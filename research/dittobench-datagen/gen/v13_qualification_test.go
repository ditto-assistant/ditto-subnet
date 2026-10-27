package gen

import "testing"

// TestV13QualificationSeeds runs every per-seed v13 invariant over the pinned
// 40 full-profile qualification seeds in one shared pass. The seeds are
// generated once per test binary (concurrently, see cachedDatasets) and each
// invariant is a read-only check, so adding one costs no generation time.
func TestV13QualificationSeeds(t *testing.T) {
	t.Parallel()
	seeds := v13FortySeeds(t)
	for _, check := range []struct {
		name string
		run  func(*testing.T, []DatasetArtifact)
	}{
		{"EnvelopeSlots", checkV13EnvelopeSlots},
		{"ComposedMemoryFloor", checkV13ComposedMemoryFloor},
		{"MixAuditStructural", checkV13MixAuditStructural},
		{"MixAuditGate", checkV13MixAuditGate},
		{"AbstentionBudgetAndFamilyMix", checkV13AbstentionBudgetAndFamilyMix},
		{"RelationTwinsAreSpacedAndNeverAdjacent", checkV13RelationTwinsAreSpacedAndNeverAdjacent},
		{"RestraintContextsSurviveSharedGraphAndRandomization", checkV13RestraintContextsSurviveSharedGraphAndRandomization},
		{"FreeFormArgumentsDoNotRequireMagicStrings", checkV13FreeFormArgumentsDoNotRequireMagicStrings},
		{"SeedSubjectLinksDoNotEnumerateTheStoryJoin", checkV13SeedSubjectLinksDoNotEnumerateTheStoryJoin},
	} {
		t.Run(check.name, func(t *testing.T) { check.run(t, seeds) })
	}
}
