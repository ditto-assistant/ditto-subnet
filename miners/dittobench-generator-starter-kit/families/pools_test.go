package families

import (
	"regexp"
	"strings"
	"testing"
)

// maxRounds is the number of rounds of the four default families the starter
// kit runs at its MaxCases (80): twenty.
const maxRounds = 20

// TestPoolsFillMaxCases holds the pool sizes to what MaxCases draws.
// Builder.Pick panics on an exhausted pool, so an undersized pool would fail
// every request near the top of the range.
func TestPoolsFillMaxCases(t *testing.T) {
	need := map[string]struct {
		pool    []string
		perPack int
	}{
		"cities":     {cities, 5},
		"firstNames": {firstNames, 6},
		"roles":      {roles, 1},
		"events":     {events, 1},
		"drinksPool": {drinksPool, 3},
		"allergens":  {allergens, 4},
	}
	for name, n := range need {
		if got, want := len(n.pool), n.perPack*maxRounds; got < want {
			t.Errorf("%s has %d values; %d rounds need %d", name, got, maxRounds, want)
		}
	}
}

// TestPoolValuesStayApart refuses a value that appears, as whole words, inside
// another value of any pool. Such a pair lets one case's gold answer ground or
// decoy another case's question.
func TestPoolValuesStayApart(t *testing.T) {
	pools := [][]string{cities, firstNames, roles, events, drinksPool, allergens}
	var all []string
	seen := map[string]bool{}
	for _, pool := range pools {
		for _, v := range pool {
			key := strings.ToLower(v)
			if seen[key] {
				t.Errorf("value %q appears twice", v)
			}
			seen[key] = true
			all = append(all, v)
		}
	}
	for _, a := range all {
		word := regexp.MustCompile(`(?i)(^|[^a-z])` + regexp.QuoteMeta(a) + `($|[^a-z])`)
		for _, b := range all {
			if a != b && word.MatchString(b) {
				t.Errorf("%q contains %q", b, a)
			}
		}
	}
}
