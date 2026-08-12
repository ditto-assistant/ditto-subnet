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

func TestV10ModelFreeAndArgumentExposureGates(t *testing.T) {
	result, err := Run(protocol.BenchVersionV10, "full", 1, 30, 10)
	if err != nil {
		t.Fatal(err)
	}
	if result.Accuracy() >= 0.50 {
		t.Fatalf("model-free complete outcome accuracy %.4f must remain below 0.50", result.Accuracy())
	}
	if result.ArgumentValues == 0 {
		t.Fatal("v10 corpus did not exercise any required argument values")
	}
	if result.VerbatimArgumentShare() >= 0.25 {
		t.Fatalf("verbatim argument share %.4f must remain below 0.25", result.VerbatimArgumentShare())
	}
}
