package toolprobe

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestRunIsDeterministic(t *testing.T) {
	a, err := Run(8, "full", 100, 2, 1)
	if err != nil {
		t.Fatal(err)
	}
	b, err := Run(8, "full", 100, 2, 1)
	if err != nil {
		t.Fatal(err)
	}
	if a.Correct != b.Correct || a.Total != b.Total {
		t.Fatalf("probe changed across identical runs: %+v vs %+v", a, b)
	}
}

func TestOutcomeSignatureBindsArgumentsAndServedResult(t *testing.T) {
	base := protocol.ToolCase{
		ExpectedTools:   []protocol.ToolSpec{{Name: "gmail_send", RequiredArgs: map[string]string{"to": "one@example.com"}}},
		FuzzyTrajectory: true,
	}
	otherArg := base
	otherArg.ExpectedTools = []protocol.ToolSpec{{Name: "gmail_send", RequiredArgs: map[string]string{"to": "two@example.com"}}}
	if toolOutcomeSignature(base, "value 1") == toolOutcomeSignature(otherArg, "value 1") {
		t.Fatal("outcome signature ignored the seed-bound action argument")
	}
	if toolOutcomeSignature(base, "value 1") == toolOutcomeSignature(base, "value 2") {
		t.Fatal("outcome signature ignored the served result")
	}
}
