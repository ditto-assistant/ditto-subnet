// Package poolcanary is the shared checker behind each generator package's
// TestPoolCardinalityCanary (#492: closed enumerable pools let miners hardcode
// dispatch tables).
//
// A pool is a package-level slice, array, or map literal. A harness that can
// read the generator source can copy a small closed pool into a lookup table and
// answer from the table instead of the records — the recurring-topic,
// declPrefDomains, and deep-join relation tables seen in #492 submissions. The
// canary treats such a pool as a benchmark defect to be reviewed, not as
// something to police in miners.
//
// Each generator package keeps a Ledger in its own test that sorts every small
// pool into one of three buckets:
//
//   - Scored: the pool reaches a scored case's question, answer, accepted
//     forms, distractors, or seeded records under the v13 scored contract or
//     the newest supported one. The test measures it through a direct
//     reference. An answer or identifier assembled from several banks is
//     measured by their product, because a table must cover every combination.
//     A cue bank is measured on its own, because matching any one entry
//     already detects the cue. A scored pool needs at least Floor entries.
//   - Allow: a scored pool already below Floor when the canary landed. Its
//     exact size is recorded. Shrinking fails. Growing below Floor means the
//     recorded size has to be raised. Reaching Floor means the entry has to be
//     deleted, so this list only gets shorter.
//   - Exempt: a small pool that neither of those contracts scores (a frozen
//     older contract, configuration, a keyed table), with a reason.
//
// The inventory is read from the package's own source, so a new small pool
// fails CI until someone sorts it into one of the buckets. A pool added to
// Allow or Exempt shows up in review as a ledger change.
//
// Two things are not measured. One is the number of surfaces a grammar bank
// expands to: persona.Grammar values and the per-key banks inside Keyed
// tables. The other is a pool built at run time rather than declared as a
// literal.
package poolcanary

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// Floor is the minimum effective cardinality of a scored pool.
//
// #492 reports that the leaderboard champion shipped a 20-entry item-noun list
// as dispatch keys, so 20 entries were already enumerable in practice. The
// floor sits above that. The issue's own remediation, djFirstHops at 28
// relations, clears it. This is a lower bound on what counts as obviously
// enumerable, not a target ("a miner who enumerates a 400-entry pool gets
// nothing"). Raising it later is a one-line ratchet. Lowering it should never
// be needed.
const Floor = 24

// Shared exemption reasons. A package may also give a reason of its own.
const (
	// Frozen marks a pool that only frozen bench_version <= 12 generators
	// reach. Those contracts' bytes cannot change, and the v13 and v14
	// contracts never draw from the pool.
	Frozen = "frozen: reached only by bench_version <= 12 generators, never drawn at v13/v14"
	// PhaseOnly marks a v8..v12 world list that v13 still draws from to keep
	// the rng phase, then discards (#1825 swapped in corpus-backed banks).
	PhaseOnly = "frozen v8..v12 world list: v13+ consumes the draw for rng phase only and replaces the value"
	// Keyed marks a lookup table whose keys are a closed enum (kinds, oracles,
	// conventions). Each value is a per-key bank, not one drawable pool.
	Keyed = "lookup table keyed by a closed enum; the per-key banks are the surface, not the key set"
	// Internal marks configuration, report ordering, or a match/filter set
	// whose entries are never sent to a harness.
	Internal = "configuration or match set: entries never reach a harness"
	// Schema marks the enum of case families, domains, or renderers that the
	// contract publishes as structure. The values are never rendered as text.
	Schema = "published case-structure enum: values never rendered as text"
)

// Pool is one scored pool, as measured by the owning package's test.
type Pool struct {
	// Name identifies the pool in messages and in the Allow ledger. For a
	// single declared pool it is the identifier itself.
	Name string
	// Parts lists the identifiers declared in this package that make up the
	// pool. It defaults to []string{Name}. A composed surface lists every bank
	// it multiplies. Qualified names such as "publicdata.AllOrgStems" refer to
	// other packages and are not checked against this inventory.
	Parts []string
	// Size is the effective cardinality: len for one pool, or the product of
	// the banks for a composed surface.
	Size int
	// Role says how a harness meets the pool (answer, question cue, record
	// cue, ...). Messages show it to the reader.
	Role string
	// Grown marks a literal that init() extends (a seed list merged with a
	// corpus). Size is then the runtime length and may exceed the literal.
	Grown bool
}

func (p Pool) parts() []string {
	if len(p.Parts) == 0 {
		return []string{p.Name}
	}
	return p.Parts
}

// Ledger is one package's classification of its pools.
type Ledger struct {
	Scored []Pool
	// Allow maps a scored pool's Name to its exact size, which is below Floor.
	Allow map[string]int
	// Exempt maps a declared identifier to the reason it is not scored.
	Exempt map[string]string
}

// Literal is one package-level slice, array, or map composite literal.
type Literal struct {
	Name  string
	Elems int
	Pos   string
}

// Inventory parses the non-test Go files in dir. It returns every
// package-level variable initialised with a composite literal whose type is
// written as a slice, array, or map. Variables of named types such as
// persona.Grammar are left out: a grammar is a set of combinatorial slots, and
// its key count says nothing about how many surfaces it produces.
func Inventory(dir string) (map[string]Literal, error) {
	fset := token.NewFileSet()
	out := map[string]Literal{}
	entries, err := filepath.Glob(filepath.Join(dir, "*.go"))
	if err != nil {
		return nil, err
	}
	for _, path := range entries {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if err != nil {
			return nil, fmt.Errorf("parse %s: %w", path, err)
		}
		for _, decl := range file.Decls {
			gen, ok := decl.(*ast.GenDecl)
			if !ok || gen.Tok != token.VAR {
				continue
			}
			for _, spec := range gen.Specs {
				vs := spec.(*ast.ValueSpec)
				for i, value := range vs.Values {
					lit, ok := value.(*ast.CompositeLit)
					if !ok || i >= len(vs.Names) {
						continue
					}
					switch lit.Type.(type) {
					case *ast.ArrayType, *ast.MapType:
					default:
						continue
					}
					pos := fset.Position(vs.Names[i].Pos())
					out[vs.Names[i].Name] = Literal{
						Name:  vs.Names[i].Name,
						Elems: len(lit.Elts),
						Pos:   fmt.Sprintf("%s:%d", filepath.Base(pos.Filename), pos.Line),
					}
				}
			}
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("no package-level pool literals under %s: %w", dir, fs.ErrNotExist)
	}
	return out, nil
}

// Violations compares a ledger with an inventory. It returns one message per
// problem, sorted so output is stable.
func Violations(l Ledger, inv map[string]Literal) []string {
	var out []string
	add := func(format string, args ...any) { out = append(out, fmt.Sprintf(format, args...)) }

	claimed := map[string]string{} // declared identifier -> scored pool name
	scoredNames := map[string]bool{}
	for _, p := range l.Scored {
		if scoredNames[p.Name] {
			add("scored pool %s is listed twice", p.Name)
		}
		scoredNames[p.Name] = true
		for _, part := range p.parts() {
			if strings.Contains(part, ".") {
				continue // another package's bank, measured through its accessor
			}
			lit, ok := inv[part]
			if !ok {
				add("scored pool %s: %s is not a package-level slice/map literal here (renamed or removed?); update the ledger", p.Name, part)
				continue
			}
			claimed[part] = p.Name
			mismatch := lit.Elems != p.Size
			if p.Grown {
				mismatch = p.Size < lit.Elems
			}
			if len(p.parts()) == 1 && mismatch {
				add("scored pool %s: the ledger measured %d entries but %s declares %d; the name and the reference disagree", p.Name, p.Size, lit.Pos, lit.Elems)
			}
		}

		recorded, allowed := l.Allow[p.Name]
		switch {
		case allowed && p.Size >= Floor:
			add("%s [%s] now has %d entries, at or above the floor of %d: delete it from the allowlist. The allowlist only shrinks (#492)", p.Name, p.Role, p.Size, Floor)
		case allowed && p.Size < recorded:
			add("%s [%s] shrank from %d to %d entries: a closed pool below the floor of %d must not get smaller (#492)", p.Name, p.Role, recorded, p.Size, Floor)
		case allowed && p.Size > recorded:
			add("%s [%s] grew from %d to %d entries but is still below the floor of %d: record %d in the allowlist", p.Name, p.Role, recorded, p.Size, Floor, p.Size)
		case !allowed && p.Size < Floor:
			add("%s [%s] has %d entries, below the floor of %d: a closed pool this small can be copied into a dispatch table (#492). Grow it, or compose it with a larger bank, to at least %d; allowlisting a new small pool needs explicit reviewer sign-off", p.Name, p.Role, p.Size, Floor, Floor)
		}
	}
	for name := range l.Allow {
		if !scoredNames[name] {
			add("allowlist entry %s has no scored pool: delete it", name)
		}
	}
	for name, reason := range l.Exempt {
		lit, ok := inv[name]
		switch {
		case !ok:
			add("exemption %s no longer names a package-level pool literal: delete it", name)
		case claimed[name] != "":
			add("%s is both exempt (%s) and part of scored pool %s: pick one", name, reason, claimed[name])
		case lit.Elems >= Floor:
			add("exemption %s is unnecessary: it has %d entries (floor %d); delete it", name, lit.Elems, Floor)
		case strings.TrimSpace(reason) == "":
			add("exemption %s needs a reason", name)
		}
	}
	for name, lit := range inv {
		if lit.Elems >= Floor || claimed[name] != "" {
			continue
		}
		if _, ok := l.Exempt[name]; ok {
			continue
		}
		add("new small pool %s (%d entries) at %s: if a harness can see its entries at the active or newest contract, register it as scored and grow it to the floor of %d; otherwise exempt it with a reason (#492)", name, lit.Elems, lit.Pos, Floor)
	}
	sort.Strings(out)
	return out
}

// Check runs Violations on the package in dir, which is "." for a test in the
// package being checked. It reports each problem as a test error.
func Check(t testing.TB, dir string, l Ledger) {
	t.Helper()
	inv, err := Inventory(dir)
	if err != nil {
		t.Fatalf("pool inventory: %v", err)
	}
	for _, v := range Violations(l, inv) {
		t.Error(v)
	}
	t.Logf("pool ledger: %d package-level pool literals, %d scored pools (%d allowlisted below the floor of %d), %d exempt",
		len(inv), len(l.Scored), len(l.Allow), Floor, len(l.Exempt))
}
