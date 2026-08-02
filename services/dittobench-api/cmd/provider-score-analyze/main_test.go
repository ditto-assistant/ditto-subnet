package main

import (
	"math"
	"testing"
)

func TestStatistics(t *testing.T) {
	values := []float64{1, 2, 3}
	if got := mean(values); got != 2 {
		t.Fatalf("mean = %v", got)
	}
	if got := sampleStdDev(values); math.Abs(got-1) > 1e-12 {
		t.Fatalf("sample stddev = %v", got)
	}
	if got := medianInt64([]int64{9, 1, 5}); got != 5 {
		t.Fatalf("median = %d", got)
	}
}

func TestResultName(t *testing.T) {
	name := "top3-deepinfra-a2b69f5c-e866-45b0-a8cf-849231d9d218-run3.json"
	match := resultName.FindStringSubmatch(name)
	if len(match) != 4 || match[1] != "deepinfra" || match[3] != "3" {
		t.Fatalf("match = %#v", match)
	}
}

func TestProviderCompleteRequiresEveryAgent(t *testing.T) {
	agentOne := baselineAgent{AgentID: "one"}
	agentTwo := baselineAgent{AgentID: "two"}
	items := []observation{
		{Provider: "example", Agent: agentOne},
		{Provider: "example", Agent: agentOne},
		{Provider: "example", Agent: agentOne},
		{Provider: "example", Agent: agentTwo},
		{Provider: "example", Agent: agentTwo},
		{Provider: "example", Agent: agentTwo},
	}

	if !summarizeProvider("example", items, 2, 3).Complete {
		t.Fatal("complete matrix was marked incomplete")
	}
	items[5].Agent = agentOne
	if summarizeProvider("example", items, 2, 3).Complete {
		t.Fatal("matrix missing an agent was marked complete")
	}
}
