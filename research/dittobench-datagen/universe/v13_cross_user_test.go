package universe

import (
	"reflect"
	"testing"
)

func TestRecoverV13CrossUserAnchorsPreservesPeople(t *testing.T) {
	w := World{People: []Person{
		{Name: "A One", Email: "one"}, {Name: "B Two"}, {Name: "C Three"},
		{Name: "D Four"}, {Name: "E Five"}, {Name: "A Six"},
		{Name: "B Seven"}, {Name: "C Eight"}, {Name: "D Nine"},
		{Name: "F Ten", Email: "ten"}, {Name: "G Eleven"},
	}}
	before := append([]Person(nil), w.People...)
	recoverV13CrossUserAnchors(&w, 3)
	if w.People[0] != before[9] || w.People[2] != before[10] {
		t.Fatal("did not recover the two missing even anchors")
	}
	for _, p := range before {
		found := false
		for _, q := range w.People {
			if reflect.DeepEqual(p, q) {
				found = true
				break
			}
		}
		if !found {
			t.Fatal("changed or lost a complete identity")
		}
	}
	after := append([]Person(nil), w.People...)
	recoverV13CrossUserAnchors(&w, 3)
	if !reflect.DeepEqual(after, w.People) {
		t.Fatal("valid world changed")
	}
}

func TestRecoverV13CrossUserAnchorsDoesNotInventFallback(t *testing.T) {
	w := World{People: []Person{{Name: "A One"}, {Name: "A Two"}}}
	before := append([]Person(nil), w.People...)
	recoverV13CrossUserAnchors(&w, 3)
	if !reflect.DeepEqual(before, w.People) {
		t.Fatal("changed identities without eligible anchors")
	}
}
