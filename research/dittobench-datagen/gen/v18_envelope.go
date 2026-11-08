package gen

import (
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Bench v18 accuracy profile: the v17 dueling datagen half (50 tool + 125
// memory cases on the v15 full world) with the memory slots moved out of the
// families every v17 agent solves and into the ones that separated them.
//
// Evidence: all 27 v17 production score rows (2026-11-07). The full-profile
// per-category means that drove each move, all agents / top three agents:
//
//   - world-isolation-contact-current 1.000 / 1.000 over 252 cases: isolation
//     9 -> 5 (the medium quota; the world allocation keeps every cross-user
//     absence person, i < 5 in steps of two).
//   - story: six oracles per arc averaged 0.96-0.99 and story-lesson-claims
//     was 1.000 on all 46 cases, while story-status-disagree sat at 0.879.
//     V18 asks seven arcs with five oracles each, dropping lesson-claims
//     first (universe.V18SaturatedStoryOracles), so more arcs and more of the
//     status, order and next-action oracles reach a run.
//   - business programs: v16/v17 render one family group per seed, always
//     owner-after-correction (0.973 / 1.000), and three enterprise groups
//     (0.979 / 0.991). V18 renders three family groups and one enterprise
//     group: group 0 is holder-vs-actor, the one v14 business family the top
//     owners still missed on their v15 rows (0.906), and groups 1-2 are drawn
//     per seed from the other v14 temporal, state-fold and role families
//     (universe.V18FamilyFor).
//   - personal programs 0.946 / 0.917 over 112 cases: one group -> two, the
//     first always subscriptions-renewals (0.850 on v17, 0.891 on v15).
//   - point-in-time contact 0.946, trip leg 0.964: 3 -> 4 of the world's six
//     as-of pairs.
//   - abstention: the probes, not their answerable twins, are where agents
//     fail (stale-removed 0.821, cross-user 0.893, pure absence 0.911, false
//     premise 0.917, insufficient composition 0.917, near miss 0.946; every
//     twin family >= 0.95). The slot stays at 12 (the 10% +/- 1 mix target)
//     and v18AbstentionQuotas moves weight from near miss and insufficient
//     composition to stale-removed and cross-user (v16: pure 2, near miss 2,
//     stale-removed 1, cross-user 1, false premise 3, insufficient 3; v18:
//     2, 1, 2, 2, 3, 2). A third stale-removed pair would put the contacts
//     sub-domain at 26/125 on 4 of 40 seeds, over the 20% mix cap.
//   - ordinary world 20 -> 19 (one plain question; the 12 answerable twins
//     and the six staged corrections stay).
//
// Integrity (14), record quantity (4) and divergence (4) are unchanged: the
// integrity tail carries injection-envelope-free (0.821), and divergence and
// the canary are anti-gaming probes whose value is not their difficulty.
// conversational-chitchat's 0.500 is the fixed chitchat credit (grade
// gradingPolicy.chitchatCredit, 0.5 since v8), a ceiling rather than a
// difficulty; v18 does not change grading.
//
// Medium and small keep the v16 tables: they are rehearsal sizes.

// profilesV18 is the v18 envelope. Only the full isolation quota moves.
var profilesV18 = map[string]Profile{
	"small":  profilesV16["small"],
	"medium": profilesV16["medium"],
	"full":   {Tools: 50, Mem: 125, Waves: 5, RawPairsFrac: 0.5, IsoCases: 5},
}

// V18FullEnvelope is the published 125-case v18 full-profile memory table.
var V18FullEnvelope = V13MemoryEnvelope{
	Story: 35, OrdinaryWorld: 19, BusinessPrograms: 16, PersonalPrograms: 8, Abstention: 12,
	RecordQuantity: 4, Divergence: 4, PointInTime: 8, Integrity: 14, Isolation: 5,
	StoryArcs: 7, StoryPerArc: 5, EnterprisePrograms: 4, DropSaturatedStoryOracles: true,
}

// v18EnvelopeFor maps a v18 Profile.Mem key to its slot table.
func v18EnvelopeFor(mem int) (V13MemoryEnvelope, bool) {
	if mem == profilesV18["full"].Mem {
		return V18FullEnvelope, true
	}
	return v16EnvelopeFor(mem)
}

// v18AbstentionQuotas fixes how many twin pairs each absence family
// contributes to the v18 12-pair slot. The answerable twins all score >= 0.95
// on v17, so the weight goes where the unanswerable probes fail.
var v18AbstentionQuotas = map[int]map[string]int{
	12: {
		universe.V13FamilyPureAbsence: 2, universe.V13FamilyNearMiss: 1, universe.V13FamilyStaleRemoved: 2,
		universe.V13FamilyCrossUser: 2, universe.V13FamilyFalsePremise: 3, universe.V13FamilyInsufficient: 2,
	},
}

// v18AbstentionSubset keeps the v18 abstention slot's twin pairs by family
// quota, falling back to the v16 rule for sizes v18 does not re-weight.
func v18AbstentionSubset(pairs [][2]StagedCase, want int) [][2]StagedCase {
	quota, ok := v18AbstentionQuotas[want]
	if !ok || len(pairs) <= want {
		return v16AbstentionSubset(pairs, want)
	}
	taken := map[string]int{}
	out := make([][2]StagedCase, 0, want)
	used := make([]bool, len(pairs))
	for i, pair := range pairs {
		family := strings.TrimPrefix(pair[0].Case.QuestionType, QTAbsence)
		if taken[family] < quota[family] {
			taken[family]++
			used[i] = true
			out = append(out, pair)
		}
	}
	// A family the pool under-supplies leaves its quota unfilled: top the slot
	// up to want from the remaining pairs in pool order, so the published
	// 12-pair slot is always full.
	for i, pair := range pairs {
		if len(out) >= want {
			break
		}
		if !used[i] {
			out = append(out, pair)
		}
	}
	return out
}
