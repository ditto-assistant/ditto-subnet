package protocol

import (
	"bytes"
	"encoding/json"
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Bench v18 tool encounters (docs/bench-versions.md, "Tool encounters").
//
// An encounter is one user request that needs several dependent tool calls:
// the id a list returns is what the next call needs, a page points at a
// correction, a job has to be polled until it finishes. The mock world is
// argument-keyed (EncounterRule), so the order of calls emerges from the data
// rather than from a scripted trajectory, and the case is graded on the
// outcome:
//
//   - the final answer must carry every EncounterAnswer value (and none of
//     its hedges beside it);
//   - every Required call (a tool plus the argument values that prove the
//     dependency was followed) must be observed, in any order;
//   - Expected tools are neutral: calling them neither earns nor costs;
//   - each Forbidden call observed halves the score.
//
// Any other served tool answers Fallback and is neutral; wandering is priced
// by the run's cost factor and budget, not by the grader. Every field is
// grader-only: the harness sees the prompt and the tool results, never this.
type Encounter struct {
	// Archetype names the scenario shape (the case category is
	// "encounter_" + Archetype).
	Archetype string
	// Variant names the branch of the shape this case takes (a correction
	// that is itself superseded, a later record still awaiting approval).
	// Variants exist so a script written for the plain path fails.
	Variant string
	// Rules are the mock world. For each call the first rule whose Tool,
	// Contains and MinPrior match answers it.
	Rules []EncounterRule
	// Fallback answers a served tool no rule matched.
	Fallback string
	// Required are the calls an honest solution must make, unordered.
	Required []EncounterCall
	// Expected are tools a good solution may well call (neutral).
	Expected []string
	// Forbidden are calls that should play no part in the request.
	Forbidden []EncounterCall
	// Answer are the values the final reply must carry.
	Answer []EncounterAnswer
}

// EncounterRule is one argument-keyed mock response.
type EncounterRule struct {
	Tool string
	// Contains are values every one of which must appear in the call's
	// arguments (EncounterArgsMatch). Empty matches any arguments.
	Contains []string
	// MinPrior makes the rule apply only once this tool has already been
	// called at least MinPrior times in the case (a job that is still running
	// on the first poll and finished on the second).
	MinPrior int
	Result   string
	// Error, when set, is served as a tool error instead of Result.
	Error string
	// Page marks a rule as one page of a multi-target read: a call that
	// matches several Page rules of its tool gets all of their results,
	// joined, so reading two links in one call reads both.
	Page bool
}

// EncounterCall is a tool call identified by name and argument values.
type EncounterCall struct {
	Tool     string
	Contains []string
	// AnyContains are alternatives at least one of which must appear (an id
	// or a name for the same target). Empty means no such condition.
	AnyContains []string
	// Excludes are values whose presence means the call is not this one: a
	// filing that names the right incident and only quotes a resolved one as
	// context is not "filing the resolved incident".
	Excludes []string
	// Field, when set, scopes Contains, AnyContains and Excludes to that one
	// argument (a write action's declared parameter, such as the filed item's
	// title) instead of every argument value: a filing titled with the wrong
	// incident does not become the right one by quoting the right ID in some
	// other field. A call without the field carries none of the values.
	Field string
	// Why is the documented reason (reported in the grader notes).
	Why string
}

// EncounterAnswer is one value the final reply must carry. A reply that also
// carries one of Hedges (the figure a wrong source or a skipped correction
// gives) has not committed to an answer and earns nothing for this value.
type EncounterAnswer struct {
	Value  string
	Hedges []string
	// Derivation, when set, says how Value is computed from served values
	// (it appears in no tool result: "12 months x 18 seats x $54").
	Derivation string
}

// Matches reports whether a call satisfies the EncounterCall.
func (c EncounterCall) Matches(name string, args json.RawMessage) bool {
	if name != c.Tool {
		return false
	}
	text := EncounterArgText(args)
	if c.Field != "" {
		text = encounterFieldText(args, c.Field)
	}
	for _, value := range c.Contains {
		if !EncounterContains(text, value) {
			return false
		}
	}
	if len(c.AnyContains) > 0 {
		found := false
		for _, value := range c.AnyContains {
			if EncounterContains(text, value) {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	for _, value := range c.Excludes {
		if EncounterContains(text, value) {
			return false
		}
	}
	return true
}

// encounterFieldText is the flattened text of one top-level argument, or ""
// when the arguments are not an object or do not carry it.
func encounterFieldText(raw json.RawMessage, field string) string {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return ""
	}
	value, ok := fields[field]
	if !ok {
		return ""
	}
	return EncounterArgText(value)
}

var (
	// Thousands separators: comma, no-break space, narrow no-break space and
	// thin space. An ordinary space is deliberately not one: "12 450" in a
	// sentence is as often two numbers as one.
	encounterThousands = regexp.MustCompile(`(\d)[,\x{00a0}\x{202f}\x{2009}](\d{3})\b`)
	encounterDashes    = strings.NewReplacer("\u2010", "-", "\u2011", "-", "\u2012", "-", "\u2013", "-", "\u2014", "-",
		"\u2015", "-", "\u2212", "-", "\u00ad", "")
	// Currency signs become spaces, never nothing: "US$18,420" must keep 18420
	// a bounded token rather than become "us18420".
	encounterSigns = strings.NewReplacer("$", " ", "\u20ac", " ", "\u00a3", " ", "*", " ", "`", " ")
)

// NormalizeEncounterText folds the surface differences a value may pick up
// on its way through a model: case, typographic dashes and soft hyphens,
// thousands separators and currency signs. "$18,420" and "18420" normalize
// alike.
func NormalizeEncounterText(s string) string {
	s = strings.ToLower(encounterDashes.Replace(s))
	for {
		next := encounterThousands.ReplaceAllString(s, "$1$2")
		if next == s {
			break
		}
		s = next
	}
	return strings.TrimSpace(encounterSigns.Replace(s))
}

// EncounterContains reports whether normalized text carries value as a
// bounded token: the characters on either side are not letters or digits,
// so "run-1234" does not match inside "run-12345". A URL value must match a
// whole URL: "https://x.com/pricing" does not match inside
// "https://x.com/pricing/notice-7" (an optional trailing slash is allowed).
func EncounterContains(text, value string) bool {
	hay := NormalizeEncounterText(text)
	want := NormalizeEncounterText(strings.TrimSpace(value))
	if want == "" {
		return false
	}
	isURL := strings.Contains(want, "://")
	if isURL {
		want = strings.TrimSuffix(want, "/")
	}
	for from := 0; ; {
		at := strings.Index(hay[from:], want)
		if at < 0 {
			return false
		}
		start, end := from+at, from+at+len(want)
		if isURL && end < len(hay) && hay[end] == '/' {
			end++
		}
		bounded := !encounterAlnumBefore(hay, start) && !encounterAlnumAfter(hay, end)
		if bounded && (!isURL || !encounterURLContinues(hay, end)) {
			return true
		}
		from = start + 1
	}
}

// encounterAlnumBefore / encounterAlnumAfter report whether the rune next to
// a match is a letter or digit (Unicode-aware: a curly quote is a boundary).
func encounterAlnumBefore(s string, i int) bool {
	if i <= 0 {
		return false
	}
	r, _ := utf8.DecodeLastRuneInString(s[:i])
	return unicode.IsLetter(r) || unicode.IsDigit(r)
}

func encounterAlnumAfter(s string, i int) bool {
	if i >= len(s) {
		return false
	}
	r, _ := utf8.DecodeRuneInString(s[i:])
	return unicode.IsLetter(r) || unicode.IsDigit(r)
}

// encounterURLContinues reports whether a URL match is followed by more URL
// path or query (so it is a prefix of a longer URL, not the URL itself).
func encounterURLContinues(s string, i int) bool {
	if i >= len(s) {
		return false
	}
	switch s[i] {
	case '/', '-', '_', '.', '~', '%', '?', '#', '&', '=', '+':
		return i+1 < len(s) && encounterAlnumAfter(s, i+1)
	}
	return false
}

// EncounterArgText flattens a call's JSON arguments into the text of their
// values (object keys sorted), so a value matches whether it was passed as a
// string, inside an array, or as a number.
func EncounterArgText(raw json.RawMessage) string {
	if len(bytes.TrimSpace(raw)) == 0 {
		return ""
	}
	var v any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&v); err != nil {
		return string(raw)
	}
	var parts []string
	var walk func(any)
	walk = func(v any) {
		switch x := v.(type) {
		case string:
			parts = append(parts, x)
		case json.Number:
			parts = append(parts, x.String())
		case bool:
			if x {
				parts = append(parts, "true")
			} else {
				parts = append(parts, "false")
			}
		case nil:
			parts = append(parts, "null")
		case []any:
			for _, item := range x {
				walk(item)
			}
		case map[string]any:
			keys := make([]string, 0, len(x))
			for k := range x {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				walk(x[k])
			}
		}
	}
	walk(v)
	return strings.Join(parts, " \n ")
}

// EncounterArgsMatch reports whether every value appears in the arguments.
func EncounterArgsMatch(raw json.RawMessage, values []string) bool {
	if len(values) == 0 {
		return true
	}
	text := EncounterArgText(raw)
	for _, value := range values {
		if !EncounterContains(text, value) {
			return false
		}
	}
	return true
}

// Respond answers one call against the mock world. prior is how many times
// this tool was already called in the case. matched=false means no rule
// applied and the caller serves Fallback.
//
// The first rule that matches decides, in Rules order. When it is a Page
// rule, every matching Page rule of the tool contributes (a read of two links
// returns both pages); a matching Page rule with an Error fails the call with
// the first such error.
func (e *Encounter) Respond(name string, args json.RawMessage, prior int) (result, errText string, matched bool) {
	matches := func(rule EncounterRule) bool {
		return rule.Tool == name && prior >= rule.MinPrior && EncounterArgsMatch(args, rule.Contains)
	}
	for _, rule := range e.Rules {
		if !matches(rule) {
			continue
		}
		if !rule.Page {
			return rule.Result, rule.Error, true
		}
		var pages []string
		for _, page := range e.Rules {
			if !page.Page || !matches(page) {
				continue
			}
			if page.Error != "" {
				return "", page.Error, true
			}
			pages = append(pages, page.Result)
		}
		return strings.Join(pages, "\n\n---\n\n"), "", true
	}
	return "", "", false
}

// EncounterRequiredTools is the deduplicated tool names of the Required
// calls, in first-seen order: the case's ExpectedTools.
func (e *Encounter) EncounterRequiredTools() []string {
	seen := map[string]bool{}
	var out []string
	for _, call := range e.Required {
		if !seen[call.Tool] {
			seen[call.Tool] = true
			out = append(out, call.Tool)
		}
	}
	return out
}
