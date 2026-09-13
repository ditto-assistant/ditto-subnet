package universe

import (
	"fmt"
	"hash/fnv"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// assignV13Timestamps (#1827) stamps every rendered world pair with a seeded
// business-hours instant drawn from the shared v13 calendar window, then
// restores chronology only where an oracle relies on it: a person's work record
// follows their identity record and their address correction follows the
// original address; a project's approval correction follows its ledger row; a
// trip's correction follows its first plan; a story arc runs origin -> decision
// -> outcome.
//
// Each such chain is anchored at one uniform instant and its later members land
// one to four working days after the previous one with an independent time of
// day, so an arc's three parts share a month, a member's hour says nothing about
// its slot, and only a reader who has already grouped the records by content
// can recover their order. Every other pair is an independent uniform draw.
//
// A single monotonic timeline over the emission order would have kept the
// 137-hour stride's real leak: pairs are rendered people-first, projects next,
// trips, then stories, so the month alone named the family; sorting independent
// draws inside each chain would instead have let the month name the story slot
// (the earliest of three uniform instants is the origin).
func (w World) assignV13Timestamps(pairs []protocol.MemoryPair) {
	index := make(map[string]int, len(pairs))
	for i := range pairs {
		index[pairs[i].PairID] = i
		pairs[i].Timestamp = protocol.OpaqueBusinessInstant(w.Seed, fmt.Sprintf("v13-world-stamp-%d", i)).Format(time.RFC3339)
	}
	var chains [][]string
	for _, p := range w.People {
		chains = append(chains, []string{p.IdentityPairID, p.WorkPairID}, []string{p.EmailPairID, p.CorrectionPairID})
	}
	for _, p := range w.Projects {
		chains = append(chains, []string{p.LedgerPairID, p.CorrectionPairID})
	}
	for _, trip := range w.Trips {
		chains = append(chains, []string{trip.PlanPairID, trip.CorrectionPairID})
	}
	for _, arc := range w.StoryArcs {
		chains = append(chains, arc.StoryPairIDs[:])
	}
	for k, chain := range chains {
		at := protocol.OpaqueBusinessInstant(w.Seed, fmt.Sprintf("v13-world-chain-%d", k))
		for j, id := range chain {
			i, ok := index[id]
			if !ok {
				continue
			}
			if j > 0 {
				days := 1 + int(v13ChainDraw(w.Seed, k, j, "days")%4)
				minute := int(v13ChainDraw(w.Seed, k, j, "minute") % 600)
				at = protocol.ShiftBusinessDays(at, days, minute)
			}
			pairs[i].Timestamp = at.Format(time.RFC3339)
		}
	}
}

func v13ChainDraw(seed int64, chain, member int, salt string) uint64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v13-world-chain|%d|%d|%d|%s", seed, chain, member, salt)
	z := h.Sum64() + 0x9E3779B97F4A7C15
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
	z = (z ^ (z >> 27)) * 0x94D049BB133111EB
	return z ^ (z >> 31)
}
