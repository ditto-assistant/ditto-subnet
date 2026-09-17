package grade_test

import (
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"testing"
)

func TestV13IntrinsicTemporalValuesAreNotSuperseded(t *testing.T) {
	for _, tc := range []struct{ kind, expected, full string }{
		{"action", "cancel the old plan", "cancel the old plan"},
		{"concept", "look", "look before you leap"},
		{"concept", "leap", "look before you leap"},
		{"concept", "current address", "check the current address before sending anything"},
		{"concept", "before sending", "check the current address before sending anything"},
	} {
		mc := protocol.MemoryCase{BenchVersion: 13, Claims: []protocol.Claim{{Kind: tc.kind, Expected: tc.expected, Accept: []string{tc.full}, Critical: true}}}
		for _, prefix := range []string{"", "The answer is "} {
			if got := grade.Memory(mc, protocol.RunResponse{Answer: tc.full, FinalText: prefix + tc.full + "."}); got.Score != 1 {
				t.Fatalf("%q: %+v", tc.full, got)
			}
		}
		for _, prefix := range []string{"Previously ", "Not ", "It used to be "} {
			if got := grade.Memory(mc, protocol.RunResponse{Answer: tc.full, FinalText: prefix + tc.full + "."}); got.Score != 0 {
				t.Fatalf("outer qualifier %q bypassed: %+v", prefix+tc.full, got)
			}
		}
	}
}
