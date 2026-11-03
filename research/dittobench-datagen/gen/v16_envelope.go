package gen

import "github.com/ditto-assistant/dittobench-datagen/protocol"

// Bench v16 dueling profile: the DATAGEN HALF of the dueling run
// (docs/dueling-bench.md sections 1 and 7.1).
//
// A canonical v16 run is 350 cases: 175 datagen cases generated here plus five
// generator columns of 35 cases that Platform delivers (dueling-slice-v1). The
// datagen half is the v15 contract (catalog, decision lane, graders, gates) at
// a smaller envelope, so `full` is 50 tool cases and a 125-case memory slot
// table. The 175/175 split is a contract constant: changing either count is a
// new bench version.
//
// One shared world. The tool phase seeds the coherent world through the v9
// world carrier and the memory phase stages corrections against the same
// world, so the two phases must draw one world scale. 50 tool cases select the
// scale-2 world (the tool passes key their world on the case count, >= 30), so
// the v16 memory table is built on the scale-2 world too: six story arcs, the
// scale-2 abstention, as-of and isolation families, and a larger share of the
// world-independent families (programs, record quantity, divergence) than the
// v13 medium table carries. The memory slot table, not Profile.Mem, is the
// contract; Mem is the key that selects the table and the world scale.
//
// Medium scales down the same way the earlier contracts do (roughly 0.4 of the
// full run): 24 tool cases on the scale-1 world with a 43-case table. Small is
// the v13 smoke profile unchanged.

// profilesV16 is the dueling datagen-half envelope. Profile.Mem is a key: full
// 96 and medium 24 select the scale-2 and scale-1 worlds (v8WorldProfile) and
// the v16 slot tables below.
var profilesV16 = map[string]Profile{
	"small":  {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
	"medium": {Tools: 24, Mem: 24, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
	"full":   {Tools: 50, Mem: 96, Waves: 4, RawPairsFrac: 0.45, IsoCases: 5},
}

// V16DatagenHalfCases is the full-profile datagen-half case count (tool plus
// memory). The generator half is the same size (protocol dueling constants).
const V16DatagenHalfCases = 175

// V16FullEnvelope is the published 125-case full-profile memory table on the
// scale-2 world. The scale-2 world fixes story (6 arcs x 6 oracles), the
// abstention families (8), the as-of twins (4) and isolation (5); the
// integrity tail is the full 14 (three chitchat, three acknowledgement, three
// behaviour, one canary, four injection). The world-independent families take
// the rest at about the v13 full proportions.
//
// Record quantity is held at 4 (the smallest compiler group) and personal
// programs take the slack, so the money share of computed answers stays under
// the v13 cap at the smaller envelope (cmd/mixaudit -bench-version 16 -gate:
// 0.227 worst case over seeds 1..40, cap 0.25).
var V16FullEnvelope = V13MemoryEnvelope{
	Story: 36, OrdinaryWorld: 14, BusinessPrograms: 16, PersonalPrograms: 16, Abstention: 8,
	RecordQuantity: 4, Divergence: 8, PointInTime: 4, Integrity: 14, Isolation: 5,
}

// v16MediumEnvelope is the 43-case medium table on the scale-1 world (one
// story arc, no abstention, as-of or isolation families, the three-probe
// injection tail).
var v16MediumEnvelope = V13MemoryEnvelope{
	Story: 6, OrdinaryWorld: 4, BusinessPrograms: 8, PersonalPrograms: 4, Abstention: 0,
	RecordQuantity: 4, Divergence: 4, PointInTime: 0, Integrity: 13, Isolation: 0,
}

// v16EnvelopeFor maps a v16 Profile.Mem key to its slot table. Like
// v13EnvelopeFor it fails closed on any size that is not a public run size.
func v16EnvelopeFor(mem int) (V13MemoryEnvelope, bool) {
	switch mem {
	case profilesV16["full"].Mem:
		return V16FullEnvelope, true
	case profilesV16["medium"].Mem:
		return v16MediumEnvelope, true
	case profilesV16["small"].Mem:
		return v13SmallEnvelope, true
	default:
		return V13MemoryEnvelope{}, false
	}
}

// memoryEnvelopeForVersion selects the slot table a contract publishes for a
// Profile.Mem key: the v16 dueling tables from v16, the v13 tables for v13
// through v15.
func memoryEnvelopeForVersion(mem, benchVersion int) (V13MemoryEnvelope, bool) {
	if !protocol.GeneratableBenchVersion(benchVersion) {
		// A version this module cannot generate publishes no slot table; it
		// never inherits the newest one through the floor below.
		return V13MemoryEnvelope{}, false
	}
	if benchVersion >= protocol.BenchVersionV16 {
		return v16EnvelopeFor(mem)
	}
	return v13EnvelopeFor(mem)
}

// MemoryEnvelopeForRunSize is the version-aware public accessor for audits and
// tests (V13EnvelopeForRunSize stays the v13..v15 table).
func MemoryEnvelopeForRunSize(runSize string, benchVersion int) (V13MemoryEnvelope, bool) {
	prof, ok := ProfileForVersion(runSize, benchVersion)
	if !ok || benchVersion < protocol.BenchVersionV13 {
		return V13MemoryEnvelope{}, false
	}
	return memoryEnvelopeForVersion(prof.Mem, benchVersion)
}

// profilesForEnvelope returns the run-size profiles whose slot tables a
// contract publishes (v13 tables for v13..v15, dueling tables from v16).
func profilesForEnvelope(benchVersion int) map[string]Profile {
	if benchVersion >= protocol.BenchVersionV16 {
		return profilesV16
	}
	return profilesV13
}

// MixGateV16 is the #1529 / #1848 gate at the dueling envelope. Every cap and
// floor is MixGateV13's except the abstention target: the scale-2 world fixes
// eight decision pairs (universe.V13AbsenceFamilyCounts), so abstention is
// 8/125 = 6.4% of the table (16 cases, 12.8%, with the answerable twins). The
// world is deliberately not rebalanced for v16: it is the same scale-2 world
// v13..v15 build, which the tool phase also reads.
var MixGateV16 = func() MixGate {
	g := MixGateV13
	g.AbstentionTarget = float64(V16FullEnvelope.Abstention) / float64(V16FullEnvelope.Total())
	return g
}()

// MixGateForVersion is the full-profile mix gate a contract publishes.
func MixGateForVersion(benchVersion int) MixGate {
	if benchVersion >= protocol.BenchVersionV16 {
		return MixGateV16
	}
	return MixGateV13
}
