package main

import (
	"encoding/json"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// scoreGeneratedToolSuite drives REAL generated tool cases through the exact
// scoring sequence runSizeJob uses (score → result-usage composition →
// mismatch penalty → unobserved cap) for one of two strategies:
//
//   - oracle: executes through tool_endpoint (observed == expected calls with
//     the expected required arguments, in order), self-reports faithfully,
//     and incorporates the served needle into result-usage answers.
//   - naive: the deterministic-parser archetype. It self-reports the expected
//     tool names but never executes through the endpoint (nothing observed),
//     never sees a served needle, and falls for the v7 negation-cue trap
//     (calls the keyword tool on negation_no_tool cases).
func scoreGeneratedToolSuite(t *testing.T, seed int64, benchVersion int, oracle bool) protocol.ScoreReport {
	t.Helper()
	prof, ok := gen.ProfileForVersion("medium", benchVersion)
	if !ok {
		t.Fatalf("no medium profile for v%d", benchVersion)
	}
	rng, err := gen.NewRNGForVersion(seed, benchVersion)
	if err != nil {
		t.Fatalf("rng v%d: %v", benchVersion, err)
	}
	cases, _ := gen.GenerateToolsForVersion(rng, seed, prof.Tools, benchVersion)

	perCase := make([]protocol.CaseScore, 0, len(cases))
	for _, c := range cases {
		fix := toolexec.BuildFixture(seed, c)
		var observed []protocol.ObservedToolCall
		var resp protocol.RunResponse
		if oracle {
			// Execute the expected trajectory through the endpoint wherever the
			// endpoint serves it; answer with the served needle when one exists.
			calls := expectedCalls(t, c)
			if toolexec.Observable(c) {
				observed = calls
			}
			resp = protocol.RunResponse{ToolCalls: calls, FinalText: "Done."}
			if datagen.IsResultUsage(c.Category) {
				resp.FinalText = "The value is " + fix.NeedleValue() + "."
			}
		} else {
			// Parser: no observed execution, best-case self-report of the expected
			// names, generic answer. On the v7 negation-restraint trap it does what
			// a keyword parser does: calls the tool the prompt names.
			calls := expectedCalls(t, c)
			if len(calls) == 0 && c.Category == "negation_no_tool" {
				calls = []protocol.ObservedToolCall{{Name: "search_web"}}
			}
			resp = protocol.RunResponse{ToolCalls: calls, FinalText: "Here's what I found."}
		}

		cs := scorer.ScoreToolCaseObservedForVersion(c, resp, true, observed, scorer.ScopePractice, benchVersion)
		if datagen.IsResultUsage(c.Category) {
			cs = scorer.ComposeResultUsageForVersion(benchVersion, cs, resp.FinalText, fix.NeedleValue(), fix.DecoyValue())
		} else {
			cs = scorer.FinishTool(cs)
		}
		cs = scorer.ApplyTrajectoryMismatch(benchVersion, cs, resp.ToolCalls, observed)
		if len(observed) == 0 && toolexec.Observable(c) {
			cs = scorer.CapUnobservedForVersion(cs, scorer.ScopePractice, benchVersion)
		}
		perCase = append(perCase, cs)
	}
	return scorer.AggregateForVersion("e2e", perCase, benchVersion)
}

// expectedCalls builds the case's expected trajectory: each expected tool, in
// order, with its required arguments.
func expectedCalls(t *testing.T, c protocol.ToolCase) []protocol.ObservedToolCall {
	t.Helper()
	calls := make([]protocol.ObservedToolCall, 0, len(c.ExpectedTools))
	for hop, spec := range c.ExpectedTools {
		args := map[string]string{}
		for k, v := range spec.RequiredArgs {
			args[k] = v
		}
		raw, err := json.Marshal(args)
		if err != nil {
			t.Fatalf("marshal args: %v", err)
		}
		calls = append(calls, protocol.ObservedToolCall{Name: spec.Name, Args: raw, Hop: hop})
	}
	return calls
}

// TestV7DifficultyOnGeneratedToolSuites is the end-to-end companion to the
// scorer-level TestV7DifficultyNaiveVsOracle: it runs REAL generated medium
// tool suites (the hardened datagen) under both contracts and pins both
// directions of the difficulty claim on live data:
//
//   - the oracle stays essentially perfect under the strict v7 rules (the
//     suite remains fully solvable), and
//   - the naive parser, which banks 0.5-per-case selection credit under v6,
//     collapses under v7 (selection-only ceiling 0.05 + the new trap
//     categories it fails outright).
func TestV7DifficultyOnGeneratedToolSuites(t *testing.T) {
	const seed = 123456789

	oracleV6 := scoreGeneratedToolSuite(t, seed, protocol.BenchVersionV6, true)
	oracleV7 := scoreGeneratedToolSuite(t, seed, protocol.BenchVersionV7, true)
	if oracleV6.Composite < 0.98 {
		t.Fatalf("oracle must stay ~perfect on the v6 suite, got %v", oracleV6.Composite)
	}
	if oracleV7.Composite < 0.98 {
		t.Fatalf("oracle must stay ~perfect on the hardened v7 suite under strict scoring, got %v", oracleV7.Composite)
	}

	naiveV6 := scoreGeneratedToolSuite(t, seed, protocol.BenchVersionV6, false)
	naiveV7 := scoreGeneratedToolSuite(t, seed, protocol.BenchVersionV7, false)
	// Deterministic per datagen revision (measured 3.2x at datagen v7-difficulty
	// fe1835e, the pinned iteration-2 deepened head; 2.8x at e1dfbbb and 3.4x at
	// the earlier a91fc9b — suite recomposition moves it somewhat). The
	// suite-level ratio is
	// bounded by cases a parser answers legitimately (abstention/no-tool reward
	// inaction in both contracts); assert at least a 2.5x collapse so a
	// regression that re-opens selection credit cannot hide behind that
	// dilution, without over-pinning the datagen suite's exact composition.
	if naiveV7.Composite > naiveV6.Composite/2.5 {
		t.Fatalf("naive parser must collapse at least 2.5x under v7: v6=%v v7=%v",
			naiveV6.Composite, naiveV7.Composite)
	}
	// The observable slice (where the parser's 0.5-per-case free credit lived)
	// is the 10x axis; suite-level dilution comes only from cases a parser
	// answers legitimately (abstention/no-tool), which reward inaction in both
	// contracts.
	t.Logf("e2e difficulty (medium tool suites, seed %d): oracle v6=%v v7=%v; naive v6=%v v7=%v (%.1fx lower)",
		seed, oracleV6.Composite, oracleV7.Composite, naiveV6.Composite, naiveV7.Composite,
		naiveV6.Composite/naiveV7.Composite)
}
