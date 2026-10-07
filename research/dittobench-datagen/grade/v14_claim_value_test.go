package grade

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func lessonCase(benchVersion int) protocol.MemoryCase {
	mc := lessonSafeSorry
	mc.BenchVersion = benchVersion
	return mc
}

func TestV14ClaimValueCueIsNotACorrection(t *testing.T) {
	for _, resp := range []protocol.RunResponse{
		{Answer: "safe; sorry", FinalText: "safe; sorry"},
		{FinalText: "safe; sorry"},
		{FinalText: "Safe. Sorry."},
	} {
		if v := Memory(lessonCase(protocol.BenchVersionV14), resp); v.Score != 1 {
			t.Fatalf("v14 %+v: score %v, want 1 (%v)", resp, v.Score, v.Notes)
		}
	}
}

func TestV13ClaimValueCueGradingIsUnchanged(t *testing.T) {
	v := Memory(lessonCase(protocol.BenchVersionV13), protocol.RunResponse{Answer: "safe; sorry", FinalText: "safe; sorry"})
	if v.Score != 0.5 {
		t.Fatalf("v13 score moved to %v (%v); v13 grading is immutable", v.Score, v.Notes)
	}
}

func TestV14UnprotectedCorrectionStillRetracts(t *testing.T) {
	for _, bv := range []int{protocol.BenchVersionV13, protocol.BenchVersionV14} {
		v := Memory(lessonCase(bv), protocol.RunResponse{FinalText: "Safe. Actually, reckless."})
		if v.Score != 0 {
			t.Fatalf("v%d: a correction that is not a claim value must still retract the prior sentence; score %v (%v)", bv, v.Score, v.Notes)
		}
	}
}

func TestV14CueSentenceCarryingMoreThanAValueIsStillACorrection(t *testing.T) {
	for _, text := range []string{"Reckless. Sorry, I mean safe.", "Careless. Sorry, the lesson was caution."} {
		resp := protocol.RunResponse{FinalText: text}
		v13 := Memory(lessonCase(protocol.BenchVersionV13), resp)
		v14 := Memory(lessonCase(protocol.BenchVersionV14), resp)
		if v13.Score != v14.Score || len(v13.Notes) != len(v14.Notes) {
			t.Fatalf("%q: v14 %v %v diverged from v13 %v %v", text, v14.Score, v14.Notes, v13.Score, v13.Notes)
		}
	}
}

func TestV14ProtectedPhraseThatIsNotAClaimValueStillCorrects(t *testing.T) {
	for _, text := range []string{"Safe. No worries.", "Safe. No problem."} {
		resp := protocol.RunResponse{FinalText: text}
		v13 := Memory(lessonCase(protocol.BenchVersionV13), resp)
		v14 := Memory(lessonCase(protocol.BenchVersionV14), resp)
		if v13.Score != v14.Score || len(v13.Notes) != len(v14.Notes) {
			t.Fatalf("%q: v14 %v %v diverged from v13 %v %v", text, v14.Score, v14.Notes, v13.Score, v13.Notes)
		}
	}
}

func TestV14PolicyAddsOnlyTheClaimValueGate(t *testing.T) {
	v13 := gradingPolicyForVersion(protocol.BenchVersionV13)
	v14 := gradingPolicyForVersion(protocol.BenchVersionV14)
	if v13.claimValuesAreNotCorrections || !v14.claimValuesAreNotCorrections {
		t.Fatalf("claim-value gate must be v14-only: v13=%+v v14=%+v", v13, v14)
	}
	v14.claimValuesAreNotCorrections = false
	if v14 != v13 {
		t.Fatalf("v14 changed more than the claim-value gate: v13=%+v v14=%+v", v13, v14)
	}
}
