package uservoice

import "testing"

func TestRenderRemovesBenchmarkFramingWithoutChangingFacts(t *testing.T) {
	got := Render("For reference, the primary router's admin password is Nofegu-3245. Nothing urgent.")
	want := "The primary router's admin password is Nofegu-3245."
	if got != want {
		t.Fatalf("Render()=%q, want %q", got, want)
	}
}
