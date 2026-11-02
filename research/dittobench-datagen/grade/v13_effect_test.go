package grade

import (
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"testing"
)

func TestV13MemoryEffectAssertions(t *testing.T) {
	for _, tc := range []struct {
		slot, prose string
		want        float64
	}{
		{"", "Monday, not Friday.", 1},
		{"", "It was Friday, now Monday.", 1},
		{"", "It was Friday, now maybe Monday.", 0},
		{"", "Monday or Friday.", 0},
		{"Monday", "Your notes say Friday and Monday.", 0},
		{"Monday", "", 1},
		{"Friday", "Monday", 0},
		{"", "Friday, not Monday.", 0},
	} {
		got := MemoryEffectV13("Monday", []string{"Friday"}, protocol.RunResponse{Answer: tc.slot, FinalText: tc.prose})
		if got.Score != tc.want {
			t.Fatalf("%+v: %+v", tc, got)
		}
	}
	if got := MemoryEffectV13("new@example.test", []string{"old@example.test"}, protocol.RunResponse{
		FinalText: "new@example.test (previously old@example.test).",
	}); got.Score != 1 {
		t.Fatal(got)
	}
	if got := MemoryEffectV13("4471", nil, protocol.RunResponse{FinalText: "Unit 4,471."}); got.Score != 1 {
		t.Fatal(got)
	}
}
