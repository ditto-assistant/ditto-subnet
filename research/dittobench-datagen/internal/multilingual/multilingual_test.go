package multilingual

import (
	"errors"
	"strings"
	"testing"
)

// stubPass is an in-test translation pass: it rewrites framing words while
// leaving protected values byte-identical, and can be made to break the rule.
type stubPass struct{ breakValues bool }

func (p stubPass) Translate(lang Language, text string, protected []string) (string, error) {
	out := strings.ReplaceAll(text, "Where does", "¿Dónde")
	out = strings.ReplaceAll(out, "live", "vive")
	if p.breakValues {
		for _, v := range protected {
			out = strings.ReplaceAll(out, v, strings.ToLower(v))
		}
	}
	return "[" + string(lang) + "] " + out, nil
}

// TestV13MultilingualValuesStayCanonical is the #1831 acceptance vector: with
// the fraction knob above zero (in-test only; the v13.0 default is 0), every
// drawn surface keeps names, amounts, and options byte-canonical, and a pass
// that rewrites a value fails closed.
func TestV13MultilingualValuesStayCanonical(t *testing.T) {
	protected := []string{"Marisol Vega", "$4,110.67", "Inter", "BUD-2231"}
	text := "Where does Marisol Vega live, and is the $4,110.67 draft filed under BUD-2231 in Inter?"
	cfg := Config{Fraction: 1, Languages: Languages(), Pass: stubPass{}}
	drawn := 0
	for i := 0; i < 64; i++ {
		out, lang, err := cfg.Apply(int64(i), "question", text, protected)
		if err != nil {
			t.Fatalf("seed %d: %v", i, err)
		}
		if lang == English {
			t.Fatalf("seed %d: fraction 1 must always draw a language", i)
		}
		drawn++
		if err := ValuesStayCanonical(text, out, protected); err != nil {
			t.Fatalf("seed %d: %v", i, err)
		}
		if !strings.HasPrefix(out, "["+string(lang)+"] ") {
			t.Fatalf("seed %d: pass output not applied: %q", i, out)
		}
	}
	if drawn == 0 {
		t.Fatal("no surface drawn")
	}
	broken := Config{Fraction: 1, Languages: Languages(), Pass: stubPass{breakValues: true}}
	if _, _, err := broken.Apply(7, "question", text, protected); !errors.Is(err, ErrValueNotCanonical) {
		t.Fatalf("value-rewriting pass must fail closed: %v", err)
	}
}

// TestDefaultConfigDrawsNothing pins the v13.0 owner default: fraction 0, so
// every surface stays English and byte-identical regardless of seed.
func TestDefaultConfigDrawsNothing(t *testing.T) {
	cfg := DefaultConfig()
	if cfg.Fraction != 0 {
		t.Fatalf("default fraction = %v, want 0", cfg.Fraction)
	}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	for i := int64(0); i < 200; i++ {
		out, lang, err := cfg.Apply(i, "q", "Where do I live?", []string{"Lisbon"})
		if err != nil || lang != English || out != "Where do I live?" {
			t.Fatalf("seed %d: fraction 0 changed the surface: %q %q %v", i, out, lang, err)
		}
	}
}

// TestDrawIsDeterministicAndSeedScoped: the same (seed, key) always draws the
// same language; distinct keys spread across the candidate set; the share of
// drawn surfaces tracks the fraction.
func TestDrawIsDeterministicAndSeedScoped(t *testing.T) {
	cfg := Config{Fraction: 0.12, Languages: Languages(), Pass: NoopPass{}}
	seen := map[Language]int{}
	drawn := 0
	const n = 5000
	for i := 0; i < n; i++ {
		a, okA := cfg.Draw(int64(i), "memq")
		b, okB := cfg.Draw(int64(i), "memq")
		if a != b || okA != okB {
			t.Fatalf("draw is not deterministic at %d", i)
		}
		if okA {
			drawn++
			seen[a]++
		}
	}
	share := float64(drawn) / n
	if share < 0.09 || share > 0.15 {
		t.Fatalf("drawn share %.3f far from fraction 0.12", share)
	}
	for _, l := range Languages() {
		if seen[l] == 0 {
			t.Fatalf("language %s never drawn", l)
		}
	}
}

// TestConfigFailsClosedOnUnsupportedLanguage pins the fail-closed rule on the
// generation side: a sampled language without a lexicon is a configuration
// error, never a silently ungradeable case.
func TestConfigFailsClosedOnUnsupportedLanguage(t *testing.T) {
	bad := Config{Fraction: 0.5, Languages: []Language{"ja"}, Pass: NoopPass{}}
	if err := bad.Validate(); err == nil {
		t.Fatal("unsupported language accepted")
	}
	if _, _, err := bad.Apply(1, "q", "text", nil); err == nil {
		t.Fatal("Apply did not fail closed")
	}
	if err := (Config{Fraction: 0.5, Languages: []Language{English}, Pass: NoopPass{}}).Validate(); err == nil {
		t.Fatal("English accepted as a draw candidate")
	}
	if err := (Config{Fraction: 1.5}).Validate(); err == nil {
		t.Fatal("fraction outside [0,1] accepted")
	}
	if err := (Config{Fraction: 0.5, Languages: Languages()}).Validate(); err == nil {
		t.Fatal("fraction > 0 without a pass accepted")
	}
}

// TestSurfaceFractionsOverridePerClass pins the per-surface knob shape: a
// class override replaces the default fraction for that class only, the
// default-zero config still draws nothing for every class, a class fraction
// outside [0,1] fails validation, and raising a class's fraction only adds
// drawn surfaces (monotone in the fraction).
func TestSurfaceFractionsOverridePerClass(t *testing.T) {
	cfg := Config{Fraction: 0, SurfaceFractions: map[string]float64{SurfaceMemory: 0.2}, Languages: Languages(), Pass: NoopPass{}}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	if cfg.FractionFor(SurfaceMemory) != 0.2 || cfg.FractionFor(SurfaceRecords) != 0 || cfg.FractionFor("") != 0 {
		t.Fatalf("FractionFor: memory=%v records=%v default=%v", cfg.FractionFor(SurfaceMemory), cfg.FractionFor(SurfaceRecords), cfg.FractionFor(""))
	}
	memoryDrawn, recordsDrawn := 0, 0
	for i := int64(0); i < 2000; i++ {
		if _, ok := cfg.DrawFor(SurfaceMemory, i, "q"); ok {
			memoryDrawn++
		}
		if _, ok := cfg.DrawFor(SurfaceRecords, i, "q"); ok {
			recordsDrawn++
		}
		if _, ok := cfg.Draw(i, "q"); ok {
			t.Fatalf("seed %d: default fraction 0 drew a language", i)
		}
	}
	if memoryDrawn == 0 || recordsDrawn != 0 {
		t.Fatalf("memory drawn %d, records drawn %d", memoryDrawn, recordsDrawn)
	}
	low := Config{Fraction: 0.1, Languages: Languages(), Pass: NoopPass{}}
	high := Config{Fraction: 0.3, Languages: Languages(), Pass: NoopPass{}}
	for i := int64(0); i < 2000; i++ {
		if l, ok := low.Draw(i, "q"); ok {
			if h, okH := high.Draw(i, "q"); !okH || h != l {
				t.Fatalf("seed %d: raising the fraction dropped or relabelled a drawn surface", i)
			}
		}
	}
	for _, d := range DefaultConfig().SurfaceFractions {
		if d != 0 {
			t.Fatalf("default config carries a non-zero surface fraction %v", d)
		}
	}
	bad := Config{SurfaceFractions: map[string]float64{SurfaceTool: 1.5}, Languages: Languages(), Pass: NoopPass{}}
	if err := bad.Validate(); err == nil {
		t.Fatal("surface fraction outside [0,1] accepted")
	}
	noPass := Config{SurfaceFractions: map[string]float64{SurfaceTool: 0.1}, Languages: Languages()}
	if err := noPass.Validate(); err == nil {
		t.Fatal("surface fraction > 0 without a pass accepted")
	}
}

func TestLexiconsAreFoldedLowercaseAndComplete(t *testing.T) {
	for _, l := range Languages() {
		lex, ok := For(string(l))
		if !ok {
			t.Fatalf("%s unsupported", l)
		}
		if lex.Language != l {
			t.Fatalf("%s lexicon labelled %q", l, lex.Language)
		}
		for name, list := range map[string][]string{
			"decline": lex.Decline, "acknowledge": lex.Acknowledge, "increase": lex.Increase,
			"decrease": lex.Decrease, "unchanged": lex.Unchanged, "minor": lex.MinorUnit,
			"rejection": lex.Rejection, "past": lex.PastStrong, "current": lex.Current,
			"hedge": lex.Hedge, "cue": lex.ClaimCue, "clarify": lex.Clarify,
		} {
			if len(list) == 0 {
				t.Fatalf("%s: empty %s lexicon", l, name)
			}
			for _, p := range list {
				if p != strings.ToLower(p) || strings.TrimSpace(p) != p || p == "" {
					t.Fatalf("%s %s entry %q is not folded lowercase", l, name, p)
				}
			}
		}
		if len(lex.Months) < 12 || len(lex.NumberWords) < 21 {
			t.Fatalf("%s: months=%d numbers=%d", l, len(lex.Months), len(lex.NumberWords))
		}
	}
	if en, ok := For("en"); !ok || len(en.Decline) != 0 {
		t.Fatalf("English must be the empty base lexicon: %+v", en)
	}
	if Normalize("PT-BR") != Portuguese || Normalize("") != English || Normalize("en_GB") != English {
		t.Fatal("Normalize drifted")
	}
}

func TestUnionMergesWithoutDuplicates(t *testing.T) {
	es, _ := For("es")
	u := Union(Lexicon{Decline: []string{"no record", "no tengo"}, NumberWords: map[string]int{"one": 1}}, es)
	count := 0
	for _, p := range u.Decline {
		if p == "no tengo" {
			count++
		}
	}
	if count != 1 || u.NumberWords["one"] != 1 || u.NumberWords["uno"] != 1 || u.Language != Spanish {
		t.Fatalf("union drifted: %+v", u)
	}
}
