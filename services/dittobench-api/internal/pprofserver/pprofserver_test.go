package pprofserver

import "testing"

func TestResolvePort(t *testing.T) {
	if got, err := resolvePort(8000, ""); err != nil || got != 11000 {
		t.Fatalf("derived port = %d, %v; want 11000", got, err)
	}
	if got, err := resolvePort(8000, "19090"); err != nil || got != 19090 {
		t.Fatalf("override port = %d, %v; want 19090", got, err)
	}
	for _, override := range []string{"bad", "0", "65536"} {
		if _, err := resolvePort(8000, override); err == nil {
			t.Errorf("resolvePort accepted %q", override)
		}
	}
}
