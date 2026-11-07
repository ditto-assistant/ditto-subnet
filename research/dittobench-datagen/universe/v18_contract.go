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
// V18 pins holder-vs-actor to business group 0 (the one v14 business family
// the top owners still missed on v15) and draws the other groups per seed
// from the rest of v18BusinessFamilies, so a three-group run asks three
// distinct families. Client-vs-vendor, whose
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

// v18PinnedBusinessFamily fills business group 0 of every v18 seed. Measured
// on the latest v15 rows of the two top v17 owners that ran v15 (dan0l,
// DrPresident; 18 runs), every other v14 business family scored 1.000;
// holder-vs-actor scored 0.906 (0.81 for one owner) and 0.927 on their 63
// v14 rows. Records-disagree has no production evidence: no v14/v15 run
// rendered it (only cycle positions 0-3 were ever asked).
const v18PinnedBusinessFamily = V14FamilyHolderVsActor

// V18FamilyFor returns the family of business group group at v18: group 0 is
// v18PinnedBusinessFamily, later groups cycle through the seed's permutation
// of the other v18BusinessFamilies.
func V18FamilyFor(seed int64, group int) V13Family {
	if group == 0 {
		return v18PinnedBusinessFamily
	}
	rest := make([]V13Family, 0, len(v18BusinessFamilies)-1)
	for _, f := range v18BusinessFamilies {
		if f != v18PinnedBusinessFamily {
			rest = append(rest, f)
		}
	}
	perm := v13Perm(seed, "v18-business-families", len(rest))
	return rest[perm[(group-1)%len(perm)]]
}

// v18PinnedPersonalDomain fills personal group 0 of every v18 seed: the
// weakest personal domain for the top owners on v15 (0.891, one owner 0.81)
// and on v17 (0.850). Later groups keep the v13 per-seed domain order,
// skipping it.
const v18PinnedPersonalDomain = V13PersonalSubscriptions

// v18PersonalDomain returns the domain of personal group group at v18.
func v18PersonalDomain(seed int64, group int) V13PersonalDomain {
	if group == 0 {
		return v18PinnedPersonalDomain
	}
	perm := v13Perm(seed, "personal-domains", len(V13PersonalDomains))
	rest := make([]V13PersonalDomain, 0, len(perm))
	for _, i := range perm {
		if V13PersonalDomains[i] != v18PinnedPersonalDomain {
			rest = append(rest, V13PersonalDomains[i])
		}
	}
	return rest[(group-1)%len(rest)]
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
