package universe

import "github.com/ditto-assistant/dittobench-datagen/protocol"

// Bench v18 business-family rotation and story-oracle trim.
//
// The v16/v17 dueling table spends four business slots on v13/v14 family
// groups, so every seed renders exactly one group, group 0, and group 0 is
// always cycle position 0: owner-after-correction. None of the v14 shapes the
// generator competition showed to separate agents (relative event sequences,
// dated status inside a non-final interval, step-state processes,
// holder-vs-actor custody, identity joins, records that disagree) reached a
// scored v16 or v17 run on the business side. Across the 27 v17 production
// rows the one family that did render averaged 0.973 (1.000 for the top three
// agents).
//
// V18 draws, per seed, an ordered subset of v18BusinessFamilies for its
// business groups, so a three-group run asks three distinct families and every
// family appears in about three of eight seeds. Client-vs-vendor, whose
// records name the role label outright, is left out. The family generators,
// their counterfactuals and their distractor variants are the v14 ones
// unchanged; chains stay at two hops or fewer (V14ExcludedShapes).

// v18BusinessFamilies are the families the v18 business groups draw from.
var v18BusinessFamilies = []V13Family{
	V13FamilyOwnerAfterCorrection,
	V13FamilyStandingStatus,
	V13FamilyLatestEvent,
	V13FamilyNextActionResponsible,
	V13FamilyCurrentChannel,
	V13FamilyRecordsDisagree,
	V14FamilyHolderVsActor,
	V14FamilyIdentityJoin,
}

// V18BusinessFamilies returns the v18 business family bank (copy).
func V18BusinessFamilies() []V13Family {
	return append([]V13Family(nil), v18BusinessFamilies...)
}

// V18FamilyFor returns the family of business group group at v18: the seed's
// permutation of v18BusinessFamilies, cycled.
func V18FamilyFor(seed int64, group int) V13Family {
	perm := v13Perm(seed, "v18-business-families", len(v18BusinessFamilies))
	return v18BusinessFamilies[perm[group%len(perm)]]
}

// v13FamilyForVersion is the one family selector every generator and audit
// reads, so provenance can never disagree with what was rendered.
func v13FamilyForVersion(seed int64, group, benchVersion int) V13Family {
	switch {
	case benchVersion >= protocol.BenchVersionV18:
		return V18FamilyFor(seed, group)
	case benchVersion >= protocol.BenchVersionV14:
		return V14FamilyFor(seed, group)
	default:
		return V13Families[group%len(V13Families)]
	}
}

// v18SaturatedStoryOracles are dropped, in order and only until an arc fits
// its five-oracle slot, before the pure-money rotation. The order is the v17
// production mean over the 27 rows, highest first: lesson-claims 1.000 (46
// cases), status-current 0.985 (135), order 0.976 (168), x-owner-email 0.976
// (168). Owner-current 0.970, quantity 0.967, next-action 0.962 and
// status-disagree 0.879 are kept.
var v18SaturatedStoryOracles = []string{oracleStoryLessonClaims, oracleStoryStatusCurrent, oracleStoryOrder, oracleStoryOwnerEmail}

// V18SaturatedStoryOracles returns the oracle kinds a v18 arc drops first
// (copy).
func V18SaturatedStoryOracles() []string {
	return append([]string(nil), v18SaturatedStoryOracles...)
}
