package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// ---- v7 per-case strictness ----

func TestUnobservedCeilingForVersion(t *testing.T) {
	// Pre-v7 contracts keep the historical ceilings.
	for _, v := range []int{2, 3, 4, 5, 6} {
		if got := UnobservedCeilingForVersion(ScopePractice, v); got != UnobservedCeiling {
			t.Fatalf("v%d practice ceiling should be %v, got %v", v, UnobservedCeiling, got)
		}
		if got := UnobservedCeilingForVersion(ScopeScored, v); got != 0 {
			t.Fatalf("v%d scored ceiling should be 0, got %v", v, got)
		}
	}
	// v7: practice selection-only ceiling drops 10x; scored stays 0.
	if got := UnobservedCeilingForVersion(ScopePractice, protocol.BenchVersionV7); got != V7UnobservedCeiling {
		t.Fatalf("v7 practice ceiling should be %v, got %v", V7UnobservedCeiling, got)
	}
	if V7UnobservedCeiling*10 != UnobservedCeiling {
		t.Fatalf("v7 ceiling must be exactly 10x below the historical one: %v vs %v",
			V7UnobservedCeiling, UnobservedCeiling)
	}
	if got := UnobservedCeilingForVersion(ScopeScored, protocol.BenchVersionV7); got != 0 {
		t.Fatalf("v7 scored ceiling should be 0, got %v", got)
	}
}

func TestCapUnobservedForVersion(t *testing.T) {
	hi := protocol.CaseScore{Score: 1.0}
	// Pre-v7 byte-identical to CapUnobservedScope.
	if got := CapUnobservedForVersion(hi, ScopePractice, 6).Score; got != CapUnobservedScope(hi, ScopePractice).Score {
		t.Fatalf("v6 cap must equal CapUnobservedScope, got %v", got)
	}
	// v7 practice: capped at 0.05.
	if got := CapUnobservedForVersion(hi, ScopePractice, 7).Score; got != V7UnobservedCeiling {
		t.Fatalf("v7 practice cap should be %v, got %v", V7UnobservedCeiling, got)
	}
	// v7 scored: 0.
	if got := CapUnobservedForVersion(hi, ScopeScored, 7).Score; got != 0 {
		t.Fatalf("v7 scored cap should be 0, got %v", got)
	}
	// No-op below the ceiling.
	lo := protocol.CaseScore{Score: 0.01}
	if got := CapUnobservedForVersion(lo, ScopePractice, 7).Score; got != 0.01 {
		t.Fatalf("cap must not raise a low score, got %v", got)
	}
}

func TestComposeResultUsageForVersionPreV7Identity(t *testing.T) {
	base := protocol.CaseScore{ToolScore: 1.0}
	for _, v := range []int{2, 5, 6} {
		got := ComposeResultUsageForVersion(v, base, "answer with 3,418", "3,418", "7,902")
		want := ComposeResultUsageWithDecoy(base, "answer with 3,418", "3,418", "7,902")
		if got.Score != want.Score || got.ResultUsage != want.ResultUsage {
			t.Fatalf("v%d must delegate to the historical composition: got %v/%v want %v/%v",
				v, got.Score, got.ResultUsage, want.Score, want.ResultUsage)
		}
		miss := ComposeResultUsageForVersion(v, base, "no needle here", "3,418", "")
		if miss.Score != 0.4 {
			t.Fatalf("v%d right-tool-no-needle must keep the 0.4 trajectory half, got %v", v, miss.Score)
		}
	}
}

func TestComposeResultUsageV7MultiplicativeGate(t *testing.T) {
	base := protocol.CaseScore{ToolScore: 1.0}
	// Oracle: right trajectory + needle → still full marks.
	hit := ComposeResultUsageForVersion(7, base, "It reached 3,418 points.", "3,418", "7,902")
	if hit.Score != 1.0 || hit.ResultUsage != 1.0 {
		t.Fatalf("v7 oracle must score 1.0, got %v (usage %v)", hit.Score, hit.ResultUsage)
	}
	// Right tool, ignored result → 0.1 (vs the v6 0.4): 4x harder.
	miss := ComposeResultUsageForVersion(7, base, "here's a guess: 999", "3,418", "7,902")
	if miss.Score != v7ResultUsageMissGate || miss.ResultUsage != 0 {
		t.Fatalf("v7 needle miss should keep only %v of trajectory, got %v", v7ResultUsageMissGate, miss.Score)
	}
	// Decoy carried → the WHOLE case zeroes, trajectory included.
	decoy := ComposeResultUsageForVersion(7, base, "The figure is 7,902.", "3,418", "7,902")
	if decoy.Score != 0 || decoy.ResultUsage != 0 {
		t.Fatalf("v7 decoy answer must zero the case, got %v", decoy.Score)
	}
	// Decoy + needle (grepping both) still zeroes.
	both := ComposeResultUsageForVersion(7, base, "Maybe 3,418 or 7,902.", "3,418", "7,902")
	if both.Score != 0 {
		t.Fatalf("v7 decoy+needle answer must zero the case, got %v", both.Score)
	}
	// Partial trajectory scales through the gate.
	partial := ComposeResultUsageForVersion(7, protocol.CaseScore{ToolScore: 0.8}, "It is 3,418.", "3,418", "")
	if partial.Score != 0.8 {
		t.Fatalf("v7 needle hit should equal the trajectory (0.8), got %v", partial.Score)
	}
}

func TestApplyTrajectoryMismatch(t *testing.T) {
	cs := protocol.CaseScore{Score: 1.0}
	observed := []protocol.ObservedToolCall{{Name: "search_web"}}
	honest := []protocol.ObservedToolCall{{Name: "search_web"}}
	lie := []protocol.ObservedToolCall{{Name: "get_weather"}}

	// Pre-v7: no-op even on a blatant mismatch.
	if got := ApplyTrajectoryMismatch(6, cs, lie, observed).Score; got != 1.0 {
		t.Fatalf("pre-v7 mismatch must be a no-op, got %v", got)
	}
	// v7: agreement (order-insensitive multiset) keeps the score.
	if got := ApplyTrajectoryMismatch(7, cs, honest, observed).Score; got != 1.0 {
		t.Fatalf("v7 honest self-report must keep its score, got %v", got)
	}
	// v7: a fabricated self-report halves the score.
	if got := ApplyTrajectoryMismatch(7, cs, lie, observed).Score; got != 0.5 {
		t.Fatalf("v7 mismatch should halve the score, got %v", got)
	}
	// Hidden call (self-report shorter than observed) is a mismatch too.
	hidden := []protocol.ObservedToolCall{{Name: "search_web"}, {Name: "gmail_send"}}
	if got := ApplyTrajectoryMismatch(7, cs, honest, hidden).Score; got != 0.5 {
		t.Fatalf("v7 hidden observed call should halve the score, got %v", got)
	}
	// Empty self-report is "no claim": not penalized.
	if got := ApplyTrajectoryMismatch(7, cs, nil, observed).Score; got != 1.0 {
		t.Fatalf("v7 empty self-report must not be penalized, got %v", got)
	}
	// Nothing observed: nothing to compare.
	if got := ApplyTrajectoryMismatch(7, cs, lie, nil).Score; got != 1.0 {
		t.Fatalf("v7 unobserved case must not be penalized here, got %v", got)
	}
}

func TestApplyTrajectoryMismatchForFuzzyV8IgnoresCompactSelfReport(t *testing.T) {
	cs := protocol.CaseScore{Score: 1.0}
	c := protocol.ToolCase{FuzzyTrajectory: true}
	self := []protocol.ObservedToolCall{{Name: "gmail_send"}}
	observed := []protocol.ObservedToolCall{{Name: "search_memories"}, {Name: "search_web"}, {Name: "gmail_send"}}
	if got := ApplyTrajectoryMismatchForCase(protocol.BenchVersionV8, c, cs, self, observed).Score; got != 1 {
		t.Fatalf("fuzzy v8 compact self-report was penalized: %v", got)
	}
	if got := ApplyTrajectoryMismatchForCase(protocol.BenchVersionV7, c, cs, self, observed).Score; got != 0.5 {
		t.Fatalf("v7 mismatch compatibility changed: %v", got)
	}
}

// ---- v7 strict deterministic trajectory ----

func TestV7StrictForbiddenArgZeroesCase(t *testing.T) {
	c := protocol.ToolCase{
		ID:       "t1",
		Category: "web_search",
		ExpectedTools: []protocol.ToolSpec{{
			Name:          "search_web",
			RequiredArgs:  map[string]string{"query": "tokyo"},
			ForbiddenArgs: []string{"raw_dump"},
		}},
	}
	calls := []protocol.ObservedToolCall{{
		Name: "search_web",
		Args: []byte(`{"query":"tokyo","raw_dump":true}`),
	}}
	resp := protocol.RunResponse{ToolCalls: calls}

	// Historical: the forbidden arg only dents arg precision.
	v6 := ScoreToolCaseObservedScope(c, protocol.RunResponse{}, true, calls, ScopePractice)
	if v6.ToolScore <= 0.5 {
		t.Fatalf("precondition: v6 forbidden-arg case should keep most credit, got %v", v6.ToolScore)
	}
	// v7 strict: the case zeroes.
	v7 := ScoreToolCaseObservedForVersion(c, protocol.RunResponse{}, true, calls, ScopePractice, 7)
	if v7.ToolScore != 0 || v7.Score != 0 {
		t.Fatalf("v7 forbidden-arg case must score 0, got %v", v7.ToolScore)
	}
	// A clean call is unaffected by strictness.
	clean := []protocol.ObservedToolCall{{Name: "search_web", Args: []byte(`{"query":"tokyo"}`)}}
	v7clean := ScoreToolCaseObservedForVersion(c, resp, true, clean, ScopePractice, 7)
	if v7clean.ToolScore != 1.0 {
		t.Fatalf("v7 clean call should score 1.0, got %v", v7clean.ToolScore)
	}
}

func TestV7StrictOrderGatesWholeScore(t *testing.T) {
	c := protocol.ToolCase{
		ID:       "hop-1",
		Category: "multi_hop",
		ExpectedTools: []protocol.ToolSpec{
			{Name: "search_web"},
			{Name: "fetch_page"},
		},
	}
	reversed := []protocol.ObservedToolCall{{Name: "fetch_page"}, {Name: "search_web"}}

	v6 := ScoreToolCaseObservedScope(c, protocol.RunResponse{}, true, reversed, ScopePractice)
	// Historical: order failure only zeroes the 0.2 trajectory term → 0.8.
	if v6.ToolScore != 0.8 {
		t.Fatalf("precondition: v6 reversed 2-hop should score 0.8, got %v", v6.ToolScore)
	}
	// v7 strict: order multiplies the whole score; fully reversed → 0.
	v7 := ScoreToolCaseObservedForVersion(c, protocol.RunResponse{}, true, reversed, ScopePractice, 7)
	if v7.ToolScore != 0 {
		t.Fatalf("v7 fully-reversed 2-hop must score 0, got %v", v7.ToolScore)
	}
	// In-order trajectory still scores full marks under strict scoring.
	ordered := []protocol.ObservedToolCall{{Name: "search_web"}, {Name: "fetch_page"}}
	v7ok := ScoreToolCaseObservedForVersion(c, protocol.RunResponse{}, true, ordered, ScopePractice, 7)
	if v7ok.ToolScore != 1.0 {
		t.Fatalf("v7 in-order 2-hop should score 1.0, got %v", v7ok.ToolScore)
	}
	// Unordered (parallel) cases are exempt from the order gate.
	cu := c
	cu.Unordered = true
	v7par := ScoreToolCaseObservedForVersion(cu, protocol.RunResponse{}, true, reversed, ScopePractice, 7)
	if v7par.ToolScore != 1.0 {
		t.Fatalf("v7 unordered case must not be order-gated, got %v", v7par.ToolScore)
	}
}

func TestV7StrictDoublesExtraCallPenalty(t *testing.T) {
	// Three expected parallel tools plus ONE unexpected extra: the historical
	// extras penalty is 1/3 (below saturation), so the v7 doubling to 2/3 is
	// visible. Unordered isolates the extra-call term from the order gate.
	c := protocol.ToolCase{
		ID:       "t1",
		Category: "parallel",
		ExpectedTools: []protocol.ToolSpec{
			{Name: "a"}, {Name: "b"}, {Name: "c"},
		},
		Unordered: true,
	}
	calls := []protocol.ObservedToolCall{
		{Name: "a"}, {Name: "b"}, {Name: "c"}, {Name: "d"},
	}
	v6 := ScoreToolCaseObservedScope(c, protocol.RunResponse{}, true, calls, ScopePractice)
	v7 := ScoreToolCaseObservedForVersion(c, protocol.RunResponse{}, true, calls, ScopePractice, 7)
	if v7.ToolScore >= v6.ToolScore {
		t.Fatalf("v7 over-budget penalty must bite harder: v6=%v v7=%v", v6.ToolScore, v7.ToolScore)
	}
}

func TestScoreToolCaseObservedForVersionPreV7Identity(t *testing.T) {
	c := protocol.ToolCase{
		ID:       "t1",
		Category: "web_search",
		ExpectedTools: []protocol.ToolSpec{{
			Name:          "search_web",
			ForbiddenArgs: []string{"raw_dump"},
		}},
	}
	calls := []protocol.ObservedToolCall{{Name: "search_web", Args: []byte(`{"raw_dump":1}`)}}
	for _, v := range []int{2, 3, 4, 5, 6} {
		got := ScoreToolCaseObservedForVersion(c, protocol.RunResponse{}, true, calls, ScopePractice, v)
		want := ScoreToolCaseObservedScope(c, protocol.RunResponse{}, true, calls, ScopePractice)
		if got.ToolScore != want.ToolScore || got.Score != want.Score {
			t.Fatalf("v%d must be byte-identical to the historical path: got %v want %v",
				v, got.ToolScore, want.ToolScore)
		}
	}
}

// ---- v7 composite gate depths ----

func TestV7CompositeGateDepths(t *testing.T) {
	// Canary leak: 0.25 under v7, 0.5 historically.
	leak := []protocol.CaseScore{
		{Kind: protocol.KindMemory, Category: "memory-canary", Score: 0.1, Notes: []string{canaryLeakNote}},
	}
	if got := CompositeGateForVersion(leak, protocol.BenchVersionV6); got != canaryLeakPenalty {
		t.Fatalf("v6 leak gate should be %v, got %v", canaryLeakPenalty, got)
	}
	if got := CompositeGateForVersion(leak, protocol.BenchVersionV7); got != v7CanaryLeakPenalty {
		t.Fatalf("v7 leak gate should be %v, got %v", v7CanaryLeakPenalty, got)
	}

	// Saturated observed overshoot: 0.85 historically, 0.60 under v7.
	overshoot := []protocol.CaseScore{{
		Kind: protocol.KindTool, Observed: true, Score: 1.0,
		Expected: []string{"a"}, Called: []string{"a", "b", "c", "d", "e", "f"},
	}}
	if got := CompositeGateForVersion(overshoot, protocol.BenchVersionV6); got != round6(1-effMaxPenalty) {
		t.Fatalf("v6 overshoot gate should be %v, got %v", 1-effMaxPenalty, got)
	}
	if got := CompositeGateForVersion(overshoot, protocol.BenchVersionV7); got != round6(1-v7EffMaxPenalty) {
		t.Fatalf("v7 overshoot gate should be %v, got %v", 1-v7EffMaxPenalty, got)
	}

	// v7 free overshoot is gone: one extra call already costs.
	oneExtra := []protocol.CaseScore{{
		Kind: protocol.KindTool, Observed: true, Score: 1.0,
		Expected: []string{"a"}, Called: []string{"a", "b"},
	}}
	if got := CompositeGateForVersion(oneExtra, protocol.BenchVersionV6); got != 1.0 {
		t.Fatalf("v6 one extra call is free, got %v", got)
	}
	if got := CompositeGateForVersion(oneExtra, protocol.BenchVersionV7); got >= 1.0 {
		t.Fatalf("v7 one extra call must already cost, got %v", got)
	}

	// Bounded-product floor: 0.40 under v7 (0.75 historically). Build a run that
	// maxes every bounded factor.
	worst := []protocol.CaseScore{
		// saturated overshoot on an observed, correct tool case
		{Kind: protocol.KindTool, Observed: true, Score: 1.0,
			Expected: []string{"a"}, Called: []string{"a", "b", "c", "d", "e", "f"}},
		// fully-split twin group
		{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 1.0, TwinGroup: "g1", Correct: true},
		{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 0.0, TwinGroup: "g1", Correct: false},
		// observed memory over-call on every retrieval case
		{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 1.0, Observed: true,
			Called: []string{"search_web"}},
	}
	v6gate := CompositeGateForVersion(worst, protocol.BenchVersionV6)
	if v6gate != boundedGateFloor {
		t.Fatalf("v6 worst bounded gate should hit the 0.75 floor, got %v", v6gate)
	}
	v7gate := CompositeGateForVersion(worst, protocol.BenchVersionV7)
	if v7gate != v7BoundedGateFloor {
		t.Fatalf("v7 worst bounded gate should hit the 0.40 floor, got %v", v7gate)
	}
}

// The v7 transform audit is ENFORCED as part of the contract (no env flag) and
// bites deeper. A symmetric (noisy-honest) split pattern stays unpenalized.
func TestV7TransformAuditEnforced(t *testing.T) {
	pair := func(g string, baseOK, xformOK bool) []protocol.CaseScore {
		group := persona.AuditTwinPrefix + g
		return []protocol.CaseScore{
			{Kind: protocol.KindMemory, TwinGroup: group, AuditHalf: protocol.AuditHalfBase, Correct: baseOK},
			{Kind: protocol.KindMemory, TwinGroup: group, AuditHalf: protocol.AuditHalfTransform, Correct: xformOK},
		}
	}
	var directional []protocol.CaseScore
	// 4 base-only splits: the surface-keyed signature, saturated.
	for _, g := range []string{"a", "b", "c", "d"} {
		directional = append(directional, pair(g, true, false)...)
	}
	// v6 default (observational): no penalty without the env flag.
	if got := CompositeGateForVersion(directional, protocol.BenchVersionV6); got != 1.0 {
		t.Fatalf("v6 default transform audit must be observational, got %v", got)
	}
	// v7: enforced, saturated → 1 - 0.40.
	if got := CompositeGateForVersion(directional, protocol.BenchVersionV7); got != round6(1-v7TransformAuditMaxPenalty) {
		t.Fatalf("v7 directional brittleness should gate at %v, got %v", 1-v7TransformAuditMaxPenalty, got)
	}
	// Symmetric splits (2 base-only, 2 transform-only): honest noise, no penalty.
	var symmetric []protocol.CaseScore
	symmetric = append(symmetric, pair("a", true, false)...)
	symmetric = append(symmetric, pair("b", true, false)...)
	symmetric = append(symmetric, pair("c", false, true)...)
	symmetric = append(symmetric, pair("d", false, true)...)
	if got := CompositeGateForVersion(symmetric, protocol.BenchVersionV7); got != 1.0 {
		t.Fatalf("v7 symmetric audit noise must not be penalized, got %v", got)
	}
}

// ---- the ~10x difficulty identity ----

// TestV7DifficultyNaiveVsOracle is the v7 difficulty claim, measured through
// the same scoring pipeline main.go drives:
//
//   - A NAIVE pattern-matching tool strategy — self-reports plausible
//     trajectories without executing through tool_endpoint, and answers
//     result-usage cases by grepping a number out of whatever tool it did
//     touch (the decoy signature) — scores ~0.475 under the v6 practice
//     contract and ~0.0375 under v7: ~12.7x lower.
//   - A correct ORACLE — executes through the endpoint, right trajectories,
//     incorporates the served needles — scores 1.0 under BOTH contracts.
func TestV7DifficultyNaiveVsOracle(t *testing.T) {
	plainCase := func(id string) protocol.ToolCase {
		return protocol.ToolCase{
			ID: id, Category: "web_search",
			ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
			MaxToolCalls:  1,
		}
	}
	usageCase := func(id string) protocol.ToolCase {
		return protocol.ToolCase{
			ID: id, Category: "result_usage",
			ExpectedTools: []protocol.ToolSpec{{Name: "get_stock_price"}},
			MaxToolCalls:  1,
		}
	}
	const needle, decoyVal = "3,418", "7,902"

	scoreRun := func(benchVersion int, naive bool) protocol.ScoreReport {
		var perCase []protocol.CaseScore
		// 6 plain observable tool cases.
		for i := 0; i < 6; i++ {
			c := plainCase("t" + string(rune('0'+i)))
			var cs protocol.CaseScore
			if naive {
				// Self-reported correct call, nothing observed.
				self := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{{Name: "search_web"}}}
				cs = ScoreToolCaseObservedForVersion(c, self, true, nil, ScopePractice, benchVersion)
				cs = FinishTool(cs)
				cs = CapUnobservedForVersion(cs, ScopePractice, benchVersion)
			} else {
				// Observed correct execution; faithful self-report.
				observed := []protocol.ObservedToolCall{{Name: "search_web"}}
				resp := protocol.RunResponse{ToolCalls: observed, FinalText: "done"}
				cs = ScoreToolCaseObservedForVersion(c, resp, true, observed, ScopePractice, benchVersion)
				cs = FinishTool(cs)
				cs = ApplyTrajectoryMismatch(benchVersion, cs, resp.ToolCalls, observed)
			}
			perCase = append(perCase, cs)
		}
		// 2 result-usage cases, observed executing the right tool.
		for i := 0; i < 2; i++ {
			c := usageCase("u" + string(rune('0'+i)))
			observed := []protocol.ObservedToolCall{{Name: "get_stock_price"}}
			answer := "It reached " + needle + " points."
			if naive {
				// Grepped a number out of the wrong tool's payload: the decoy.
				answer = "The figure is " + decoyVal + "."
			}
			resp := protocol.RunResponse{ToolCalls: observed, FinalText: answer}
			cs := ScoreToolCaseObservedForVersion(c, resp, true, observed, ScopePractice, benchVersion)
			cs = ComposeResultUsageForVersion(benchVersion, cs, answer, needle, decoyVal)
			cs = ApplyTrajectoryMismatch(benchVersion, cs, resp.ToolCalls, observed)
			perCase = append(perCase, cs)
		}
		return AggregateForVersion("run", perCase, benchVersion)
	}

	oracleV6 := scoreRun(protocol.BenchVersionV6, false)
	oracleV7 := scoreRun(protocol.BenchVersionV7, false)
	if oracleV6.Composite != 1.0 || oracleV7.Composite != 1.0 {
		t.Fatalf("oracle must score full marks under both contracts: v6=%v v7=%v",
			oracleV6.Composite, oracleV7.Composite)
	}

	naiveV6 := scoreRun(protocol.BenchVersionV6, true)
	naiveV7 := scoreRun(protocol.BenchVersionV7, true)
	// v6: 6×0.5 (selection-only ceiling) + 2×0.4 (trajectory half) over 8 = 0.475.
	if naiveV6.Composite != 0.475 {
		t.Fatalf("naive v6 composite should be 0.475, got %v", naiveV6.Composite)
	}
	// v7: 6×0.05 + 2×0 over 8 = 0.0375.
	if naiveV7.Composite != 0.0375 {
		t.Fatalf("naive v7 composite should be 0.0375, got %v", naiveV7.Composite)
	}
	ratio := naiveV6.Composite / naiveV7.Composite
	if ratio < 10 {
		t.Fatalf("v7 must be at least 10x harder for the naive strategy: ratio %v", ratio)
	}
	t.Logf("v7 difficulty identity: naive v6=%v v7=%v (%.1fx lower); oracle 1.0 under both",
		naiveV6.Composite, naiveV7.Composite, ratio)
}
