package universe

import (
	"reflect"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// StoryArcLimit (bench v16) asks a seeded subset of arcs that keeps the
// world's business/personal proportion; zero asks every arc exactly as before.
func TestStoryArcLimitKeepsKindProportion(t *testing.T) {
	for _, seed := range []int64{1, 7, 123456789} {
		w := GenerateForVersion(seed, 3, protocol.BenchVersionV15)
		all := make([]int, len(w.StoryArcs))
		business := 0
		for i := range all {
			all[i] = i
			if w.StoryArcs[i].V2.Kind == StoryBusiness {
				business++
			}
		}
		got := w.limitedStoryArcs("v13", all, 6)
		if !reflect.DeepEqual(got, w.limitedStoryArcs("v13", all, 6)) || len(got) != 6 {
			t.Fatalf("seed %d: arcs %v", seed, got)
		}
		gotBusiness := 0
		for i, arc := range got {
			if i > 0 && arc <= got[i-1] {
				t.Fatalf("seed %d: arcs not sorted and distinct: %v", seed, got)
			}
			if w.StoryArcs[arc].V2.Kind == StoryBusiness {
				gotBusiness++
			}
		}
		lo, hi := 6*business/len(all), (6*business+len(all)-1)/len(all)
		if gotBusiness < lo || gotBusiness > hi {
			t.Fatalf("seed %d: %d business arcs of 6, world %d of %d", seed, gotBusiness, business, len(all))
		}
		base := WorldPlanSelection{StoryPerArc: 6, Salt: "v13"}
		a, _, errA := w.SelectQuestionPlans(base)
		limited := base
		limited.StoryArcLimit = len(w.StoryArcs)
		b, _, errB := w.SelectQuestionPlans(limited)
		if errA != nil || errB != nil || !reflect.DeepEqual(a, b) {
			t.Fatalf("seed %d: a limit of every arc must equal no limit (%v, %v)", seed, errA, errB)
		}
	}
}
