package universe

import (
	"strings"
	"testing"
)

// A hidden arithmetic value is not evidence. Each swap's actual operation
// must be rendered alongside its operands, not merely stored in Slots.
func TestV13StorySwapQuantityOperationReachesRecords(t *testing.T) {
	checked := 0
	for seed := int64(1); seed <= 20; seed++ {
		w := v13World(seed)
		for _, arc := range w.StoryArcs {
			v := arc.V2
			if v.Quantity == nil || v.Quantity.Kind == "money" || v.Quantity.Op == "replace" {
				continue
			}
			// Literal words receive spelling edits on the final wire. Inspect the
			// complete pre-projection record, not just hidden event-slot presence.
			for _, e := range v.Events {
				if (e.Kind == EventVendorSwapped || e.Kind == EventProviderSwapped) && e.Slots["qtyphrase"] != "" {
					found := false
					for _, story := range w.Stories {
						if story.PairID != v.PairIDs[e.Memory] {
							continue
						}
						for _, text := range story.Middle.Events {
							found = found || strings.Contains(strings.ToLower(text), strings.ToLower(e.Slots["qtyphrase"])+".")
						}
					}
					if !found {
						t.Fatalf("seed %d arc %s: complete arithmetic operation absent from rendered record", seed, v.SubjectAlias)
					}
					checked++
				}
			}
		}
	}
	if checked < 20 {
		t.Fatalf("only exercised %d quantity swaps", checked)
	}
}
