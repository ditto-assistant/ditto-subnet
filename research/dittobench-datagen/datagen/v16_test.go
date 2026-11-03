package datagen

import (
	"reflect"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// The published v16 connector quota installs in full on every swept full and
// medium seed (the pass trims only below the convertible-slot floor, which no
// swept seed reaches), and never touches a v15 run.
func TestV16ConnectorQuotaHoldsAcrossSeeds(t *testing.T) {
	for _, n := range []int{50, 24} {
		want := V16ConnectorQuotaFor(n)
		for seed := int64(1); seed <= 120; seed++ {
			ds, err := GenerateForVersion(seed, n, protocol.BenchVersionV16)
			if err != nil {
				t.Fatal(err)
			}
			got := V16ConnectorQuota{}
			for _, c := range ds.ToolCases {
				switch c.Category {
				case V16CategoryLinkedRead:
					got.LinkedRead++
				case V16CategoryLinkedAction:
					got.LinkedAction++
				case V16CategoryUnlinkedProbe:
					got.Unlinked++
				case V16CategoryNamedProduct:
					got.NamedProduct++
				}
			}
			if got != want {
				t.Fatalf("n=%d seed %d: connector quota %+v, want %+v", n, seed, got, want)
			}
		}
	}
	for seed := int64(1); seed <= 20; seed++ {
		ds, err := GenerateForVersion(seed, 6, protocol.BenchVersionV16)
		if err != nil {
			t.Fatal(err)
		}
		linked := 0
		for _, c := range ds.ToolCases {
			if IsV16ConnectorCase(c.Category) {
				if c.Category != V16CategoryLinkedRead {
					t.Fatalf("small seed %d: unexpected %s", seed, c.Category)
				}
				linked++
			}
		}
		if linked > 1 {
			t.Fatalf("small seed %d: %d connector cases", seed, linked)
		}
	}
	v15, err := GenerateForVersion(7, 100, protocol.BenchVersionV15)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range v15.ToolCases {
		if IsV16ConnectorCase(c.Category) {
			t.Fatalf("v15 run carries %s", c.Category)
		}
	}
}

func TestV16ConnectorQuotaFitRotatesFamilies(t *testing.T) {
	q := V16ConnectorQuotaFor(50)
	if q.Total() != 11 || V16ConnectorQuotaFor(24).Total() != 5 || V16ConnectorQuotaFor(6).Total() != 1 || V16ConnectorQuotaFor(5).Total() != 0 {
		t.Fatalf("published quotas moved: %+v", q)
	}
	if got := q.fit(11); got != q {
		t.Fatalf("fit at quota changed it: %+v", got)
	}
	if got := q.fit(10); got.NamedProduct != 2 || got.Total() != 10 {
		t.Fatalf("first trim must take a named-product case: %+v", got)
	}
	if got := q.fit(7); got != (V16ConnectorQuota{LinkedRead: 2, LinkedAction: 2, Unlinked: 1, NamedProduct: 2}) {
		t.Fatalf("fit(7) must keep every family: %+v", got)
	}
	if got := q.fit(1); got != (V16ConnectorQuota{LinkedRead: 1}) {
		t.Fatalf("the last case kept must be a linked read: %+v", got)
	}
	if got := q.fit(0); got.Total() != 0 {
		t.Fatalf("fit(0) = %+v", got)
	}
}

func TestV16ToolCasesAreDeterministic(t *testing.T) {
	a, err := GenerateForVersion(42, 50, protocol.BenchVersionV16)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := GenerateForVersion(42, 50, protocol.BenchVersionV16)
	if !reflect.DeepEqual(a, b) {
		t.Fatal("v16 tool generation is not deterministic")
	}
}

// From v16 the conversion order recognises world carriers in the run's own
// world: no case carrying a pair of the v16 world is ever offered for
// conversion.
func TestV16ConversionOrderProtectsCarriersOfTheRunWorld(t *testing.T) {
	for _, seed := range []int64{1, 7, 42} {
		ds, err := GenerateForVersion(seed, 50, protocol.BenchVersionV16)
		if err != nil {
			t.Fatal(err)
		}
		world := universe.GenerateForVersion(seed, ToolWorldScale(50, protocol.BenchVersionV16), protocol.BenchVersionV16)
		pairs := map[string]bool{}
		for _, pair := range world.Pairs {
			pairs[pair.PairID] = true
		}
		carriers := 0
		for _, c := range ds.ToolCases {
			for _, p := range c.PrerequisitePairs {
				if pairs[p.PairID] {
					carriers++
					break
				}
			}
		}
		if carriers == 0 {
			t.Fatalf("seed %d: no case carries the v16 world", seed)
		}
		for _, i := range v13ConversionOrderForVersion(seed, protocol.BenchVersionV16, ds.ToolCases) {
			for _, p := range ds.ToolCases[i].PrerequisitePairs {
				if pairs[p.PairID] {
					t.Fatalf("seed %d: carrier case %s offered for conversion", seed, ds.ToolCases[i].ID)
				}
			}
		}
	}
}
