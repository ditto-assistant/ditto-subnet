package universe

import "strings"

// recoverV13CrossUserAnchors keeps valid worlds byte-identical. A rare draw
// leaves too few unique given names in the isolation prefix for the absence
// quota. Move complete existing people into that prefix before any dependent
// projects, trips, stories or records are built. No identity is renamed, no RNG
// draw is consumed, and repeated names elsewhere remain deliberately ambiguous.
// If the entire population lacks enough unique names, the ordinary abstention
// validator still fails closed rather than lowering its quota.
func recoverV13CrossUserAnchors(w *World, scale int) {
	iso := 0
	if scale >= 3 {
		iso = 9
	} else if scale == 2 {
		iso = 5
	}
	iso = min(iso, len(w.People))
	want := V13AbsenceFamilyCounts(scale)[V13FamilyCrossUser]
	counts := map[string]int{}
	given := func(p Person) string { return strings.Fields(p.Name)[0] }
	for _, p := range w.People {
		counts[given(p)]++
	}
	eligible := 0
	for _, p := range w.People[:iso] {
		if counts[given(p)] == 1 {
			eligible++
		}
	}
	if eligible >= want {
		return
	}
	// Match the absence family's preference for even anchors, then odd ones.
	for parity := 0; parity < 2; parity++ {
		for i := parity; i < iso && eligible < want; i += 2 {
			if counts[given(w.People[i])] == 1 {
				continue
			}
			for j := iso; j < len(w.People); j++ {
				if counts[given(w.People[j])] != 1 {
					continue
				}
				w.People[i], w.People[j] = w.People[j], w.People[i]
				eligible++
				break
			}
		}
	}
}
