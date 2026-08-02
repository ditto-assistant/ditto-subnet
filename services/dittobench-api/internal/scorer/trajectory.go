package scorer

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Deterministic tool-trajectory scoring. Ported from the backend's
// pkg/dittobench/scorers/toolcall.go (name-F1 / arg-F1 / trajectory), extended
// with explicit order credit for multi-hop expected sequences. The composite
// weights name-F1, arg-F1, and the trajectory term (order × extra-call
// discipline) 0.4 / 0.4 / 0.2.
const (
	wName       = 0.4
	wArg        = 0.4
	wTrajectory = 0.2
)

// deterministicToolScore compares an observed trajectory against a case's
// expected tools, returning a [0,1] score and human-readable notes. It assumes
// the caller has already handled the no-expected-tool and no-response cases.
func deterministicToolScore(c protocol.ToolCase, calls []protocol.ObservedToolCall) (float64, []string) {
	return deterministicToolScoreStrict(c, calls, false)
}

// deterministicToolScoreStrict is deterministicToolScore with the v7 strict
// contract (strict=true, bench_version >= 7):
//
//   - A FORBIDDEN argument on any expected tool's call zeroes the whole case.
//     Pre-v7 it only counted against arg precision, so a harness could carry a
//     banned selector and still keep most of its credit.
//   - Hop ORDER multiplies the whole score for ordered multi-hop cases, not
//     just the 0.2-weight trajectory term: an out-of-order chain cannot hide
//     behind name/arg F1.
//   - The extra-call / over-budget penalty is DOUBLED before it enters the
//     trajectory term.
//
// strict=false reproduces the historical scoring byte-for-byte.
func deterministicToolScoreStrict(c protocol.ToolCase, calls []protocol.ObservedToolCall, strict bool) (float64, []string) {
	var notes []string

	if strict && forbiddenArgPresent(c.ExpectedTools, calls) {
		return 0, append(notes, "v7 strict: forbidden argument present — case scored 0")
	}

	expectedNames := map[string]int{}
	for _, t := range c.ExpectedTools {
		expectedNames[t.Name]++
	}
	observedNames := map[string]int{}
	for _, o := range calls {
		observedNames[o.Name]++
	}

	truePositive, expectedTotal, observedTotal := 0, 0, 0
	for name, needed := range expectedNames {
		truePositive += min(observedNames[name], needed)
		expectedTotal += needed
	}
	for _, v := range observedNames {
		observedTotal += v
	}

	// Nothing the case asked for was called → no deterministic credit.
	if truePositive == 0 {
		return 0, append(notes, "no expected tool was called")
	}
	if c.FuzzyTrajectory && truePositive < expectedTotal {
		return 0, append(notes, "v8 fuzzy trajectory: a required capability was not observed — case scored 0")
	}

	prec := ratio(truePositive, observedTotal)
	rec := ratio(truePositive, expectedTotal)
	nameScore := f1(prec, rec)
	// When extra calls are explicitly allowed, judge names on recall only so a
	// permitted extra call does not depress precision.
	if c.AllowExtraTools {
		nameScore = rec
	}

	argF1 := argCorrectness(c.ExpectedTools, calls, &notes)
	if c.FuzzyTrajectory {
		if !requiredOutcomesSatisfied(c.ExpectedTools, calls, &notes) {
			return 0, append(notes, "v8 fuzzy trajectory: required outcome arguments were not observed — case scored 0")
		}
		// Fuzzy tasks accept a corrected retry. Once one call for each outcome
		// tool carries every required value, an earlier exploratory attempt does
		// not dilute the final outcome's argument score.
		argF1 = 1
	}
	// Parallel (Unordered) cases: the expected calls are independent, so relative
	// order carries no signal. Fuzzy trajectories are dependent tasks, but their
	// data/outcome gates carry the dependency rather than one prescribed order.
	order := 1.0
	if !c.Unordered && !c.FuzzyTrajectory {
		order = orderCredit(expectedNames, c.ExpectedTools, calls)
	}

	// Extra-call / over-budget penalty (scales with call count, unlike v1's flat
	// 0.1). Skipped entirely when the case allows extra tools.
	penalty := 0.0
	if !c.AllowExtraTools {
		if c.MaxToolCalls > 0 && observedTotal > c.MaxToolCalls {
			penalty = ratioF(observedTotal-c.MaxToolCalls, c.MaxToolCalls)
		}
		extras := 0
		for name, got := range observedNames {
			if e := got - expectedNames[name]; e > 0 {
				extras += e
			}
		}
		if expectedTotal > 0 && extras > 0 {
			if p := ratioF(extras, expectedTotal); p > penalty {
				penalty = p
			}
			notes = append(notes, fmt.Sprintf("%d extra/unexpected tool call(s)", extras))
		}
	}
	if strict {
		penalty *= 2 // v7: over-budget/extra calls cost double
	}
	if penalty > 1 {
		penalty = 1
	}

	trajectory := order * (1 - penalty)
	score := wName*nameScore + wArg*argF1 + wTrajectory*trajectory
	if strict && !c.Unordered && !c.FuzzyTrajectory && order < 1 {
		// v7: hop order gates the WHOLE score for ordered multi-hop cases. The
		// trajectory term already carries order at 0.2 weight; multiplying again
		// is intentional — a fully out-of-order chain scores 0.
		score *= order
		notes = append(notes, "v7 strict: out-of-order multi-hop — score multiplied by order credit")
	}
	return clamp01(round6(score)), notes
}

// requiredOutcomesSatisfied reports whether every expected tool with required
// arguments has at least one observed call satisfying all of them. It is
// intentionally retry-friendly: a fuzzy agent may make an exploratory or
// malformed first attempt, inspect the error, and then perform the right action.
func requiredOutcomesSatisfied(expected []protocol.ToolSpec, calls []protocol.ObservedToolCall, notes *[]string) bool {
	for _, spec := range expected {
		if len(spec.RequiredArgs) == 0 {
			continue
		}
		satisfied := false
		for _, call := range calls {
			if call.Name != spec.Name {
				continue
			}
			args := parseArgs(call.Args)
			matches := true
			for _, key := range sortedArgKeys(spec.RequiredArgs) {
				got, ok := args[key]
				if !ok || !fuzzyOutcomeArgEqual(got, spec.RequiredArgs[key]) {
					matches = false
					break
				}
			}
			if matches {
				satisfied = true
				break
			}
		}
		if !satisfied {
			*notes = append(*notes, "no "+spec.Name+" call satisfied the required outcome arguments")
			return false
		}
	}
	return true
}

// fuzzyOutcomeArgEqual retains the frozen argument comparison and additionally
// accepts a required numeric value as a punctuation-delimited token inside a
// natural action payload (for example an email body ending "4,219."). The v7
// comparator treats that final period as a decimal attachment; this v8-only
// wrapper fixes the prose outcome without changing historical scores.
func fuzzyOutcomeArgEqual(got any, want string) bool {
	if argValueEqual(got, want) {
		return true
	}
	want = strings.ToLower(strings.TrimSpace(want))
	if !isPureNumber(want) {
		return false
	}
	var gotStr string
	switch value := got.(type) {
	case string:
		gotStr = value
	default:
		encoded, err := json.Marshal(value)
		if err != nil {
			return false
		}
		gotStr = string(encoded)
	}
	for _, token := range strings.Fields(strings.ToLower(gotStr)) {
		if strings.Trim(token, `"'()[]{}!?;:.`) == want {
			return true
		}
	}
	return false
}

// forbiddenArgPresent reports whether any observed call to an expected tool
// carries one of that tool's forbidden arguments. Used only by the v7 strict
// contract, where a forbidden argument zeroes the case.
func forbiddenArgPresent(expected []protocol.ToolSpec, calls []protocol.ObservedToolCall) bool {
	for _, et := range expected {
		if len(et.ForbiddenArgs) == 0 {
			continue
		}
		for _, o := range calls {
			if o.Name != et.Name {
				continue
			}
			observedArgs := parseArgs(o.Args)
			for _, forbidden := range et.ForbiddenArgs {
				if _, present := observedArgs[forbidden]; present {
					return true
				}
			}
		}
	}
	return false
}

// argCorrectness scores required/forbidden arguments over the matched expected
// tools (F1 of value-correct required args). Returns 1.0 when no required args
// are specified. Appends notes for forbidden-arg violations.
func argCorrectness(expected []protocol.ToolSpec, calls []protocol.ObservedToolCall, notes *[]string) float64 {
	totalExpected, totalObservedRequired, correct := 0, 0, 0
	used := make([]bool, len(calls))
	for _, et := range expected {
		idx := -1
		for i, o := range calls {
			if !used[i] && o.Name == et.Name {
				idx = i
				break
			}
		}
		if idx == -1 {
			totalExpected += len(et.RequiredArgs)
			continue
		}
		used[idx] = true
		observedArgs := parseArgs(calls[idx].Args)
		for _, key := range sortedArgKeys(et.RequiredArgs) {
			totalExpected++
			got, ok := observedArgs[key]
			if !ok {
				continue
			}
			totalObservedRequired++
			if argValueEqual(got, et.RequiredArgs[key]) {
				correct++
			} else {
				*notes = append(*notes, "wrong value for arg "+key)
			}
		}
		for _, forbidden := range et.ForbiddenArgs {
			if _, ok := observedArgs[forbidden]; ok {
				*notes = append(*notes, "forbidden arg present: "+forbidden)
				totalObservedRequired++ // counts against precision
			}
		}
	}
	if totalExpected == 0 && totalObservedRequired == 0 {
		return 1.0 // no argument expectations → full credit
	}
	return f1(ratio(correct, totalObservedRequired), ratio(correct, totalExpected))
}

// orderCredit rewards a multi-hop trajectory for calling the expected tools in
// the expected relative order: the fraction of consecutive expected pairs
// (e[i], e[i+1]) whose first observed occurrences appear in that order. Single-
// tool expectations have no ordering constraint and score 1.0.
func orderCredit(expectedNames map[string]int, expected []protocol.ToolSpec, calls []protocol.ObservedToolCall) float64 {
	if len(expected) < 2 {
		return 1.0
	}
	firstIdx := map[string]int{}
	for i, o := range calls {
		if _, seen := firstIdx[o.Name]; !seen {
			firstIdx[o.Name] = i
		}
	}
	pairs, satisfied := 0, 0
	for i := 0; i+1 < len(expected); i++ {
		a, b := expected[i].Name, expected[i+1].Name
		pairs++
		ia, oka := firstIdx[a]
		ib, okb := firstIdx[b]
		if oka && okb && ia < ib {
			satisfied++
		}
	}
	if pairs == 0 {
		return 1.0
	}
	return float64(satisfied) / float64(pairs)
}

func parseArgs(raw json.RawMessage) map[string]any {
	if len(raw) == 0 || strings.TrimSpace(string(raw)) == "" {
		return map[string]any{}
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return map[string]any{}
	}
	return m
}

// argValueEqual compares an observed arg value (of any JSON type) to the
// expected string. Strings compare case-insensitively after trimming; other
// types compare via their JSON encoding. Containment is allowed so a query arg
// that embeds the required entity ("news about Tokyo" ⊇ "Tokyo") still credits.
func argValueEqual(got any, want string) bool {
	want = strings.ToLower(strings.TrimSpace(want))
	if want == "" {
		return false // an empty required value is undefined; never auto-credit
	}
	var gotStr string
	switch v := got.(type) {
	case string:
		gotStr = v
	default:
		b, err := json.Marshal(v)
		if err != nil {
			return false
		}
		gotStr = string(b)
	}
	gotStr = strings.ToLower(strings.TrimSpace(gotStr))
	// A numeric expected value matches only as a whole number token, so "5" is
	// not credited by an observed "5.5", "-5", or "15" (same boundary rule the
	// memory grader uses). A non-numeric value uses whole-token/phrase
	// containment (not raw substring) so a short enum like "low" is not satisfied
	// by "yellow"/"below".
	if isPureNumber(want) {
		return containsNumberToken(gotStr, want)
	}
	if gotStr == want {
		return true
	}
	// Containment credits a value naturally embedded in a phrase ("news about
	// Tokyo" ⊇ "Tokyo"), but must not credit ARGUMENT STUFFING — packing many
	// candidate pool values into one arg so the right one is present by brute
	// force ("Tokyo Paris London Berlin ..."). A genuine phrasing of a single
	// entity stays short relative to the entity; reject a containment match when
	// the observed value is grossly longer than the expected token.
	if argStuffed(gotStr, want) {
		return false
	}
	return containsBoundedPhrase(gotStr, want)
}

// argStuffingRatio is how many times longer than the expected value an observed
// arg may be and still count as a natural phrasing rather than stuffing. A
// natural embedding ("the latest news about Tokyo" for "Tokyo") stays well
// under this; a brute-force candidate list blows past it.
const argStuffingRatio = 8

// argStuffed reports whether an observed arg looks like candidate-stuffing
// around the expected value: much longer than the value AND carrying several
// comma/space-separated tokens (the shape of a packed candidate list). Short
// natural phrases are never flagged.
func argStuffed(got, want string) bool {
	if len(got) <= argStuffingRatio*len(want) {
		return false
	}
	return len(strings.Fields(got)) >= 6
}

func sortedArgKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys) // deterministic order (map iteration is randomized)
	return keys
}

func ratio(num, denom int) float64 {
	if denom <= 0 {
		return 0
	}
	return float64(num) / float64(denom)
}

func ratioF(num, denom int) float64 { return ratio(num, denom) }

func f1(p, r float64) float64 {
	if p+r == 0 {
		return 0
	}
	return 2 * p * r / (p + r)
}
