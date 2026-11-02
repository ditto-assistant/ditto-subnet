package grade_test

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV13OrderClaimCannotBeOverriddenByLegacyFields(t *testing.T) {
	mc := protocol.MemoryCase{BenchVersion: 13, ExpectedAnswer: "unused", Claims: []protocol.Claim{
		{Kind: protocol.ClaimKindOrder, Expected: "Alder -> Birch", Critical: true},
	}}
	for _, tc := range []struct {
		answer, prose string
		want          float64
	}{
		{"Alder then Birch", "", 1},
		{"", "First Alder, then Birch.", 1},
		{"Alder then Birch", "First Alder, then Birch.", 1},
		{"Birch then Alder", "", 0},
		{"Alder then Birch", "First Birch, then Alder.", 0},
		{"", "It is not Alder then Birch.", 0},
		{"", "Was it Alder then Birch?", 0},
	} {
		got := grade.Memory(mc, protocol.RunResponse{Answer: tc.answer, FinalText: tc.prose})
		if got.Score != tc.want {
			t.Errorf("%q / %q: %+v, want %v", tc.answer, tc.prose, got, tc.want)
		}
	}
	for _, malformed := range []string{"Alder", "Alder -> ", "Alder -> Alder"} {
		mc.Claims[0].Expected = malformed
		if got := grade.Memory(mc, protocol.RunResponse{Answer: "Alder"}); got.Score != 0 {
			t.Errorf("malformed order %q: %+v", malformed, got)
		}
	}
}
