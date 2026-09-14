package scorer

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 tool grading (issues #1845, #1846, #1847). Everything in this file
// is reachable only through ScoreToolCaseObservedForVersion with
// bench_version >= 13, so the v2..v12 per-case contracts (scoreCaseStrict,
// deterministicToolScoreStrict, argValueEqual) are byte-for-byte unchanged.
//
//   - Argument claims (#1847): when a ToolSpec carries RequiredArgClaims the
//     grader scores the CLAIM (a typed, paraphrase-accepting expectation) in
//     place of exact containment. Honest paraphrase passes; only a claim's
//     Forbidden surface (the distractor party) fails it. argStuffed is kept and
//     the claim count per spec is capped.
//   - Alternative outcomes (#1845): a case may list end-state-equivalent
//     capability sets; the best-scoring one counts, so delete + save earns the
//     same credit as update.
//   - Effect-graded memory reads (#1845): a memory-read case scores 1.0 only
//     when the answer carries the planted value and no non-memory tool was
//     called. The pre-v13 "any non-empty text" routing credit is gone.
//   - Restraint (#1846): a case whose correct move is restraint scores on the
//     TEXT, not merely on the absence of a call — a clarifying question must
//     name the slot AND cite a record token, a grounded no-call answer must be
//     substantive and cite the stored value.
//   - Forbidden tools: an observed call to a case's ForbiddenTools zeroes it.
//   - Group rule: the members of one decision_twin restraint group are scored
//     together (concordant-zero); it ships in SHADOW and only annotates unless
//     the posture is enforce.

// V13Posture is the operator posture for the v13 shadow gates in this file.
type V13Posture string

const (
	V13PostureOff     V13Posture = "off"
	V13PostureShadow  V13Posture = "shadow"
	V13PostureEnforce V13Posture = "enforce"
)

// V13PostureFromEnv reads a posture from an environment variable, defaulting to
// shadow. Anything other than off/shadow/enforce is shadow: the safe end.
func V13PostureFromEnv(name string) V13Posture {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(name))) {
	case string(V13PostureOff):
		return V13PostureOff
	case string(V13PostureEnforce):
		return V13PostureEnforce
	default:
		return V13PostureShadow
	}
}

// maxV13ArgClaims caps the claims scored per ToolSpec; extra keys (sorted) are
// ignored so a spec cannot be made unsatisfiable by claim inflation.
const maxV13ArgClaims = 4

// v13RestraintMinWords is the smallest word count a no-call answer needs to be
// "substantive": a bare "ok" is not an answer to a general-knowledge question.
const v13RestraintMinWords = 3

// v13GroupWrongBelow is the per-case score below which a restraint-group member
// counts as wrong for the concordant-zero rule (the memory Correct threshold).
const v13GroupWrongBelow = 0.5

// scoreToolCaseV13 is the bench_version >= 13 per-case tool grader.
func scoreToolCaseV13(c protocol.ToolCase, resp protocol.RunResponse, ok bool, observed []protocol.ObservedToolCall) protocol.CaseScore {
	auth := resp
	if len(observed) > 0 {
		auth.ToolCalls = observed
	}
	cs := protocol.CaseScore{
		CaseID:          c.ID,
		Category:        c.Category,
		Kind:            protocol.KindTool,
		LatencyMs:       resp.LatencyMs,
		Called:          calledNames(auth.ToolCalls),
		Expected:        expectedNames(c.ExpectedTools),
		AllowExtraTools: c.AllowExtraTools,
		TwinGroup:       c.TwinGroup,
		Relation:        c.TwinRelation,
	}
	if !ok {
		cs.Notes = append(cs.Notes, "no response from harness (error or timeout)")
		cs.Score = 0
		return cs
	}
	if name, hit := v13ForbiddenToolCalled(c.ForbiddenTools, auth.ToolCalls); hit {
		cs.Notes = append(cs.Notes, "v13: forbidden tool called: "+name+" — case scored 0")
		cs.Score = 0
		return cs
	}
	switch {
	case c.Restraint != nil:
		cs.ToolScore, cs.Notes = gradeV13Restraint(c, auth, cs.Notes)
	case c.EffectAnswer != "":
		cs.ToolScore, cs.Notes = gradeV13MemoryEffect(c, auth, cs.Notes)
	case allMemoryTools(c.ExpectedTools):
		// v13 removed the "any non-empty text" routing credit. A memory-read
		// case without a planted effect answer is a generator defect, not a
		// harness one, and it is not credited silently.
		cs.ToolScore = 0
		cs.Notes = append(cs.Notes, "v13: memory-read case carries no effect answer; no routing-only credit")
	case len(c.ExpectedTools) == 0:
		// Retired at v13 (restraint groups replaced the no-tool families); kept
		// as the strict rule so a hand-built case still grades.
		strict := scoreCaseStrict(c, auth, ok, ScopeScored, true)
		cs.ToolScore, cs.Notes = strict.ToolScore, append(cs.Notes, strict.Notes...)
	default:
		best, bestNotes := deterministicToolScoreV13(c, c.ExpectedTools, auth.ToolCalls)
		for _, alternative := range c.AlternativeExpectedTools {
			score, notes := deterministicToolScoreV13(c, alternative, auth.ToolCalls)
			if score > best {
				best, bestNotes = score, append(notes, "v13: an alternative end-state-equivalent outcome scored best")
			}
		}
		cs.ToolScore, cs.Notes = best, append(cs.Notes, bestNotes...)
	}
	cs.Score = cs.ToolScore
	if len(observed) > 0 {
		cs.Observed = true
		cs.Notes = append(cs.Notes, "trajectory observed via tool_endpoint (authoritative)")
	}
	return cs
}

func v13ForbiddenToolCalled(forbidden []string, calls []protocol.ObservedToolCall) (string, bool) {
	if len(forbidden) == 0 {
		return "", false
	}
	banned := make(map[string]bool, len(forbidden))
	for _, name := range forbidden {
		banned[name] = true
	}
	for _, call := range calls {
		if banned[call.Name] {
			return call.Name, true
		}
	}
	return "", false
}

// v13NonMemoryActions counts observed calls that are not harness-internal
// memory tools. Memory tools are never served, so they are never an action.
func v13NonMemoryActions(calls []protocol.ObservedToolCall) []string {
	var out []string
	for _, call := range calls {
		if !memoryTools[call.Name] {
			out = append(out, call.Name)
		}
	}
	return out
}

// v13ResponseText is the graded text: the answer slot and the prose together,
// lowercased. A clarifying question or a grounded answer may live in either.
func v13ResponseText(resp protocol.RunResponse) string {
	return strings.ToLower(strings.TrimSpace(resp.Answer + " " + resp.FinalText))
}

// v13Cites reports whether text carries any of the tokens as a bounded phrase
// (numeric tokens as whole number tokens).
func v13Cites(text string, tokens []string) (string, bool) {
	for _, token := range tokens {
		want := strings.ToLower(strings.TrimSpace(token))
		if want == "" {
			continue
		}
		if isPureNumber(want) {
			if containsNumberToken(text, want) {
				return token, true
			}
			continue
		}
		if containsBoundedPhrase(text, want) {
			return token, true
		}
	}
	return "", false
}

// gradeV13Restraint scores a restraint case on the text of the response: no
// non-memory action, then the kind-specific claim.
func gradeV13Restraint(c protocol.ToolCase, resp protocol.RunResponse, notes []string) (float64, []string) {
	if actions := v13NonMemoryActions(resp.ToolCalls); len(actions) > 0 {
		return 0, append(notes, "v13 restraint: acted instead of restraining ("+strings.Join(actions, ", ")+") — case scored 0")
	}
	text := v13ResponseText(resp)
	claim := c.Restraint
	switch claim.Kind {
	case protocol.RestraintClarifyFirst:
		slot, named := v13Cites(text, claim.Accept)
		if !named {
			return 0, append(notes, "v13 restraint: clarification names no slot (generic \"what would you like?\") — case scored 0")
		}
		if len(claim.Grounding) > 0 {
			if _, grounded := v13Cites(text, claim.Grounding); !grounded {
				return 0, append(notes, "v13 restraint: clarification cites no token from the records searched — case scored 0")
			}
		}
		return 1, append(notes, "v13 restraint: clarifying question names the "+slot+" slot and cites the searched records")
	default: // no_call, decline
		if len(strings.Fields(text)) < v13RestraintMinWords {
			return 0, append(notes, "v13 restraint: no substantive answer — case scored 0")
		}
		if len(claim.Grounding) > 0 {
			if _, grounded := v13Cites(text, claim.Grounding); !grounded {
				return 0, append(notes, "v13 restraint: answer does not cite the stored value — case scored 0")
			}
		}
		return 1, append(notes, "v13 restraint: answered without acting")
	}
}

// v13AnswerCarries reports whether the answer carries a planted value: whole
// number token for a numeric value, bounded phrase otherwise.
func v13AnswerCarries(answer, value string) bool {
	want := strings.ToLower(strings.TrimSpace(value))
	if want == "" {
		return false
	}
	got := strings.ToLower(answer)
	if isPureNumber(want) {
		return containsNumberToken(stripSeparators(got), stripSeparators(want))
	}
	return containsBoundedPhrase(got, want)
}

// gradeV13MemoryEffect scores an effect-graded memory read: no misrouting, the
// answer (slot or prose) carries the planted value, and — for a follow-up read
// — asserts none of the stale pre-mutation values beside it. A hedge that lists
// the old and the new state ("Friday, now maybe Monday") reports the store, not
// the end state, and scores 0.
func gradeV13MemoryEffect(c protocol.ToolCase, resp protocol.RunResponse, notes []string) (float64, []string) {
	if actions := v13NonMemoryActions(resp.ToolCalls); len(actions) > 0 {
		return 0, append(notes, "v13: misrouted a memory request to a non-memory tool: "+strings.Join(actions, ", ")+" — case scored 0")
	}
	if !v13AnswerCarries(resp.Answer, c.EffectAnswer) && !v13AnswerCarries(resp.FinalText, c.EffectAnswer) {
		return 0, append(notes, "v13: answer does not carry the planted value — no routing-only credit")
	}
	for _, stale := range c.EffectForbidden {
		if v13AnswerCarries(resp.Answer, stale) || v13AnswerCarries(resp.FinalText, stale) {
			return 0, append(notes, fmt.Sprintf("v13: answer also asserts the stale pre-mutation value %q — end state not discriminated — case scored 0", stale))
		}
	}
	return 1, append(notes, "v13: answer carries the planted value (effect verified)")
}

// deterministicToolScoreV13 is the v7 strict trajectory rule with claim-aware
// argument grading (#1847). expected may be the canonical set or one of the
// case's alternatives.
func deterministicToolScoreV13(c protocol.ToolCase, expected []protocol.ToolSpec, calls []protocol.ObservedToolCall) (float64, []string) {
	var notes []string
	if forbiddenArgPresent(expected, calls) {
		return 0, append(notes, "v7 strict: forbidden argument present — case scored 0")
	}
	if key, value, hit := v13ForbiddenClaimValue(expected, calls); hit {
		return 0, append(notes, fmt.Sprintf("v13: argument %s carries the forbidden %q — case scored 0", key, value))
	}

	expectedNames := map[string]int{}
	for _, t := range expected {
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
	if truePositive == 0 {
		return 0, append(notes, "no expected tool was called")
	}
	if c.FuzzyTrajectory && truePositive < expectedTotal {
		return 0, append(notes, "v8 fuzzy trajectory: a required capability was not observed — case scored 0")
	}
	prec := ratio(truePositive, observedTotal)
	rec := ratio(truePositive, expectedTotal)
	nameScore := f1(prec, rec)
	if c.AllowExtraTools {
		nameScore = rec
	}

	argF1 := argCorrectnessV13(expected, calls, &notes)
	if c.FuzzyTrajectory {
		if !requiredOutcomesSatisfiedV13(expected, calls, &notes) {
			return 0, append(notes, "v8 fuzzy trajectory: required outcome arguments were not observed — case scored 0")
		}
		argF1 = 1
	}
	order := 1.0
	if !c.Unordered && !c.FuzzyTrajectory {
		order = orderCredit(expectedNames, expected, calls)
	}
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
	penalty *= 2
	if penalty > 1 {
		penalty = 1
	}
	trajectory := order * (1 - penalty)
	score := wName*nameScore + wArg*argF1 + wTrajectory*trajectory
	if !c.Unordered && !c.FuzzyTrajectory && order < 1 {
		score *= order
		notes = append(notes, "v7 strict: out-of-order multi-hop — score multiplied by order credit")
	}
	return clamp01(round6(score)), notes
}

// v13ClaimKeys returns the graded claim keys of a spec: the union of RequiredArgs
// and RequiredArgClaims keys, sorted, capped at maxV13ArgClaims claims.
func v13ClaimKeys(spec protocol.ToolSpec) []string {
	set := map[string]bool{}
	for key := range spec.RequiredArgs {
		set[key] = true
	}
	claimKeys := make([]string, 0, len(spec.RequiredArgClaims))
	for key := range spec.RequiredArgClaims {
		claimKeys = append(claimKeys, key)
	}
	sort.Strings(claimKeys)
	if len(claimKeys) > maxV13ArgClaims {
		claimKeys = claimKeys[:maxV13ArgClaims]
	}
	for _, key := range claimKeys {
		set[key] = true
	}
	keys := make([]string, 0, len(set))
	for key := range set {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// v13ArgSatisfied grades one argument: the claim when the spec carries one for
// the key (within the cap), otherwise the frozen v8 outcome comparison.
func v13ArgSatisfied(spec protocol.ToolSpec, key string, got any) bool {
	if claim, ok := spec.RequiredArgClaims[key]; ok && v13ClaimGraded(spec, key) {
		return argClaimSatisfied(got, claim)
	}
	want, ok := spec.RequiredArgs[key]
	if !ok {
		return false
	}
	return fuzzyOutcomeArgEqual(got, want)
}

func v13ClaimGraded(spec protocol.ToolSpec, key string) bool {
	keys := make([]string, 0, len(spec.RequiredArgClaims))
	for k := range spec.RequiredArgClaims {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for i, k := range keys {
		if k == key {
			return i < maxV13ArgClaims
		}
	}
	return false
}

// v13ForbiddenClaimValue reports whether ANY observed call to an expected tool
// carries a claim's Forbidden surface in the claimed argument. It is checked
// across every call, not only the best one, so "clean up" trajectories that
// touch a protected record on the way to the right one still fail.
func v13ForbiddenClaimValue(expected []protocol.ToolSpec, calls []protocol.ObservedToolCall) (string, string, bool) {
	for _, spec := range expected {
		for _, key := range v13ClaimKeys(spec) {
			claim, ok := spec.RequiredArgClaims[key]
			if !ok || len(claim.Forbidden) == 0 {
				continue
			}
			for _, call := range calls {
				if call.Name != spec.Name {
					continue
				}
				got, present := parseArgs(call.Args)[key]
				if !present {
					continue
				}
				if value, hit := v13Cites(v13ArgText(got), claim.Forbidden); hit {
					return key, value, true
				}
			}
		}
	}
	return "", "", false
}

// requiredOutcomesSatisfiedV13 is requiredOutcomesSatisfied with claim-aware
// argument grading; it keeps the retry-friendly "one call satisfies every
// required value" rule.
func requiredOutcomesSatisfiedV13(expected []protocol.ToolSpec, calls []protocol.ObservedToolCall, notes *[]string) bool {
	for _, spec := range expected {
		keys := v13ClaimKeys(spec)
		if len(keys) == 0 {
			continue
		}
		satisfied := false
		for _, call := range calls {
			if call.Name != spec.Name {
				continue
			}
			args := parseArgs(call.Args)
			matches := true
			for _, key := range keys {
				got, ok := args[key]
				if !ok || !v13ArgSatisfied(spec, key, got) {
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

// argCorrectnessV13 is argCorrectness with claim-aware value grading.
func argCorrectnessV13(expected []protocol.ToolSpec, calls []protocol.ObservedToolCall, notes *[]string) float64 {
	totalExpected, totalObservedRequired, correct := 0, 0, 0
	used := make([]bool, len(calls))
	for _, et := range expected {
		keys := v13ClaimKeys(et)
		idx := -1
		for i, o := range calls {
			if !used[i] && o.Name == et.Name {
				idx = i
				break
			}
		}
		if idx == -1 {
			totalExpected += len(keys)
			continue
		}
		used[idx] = true
		observedArgs := parseArgs(calls[idx].Args)
		for _, key := range keys {
			totalExpected++
			got, ok := observedArgs[key]
			if !ok {
				continue
			}
			totalObservedRequired++
			if v13ArgSatisfied(et, key, got) {
				correct++
			} else {
				*notes = append(*notes, "wrong value for arg "+key)
			}
		}
		for _, forbidden := range et.ForbiddenArgs {
			if _, ok := observedArgs[forbidden]; ok {
				*notes = append(*notes, "forbidden arg present: "+forbidden)
				totalObservedRequired++
			}
		}
	}
	if totalExpected == 0 && totalObservedRequired == 0 {
		return 1.0
	}
	return f1(ratio(correct, totalObservedRequired), ratio(correct, totalExpected))
}

// v13ArgText renders an observed argument value as lowercased text.
func v13ArgText(got any) string {
	var text string
	switch v := got.(type) {
	case string:
		text = v
	default:
		b, err := json.Marshal(v)
		if err != nil {
			return ""
		}
		text = string(b)
	}
	return strings.ToLower(strings.TrimSpace(text))
}

// v13Canonical lowercases and strips everything but letters and digits, so an
// enum or identifier compares across case, spacing, and punctuation.
func v13Canonical(s string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(s) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// v13EmailAddresses extracts the mailbox tokens of a recipient string:
// "Dana <dana@x.com>, ops@x.com" yields both addresses.
func v13EmailAddresses(text string) []string {
	fields := strings.FieldsFunc(text, func(r rune) bool {
		switch r {
		case ' ', ',', ';', '<', '>', '(', ')', '"', '\'', '[', ']', '\n', '\t':
			return true
		}
		return false
	})
	var out []string
	for _, field := range fields {
		if strings.Count(field, "@") == 1 {
			out = append(out, strings.Trim(field, ".:"))
		}
	}
	return out
}

// v13SplitFact splits "handoff is Monday" into ("handoff", "monday"); a fact
// without " is " is (fact, "").
func v13SplitFact(fact string) (slot, value string) {
	fact = strings.ToLower(strings.TrimSpace(fact))
	if i := strings.Index(fact, " is "); i > 0 {
		return strings.TrimSpace(fact[:i]), strings.TrimSpace(fact[i+4:])
	}
	return fact, ""
}

// v13FactCarried reports whether text states the fact: it carries the new value
// AND the slot noun, in any copula/colon/arrow/sentence form. The slot noun may
// be inflected ("handoff" ⊂ "handoffs"), so it is matched as a substring on a
// word start; the value is bounded.
func v13FactCarried(text, fact string) bool {
	slot, value := v13SplitFact(fact)
	if value == "" {
		return containsBoundedPhrase(text, slot)
	}
	if !v13CarriesValue(text, value) {
		return false
	}
	for _, word := range strings.FieldsFunc(text, func(r rune) bool { return !(r >= 'a' && r <= 'z') && !(r >= '0' && r <= '9') }) {
		if strings.HasPrefix(word, slot) {
			return true
		}
	}
	return false
}

func v13CarriesValue(text, value string) bool {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" {
		return false
	}
	if isPureNumber(value) {
		return containsNumberToken(text, value)
	}
	return containsBoundedPhrase(text, value)
}

// argClaimSatisfied is the v13 claim grader (#1847). It accepts an honest
// paraphrase of the semantic value and fails only on the claim's Forbidden
// surface or on candidate-stuffing; the kinds are the vocabulary the v13
// generator emits.
func argClaimSatisfied(got any, claim protocol.Claim) bool {
	text := v13ArgText(got)
	if text == "" {
		return false
	}
	expected := strings.ToLower(strings.TrimSpace(claim.Expected))
	if expected == "" {
		return false
	}
	if _, hit := v13Cites(text, claim.Forbidden); hit {
		return false
	}
	if argStuffed(text, expected) {
		return false
	}
	accepts := append([]string{claim.Expected}, claim.Accept...)
	switch claim.Kind {
	case "email":
		want := v13Canonical(expected)
		for _, address := range v13EmailAddresses(text) {
			if v13Canonical(address) == want {
				return true
			}
		}
		return false
	case "enum", "id":
		got := v13Canonical(text)
		for _, accept := range accepts {
			if got == v13Canonical(accept) {
				return true
			}
		}
		return false
	case "set":
		for _, item := range strings.Split(expected, ";") {
			item = strings.TrimSpace(item)
			if item == "" {
				continue
			}
			if strings.Contains(item, "@") {
				found := false
				for _, address := range v13EmailAddresses(text) {
					if v13Canonical(address) == v13Canonical(item) {
						found = true
						break
					}
				}
				if !found {
					return false
				}
				continue
			}
			if !v13CarriesValue(text, item) {
				return false
			}
		}
		return true
	case "fact_update":
		for _, accept := range accepts {
			if strings.Contains(accept, " is ") && v13FactCarried(text, accept) {
				return true
			}
		}
		return v13FactCarried(text, expected)
	case "facts":
		for _, fact := range strings.Split(expected, ";") {
			if strings.TrimSpace(fact) == "" {
				continue
			}
			if !v13FactCarried(text, fact) {
				return false
			}
		}
		return true
	default: // entity and anything unrecognized: bounded containment of any accepted form
		for _, accept := range accepts {
			if v13CarriesValue(text, accept) {
				return true
			}
		}
		return false
	}
}

// V13RestraintEvidence is the broker-side evidence the restraint provenance
// rule reads: the model-emitted calls the harness never executed while this
// case ran, and whether the tempting tool was offered on the deciding turn
// (nil = the relay did not record the offered catalog for this case).
type V13RestraintEvidence struct {
	ModelSelectedNotExecuted int
	CatalogPresent           *bool
}

// ApplyV13RestraintProvenance is the symmetric-provenance half of #1846: a
// restraint case is credited on what the MODEL decided, not on what the host
// let through. Two findings:
//
//   - swallowed_model_call: a model-emitted call the harness never executed.
//     On the scored path this is NOT new at v13 — the v10 provenance gate
//     (applyV10ToolProvenance, ModelSelectedNotExecuted > 0) already zeroes
//     every tool case that shows it and runs before this rule, so here it is
//     a named annotation on a score the v10 gate already moved. The posture
//     only decides what this rule does with an unscored (practice) run.
//   - restraint_without_offer: a deciding turn that never offered the
//     tempting tool. This is the genuinely new v13 finding; the evidence is
//     nil (unknown, never a finding) until the relay records the offered
//     catalog per completion.
//
// Under enforce a finding zeroes the case; under shadow it is annotated only;
// off leaves the case untouched.
func ApplyV13RestraintProvenance(benchVersion int, c protocol.ToolCase, cs protocol.CaseScore, evidence V13RestraintEvidence, posture V13Posture) protocol.CaseScore {
	if benchVersion < protocol.BenchVersionV13 || c.Restraint == nil || posture == V13PostureOff {
		return cs
	}
	var findings []string
	if evidence.ModelSelectedNotExecuted > 0 {
		finding := "swallowed_model_call"
		if cs.ToolScore == 0 {
			finding += " (already zeroed by v10 provenance)"
		}
		findings = append(findings, finding)
	}
	if evidence.CatalogPresent != nil && !*evidence.CatalogPresent {
		findings = append(findings, "restraint_without_offer")
	}
	if len(findings) == 0 {
		return cs
	}
	if posture == V13PostureEnforce {
		cs.ToolScore, cs.Score = 0, 0
		cs.Notes = append(cs.Notes, "v13 restraint provenance: "+strings.Join(findings, ", ")+" — case scored 0")
		return cs
	}
	cs.Notes = append(cs.Notes, "v13 restraint provenance (shadow): "+strings.Join(findings, ", "))
	return cs
}

// ApplyV13RestraintGroupRule scores the members of each decision_twin
// restraint group together: if any member is wrong (Score below
// v13GroupWrongBelow), every member scores 0 under enforce (concordant-zero) —
// an always-ask, always-act, or random-split rule is wrong on some member of
// every group, so it earns nothing from the slice, while a harness that reads
// the record keeps every member. Shadow annotates the verdict without changing
// scores; off is a no-op. Pre-v13 reports are untouched.
func ApplyV13RestraintGroupRule(benchVersion int, perCase []protocol.CaseScore, posture V13Posture) []protocol.CaseScore {
	if benchVersion < protocol.BenchVersionV13 || posture == V13PostureOff {
		return perCase
	}
	groups := map[string][]int{}
	for i, cs := range perCase {
		if cs.Kind != protocol.KindTool || cs.TwinGroup == "" || cs.Relation != protocol.TwinRelationDecision {
			continue
		}
		groups[cs.TwinGroup] = append(groups[cs.TwinGroup], i)
	}
	out := append([]protocol.CaseScore(nil), perCase...)
	for _, members := range groups {
		if len(members) < 2 {
			continue
		}
		wrong := 0
		for _, i := range members {
			if out[i].Score < v13GroupWrongBelow {
				wrong++
			}
		}
		if wrong == 0 {
			continue
		}
		for _, i := range members {
			if posture == V13PostureEnforce {
				out[i].Score, out[i].ToolScore = 0, 0
				out[i].Notes = append(out[i].Notes, fmt.Sprintf("v13 restraint group: %d of %d members wrong — concordant-zero", wrong, len(members)))
			} else {
				out[i].Notes = append(out[i].Notes, fmt.Sprintf("v13 restraint group (shadow): %d of %d members wrong", wrong, len(members)))
			}
		}
	}
	return out
}
