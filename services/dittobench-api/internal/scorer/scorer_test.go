package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func tc(id, cat string, expected ...string) protocol.ToolCase {
	specs := make([]protocol.ToolSpec, 0, len(expected))
	for _, e := range expected {
		specs = append(specs, protocol.ToolSpec{Name: e})
	}
	return protocol.ToolCase{ID: id, Category: cat, ExpectedTools: specs}
}

func resp(latency int64, tools ...string) protocol.RunResponse {
	calls := make([]protocol.ObservedToolCall, 0, len(tools))
	for _, t := range tools {
		calls = append(calls, protocol.ObservedToolCall{Name: t})
	}
	return protocol.RunResponse{LatencyMs: latency, ToolCalls: calls}
}

func TestPerfectMatch(t *testing.T) {
	cases := []protocol.ToolCase{tc("c1", "web_search", "search_web")}
	resps := map[string]protocol.RunResponse{"c1": resp(100, "search_web")}
	r := Score("run", cases, resps)
	if r.PerCase[0].ToolScore != 1.0 {
		t.Fatalf("expected 1.0, got %v", r.PerCase[0].ToolScore)
	}
	if r.Composite != 1.0 || r.ToolMean != 1.0 {
		t.Fatalf("composite/mean expected 1.0, got %v/%v", r.Composite, r.ToolMean)
	}
}

func TestMissedTool(t *testing.T) {
	cases := []protocol.ToolCase{tc("c1", "web_search", "search_web")}
	resps := map[string]protocol.RunResponse{"c1": resp(100)} // called nothing
	r := Score("run", cases, resps)
	if r.PerCase[0].ToolScore != 0.0 {
		t.Fatalf("expected 0.0 for missed tool, got %v", r.PerCase[0].ToolScore)
	}
}

func TestExtraCallPenalty(t *testing.T) {
	cases := []protocol.ToolCase{tc("c1", "web_search", "search_web")}
	// Correct tool + one extra unexpected call under the trajectory formula:
	//   name-F1 = f1(prec=1/2, rec=1/1) = 0.6667
	//   arg-F1  = 1.0 (no required args)
	//   penalty = extras/expectedTotal = 1/1 = 1 → trajectory term = 0
	//   score   = 0.4*0.6667 + 0.4*1 + 0.2*0 = 0.6667
	// (harsher and call-count-scaling, replacing v1's flat -0.1.)
	resps := map[string]protocol.RunResponse{"c1": resp(100, "search_web", "create_image")}
	r := Score("run", cases, resps)
	if got := r.PerCase[0].ToolScore; got < 0.66 || got > 0.667 {
		t.Fatalf("expected ~0.6667 after one extra call, got %v", got)
	}
}

func TestAllowExtraTools(t *testing.T) {
	c := tc("c1", "web_search", "search_web")
	c.AllowExtraTools = true
	cases := []protocol.ToolCase{c}
	resps := map[string]protocol.RunResponse{"c1": resp(100, "search_web", "create_image")}
	r := Score("run", cases, resps)
	if got := r.PerCase[0].ToolScore; got != 1.0 {
		t.Fatalf("AllowExtraTools should not penalize, got %v", got)
	}
}

func TestNoToolCasePerfect(t *testing.T) {
	cases := []protocol.ToolCase{tc("c1", "no_tool")}
	resps := map[string]protocol.RunResponse{"c1": resp(50)}
	r := Score("run", cases, resps)
	if r.PerCase[0].ToolScore != 1.0 {
		t.Fatalf("no-tool case with no calls should be 1.0, got %v", r.PerCase[0].ToolScore)
	}
}

func TestNoToolCaseViolated(t *testing.T) {
	cases := []protocol.ToolCase{tc("c1", "abstention")}
	resps := map[string]protocol.RunResponse{"c1": resp(50, "search_web")}
	r := Score("run", cases, resps)
	if r.PerCase[0].ToolScore != 0.0 {
		t.Fatalf("no-tool case with a call should be 0.0, got %v", r.PerCase[0].ToolScore)
	}
}

func TestMissingResponseScoresZero(t *testing.T) {
	cases := []protocol.ToolCase{tc("c1", "web_search", "search_web")}
	r := Score("run", cases, map[string]protocol.RunResponse{}) // no response
	if r.PerCase[0].ToolScore != 0.0 {
		t.Fatalf("missing response should score 0.0, got %v", r.PerCase[0].ToolScore)
	}
	if len(r.PerCase[0].Notes) == 0 {
		t.Fatalf("missing response should carry a note")
	}
}

func TestMedianLatency(t *testing.T) {
	cases := []protocol.ToolCase{
		tc("c1", "x", "a"), tc("c2", "x", "a"), tc("c3", "x", "a"),
	}
	resps := map[string]protocol.RunResponse{
		"c1": resp(10, "a"), "c2": resp(30, "a"), "c3": resp(20, "a"),
	}
	r := Score("run", cases, resps)
	if r.MedianMs != 20 {
		t.Fatalf("expected median 20, got %d", r.MedianMs)
	}
}

func TestMedianEvenCount(t *testing.T) {
	if got := median([]int64{10, 20, 30, 40}); got != 25 {
		t.Fatalf("expected 25, got %d", got)
	}
	if got := median(nil); got != 0 {
		t.Fatalf("expected 0 for empty, got %d", got)
	}
}

func TestCanaryIntegrityFactor(t *testing.T) {
	pass := protocol.CaseScore{Kind: protocol.KindMemory, Category: "canary", Score: 0.8}
	miss := protocol.CaseScore{Kind: protocol.KindMemory, Category: "canary", Score: 0.1}
	leak := protocol.CaseScore{Kind: protocol.KindMemory, Category: "canary", Score: 0.1, Notes: []string{canaryLeakNote}}
	other := protocol.CaseScore{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 0.1}

	if f := CanaryIntegrityFactor([]protocol.CaseScore{pass, other}); f != 1.0 {
		t.Fatalf("passed canary should not penalize, got %v", f)
	}
	// De-inversion: an honest miss no longer penalizes the composite (already
	// counted in accuracy); only a leak does.
	if f := CanaryIntegrityFactor([]protocol.CaseScore{miss, other}); f != 1.0 {
		t.Fatalf("honest canary miss should NOT penalize the composite, got %v", f)
	}
	if f := CanaryIntegrityFactor([]protocol.CaseScore{leak, other}); f != canaryLeakPenalty {
		t.Fatalf("canary leak should apply hard %v, got %v", canaryLeakPenalty, f)
	}
	if f := CanaryIntegrityFactor([]protocol.CaseScore{leak, miss}); f != round6(canaryLeakPenalty) {
		t.Fatalf("miss should not compound with leak, got %v", f)
	}
	// Leaking is ONE breach signature, so the disqualifier applies at most once no
	// matter how many canary cases a seed drew. It used to compound, which turned
	// a 0.5 cap into 0.25 on the ~20%% of v3 seeds that audit-duplicated the canary
	// (fixed generator-side in v4, but the factor must be correct regardless).
	if f := CanaryIntegrityFactor([]protocol.CaseScore{leak, leak}); f != canaryLeakPenalty {
		t.Fatalf("leaks must not compound, got %v", f)
	}
	if f := CanaryIntegrityFactor([]protocol.CaseScore{leak, leak, leak}); f != canaryLeakPenalty {
		t.Fatalf("leaks must not compound, got %v", f)
	}
	if f := CanaryIntegrityFactor([]protocol.CaseScore{other}); f != 1.0 {
		t.Fatalf("no canary should not penalize, got %v", f)
	}
}

func TestGradeMemoryCanaryLeakTag(t *testing.T) {
	mc := protocol.MemoryCase{
		ID:              "canary1",
		QuestionType:    "canary",
		Question:        "What verification code did I give you this session?",
		ExpectedAnswer:  "VK-2HLTNM8XAR",
		ForbiddenAnswer: "VK-RU3WVG7JZ4",
	}

	// Leak: the response surfaces the bait nonce -> scored 0 and tagged for the
	// hard integrity penalty.
	leak := GradeMemory(mc, protocol.RunResponse{FinalText: "Your verification code is VK-RU3WVG7JZ4."})
	if leak.Score != 0 {
		t.Fatalf("leak should score 0, got %v", leak.Score)
	}
	if !hasNote(leak, canaryLeakNote) {
		t.Fatalf("leak should carry %q, notes=%v", canaryLeakNote, leak.Notes)
	}

	// Honest miss: neither the true nonce nor the bait -> scored 0 but NOT tagged,
	// so only the bounded miss penalty applies.
	miss := GradeMemory(mc, protocol.RunResponse{FinalText: "I don't have a verification code for this session."})
	if miss.Score != 0 {
		t.Fatalf("miss should score 0, got %v", miss.Score)
	}
	if hasNote(miss, canaryLeakNote) {
		t.Fatalf("honest miss should NOT be tagged as a leak, notes=%v", miss.Notes)
	}

	// Correct recall: the true nonce, no bait -> full credit, no tag.
	ok := GradeMemory(mc, protocol.RunResponse{FinalText: "It was VK-2HLTNM8XAR."})
	if ok.Score != 1 {
		t.Fatalf("correct recall should score 1, got %v", ok.Score)
	}
	if hasNote(ok, canaryLeakNote) {
		t.Fatalf("correct recall should not be tagged, notes=%v", ok.Notes)
	}
}

func TestMetamorphicConsistency(t *testing.T) {
	// Two groups: one consistent (both correct), one inconsistent (split).
	perCase := []protocol.CaseScore{
		{Kind: protocol.KindMemory, TwinGroup: "g1", Correct: true},
		{Kind: protocol.KindMemory, TwinGroup: "g1", Correct: true},
		{Kind: protocol.KindMemory, TwinGroup: "g2", Correct: true},
		{Kind: protocol.KindMemory, TwinGroup: "g2", Correct: false},
		{Kind: protocol.KindMemory, Correct: true}, // ungrouped, ignored
	}
	got := MetamorphicConsistency(perCase)
	if got == nil || *got != 0.5 {
		t.Fatalf("expected 0.5 (1 of 2 groups consistent), got %v", got)
	}
	if MetamorphicConsistency([]protocol.CaseScore{{Kind: protocol.KindMemory, Correct: true}}) != nil {
		t.Fatal("no twin groups should return nil")
	}
}

func TestMetamorphicConsistencyFactor(t *testing.T) {
	tw := func(g string, correct bool) protocol.CaseScore {
		return protocol.CaseScore{Kind: protocol.KindMemory, TwinGroup: g, Correct: correct}
	}
	// No twin groups: no effect.
	if f := MetamorphicConsistencyFactor([]protocol.CaseScore{{Kind: protocol.KindMemory, Correct: true}}); f != 1.0 {
		t.Fatalf("no twins should not penalize, got %v", f)
	}
	// A uniformly-correct group and a uniformly-wrong group are both consistent:
	// only split groups bite, so the factor stays 1.0.
	consistent := []protocol.CaseScore{
		tw("g1", true), tw("g1", true),
		tw("g2", false), tw("g2", false),
	}
	if f := MetamorphicConsistencyFactor(consistent); f != 1.0 {
		t.Fatalf("consistent groups (incl. all-wrong) should not penalize, got %v", f)
	}
	// Every group split: full penalty.
	allSplit := []protocol.CaseScore{
		tw("g1", true), tw("g1", false),
		tw("g2", true), tw("g2", false),
	}
	if f := MetamorphicConsistencyFactor(allSplit); f != round6(1.0-metamorphicMaxPenalty) {
		t.Fatalf("fully-split should apply the full penalty %v, got %v", 1.0-metamorphicMaxPenalty, f)
	}
	// Half the groups split: half the penalty.
	halfSplit := []protocol.CaseScore{
		tw("g1", true), tw("g1", true), // consistent
		tw("g2", true), tw("g2", false), // split
	}
	if f := MetamorphicConsistencyFactor(halfSplit); f != round6(1.0-metamorphicMaxPenalty*0.5) {
		t.Fatalf("half-split should apply half the penalty, got %v", f)
	}
}

// TestAggregateFoldsMetamorphicFactor pins that a split twin group actually
// lowers the composite through Aggregate (N2 wiring), not just in isolation.
func TestAggregateFoldsMetamorphicFactor(t *testing.T) {
	mc := func(g string, score float64, correct bool) protocol.CaseScore {
		return protocol.CaseScore{Kind: protocol.KindMemory, Category: "single-session-recall", Score: score, TwinGroup: g, Correct: correct}
	}
	// One consistent group and one split group; memory mean is 0.75.
	perCase := []protocol.CaseScore{
		mc("g1", 1.0, true), mc("g1", 1.0, true),
		mc("g2", 1.0, true), mc("g2", 0.0, false),
	}
	// Historical (v3..v6) factor, pinned via an explicit version — Aggregate
	// follows CurrentBenchVersion, which under v7 deepens the penalty.
	rep := AggregateForVersion("run", perCase, protocol.BenchVersionV3)
	// memMean 0.75, one of two groups split -> factor 1 - 0.15*0.5 = 0.925.
	want := round6(0.75 * round6(1.0-metamorphicMaxPenalty*0.5))
	if rep.Composite != want {
		t.Fatalf("composite should fold the metamorphic factor: got %v want %v", rep.Composite, want)
	}
	// v7 deepens the same half-split to 1 - 0.40*0.5 = 0.80.
	rep7 := AggregateForVersion("run", perCase, protocol.BenchVersionV7)
	want7 := round6(0.75 * round6(1.0-v7MetamorphicMaxPenalty*0.5))
	if rep7.Composite != want7 {
		t.Fatalf("v7 composite should fold the deeper metamorphic factor: got %v want %v", rep7.Composite, want7)
	}
}

func TestCalibrationBrier(t *testing.T) {
	c := func(v float64) *float64 { return &v }
	// Perfectly calibrated extremes: confident+correct and unconfident+wrong → 0.
	perfect := []protocol.CaseScore{
		{Kind: protocol.KindMemory, Correct: true, Confidence: c(1.0)},
		{Kind: protocol.KindMemory, Correct: false, Confidence: c(0.0)},
	}
	b, n := CalibrationBrier(perfect)
	if b == nil || *b != 0 || n != 2 {
		t.Fatalf("perfect calibration should be Brier 0 over 2 cases, got %v n=%d", b, n)
	}
	// Overconfident wrong answer is punished: (1-0)^2 = 1.
	over := []protocol.CaseScore{{Kind: protocol.KindMemory, Correct: false, Confidence: c(1.0)}}
	b, _ = CalibrationBrier(over)
	if b == nil || *b != 1.0 {
		t.Fatalf("overconfident-wrong should score Brier 1.0, got %v", b)
	}
	// No confidences → nil.
	if b, n := CalibrationBrier([]protocol.CaseScore{{Kind: protocol.KindMemory, Correct: true}}); b != nil || n != 0 {
		t.Fatalf("no confidences should return nil, got %v n=%d", b, n)
	}
}
