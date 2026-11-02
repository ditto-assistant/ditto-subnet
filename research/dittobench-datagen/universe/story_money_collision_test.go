package universe

import "testing"

func TestStoryMoneyAvoidsShortInvoiceRecords(t *testing.T) {
	b := storyV2Builder{w: World{Projects: []Project{{OriginalCents: 1080364, CorrectedCents: 1080365, PaidCents: 1080366}}}}
	if got := b.privateMoneyCents(1080364, 800000, 4000000); got != 1080367 {
		t.Fatalf("invoice collision was not removed: %d", got)
	}
	if got := b.privateMoneyCents(1080368, 800000, 4000000); got != 1080368 {
		t.Fatal("noncolliding amount changed")
	}
	b.w.Projects = []Project{{OriginalCents: 4799999}}
	if got := b.privateMoneyCents(4799999, 800000, 4000000); got != 800000 {
		t.Fatal("collision walk did not wrap inside its range")
	}
}
