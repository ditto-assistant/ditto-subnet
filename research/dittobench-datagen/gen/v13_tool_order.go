package gen

import (
	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// V13ArrangeToolOrder repairs a projected (per-run permuted) tool order so the
// v13 grouping constraints hold in the order the harness actually sees:
//
//   - two members of one restraint group (ToolCase.TwinGroup) are at least
//     V13TwinGap(n) positions apart and never adjacent, so pairing them in-run
//     (N11) is not cheaper than reading each request;
//   - a follow-up read (ToolCase.RunAfterCaseID) runs at least
//     V13FollowUpGap(n) positions after the mutation it verifies.
//
// The repair is a deterministic function of the incoming order. Constrained
// cases are assigned spread slots first — group members at stride n/k with a
// per-group offset taken from the incoming order, mutations early enough for
// their follow-up to fit, follow-ups after their mutation — and every other
// case fills the remaining slots in incoming order. The incoming permutation
// still decides every relative order the constraints do not touch, so the
// harness sees no fixed structure beyond the gaps.
func V13ArrangeToolOrder(cases []protocol.ToolCase) []protocol.ToolCase {
	n := len(cases)
	if n < 2 {
		return cases
	}
	twinGap := V13TwinGap(n)
	followGap := V13FollowUpGap(n)
	slots := make([]*protocol.ToolCase, n)
	firstFree := func(from int) int {
		for i := 0; i < n; i++ {
			at := (from + i) % n
			if slots[at] == nil {
				return at
			}
		}
		return -1
	}
	place := func(at int, tc protocol.ToolCase) {
		copyTC := tc
		slots[at] = &copyTC
	}
	placed := map[string]bool{}
	position := map[string]int{}

	// 1. Restraint groups: stride n/k from a group offset derived from the
	// incoming rank of the group's first member, bumped forward to a free slot.
	groupOrder := []string{}
	groupMembers := map[string][]protocol.ToolCase{}
	rank := map[string]int{}
	for i, tc := range cases {
		rank[tc.ID] = i
		if tc.TwinGroup == "" {
			continue
		}
		if _, seen := groupMembers[tc.TwinGroup]; !seen {
			groupOrder = append(groupOrder, tc.TwinGroup)
		}
		groupMembers[tc.TwinGroup] = append(groupMembers[tc.TwinGroup], tc)
	}
	for _, group := range groupOrder {
		members := groupMembers[group]
		k := len(members)
		stride := n / k
		if stride < twinGap+1 {
			stride = twinGap + 1
		}
		offset := rank[members[0].ID] % max(1, n-(k-1)*stride)
		for j, tc := range members {
			want := offset + j*stride
			if want >= n {
				want = n - 1
			}
			at := firstFree(want)
			place(at, tc)
			placed[tc.ID] = true
			position[tc.ID] = at
		}
	}

	// 2. Mutation / follow-up pairs: the mutation lands at the first free slot
	// from its incoming rank that leaves room for the follow-up; the follow-up
	// lands at the first free slot at least followGap later (best effort: the
	// latest free slot when the run is too short).
	byID := map[string]protocol.ToolCase{}
	for _, tc := range cases {
		byID[tc.ID] = tc
	}
	for _, tc := range cases {
		if tc.RunAfterCaseID == "" || placed[tc.ID] {
			continue
		}
		mutation, ok := byID[tc.RunAfterCaseID]
		if !ok {
			continue
		}
		if !placed[mutation.ID] {
			// Walk back from the mutation's incoming rank to the latest free slot
			// that still has a free follow-up slot followGap later.
			at := -1
			for w := min(rank[mutation.ID], n-followGap-1); w >= 0; w-- {
				if slots[w] != nil {
					continue
				}
				for i := w + followGap; i < n; i++ {
					if slots[i] == nil {
						at = w
						break
					}
				}
				if at >= 0 {
					break
				}
			}
			if at < 0 {
				at = firstFree(0)
			}
			place(at, mutation)
			placed[mutation.ID] = true
			position[mutation.ID] = at
		}
		at := -1
		for i := position[mutation.ID] + followGap; i < n; i++ {
			if slots[i] == nil {
				at = i
				break
			}
		}
		if at < 0 {
			for i := n - 1; i >= 0; i-- {
				if slots[i] == nil {
					at = i
					break
				}
			}
		}
		place(at, tc)
		placed[tc.ID] = true
		position[tc.ID] = at
	}

	// 3. Everything else in incoming order.
	for _, tc := range cases {
		if placed[tc.ID] {
			continue
		}
		at := firstFree(0)
		place(at, tc)
		placed[tc.ID] = true
	}
	out := make([]protocol.ToolCase, 0, n)
	for _, tc := range slots {
		if tc != nil {
			out = append(out, *tc)
		}
	}
	return out
}

// V13TwinGap is the effective minimum run-order gap between two members of a
// restraint group for a run of n tool cases: the published gap, scaled down
// for short profiles so a medium run can still hold its triplets.
func V13TwinGap(n int) int {
	return min(datagen.V13TwinMinGap, n/5)
}

// V13FollowUpGap is the effective minimum run-order gap between a mutation and
// its follow-up read for a run of n tool cases.
func V13FollowUpGap(n int) int {
	return min(datagen.V13FollowUpMinGap, n*2/5)
}
