// Package multilingual holds the grader-side per-language lexicons and the
// value-protection rules for the Bench v13 multilingual fraction.
//
// It deliberately contains NO translations and no per-language grammars. A
// checked-in Spanish/Portuguese/French grammar is just three more public tables
// a template-fitting harness can invert; the surface text of a multilingual
// case is produced by the PRIVATE translation pass (TranslationPass, stubbed
// here) with a seed-derived, unannounced language subset that is not
// regenerable from this repository. What this package owns is the part that
// must be public for a score to be auditable:
//
//   - the reply-language policy: the grader accepts an answer in the question's
//     language or in English, so the per-language decline / acknowledgement /
//     direction / number / month lexicons live here;
//   - fail-closed language support: a case sampled into a language without a
//     lexicon cannot be graded and must not be generated;
//   - the value-protection rule: names, amounts, options, identifiers, and every
//     other graded value stay byte-canonical through the pass.
//
// English is the grader's native language: its kind lexicons live in the grade
// package (they are the frozen v2..v12 tables plus the v13 closure), so For
// returns an empty lexicon for "en" and the grader unions it with its own.
package multilingual

import (
	"errors"
	"fmt"
	"hash/fnv"
	"sort"
	"strings"
)

// Language is a BCP-47 primary language subtag ("es"). The empty string means
// English (the historical default for every case without a Language field).
type Language string

const (
	English    Language = "en"
	Spanish    Language = "es"
	Portuguese Language = "pt"
	French     Language = "fr"
	Italian    Language = "it"
	German     Language = "de"
	Dutch      Language = "nl"
)

// Lexicon is the grader-facing vocabulary of one language. Every phrase is
// matched by the grader as a case-folded bounded phrase, so entries are stored
// lowercase and without surrounding punctuation. Lists are additive to the
// English tables; they never replace them.
type Lexicon struct {
	Language Language

	// Decline phrases mark a grounded decline ("no tengo constancia").
	Decline []string
	// Acknowledge phrases confirm an instruction was carried out ("eliminado").
	Acknowledge []string
	// Increase, Decrease, and Unchanged are the three-valued direction closure.
	Increase  []string
	Decrease  []string
	Unchanged []string
	// NumberWords maps spelled-out small counts (0..20) to their value.
	NumberWords map[string]int
	// Months maps month names and common abbreviations to 1..12.
	Months map[string]int
	// MinorUnit words mark an amount stated in minor currency units.
	MinorUnit []string
	// MajorUnit words mark an amount stated in major units ("dólares").
	MajorUnit []string

	// Claim-structure markers used by the v13 typed-claim engine.
	Rejection  []string // opens a rejected span: "no", "nunca", "en lugar de"
	Contrast   []string // clause connective that closes a span: "pero", "sino"
	PastStrong []string // always marks a superseded/past mention: "antes", "solía"
	PastWeak   []string // past only when a current mention exists: "era", "fue"
	Current    []string // marks the current value: "ahora", "actualmente"
	Correction []string // sentence-initial retroactive correction: "en realidad"
	Hedge      []string // enumerating hedge: "quizás", "podría ser"
	Enumerator []string // value-list joiner: "o", "u"
	ClaimCue   []string // precedes/follows the claimed value: "es", "queda"
	Clarify    []string // names a missing slot as a question: "cuál", "podrías especificar"
	Echo       []string // marks a restated question, not an assertion: "preguntaste"
	Interrog   []string // sentence-initial interrogatives: "cuál", "qué"
}

// lexicons is the closed set of languages this release can grade. Adding a
// language is a grader contract change: it lands in a new bench version.
var lexicons = map[Language]Lexicon{
	English:    {Language: English},
	Spanish:    spanish,
	Portuguese: portuguese,
	French:     french,
	Italian:    italian,
	German:     german,
	Dutch:      dutch,
}

// Normalize maps a case language field to its canonical subtag. The empty
// string and any casing/region variant of English ("EN", "en-US") are English.
func Normalize(lang string) Language {
	l := strings.ToLower(strings.TrimSpace(lang))
	if i := strings.IndexAny(l, "-_"); i >= 0 {
		l = l[:i]
	}
	if l == "" {
		return English
	}
	return Language(l)
}

// Supported reports whether the grader holds a lexicon for lang. It is the
// fail-closed gate both the generator (do not sample it) and the grader (do
// not credit a case in it) must consult.
func Supported(lang string) bool {
	_, ok := lexicons[Normalize(lang)]
	return ok
}

// For returns the lexicon for lang. ok is false for a language without one.
func For(lang string) (Lexicon, bool) {
	lex, ok := lexicons[Normalize(lang)]
	return lex, ok
}

// Languages lists the supported non-English languages in stable order.
func Languages() []Language {
	out := make([]Language, 0, len(lexicons)-1)
	for l := range lexicons {
		if l != English {
			out = append(out, l)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// Union returns base extended with every phrase of extra. Map entries of extra
// win on collision; slices are appended in order with duplicates dropped.
func Union(base, extra Lexicon) Lexicon {
	out := base
	out.Language = base.Language
	if extra.Language != "" {
		out.Language = extra.Language
	}
	join := func(a, b []string) []string {
		seen := make(map[string]bool, len(a)+len(b))
		res := make([]string, 0, len(a)+len(b))
		for _, list := range [][]string{a, b} {
			for _, p := range list {
				if p == "" || seen[p] {
					continue
				}
				seen[p] = true
				res = append(res, p)
			}
		}
		return res
	}
	mergeInt := func(a, b map[string]int) map[string]int {
		res := make(map[string]int, len(a)+len(b))
		for k, v := range a {
			res[k] = v
		}
		for k, v := range b {
			res[k] = v
		}
		return res
	}
	out.Decline = join(base.Decline, extra.Decline)
	out.Acknowledge = join(base.Acknowledge, extra.Acknowledge)
	out.Increase = join(base.Increase, extra.Increase)
	out.Decrease = join(base.Decrease, extra.Decrease)
	out.Unchanged = join(base.Unchanged, extra.Unchanged)
	out.NumberWords = mergeInt(base.NumberWords, extra.NumberWords)
	out.Months = mergeInt(base.Months, extra.Months)
	out.MinorUnit = join(base.MinorUnit, extra.MinorUnit)
	out.MajorUnit = join(base.MajorUnit, extra.MajorUnit)
	out.Rejection = join(base.Rejection, extra.Rejection)
	out.Contrast = join(base.Contrast, extra.Contrast)
	out.PastStrong = join(base.PastStrong, extra.PastStrong)
	out.PastWeak = join(base.PastWeak, extra.PastWeak)
	out.Current = join(base.Current, extra.Current)
	out.Correction = join(base.Correction, extra.Correction)
	out.Hedge = join(base.Hedge, extra.Hedge)
	out.Enumerator = join(base.Enumerator, extra.Enumerator)
	out.ClaimCue = join(base.ClaimCue, extra.ClaimCue)
	out.Clarify = join(base.Clarify, extra.Clarify)
	out.Echo = join(base.Echo, extra.Echo)
	out.Interrog = join(base.Interrog, extra.Interrog)
	return out
}

// ErrValueNotCanonical is returned when a translation moved, dropped, or
// rewrote a protected value.
var ErrValueNotCanonical = errors.New("multilingual: protected value is not canonical after translation")

// ValuesStayCanonical is the value-protection rule every translation pass must
// satisfy: each protected value (a name, amount, option, identifier, or other
// graded token) occurs in the translated text byte-for-byte exactly as many
// times as in the original. Story prose may carry the language; planted values
// never do. It is deliberately a byte comparison, not a normalized one: the
// grader's own normalization is what makes a reply lenient, and the generated
// side must stay strict so the two never drift apart.
func ValuesStayCanonical(original, translated string, protected []string) error {
	for _, v := range protected {
		if v == "" {
			continue
		}
		want := strings.Count(original, v)
		got := strings.Count(translated, v)
		if want != got {
			return fmt.Errorf("%w: %q occurs %d time(s) in the source and %d in the translation", ErrValueNotCanonical, v, want, got)
		}
	}
	return nil
}

// TranslationPass is the private surface pass that renders a fraction of
// generated text in a sampled language. Implementations live outside this
// module (a cached LLM translation pinned per dataset); the public contract is
// only that protected values survive byte-identically (ValuesStayCanonical) and
// that the same (lang, text) input yields the same output within one dataset.
type TranslationPass interface {
	Translate(lang Language, text string, protected []string) (string, error)
}

// NoopPass returns its input unchanged. It is the public rehearsal default and
// the pass every English-only bench version implicitly uses.
type NoopPass struct{}

// Translate implements TranslationPass.
func (NoopPass) Translate(_ Language, text string, _ []string) (string, error) { return text, nil }

// Config is the multilingual profile knob. Fraction is the share of eligible
// surfaces drawn into a non-English language; Languages is the candidate set;
// Pass renders the draw. The v13.0 default is Fraction 0 (owner decision: the
// fraction is set by the starter-kit <2-point calibration, not here).
type Config struct {
	Fraction  float64
	Languages []Language
	Pass      TranslationPass
}

// DefaultConfig is the v13.0 rehearsal configuration: no multilingual surface,
// every supported language eligible, and the no-op pass.
func DefaultConfig() Config {
	return Config{Fraction: 0, Languages: Languages(), Pass: NoopPass{}}
}

// Validate fails closed on a configuration the grader could not honour: a
// fraction outside [0,1], or a candidate language without a lexicon.
func (c Config) Validate() error {
	if c.Fraction < 0 || c.Fraction > 1 {
		return fmt.Errorf("multilingual: fraction %v outside [0,1]", c.Fraction)
	}
	if c.Fraction > 0 && len(c.Languages) == 0 {
		return errors.New("multilingual: fraction > 0 with no candidate languages")
	}
	for _, l := range c.Languages {
		if !Supported(string(l)) {
			return fmt.Errorf("multilingual: language %q has no grader lexicon", l)
		}
		if Normalize(string(l)) == English {
			return errors.New("multilingual: English is the base language, not a draw candidate")
		}
	}
	if c.Fraction > 0 && c.Pass == nil {
		return errors.New("multilingual: fraction > 0 without a translation pass")
	}
	return nil
}

// Draw deterministically decides whether the surface identified by (seed, key)
// is rendered in a non-English language and which one. The draw is a pure
// function of its inputs, so a dataset regenerates identically; with Fraction 0
// it never selects a language.
func (c Config) Draw(seed int64, key string) (Language, bool) {
	if c.Fraction <= 0 || len(c.Languages) == 0 {
		return English, false
	}
	h := fnv.New64a()
	fmt.Fprintf(h, "%d|%s", seed, key)
	sum := h.Sum64()
	// Top 53 bits as a uniform draw in [0,1); the remaining bits pick the language.
	u := float64(sum>>11) / float64(uint64(1)<<53)
	if u >= c.Fraction {
		return English, false
	}
	langs := append([]Language(nil), c.Languages...)
	sort.Slice(langs, func(i, j int) bool { return langs[i] < langs[j] })
	return langs[int(sum&0x7ff)%len(langs)], true
}

// Apply renders text for the surface (seed, key) through the configured pass
// when the draw selects a language, verifying the value-protection rule and
// failing closed on an unsupported language. The returned language is English
// when the text was left alone.
func (c Config) Apply(seed int64, key, text string, protected []string) (string, Language, error) {
	if err := c.Validate(); err != nil {
		return "", English, err
	}
	lang, ok := c.Draw(seed, key)
	if !ok {
		return text, English, nil
	}
	if !Supported(string(lang)) {
		return "", English, fmt.Errorf("multilingual: sampled language %q has no grader lexicon", lang)
	}
	out, err := c.Pass.Translate(lang, text, protected)
	if err != nil {
		return "", English, fmt.Errorf("multilingual: translate %s: %w", lang, err)
	}
	if err := ValuesStayCanonical(text, out, protected); err != nil {
		return "", English, err
	}
	return out, lang, nil
}
