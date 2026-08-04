package gen

import (
	"testing"
)

func TestAbstentionQuota(t *testing.T) {
	// ~1/12 of a run (see abstentionDenom): 0 below the denom, then n/12.
	cases := map[int]int{0: 0, 3: 0, 11: 0, 12: 1, 20: 1, 50: 4}
	for n, want := range cases {
		if got := abstentionQuota(n); got != want {
			t.Errorf("abstentionQuota(%d)=%d want %d", n, got, want)
		}
	}
}

// TestSuiteAbstentionQuotaMet checks the v2 suite carries the abstention share:
// at n=30 the quota is 2, and every abstention case carries the decline
// sentinel as its expected answer.
func TestSuiteAbstentionQuotaMet(t *testing.T) {
	const n = 30
	suite := GenerateMemorySuite(NewRNG(42), 42, n, 1, 0)
	abs := 0
	for _, sc := range suite.Cases {
		c := sc.Case
		if c.QuestionType == abstentionType {
			abs++
			if c.ExpectedAnswer != abstentionExpectedAnswer {
				t.Fatalf("abstention case %s should carry the decline sentinel, got %q", c.ID, c.ExpectedAnswer)
			}
		}
	}
	if want := abstentionQuota(n); abs != want {
		t.Fatalf("abstention quota: got %d want %d", abs, want)
	}
}
