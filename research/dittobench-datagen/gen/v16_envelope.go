package gen

import (
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

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
// One shared world, at the v15 scale. The tool phase seeds the coherent world
// through the v9 world carrier and the memory phase stages corrections against
// the same world, so both phases draw one world scale, and v16 pins it per
// profile to the v15 scale of the same run size: the full run reads the v15
// full world (28 people, 18 projects, 12 trips, 13 story arcs) and medium the
// v15 medium world. The tool passes take the scale from
// datagen.ToolWorldScale, which maps the v16 tool counts to these scales
// instead of keying on the case count.
//
// The 125-case table asks about part of that world: six of the 13 story arcs
// (the other seven stay seeded as haystack), half of the world's abstention
// and as-of twins (a family-quota subset; every probe record stays
// planted), and the world-independent families at about the v15 proportions.
// The memory slot table, not Profile.Mem, is the contract; Mem is the key that
// selects the table and the world scale (v8WorldProfile).
//
// Small is the v15 smoke profile unchanged.

// profilesV16 is the dueling datagen-half envelope. Profile.Mem is a key:
// full 125 and medium 64 select the scale-3 and scale-2 worlds
// (v8WorldProfile), the v15 full and medium scales, and the v16 slot tables
// below. Waves, raw-pairs fraction and isolation quota are the v15 values for
// the same world.
var profilesV16 = map[string]Profile{
	"small":  {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
	"medium": {Tools: 24, Mem: 64, Waves: 4, RawPairsFrac: 0.45, IsoCases: 5},
	"full":   {Tools: 50, Mem: 125, Waves: 5, RawPairsFrac: 0.5, IsoCases: 9},
}

// V16DatagenHalfCases is the full-profile datagen-half case count (tool plus
// memory). The generator half is the same size (protocol dueling constants).
const V16DatagenHalfCases = 175

// V16FullEnvelope is the published 125-case full-profile memory table on the
// v15 full (scale-3) world: six story arcs x six oracles, 12 of the world's 25
// abstention twins (about 10%, the v15 share; v16AbstentionQuotas), 3 of its 6
// as-of twin pairs,
// the 14-case integrity tail, the 9 isolation cases the scale-3 world fixes,
// and the world-independent families. Ordinary world holds the 12 answerable
// abstention twins plus 8 plain questions (the staged corrections need six).
var V16FullEnvelope = V13MemoryEnvelope{
	Story: 36, OrdinaryWorld: 20, BusinessPrograms: 16, PersonalPrograms: 4, Abstention: 12,
	RecordQuantity: 4, Divergence: 4, PointInTime: 6, Integrity: 14, Isolation: 9,
	StoryArcs: 6,
}

// v16MediumEnvelope is the medium table on the v15 medium (scale-2) world:
// three story arcs, 4 of 8 abstention twins, 1 of 2 as-of pairs, the full
// integrity tail and the scale-2 isolation quota.
var v16MediumEnvelope = V13MemoryEnvelope{
	Story: 18, OrdinaryWorld: 8, BusinessPrograms: 4, PersonalPrograms: 4, Abstention: 4,
	RecordQuantity: 0, Divergence: 4, PointInTime: 2, Integrity: 14, Isolation: 5,
	StoryArcs: 3,
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
	if benchVersion >= protocol.BenchVersionV19 {
		return v19EnvelopeFor(mem)
	}
	if benchVersion >= protocol.BenchVersionV18 {
		return v18EnvelopeFor(mem)
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
	if benchVersion >= protocol.BenchVersionV18 {
		return profilesV18
	}
	if benchVersion >= protocol.BenchVersionV16 {
		return profilesV16
	}
	return profilesV13
}

// MixGateV16 is the #1529 / #1848 gate at the dueling envelope: MixGateV13
// unchanged, abstention target included (12/125 = 9.6%, inside 10% +/- 1%).
// It stays a named gate so a later v16 calibration has one place to land.
var MixGateV16 = MixGateV13

// MixGateForVersion is the full-profile mix gate a contract publishes.
func MixGateForVersion(benchVersion int) MixGate {
	if benchVersion >= protocol.BenchVersionV16 {
		return MixGateV16
	}
	return MixGateV13
}

// v16AbstentionQuotas fixes, per v16 abstention slot size, how many twin pairs
// each absence family contributes. The two misleading-evidence families that
// are not about contacts (false premise on trips, insufficient composition on
// projects) take one more pair each than a plain rotation would give them:
// the v16 table asks half the world's twins, and an even split would put the
// contacts sub-domain over the 20% mix cap. Every family stays represented.
var v16AbstentionQuotas = map[int]map[string]int{
	12: {
		universe.V13FamilyPureAbsence: 2, universe.V13FamilyNearMiss: 2, universe.V13FamilyStaleRemoved: 1,
		universe.V13FamilyCrossUser: 1, universe.V13FamilyFalsePremise: 3, universe.V13FamilyInsufficient: 3,
	},
	4: {
		universe.V13FamilyPureAbsence: 1, universe.V13FamilyNearMiss: 1,
		universe.V13FamilyFalsePremise: 1, universe.V13FamilyInsufficient: 1,
	},
}

// v16AbstentionSubset keeps the v16 abstention slot's twin pairs by family
// quota, preserving the builder's order.
func v16AbstentionSubset(pairs [][2]StagedCase, want int) [][2]StagedCase {
	quota, ok := v16AbstentionQuotas[want]
	if !ok || len(pairs) <= want {
		return balancedTwinSubset(pairs, want)
	}
	taken := map[string]int{}
	out := make([][2]StagedCase, 0, want)
	for _, pair := range pairs {
		family := strings.TrimPrefix(pair[0].Case.QuestionType, QTAbsence)
		if taken[family] < quota[family] {
			taken[family]++
			out = append(out, pair)
		}
	}
	return out
}

// balancedTwinSubset keeps want twin pairs, taking one pair per family in
// rotation (families in first-appearance order) and preserving the builder's
// order among the kept pairs. A builder that produced want pairs or fewer is
// returned unchanged, so the generator's own count check still fails closed.
func balancedTwinSubset(pairs [][2]StagedCase, want int) [][2]StagedCase {
	if want < 0 || len(pairs) <= want {
		return pairs
	}
	var families []string
	byFamily := map[string][]int{}
	for i, pair := range pairs {
		family := pair[0].Case.QuestionType
		if _, seen := byFamily[family]; !seen {
			families = append(families, family)
		}
		byFamily[family] = append(byFamily[family], i)
	}
	keep := make([]bool, len(pairs))
	for kept, round := 0, 0; kept < want; round++ {
		for _, family := range families {
			if kept == want {
				break
			}
			if round < len(byFamily[family]) {
				keep[byFamily[family][round]] = true
				kept++
			}
		}
	}
	out := make([][2]StagedCase, 0, want)
	for i, pair := range pairs {
		if keep[i] {
			out = append(out, pair)
		}
	}
	return out
}
