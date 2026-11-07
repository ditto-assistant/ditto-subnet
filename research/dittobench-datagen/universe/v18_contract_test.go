package universe

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV18FamilyDraws(t *testing.T) {
	seen := map[V13Family]bool{}
	for seed := int64(1); seed <= 64; seed++ {
		if f := V18FamilyFor(seed, 0); f != v18PinnedBusinessFamily {
			t.Fatalf("seed %d group 0 = %s", seed, f)
		}
		groups := map[V13Family]bool{}
		for g := 0; g < 3; g++ {
			f := V18FamilyFor(seed, g)
			if f == V13FamilyClientVsVendor {
				t.Fatalf("seed %d draws client-vs-vendor", seed)
			}
			if f != v13FamilyForVersion(seed, g, protocol.BenchVersionV18) {
				t.Fatal("family selector disagrees with V18FamilyFor")
			}
			groups[f], seen[f] = true, true
		}
		if len(groups) != 3 {
			t.Fatalf("seed %d: %d distinct families", seed, len(groups))
		}
		if d := v18PersonalDomain(seed, 0); d != v18PinnedPersonalDomain {
			t.Fatalf("seed %d personal group 0 = %s", seed, d)
		}
		if d := v18PersonalDomain(seed, 1); d == v18PinnedPersonalDomain {
			t.Fatalf("seed %d asks the pinned personal domain twice", seed)
		}
	}
	if len(seen) != len(v18BusinessFamilies) {
		t.Fatalf("64 seeds reached %d of %d v18 business families", len(seen), len(v18BusinessFamilies))
	}
	// Below v18 the selectors are the v14 ones.
	for seed := int64(1); seed <= 8; seed++ {
		if v13FamilyForVersion(seed, 0, protocol.BenchVersionV17) != V14FamilyFor(seed, 0) {
			t.Fatal("v17 family selection moved")
		}
	}
}
