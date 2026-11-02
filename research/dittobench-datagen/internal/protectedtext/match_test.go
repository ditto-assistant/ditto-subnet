package protectedtext

import "testing"

func TestLiteralBoundaries(t *testing.T) {
	for _, tc := range []struct {
		text, value string
		want        int
	}{
		{"record or orchard", "or", 1},
		{"Richard Thompson, Richard.", "Richard", 2},
		{"130 30 301 id30 30_id", "30", 1},
		{"élan élanx xélan élań", "élan", 1},
		{"東京 東京都", "東京", 1},
		{"USD 5.00 and USD 5.00", "USD 5.00", 2},
		{"$5.00 $5.00", "$5.00", 2},
		{"x", "", 0},
	} {
		if got := Count(tc.text, tc.value); got != tc.want {
			t.Errorf("%q / %q: got %d want %d", tc.text, tc.value, got, tc.want)
		}
	}
}

func TestReplacementIsBoundaryAwareAndSinglePass(t *testing.T) {
	got := Replace("Richard Thompson and Richard recorded or orchard", []string{"Richard Thompson", "Richard", "or"}, []string{"[full]", "[name]", "[or]"})
	if got != "[full] and [name] recorded [or] orchard" {
		t.Fatal(got)
	}
}
