package grade_test

import (
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
	"math"
	"testing"
)

func TestV13GeneratorClaimsGradeEndToEnd(t *testing.T) {
	for _, generator := range []struct {
		name     string
		generate func(int64, int) ([]universe.V10GeneratedCase, error)
	}{{"business", universe.GenerateV13Programs}, {"personal", universe.GenerateV13PersonalPrograms}} {
		t.Run(generator.name, func(t *testing.T) {
			for seed := int64(1); seed <= 40; seed++ {
				cases, err := generator.generate(seed, 28)
				if err != nil {
					t.Fatal(err)
				}
				for _, c := range cases {
					mc := c.Plan.Case
					if len(mc.Claims) == 0 {
						t.Fatal("missing claims")
					}
					response := mc.ExpectedAnswer
					// Claim sets, not a stale legacy flattened answer, are authoritative.
					mc.ExpectedAnswer = "legacy-answer-must-not-be-used"
					if got := grade.Memory(mc, protocol.RunResponse{FinalText: response}); got.Score != 1 {
						t.Fatalf("seed %d case %s claims %+v response %q: %+v", seed, mc.ID, mc.Claims, response, got)
					}
					if got := grade.Memory(mc, protocol.RunResponse{FinalText: "I don't know."}); got.Score != 0 {
						t.Fatalf("ungrounded answer passed: %+v", got)
					}
					for _, claim := range mc.Claims {
						one := mc
						one.Claims = []protocol.Claim{claim}
						for _, accepted := range claim.Accept {
							if got := grade.Memory(one, protocol.RunResponse{Answer: accepted, FinalText: "It is " + accepted + "."}); got.Score != 1 {
								t.Fatalf("seed %d %s %s accepted %q: %+v", seed, generator.name, claim.Kind, accepted, got)
							}
						}
					}
				}
			}
		})
	}
}

func TestV13ClaimSetWeightAndCriticalContract(t *testing.T) {
	mc := protocol.MemoryCase{BenchVersion: 13, Claims: []protocol.Claim{
		{Kind: "person", Expected: "Alice", Accept: []string{"Alicia"}, Weight: 3},
		{Kind: "action", Expected: "send the report", Weight: 1},
	}}
	if got := grade.Memory(mc, protocol.RunResponse{FinalText: "Alicia"}); got.Score != .75 {
		t.Fatal(got)
	}
	mc.Claims[1].Critical = true
	if got := grade.Memory(mc, protocol.RunResponse{FinalText: "Alicia"}); got.Score != 0 {
		t.Fatal(got)
	}
	mc.Claims[1].Critical = false
	for _, w := range []float64{-1, math.NaN(), math.Inf(1)} {
		mc.Claims[1].Weight = w
		if got := grade.Memory(mc, protocol.RunResponse{FinalText: "Alice; send the report"}); got.Score != 0 {
			t.Fatal(got)
		}
	}
	mc.Claims[1].Weight = 0
	mc.Claims[1].Kind = "unknown"
	if got := grade.Memory(mc, protocol.RunResponse{FinalText: "Alice; send the report"}); got.Score != 0 {
		t.Fatal(got)
	}
}

func TestV13ClaimSetRespectsAssertionAndLegacyBoundary(t *testing.T) {
	mc := protocol.MemoryCase{BenchVersion: 13, ExpectedAnswer: "Monday",
		DistractorAnswers: []string{"Friday"},
		Claims:            []protocol.Claim{{Kind: "value", Expected: "Monday", Critical: true}},
	}
	for _, tc := range []struct {
		text  string
		score float64
	}{
		{"Monday, not Friday.", 1}, {"It was Friday; now it is Monday.", 1},
		{"Friday and Monday.", 0}, {"Friday, not Monday.", 0},
	} {
		if got := grade.Memory(mc, protocol.RunResponse{FinalText: tc.text}); got.Score != tc.score {
			t.Fatalf("%q: %+v", tc.text, got)
		}
	}
	mc.BenchVersion = 12
	mc.ExpectedAnswer = "Tuesday"
	if got := grade.Memory(mc, protocol.RunResponse{FinalText: "Monday"}); got.Score != 0 {
		t.Fatal(got)
	}
}

func TestV13ClaimIntrinsicNegationDoesNotExcuseOuterRejection(t *testing.T) {
	for _, tc := range []struct {
		kind, expected, accept, text string
		score                        float64
	}{
		{"status", "cancelled", "not happening", "It is not happening.", 1},
		{"status", "cancelled", "not happening", "It is not cancelled.", 0},
		{"status", "cancelled", "not happening", "It is not not happening.", 0},
		{"conflict", "disagree", "do not agree", "The records do not agree.", 1},
		{"conflict", "disagree", "do not agree", "The records do not disagree.", 0},
		{"status", "superseded", "superseded", "The status is superseded.", 1},
		{"status", "superseded", "superseded", "Previously superseded.", 0},
	} {
		mc := protocol.MemoryCase{BenchVersion: 13, Claims: []protocol.Claim{{Kind: tc.kind, Expected: tc.expected, Accept: []string{tc.accept}, Critical: true}}}
		if got := grade.Memory(mc, protocol.RunResponse{FinalText: tc.text}); got.Score != tc.score {
			t.Fatalf("%+v: %+v", tc, got)
		}
	}
}

func TestV13ClaimQuantityRequestedUnits(t *testing.T) {
	for _, tc := range []struct {
		expected, unit, text string
		score                float64
	}{
		{"2318", "GBP", "£2,318.00", 1}, {"2318", "GBP", "£23.18", 0},
		{"2318", "GBP", "$2,318.00", 0},
		{"411067", "cents", "411067", 1}, {"411067", "cents", "$4,110.67", 1},
		{"411067", "cents", "$411,067", 0},
		{"12", "units", "12 units", 1}, {"12", "units", "13 units", 0},
	} {
		mc := protocol.MemoryCase{BenchVersion: 13, Claims: []protocol.Claim{{Kind: "quantity", Expected: tc.expected, Unit: tc.unit, Critical: true}}}
		if got := grade.Memory(mc, protocol.RunResponse{FinalText: tc.text}); got.Score != tc.score {
			t.Fatalf("%+v: %+v", tc, got)
		}
	}
}
