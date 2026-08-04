package humandata

import (
	"crypto/sha256"
	"fmt"
	"math/rand"
	"testing"
)

func TestFrozenCorpusIdentity(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		want string
	}{
		{"given names", givenNamesTSV, "f0f08ad16ba65cf77ca3009c1754926f9655148f7f5d040adf6c8fe8b8287249"},
		{"surnames", surnamesTSV, "c9288bad400045790dd196bf10b080f7cf67a4860b4872af01df8c519c4b2a82"},
		{"nicknames", nicknamesTSV, "b6ca554c42d0201950509598049cec58aff96cff686e7a08b4dccb5cf5fb4aa5"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := fmt.Sprintf("%x", sha256.Sum256([]byte(tt.raw)))
			if got != tt.want {
				t.Fatalf("corpus drift: got %s want %s", got, tt.want)
			}
		})
	}
	if len(givenNames) != 10_000 || len(surnames) != 10_000 {
		t.Fatalf("unexpected corpus sizes: given=%d surnames=%d", len(givenNames), len(surnames))
	}
}

func TestPreferredNamesAreRealMappingsOrGivenNames(t *testing.T) {
	r := rand.New(rand.NewSource(7))
	for _, name := range []string{"Nicholas", "Elizabeth", "William", "Priya"} {
		got := PreferredName(name, r)
		if got == "" {
			t.Fatalf("empty preferred name for %s", name)
		}
		if len(nicknames[lower(name)]) == 0 && got != name {
			t.Fatalf("invented preferred name %q for %q", got, name)
		}
	}
}

func TestDistinctPreferredNamesNeverRepeatTheGivenName(t *testing.T) {
	r := rand.New(rand.NewSource(17))
	for i, name := range []string{"Savanna", "Ravi", "Leah", "Nicholas", "Elizabeth", "Priya", "Bo"} {
		got := DistinctPreferredName(name, r, i)
		if got == "" || lower(got) == lower(name) {
			t.Fatalf("distinct preferred name for %q is %q", name, got)
		}
	}
}

func TestDistinctPreferredNamesCanExcludeWorldCollisions(t *testing.T) {
	r := rand.New(rand.NewSource(23))
	seen := map[string]bool{}
	for i := 0; i < len(socialNicknames); i++ {
		got := DistinctPreferredNameExcluding("Leah", r, i, seen)
		key := lower(got)
		if key == lower("Leah") || seen[key] {
			t.Fatalf("preferred name %d is not distinct: %q", i, got)
		}
		seen[key] = true
	}
}

func TestInformalShortFormsAreExplicitAndBelievable(t *testing.T) {
	for name, want := range map[string]string{
		"Savanna": "Sav",
		"Juliana": "Jules",
		"Ravi":    "Rav",
		"Niko":    "Nik",
		"Leah":    "",
		"Robert":  "",
	} {
		if got := informalShortForms[lower(name)]; got != want {
			t.Fatalf("informalShortForms[%q]=%q want %q", name, got, want)
		}
	}
}

func TestSamplingIsDeterministicAndIncludesLongTail(t *testing.T) {
	a := rand.New(rand.NewSource(1234))
	b := rand.New(rand.NewSource(1234))
	for i := 0; i < 100; i++ {
		ga, gb := GivenName(a, i), GivenName(b, i)
		if ga != gb {
			t.Fatalf("given-name sampling drift at %d: %q != %q", i, ga, gb)
		}
	}
	longTail := rand.New(rand.NewSource(99))
	for i := 4; i < 100; i += 5 {
		got := GivenName(longTail, i)
		found := false
		for _, entry := range givenNames[1000:] {
			if entry.value == got {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("long-tail draw %q came from common head", got)
		}
	}
}

func lower(s string) string {
	b := []byte(s)
	for i := range b {
		if b[i] >= 'A' && b[i] <= 'Z' {
			b[i] += 'a' - 'A'
		}
	}
	return string(b)
}
