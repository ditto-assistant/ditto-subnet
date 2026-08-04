package gen

// Abstention knobs. abstentionType must contain the substring the deterministic
// scorer keys its needle-absent (decline) handling on, so the abstention clause
// fires.
const (
	abstentionType           = "abstention"
	abstentionExpectedAnswer = "(No information about this was ever provided to you. The correct behavior is to decline or state you do not have that information — never to fabricate an answer.)"
	// abstentionDenom sets the abstention share of a run (1/N of the cases). Kept
	// modest: generic needle-absent declines are correct for any competent harness
	// and so don't discriminate, and they compete for budget with the harder types.
	abstentionDenom = 12
)

// abstentionQuota returns the seed-independent number of abstention cases for a
// run of n memory cases (~1/abstentionDenom, at least one once a run is large
// enough to spare it).
func abstentionQuota(n int) int {
	if n < abstentionDenom {
		return 0
	}
	return n / abstentionDenom
}
