package gen

import (
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Bench v19 generalization envelope, shortcut-breaker half (DITTO-2737): the
// v18 run size (50 tool + 125 memory cases, the tool half unchanged) with ten
// story slots moved into universe.GenerateV19Breakers, two per family.
//
// The v18 top four agents score .921-.988 on the memory half (leaderboard,
// 2026-11-12). Per-family v18 means were not read for this table. The slots
// come from the story slot, 35 -> 25 (five arcs of five oracles instead of
// seven): story was the most saturated slot in the v17 evidence behind
// gen/v18_envelope.go (six oracles averaged 0.96-0.99 per arc), and four of
// the five breaker families aim at rules agents learned from story arcs (the
// lesson oracle, same-day records, person-only holders, condition-free next
// steps). The v18 drop order (universe.V18SaturatedStoryOracles) still trims
// each arc.
//
// Business programs cannot give slots: they move in four-case groups, and
// four fewer business cases put every seed under MixGateV13's 0.40 business
// floor (0.384 with six business breaker cases). Every other slot keeps its
// v18 count. Medium and small keep the v16 tables: they are rehearsal sizes
// and ask no breakers.

// V19FullEnvelope is the published 125-case v19 full-profile memory table.
var V19FullEnvelope = V13MemoryEnvelope{
	Story: 25, OrdinaryWorld: 19, BusinessPrograms: 16, PersonalPrograms: 8, Abstention: 12,
	RecordQuantity: 4, Divergence: 4, PointInTime: 8, Integrity: 14, Isolation: 5,
	StoryArcs: 5, StoryPerArc: 5, EnterprisePrograms: 4, DropSaturatedStoryOracles: true,
	Breakers: 10,
}

// v19EnvelopeFor maps a v19 Profile.Mem key to its slot table.
func v19EnvelopeFor(mem int) (V13MemoryEnvelope, bool) {
	if mem == profilesV18["full"].Mem {
		return V19FullEnvelope, true
	}
	return v18EnvelopeFor(mem)
}

// v19BreakerCases renders the breaker slot as staged cases and their seeded
// records. Each case is its own thread, so the records join wave 0.
func v19BreakerCases(seed int64, world universe.World, count, benchVersion int) ([]StagedCase, []protocol.MemoryPair, error) {
	breakers, err := universe.GenerateV19Breakers(seed, world, count)
	if err != nil {
		return nil, nil, err
	}
	cases := make([]StagedCase, 0, len(breakers))
	var pairs []protocol.MemoryPair
	for _, b := range breakers {
		b.Plan.Case.BenchVersion = benchVersion
		cases = append(cases, StagedCase{Case: b.Plan.Case, RequiredPairIDs: append([]string(nil), b.Plan.RequiredPairIDs...)})
		pairs = append(pairs, b.Pairs...)
	}
	return cases, pairs, nil
}

// v19BreakerMix classifies the breaker families for the mix audit. The
// late-recorded, company-holder and conditional-next threads are work records
// (supplier contracts, custody of work items, project next steps); the
// trigger-fact and near-twin threads are personal. None joins the contacts
// sub-domain, which v18 already holds near its cap.
var v19BreakerMix = map[string]mixFamily{
	universe.V19BreakerTriggerFact:     {V19SlotBreakers, MixDomainPersonal, "household", "trigger-fact", false, false},
	universe.V19BreakerLateRecorded:    {V19SlotBreakers, MixDomainBusiness, "late-records", "effective-date", false, false},
	universe.V19BreakerCompanyHolder:   {V19SlotBreakers, MixDomainBusiness, "custody", "holder", false, false},
	universe.V19BreakerNearTwin:        {V19SlotBreakers, MixDomainPersonal, "people", "name-select", false, false},
	universe.V19BreakerConditionalNext: {V19SlotBreakers, MixDomainBusiness, "next-steps", "next-action-condition", false, false},
}
