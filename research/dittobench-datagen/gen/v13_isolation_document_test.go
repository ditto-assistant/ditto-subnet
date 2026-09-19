package gen

import (
	"context"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestIsolationFactProjection(t *testing.T) {
	for seed := int64(1); seed <= 12; seed++ {
		_, projected, _, err := v8IsolationProjection(seed, 40, 4, 13)
		if err != nil {
			t.Fatal(err)
		}
		f := &quantityFixture{}
		suite, err := generateWorldIsolationWithFacts(context.Background(), seed, 40, 4, 13, f)
		if err != nil {
			t.Fatal(err)
		}
		if f.checks != 4 || len(suite.SecondaryWave.Pairs) != 12 || suite.SecondaryWave.UserID != SecondaryUser {
			t.Fatal("scope or coverage changed")
		}
		byID := map[string]protocol.MemoryPair{}
		for _, p := range suite.SecondaryWave.Pairs {
			byID[p.PairID] = p
		}
		for i, c := range suite.Cases {
			if i%2 == 0 {
				if c.UserID != SecondaryUser {
					t.Fatal("wrong user")
				}
				for _, id := range c.RequiredPairIDs {
					if _, ok := byID[id]; !ok {
						t.Fatal("missing secondary evidence")
					}
				}
			}
			r, err := isolationFactDocument(projected.People[i])
			if err != nil {
				t.Fatal(err)
			}
			if r.Bindings["{{email0}}"] != projected.People[i].Email || r.Bindings["{{name0}}"] != projected.People[i].Name {
				t.Fatal("unprojected identity")
			}
			if len(r.Records) != 3 {
				t.Fatal("join collapsed")
			}
		}
	}
}

func TestIsolationFactAtomicFailure(t *testing.T) {
	suite, err := generateWorldIsolationWithFacts(context.Background(), 1, 40, 4, 13, &quantityFixture{failAt: 2})
	if err == nil || len(suite.SecondaryWave.Pairs) != 0 || len(suite.Cases) != 0 {
		t.Fatal("partial isolation escaped")
	}
}
