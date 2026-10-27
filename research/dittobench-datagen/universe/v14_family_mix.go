package universe

// Bench v14 family-mix guardrails (SN-351).
//
// Generator-competition rounds measure every case shape a generator emits
// against the current top agent panel. Two kinds of shape add the wrong mass to
// a scored contract. Shapes every top agent solves in every round (the public
// default's update chain, two-hop, single retraction and scattered list; a
// needle or a single-hop as-of lookup) are easy, cue-solvable cases. Shapes no
// top agent solves (conditional and alias-reroute at their current
// difficulty) may simply be broken.
//
// V14FamilyMix is the admission ledger for the program families bench v14
// generates. Every business family and personal domain the v14 generator can
// emit has one entry stating where the mechanic came from and whether it has
// cleared the gate in internal/familygate. Origins:
//
//   - v13-carried: inherited from v13 unchanged. Not re-measured, but an
//     entry whose shape is excluded below must name the issue replacing it.
//   - v14-structural: written for v14 from the sibling issues under SN-361.
//     It stays pending until the v14 qualification report shows it admitted.
//   - generator-derived: a mechanic ported from a generator submission. It
//     may only be generated once admitted with at least three rounds of
//     evidence, and it may never use an excluded shape.
//
// V14ExcludedShapes lists the shapes SN-351 says not to port, with the round
// evidence. A future port that wants one of them back must first remove it
// here with new panel evidence, in the same change that adds the family.

// V14FamilyMixEntry records the admission state of one family or domain.
type V14FamilyMixEntry struct {
	// Family is the V13Family or V13PersonalDomain key the generator emits.
	Family string
	// Shape names the mechanic a case of this family exercises.
	Shape string
	// Origin is v13-carried, v14-structural or generator-derived.
	Origin string
	// Status is carried, pending-panel or admitted.
	Status string
	// Tracking names the issue replacing an excluded carried shape.
	Tracking string
	// Evidence cites the rounds behind an admitted entry.
	Evidence string
}

// Family-mix origins.
const (
	V14OriginCarried    = "v13-carried"
	V14OriginStructural = "v14-structural"
	V14OriginGenerator  = "generator-derived"
)

// Family-mix statuses.
const (
	V14StatusCarried      = "carried"
	V14StatusPendingPanel = "pending-panel"
	V14StatusAdmitted     = "admitted"
)

// V14ExcludedShape is a shape the generator evidence says not to port.
type V14ExcludedShape struct {
	Shape    string
	Reason   string // "trivial" or "unsolved"
	Evidence string
}

// V14ExcludedShapes are the shapes SN-351 rules out of v14 datagen, from the
// Backroom generator rounds 25944-25963 (panel: five top agents from five
// distinct owners; solved = score >= 0.5). Generator-competition miners are
// credited by letter only (docs/v14-generator-credits.md).
var V14ExcludedShapes = []V14ExcludedShape{
	{Shape: "simple-update-chain", Reason: "trivial", Evidence: "public default starter kit update-chain: 0 discriminating in rounds 25946, 25955, 25963"},
	{Shape: "two-hop", Reason: "trivial", Evidence: "public default starter kit two-hop: round 25955 trivial 32/32, round 25963 trivial 29/32"},
	{Shape: "single-retraction", Reason: "trivial", Evidence: "public default starter kit retracted-pref: 0 discriminating in rounds 25946, 25955, 25963"},
	{Shape: "scattered-list", Reason: "trivial", Evidence: "public default starter kit scattered-list: round 25946 trivial 15/16"},
	{Shape: "single-hop-as-of", Reason: "trivial", Evidence: "generator G: round 25955 trivial 29/32; round 25963 trivial 30/32"},
	{Shape: "needle", Reason: "trivial", Evidence: "generator G: round 25955 trivial 29/32"},
	{Shape: "conditional", Reason: "unsolved", Evidence: "generator C: rounds 25955 and 25963, 6 of 6 cases solved by 0/5 panel agents"},
	{Shape: "alias-reroute", Reason: "unsolved", Evidence: "generator C: rounds 25955 and 25963, 5 cases solved by 0/5 or 1/5 panel agents"},
	{Shape: "owner-alias-email", Reason: "unsolved", Evidence: "generator D: round 25955, 3 of 6 cases solved by 0/5 panel agents"},
}

// V14FamilyMix is the admission ledger. Keep one entry per business family
// and personal domain the v14 generator emits.
var V14FamilyMix = []V14FamilyMixEntry{
	{Family: string(V13FamilyOwnerAfterCorrection), Shape: "supersession-chain", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-353/354: revert, declined-proposal and logged-in-error chains, seeded cue placement; unnamed revert ported from generator D"},
	{Family: string(V13FamilyStandingStatus), Shape: "supersession-chain", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-353/354: revert, declined-proposal and logged-in-error chains, seeded cue placement; unnamed revert ported from generator D"},
	{Family: string(V13FamilyLatestEvent), Shape: "relative-dated-sequence", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-358: relative event sequence from generator C's long-order and relative-trips shapes, rounds 25955, 25960 and 25963; re-implemented, so its own panel is pending"},
	{Family: string(V13FamilyClientVsVendor), Shape: "role-mapping", Origin: V14OriginCarried, Status: V14StatusCarried},
	{Family: string(V13FamilyNextActionResponsible), Shape: "multi-step-process-fold", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-356: multi-step process whose step states change across records, from generator D's next-open-step shape; re-implemented, so its own panel is pending"},
	{Family: string(V13FamilyCurrentChannel), Shape: "dated-interval-as-of", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-355: channel as of a date inside a non-final interval, from generator C's dated-status and range-lookup shapes, rounds 25946 and 25963 (not the excluded single-hop as-of lookup); re-implemented, so its own panel is pending"},
	{Family: string(V13FamilyRecordsDisagree), Shape: "conflict-report", Origin: V14OriginCarried, Status: V14StatusCarried},
	{Family: string(V13PersonalHousehold), Shape: "supersession-chain", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-353/354: revert, declined-proposal and logged-in-error chains, seeded cue placement; unnamed revert ported from generator D"},
	{Family: string(V13PersonalAppointments), Shape: "supersession-chain", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-353/354: revert, declined-proposal and logged-in-error chains, seeded cue placement; unnamed revert ported from generator D"},
	{Family: string(V13PersonalSchool), Shape: "membership-fold", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-357: membership fold over several add/remove events, from generator D's removed-member and generator C's set-intersection shapes (not the excluded scattered list); re-implemented, so its own panel is pending"},
	{Family: string(V13PersonalMilestones), Shape: "supersession-chain", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-353/354: revert, declined-proposal and logged-in-error chains, seeded cue placement; unnamed revert ported from generator D"},
	{Family: string(V13PersonalSubscriptions), Shape: "supersession-chain", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-353/354: revert, declined-proposal and logged-in-error chains, seeded cue placement; unnamed revert ported from generator D"},
	{Family: string(V13PersonalTravel), Shape: "supersession-chain", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-353/354: revert, declined-proposal and logged-in-error chains, seeded cue placement; unnamed revert ported from generator D"},
	{Family: string(V13PersonalHobbies), Shape: "supersession-chain", Origin: V14OriginStructural, Status: V14StatusPendingPanel, Evidence: "SN-353/354: revert, declined-proposal and logged-in-error chains, seeded cue placement; unnamed revert ported from generator D"},
}

// V14FamilyMixFor returns the ledger entry for a family or domain key.
func V14FamilyMixFor(family string) (V14FamilyMixEntry, bool) {
	for _, e := range V14FamilyMix {
		if e.Family == family {
			return e, true
		}
	}
	return V14FamilyMixEntry{}, false
}

// V14ExcludedShapeFor returns the exclusion record for a shape.
func V14ExcludedShapeFor(shape string) (V14ExcludedShape, bool) {
	for _, s := range V14ExcludedShapes {
		if s.Shape == shape {
			return s, true
		}
	}
	return V14ExcludedShape{}, false
}

// V14FamiliesAwaitingPanel lists the entries the v14 qualification report must
// measure with internal/familygate before activation: everything new to v14
// that has not yet been admitted.
func V14FamiliesAwaitingPanel() []string {
	var out []string
	for _, e := range V14FamilyMix {
		if e.Origin != V14OriginCarried && e.Status != V14StatusAdmitted {
			out = append(out, e.Family)
		}
	}
	return out
}
