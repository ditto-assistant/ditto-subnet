package poolcanary

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestInventoryReadsPackageLevelLiterals checks the source scan the canary
// relies on to catch new pools. Package-level slice, array, and map literals
// are counted, including grouped and multi-name declarations. Function-local
// literals, named-type literals, and test files are skipped.
func TestInventoryReadsPackageLevelLiterals(t *testing.T) {
	dir := t.TempDir()
	write := func(name, src string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(dir, name), []byte(src), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("pools.go", `package p

type grammar map[string][]string

var topics = []string{"move", "garden", "car", "back pain"}

var (
	pairs   = [][2]string{{"a", "b"}, {"c", "d"}}
	byKind  = map[string]int{"x": 1, "y": 2, "z": 3}
	fixed   = [3]int{1, 2, 3}
	g       = grammar{"root": {"hi"}}
	derived = append([]string(nil), topics...)
)

var left, right = []int{1}, []int{1, 2}

func local() []string { return []string{"never", "counted"} }
`)
	write("pools_test.go", `package p

var testOnly = []string{"x"}
`)
	inv, err := Inventory(dir)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]int{"topics": 4, "pairs": 2, "byKind": 3, "fixed": 3, "left": 1, "right": 2}
	if len(inv) != len(want) {
		t.Errorf("inventory = %v, want exactly %v", inv, want)
	}
	for name, n := range want {
		if inv[name].Elems != n {
			t.Errorf("%s: %d entries, want %d", name, inv[name].Elems, n)
		}
	}
	if pos := inv["topics"].Pos; pos != "pools.go:5" {
		t.Errorf("topics position = %q, want pools.go:5", pos)
	}
}

// TestViolationsEnforceTheRatchet runs each failure the canary promises against
// a synthetic package. A new small pool fails. A shrinking pool fails. An
// allowlisted pool that reaches the floor has to leave the allowlist. Stale or
// contradictory entries fail. A clean ledger passes. Without this test, a
// Violations that returned nothing would turn every package canary green.
func TestViolationsEnforceTheRatchet(t *testing.T) {
	inv := map[string]Literal{
		"bigNames":   {Name: "bigNames", Elems: 40, Pos: "a.go:1"},
		"topics":     {Name: "topics", Elems: 4, Pos: "a.go:2"},
		"stems":      {Name: "stems", Elems: 8, Pos: "a.go:3"},
		"suffixes":   {Name: "suffixes", Elems: 6, Pos: "a.go:4"},
		"profiles":   {Name: "profiles", Elems: 3, Pos: "a.go:5"},
		"legacyHops": {Name: "legacyHops", Elems: 4, Pos: "a.go:6"},
	}
	clean := func() Ledger {
		return Ledger{
			Scored: []Pool{
				{Name: "bigNames", Size: 40, Role: "answer"},
				{Name: "topics", Size: 4, Role: "question cue"},
				{Name: "stems×suffixes", Parts: []string{"stems", "suffixes"}, Size: 48, Role: "answer"},
			},
			Allow:  map[string]int{"topics": 4},
			Exempt: map[string]string{"profiles": Internal, "legacyHops": Frozen},
		}
	}
	if v := Violations(clean(), inv); len(v) != 0 {
		t.Fatalf("clean ledger reported violations: %v", v)
	}

	cases := []struct {
		name   string
		mutate func(*Ledger, map[string]Literal)
		want   string
	}{
		{"a new small pool fails until classified", func(l *Ledger, inv map[string]Literal) {
			inv["recurringTopics"] = Literal{Name: "recurringTopics", Elems: 4, Pos: "b.go:9"}
		}, "new small pool recurringTopics (4 entries) at b.go:9"},
		{"a new large pool needs no classification", func(l *Ledger, inv map[string]Literal) {
			inv["corpus"] = Literal{Name: "corpus", Elems: 200, Pos: "b.go:1"}
		}, ""},
		{"a scored pool below the floor fails", func(l *Ledger, inv map[string]Literal) {
			inv["bigNames"] = Literal{Name: "bigNames", Elems: 20, Pos: "a.go:1"}
			l.Scored[0].Size = 20
		}, "bigNames [answer] has 20 entries, below the floor of 24"},
		{"an allowlisted pool cannot shrink", func(l *Ledger, inv map[string]Literal) {
			inv["topics"] = Literal{Name: "topics", Elems: 3, Pos: "a.go:2"}
			l.Scored[1].Size = 3
		}, "topics [question cue] shrank from 4 to 3 entries"},
		{"an allowlisted pool that grows records its new size", func(l *Ledger, inv map[string]Literal) {
			inv["topics"] = Literal{Name: "topics", Elems: 10, Pos: "a.go:2"}
			l.Scored[1].Size = 10
		}, "topics [question cue] grew from 4 to 10 entries but is still below the floor of 24: record 10"},
		{"an allowlisted pool that reaches the floor leaves the allowlist", func(l *Ledger, inv map[string]Literal) {
			inv["topics"] = Literal{Name: "topics", Elems: 30, Pos: "a.go:2"}
			l.Scored[1].Size = 30
		}, "topics [question cue] now has 30 entries, at or above the floor of 24: delete it from the allowlist"},
		{"a composed surface is measured by its product", func(l *Ledger, inv map[string]Literal) {
			l.Scored[2].Size = 8 * 2
		}, "stems×suffixes [answer] has 16 entries, below the floor of 24"},
		{"the ledger's name must match the reference it measures", func(l *Ledger, inv map[string]Literal) {
			l.Scored[0].Size = 41
		}, "scored pool bigNames: the ledger measured 41 entries but a.go:1 declares 40"},
		{"an init-extended pool may exceed its literal", func(l *Ledger, inv map[string]Literal) {
			l.Scored[0].Size, l.Scored[0].Grown = 90, true
		}, ""},
		{"an init-extended pool may not be smaller than its literal", func(l *Ledger, inv map[string]Literal) {
			l.Scored[0].Size, l.Scored[0].Grown = 30, true
		}, "scored pool bigNames: the ledger measured 30 entries but a.go:1 declares 40"},
		{"a renamed scored pool is reported", func(l *Ledger, inv map[string]Literal) {
			delete(inv, "stems")
		}, "scored pool stems×suffixes: stems is not a package-level slice/map literal here"},
		{"a stale allowlist entry is reported", func(l *Ledger, inv map[string]Literal) {
			l.Allow["gone"] = 3
		}, "allowlist entry gone has no scored pool"},
		{"a stale exemption is reported", func(l *Ledger, inv map[string]Literal) {
			delete(inv, "legacyHops")
		}, "exemption legacyHops no longer names a package-level pool literal"},
		{"an exemption that grew past the floor is reported", func(l *Ledger, inv map[string]Literal) {
			inv["profiles"] = Literal{Name: "profiles", Elems: 24, Pos: "a.go:5"}
		}, "exemption profiles is unnecessary"},
		{"a pool cannot be both scored and exempt", func(l *Ledger, inv map[string]Literal) {
			l.Exempt["topics"] = Frozen
		}, "topics is both exempt"},
		{"an exemption needs a reason", func(l *Ledger, inv map[string]Literal) {
			l.Exempt["profiles"] = " "
		}, "exemption profiles needs a reason"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			l := clean()
			local := make(map[string]Literal, len(inv))
			for k, v := range inv {
				local[k] = v
			}
			tc.mutate(&l, local)
			got := Violations(l, local)
			if tc.want == "" {
				if len(got) != 0 {
					t.Fatalf("want no violations, got %v", got)
				}
				return
			}
			if len(got) != 1 || !strings.Contains(got[0], tc.want) {
				t.Fatalf("want exactly one violation containing %q, got %q", tc.want, got)
			}
		})
	}
}
