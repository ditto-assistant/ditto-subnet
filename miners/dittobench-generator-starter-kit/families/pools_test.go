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

// TestCustodyItemsFillMaxCases holds the hardened custody pool to MaxCases
// (one item per round) and keeps every item apart from the other pools.
func TestCustodyItemsFillMaxCases(t *testing.T) {
	if len(custodyItems) < maxRounds {
		t.Fatalf("custodyItems has %d values; %d rounds need %d", len(custodyItems), maxRounds, maxRounds)
	}
	seen := map[string]bool{}
	for _, item := range custodyItems {
		if seen[item[0]] || seen["noun:"+item[1]] {
			t.Errorf("custody item %v repeats", item)
		}
		seen[item[0]], seen["noun:"+item[1]] = true, true
		if !strings.Contains(item[0], item[1]) && item[1] != "radios" {
			t.Errorf("bare noun %q is not part of %q", item[1], item[0])
		}
		for _, pool := range [][]string{cities, firstNames, roles, events, drinksPool, allergens} {
			for _, v := range pool {
				word := regexp.MustCompile(`(?i)(^|[^a-z])` + regexp.QuoteMeta(v) + `($|[^a-z])`)
				if word.MatchString(item[0]) || word.MatchString(item[1]) {
					t.Errorf("custody item %v contains pool value %q", item, v)
				}
			}
		}
	}
}
