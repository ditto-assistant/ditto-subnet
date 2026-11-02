package grade

import (
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"reflect"
	"testing"
)

func TestTypedClaimProvenanceTracksOnlyCreditedClaims(t *testing.T) {
	mc := protocol.MemoryCase{BenchVersion: 13, ExpectedAnswer: "stale legacy answer",
		Claims: []protocol.Claim{
			{Kind: "person", Expected: "Alice", Accept: []string{"Alicia"}, Weight: 3},
			{Kind: "action", Expected: "send the report", Weight: 1},
		}}
	v := Memory(mc, protocol.RunResponse{FinalText: "Alicia"})
	if v.Score != .75 || v.Provenance == nil || !reflect.DeepEqual(v.Provenance.Alternatives, [][]string{{"Alice", "Alicia"}}) {
		t.Fatalf("partial typed claim provenance: %+v / %+v", v, v.Provenance)
	}
	mc.Claims[1].Critical = true
	if v := Memory(mc, protocol.RunResponse{FinalText: "Alicia"}); v.Score != 0 || v.Provenance != nil {
		t.Fatalf("failed critical claim leaked provenance: %+v", v)
	}
}

func TestTypedMinorUnitProvenanceDoesNotRequireMajorUnitRewrite(t *testing.T) {
	mc := protocol.MemoryCase{BenchVersion: 13, Claims: []protocol.Claim{{Kind: "quantity", Unit: "cents", Expected: "411067", Critical: true}}}
	v := Memory(mc, protocol.RunResponse{FinalText: "411067"})
	if v.Score != 1 || v.Provenance == nil || !reflect.DeepEqual(v.Provenance.Alternatives, [][]string{{"4110.67", "411067"}}) {
		t.Fatalf("minor-unit provenance: %+v / %+v", v, v.Provenance)
	}
}
