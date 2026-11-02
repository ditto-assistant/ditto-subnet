package privatesurface

import (
	"strings"
	"testing"
)

func TestMaskRestoresExactOverlappingValuesAndTypos(t *testing.T) {
	source := "Richard Thompson is in the criwd, Richard."
	masked, markers, restore, err := maskProtected(source, []string{"Richard", "Richard Thompson", "criwd", "Richard"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(masked, "Richard") || strings.Contains(masked, "criwd") || len(markers) != 3 {
		t.Fatal("protected values not masked")
	}
	after, err := restore("For the record, " + masked)
	if err != nil || after != "For the record, "+source {
		t.Fatal("restoration changed values")
	}
	for _, candidate := range []string{strings.Replace(masked, markers[0], "fixed", 1), masked + markers[0], masked + " criwd"} {
		if _, err := restore(candidate); err == nil {
			t.Fatal("changed protected value accepted")
		}
	}
}

func TestMaskDoesNotSplitWordsAndRejectsAttachedMarker(t *testing.T) {
	masked, markers, restore, err := maskProtected("Richard recorded 30 apples.", []string{"Richard", "or", "30"})
	if err != nil || len(markers) != 2 || !strings.Contains(masked, "recorded") {
		t.Fatal("masked unrelated word fragment")
	}
	if _, err := restore(strings.Replace(masked, "recorded", "reported", 1)); err != nil {
		t.Fatal("safe surrounding rewrite rejected")
	}
	for _, marker := range markers {
		if _, err := restore(strings.Replace(masked, marker, marker+"suffix", 1)); err == nil {
			t.Fatal("attached marker evaded exact literal preservation")
		}
	}
}
