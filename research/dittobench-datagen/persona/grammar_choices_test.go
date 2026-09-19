package persona

import (
	"math/rand"
	"testing"
)

func TestGrammarChoicesPreserveOutputAndRNG(t *testing.T) {
	g := Grammar{"root": {"#x# then #x# via #branch#"}, "x": {"first", "second", "third"}, "branch": {"#x#", "fixed"}}
	for seed := int64(0); seed < 100; seed++ {
		a, b := rand.New(rand.NewSource(seed)), rand.New(rand.NewSource(seed))
		want := Expand(a, g, "root")
		got, choices := ExpandWithChoices(b, g, "root")
		if got != want || a.Int63() != b.Int63() {
			t.Fatal("trace changed public bytes or random stream")
		}
		if len(choices["root"]) != 1 || len(choices["x"]) < 2 {
			t.Fatal("source choices not retained")
		}
	}
}
