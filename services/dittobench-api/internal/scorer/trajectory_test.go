package scorer

import (
	"encoding/json"
	"math"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func specs(names ...string) []protocol.ToolSpec {
	out := make([]protocol.ToolSpec, len(names))
	for i, n := range names {
		out[i] = protocol.ToolSpec{Name: n}
	}
	return out
}

func call(name, args string) protocol.ObservedToolCall {
	return protocol.ObservedToolCall{Name: name, Args: json.RawMessage(args)}
}

func detScore(c protocol.ToolCase, calls ...protocol.ObservedToolCall) float64 {
	s, _ := deterministicToolScore(c, calls)
	return s
}

func near(a, b float64) bool { return math.Abs(a-b) < 1e-6 }

// TestUnorderedParallelIgnoresOrder: a parallel (Unordered) case scores full for
// either call order, while an ordered multi-hop case is penalized when reversed.
func TestUnorderedParallelIgnoresOrder(t *testing.T) {
	seq := specs("search_web", "create_image")
	ordered := protocol.ToolCase{ID: "o", Category: "multi", ExpectedTools: seq, MaxToolCalls: 2}
	parallel := protocol.ToolCase{ID: "p", Category: "parallel_web_image", ExpectedTools: seq, MaxToolCalls: 2, Unordered: true}

	fwd := call("search_web", `{}`)
	rev := call("create_image", `{}`)

	if got := detScore(parallel, fwd, rev); !near(got, 1.0) {
		t.Fatalf("parallel in nominal order should be 1.0, got %v", got)
	}
	if got := detScore(parallel, rev, fwd); !near(got, 1.0) {
		t.Fatalf("parallel reversed should still be 1.0 (order not graded), got %v", got)
	}
	// The same reversal on an ORDERED case must lose the trajectory term.
	if got := detScore(ordered, rev, fwd); got >= 1.0 {
		t.Fatalf("ordered reversed should be penalized, got %v", got)
	}
}

func TestV8FuzzyTrajectoryGradesCapabilitiesAndOutcomeNotExactTrace(t *testing.T) {
	c := protocol.ToolCase{
		ID: "fuzzy", Category: "world_contact_research_email_result_usage",
		ExpectedTools: []protocol.ToolSpec{
			{Name: "search_web"},
			{Name: "gmail_send", RequiredArgs: map[string]string{"to": "new@example.com", "body": "4,219"}},
		},
		MaxToolCalls: 15, AllowExtraTools: true, FuzzyTrajectory: true,
	}
	calls := []protocol.ObservedToolCall{
		call("gmail_send", `{"to":"old@example.com","body":"checking"}`),
		call("discover_capabilities", `{"query":"email"}`),
		call("search_web", `{"query":"the current figure"}`),
		call("gmail_send", `{"to":"new@example.com","body":"The figure is 4,219."}`),
	}
	for i := 0; i < 20; i++ {
		calls = append(calls, call("search_memories", `{"query":"contact context"}`))
	}
	got, notes := deterministicToolScoreStrict(c, calls, true)
	if !near(got, 1) {
		t.Fatalf("correct fuzzy outcome with retries, exploration, and >15 calls = %v, want 1; notes=%v", got, notes)
	}

	missingCapability := []protocol.ObservedToolCall{
		call("gmail_send", `{"to":"new@example.com","body":"The figure is 4,219."}`),
	}
	if got, _ := deterministicToolScoreStrict(c, missingCapability, true); got != 0 {
		t.Fatalf("fuzzy case missing search capability = %v, want 0", got)
	}

	wrongOutcome := []protocol.ObservedToolCall{
		call("search_web", `{}`),
		call("gmail_send", `{"to":"old@example.com","body":"The figure is 4,219."}`),
	}
	if got, _ := deterministicToolScoreStrict(c, wrongOutcome, true); got != 0 {
		t.Fatalf("fuzzy case with wrong mutation target = %v, want 0", got)
	}
}

func TestArgScoringExactValue(t *testing.T) {
	c := protocol.ToolCase{
		ID: "c", Category: "link_read", MaxToolCalls: 1,
		ExpectedTools: []protocol.ToolSpec{{
			Name:         "read_links",
			RequiredArgs: map[string]string{"url": "https://example.com/article"},
		}},
	}
	right := detScore(c, call("read_links", `{"url":"https://example.com/article"}`))
	if !near(right, 1.0) {
		t.Fatalf("right tool+arg should be 1.0, got %v", right)
	}
	wrong := detScore(c, call("read_links", `{"url":"https://evil.example/phish"}`))
	// name 1, arg 0, trajectory 1 → 0.4 + 0.2 = 0.6
	if !near(wrong, 0.6) {
		t.Fatalf("right tool + wrong arg should be 0.6, got %v", wrong)
	}
	if wrong >= right {
		t.Fatal("wrong args must score strictly below correct args")
	}
}

func TestArgValueBoundaryNotSubstring(t *testing.T) {
	c := protocol.ToolCase{
		ID: "c", MaxToolCalls: 1,
		ExpectedTools: []protocol.ToolSpec{{Name: "set_reasoning_effort", RequiredArgs: map[string]string{"effort": "low"}}},
	}
	good := detScore(c, call("set_reasoning_effort", `{"effort":"low"}`))
	// "yellow" contains "low" as a substring but is not the enum value.
	bad := detScore(c, call("set_reasoning_effort", `{"effort":"yellow"}`))
	if !near(good, 1.0) {
		t.Fatalf("exact enum should be 1.0, got %v", good)
	}
	if bad >= good {
		t.Fatalf("'yellow' must not satisfy required 'low': good=%v bad=%v", good, bad)
	}
}

func TestArgValueNumericBoundary(t *testing.T) {
	c := protocol.ToolCase{
		ID: "c", MaxToolCalls: 1,
		ExpectedTools: []protocol.ToolSpec{{Name: "set_limit", RequiredArgs: map[string]string{"count": "5"}}},
	}
	good := detScore(c, call("set_limit", `{"count":5}`))
	// "5.5" and "-5" both contain the digit "5" but are different numbers.
	badDecimal := detScore(c, call("set_limit", `{"count":5.5}`))
	badNegative := detScore(c, call("set_limit", `{"count":-5}`))
	if !near(good, 1.0) {
		t.Fatalf("exact numeric arg should be 1.0, got %v", good)
	}
	if badDecimal >= good || badNegative >= good {
		t.Fatalf("5.5/-5 must not satisfy required 5: good=%v decimal=%v negative=%v", good, badDecimal, badNegative)
	}
}

func TestForbiddenArgPenalized(t *testing.T) {
	c := protocol.ToolCase{
		ID: "c", MaxToolCalls: 1,
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web", ForbiddenArgs: []string{"user_id"}}},
	}
	clean := detScore(c, call("search_web", `{"query":"weather"}`))
	dirty := detScore(c, call("search_web", `{"query":"weather","user_id":"leak"}`))
	if dirty >= clean {
		t.Fatalf("forbidden arg should lower the score: clean=%v dirty=%v", clean, dirty)
	}
}

func TestMultiHopOrderCredit(t *testing.T) {
	c := protocol.ToolCase{ID: "m", MaxToolCalls: 2, ExpectedTools: specs("search_web", "read_links")}

	inOrder := detScore(c, call("search_web", ""), call("read_links", ""))
	if !near(inOrder, 1.0) {
		t.Fatalf("correct sequence should be 1.0, got %v", inOrder)
	}
	reversed := detScore(c, call("read_links", ""), call("search_web", ""))
	// names match (F1 1), args 1, order 0 → 0.4 + 0.4 + 0.2*0 = 0.8
	if !near(reversed, 0.8) {
		t.Fatalf("reversed sequence should lose the 0.2 order term (0.8), got %v", reversed)
	}
	partial := detScore(c, call("search_web", ""))
	// name F1 = f1(1, 0.5) = 0.667; arg 1; order 0 → 0.4*0.667 + 0.4 = 0.667
	if partial <= 0 || partial >= reversed {
		t.Fatalf("one-of-two should score partial and below reversed, got %v", partial)
	}
	none := detScore(c, call("create_image", ""))
	if none != 0 {
		t.Fatalf("calling none of the expected tools should be 0, got %v", none)
	}
}

func TestArgStuffingRejected(t *testing.T) {
	// Natural phrasing embedding the entity → credited.
	if !argValueEqual("the latest news about Tokyo please", "Tokyo") {
		t.Fatal("natural phrase embedding the entity should credit")
	}
	// A short two-word query still credits.
	if !argValueEqual("Tokyo weather", "Tokyo") {
		t.Fatal("short query should credit")
	}
	// Candidate-stuffing a long list of pool values → rejected.
	if argValueEqual("Tokyo Paris London Berlin Madrid Rome Cairo Oslo", "Tokyo") {
		t.Fatal("stuffed candidate list should NOT credit the embedded value")
	}
}
