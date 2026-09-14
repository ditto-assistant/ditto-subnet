package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV13ProjectionKeepsGroupsApartAndFollowUpsAfterMutations pins the two
// run-order constraints of the v13 tool bench in the order the harness actually
// sees (the per-run permuted HarnessProjection): members of one restraint
// group are never adjacent and at least datagen.V13TwinMinGap apart, and a
// mutation follow-up read runs at least datagen.V13FollowUpMinGap after its
// mutation. It also pins that the grader-only claim identities ride the same
// alias map as RequiredArgs, so a projected pair-id claim names the wire id.
func TestV13ProjectionKeepsGroupsApartAndFollowUpsAfterMutations(t *testing.T) {
	prof, ok := ProfileForVersion("full", protocol.BenchVersionV13)
	if !ok {
		t.Fatal("v13 full profile missing")
	}
	followUps, claims := 0, 0
	for seed := int64(1); seed <= 6; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		key := make([]byte, 32)
		for i := range key {
			key[i] = byte(seed*7 + int64(i))
		}
		projection, err := BuildHarnessProjection(seed, key, protocol.BenchVersionV13, artifact.ToolCases, nil, artifact.MemoryWaves)
		if err != nil {
			t.Fatal(err)
		}
		canonicalPairs := map[string]bool{}
		for _, tc := range artifact.ToolCases {
			for _, pair := range tc.PrerequisitePairs {
				canonicalPairs[pair.PairID] = true
			}
		}
		wirePairs := map[string]bool{}
		for _, tc := range projection.ToolCases {
			for _, pair := range tc.PrerequisitePairs {
				wirePairs[pair.PairID] = true
			}
		}
		position := map[string]int{}
		lastGroup := map[string]int{}
		for i, tc := range projection.ToolCases {
			position[tc.ID] = i
			if tc.TwinGroup != "" {
				if last, seen := lastGroup[tc.TwinGroup]; seen && i-last < V13TwinGap(len(projection.ToolCases)) {
					t.Fatalf("seed %d: group %s members at %d and %d (gap %d < %d)", seed, tc.TwinGroup, last, i, i-last, V13TwinGap(len(projection.ToolCases)))
				}
				lastGroup[tc.TwinGroup] = i
			}
			for _, spec := range tc.ExpectedTools {
				for arg, claim := range spec.RequiredArgClaims {
					claims++
					if want, ok := spec.RequiredArgs[arg]; ok && (claim.Kind == "id" || claim.Kind == "email" || claim.Kind == "enum") && claim.Expected != want {
						t.Fatalf("seed %d: claim %s of %s projected to %q but RequiredArgs to %q", seed, arg, tc.ID, claim.Expected, want)
					}
					for _, forbidden := range claim.Forbidden {
						if claim.Kind == "id" && (canonicalPairs[forbidden] || !wirePairs[forbidden]) {
							t.Fatalf("seed %d: forbidden pair id %q was not projected to a wire pair id", seed, forbidden)
						}
					}
				}
			}
		}
		for _, tc := range projection.ToolCases {
			if tc.RunAfterCaseID == "" {
				continue
			}
			followUps++
			after, ok := position[tc.RunAfterCaseID]
			if !ok {
				t.Fatalf("seed %d: follow-up %s points at %q which is not a projected case", seed, tc.ID, tc.RunAfterCaseID)
			}
			if position[tc.ID]-after < V13FollowUpGap(len(projection.ToolCases)) {
				t.Fatalf("seed %d: follow-up %s at %d, mutation at %d (gap < %d)", seed, tc.ID, position[tc.ID], after, V13FollowUpGap(len(projection.ToolCases)))
			}
		}
		if len(projection.ToolCases) != len(artifact.ToolCases) {
			t.Fatalf("seed %d: projection dropped cases", seed)
		}
	}
	if followUps == 0 || claims == 0 {
		t.Fatalf("no follow-ups (%d) or claims (%d) projected", followUps, claims)
	}
}

// TestV13ArrangeToolOrderIsDeterministicAndComplete: the order repair is a
// pure function of its input, keeps every case exactly once, and leaves an
// unconstrained order untouched.
func TestV13ArrangeToolOrderIsDeterministicAndComplete(t *testing.T) {
	plain := []protocol.ToolCase{{ID: "a"}, {ID: "b"}, {ID: "c"}}
	got := V13ArrangeToolOrder(plain)
	for i := range plain {
		if got[i].ID != plain[i].ID {
			t.Fatalf("unconstrained order moved: %v", got)
		}
	}
	var cases []protocol.ToolCase
	for i := 0; i < 60; i++ {
		tc := protocol.ToolCase{ID: string(rune('A'+i%26)) + string(rune('a'+i/26))}
		switch {
		case i%20 == 0:
			tc.TwinGroup = "g"
		case i == 3:
			tc.RunAfterCaseID = cases[1].ID
		}
		cases = append(cases, tc)
	}
	first := V13ArrangeToolOrder(cases)
	second := V13ArrangeToolOrder(cases)
	seen := map[string]int{}
	last := -1
	for i, tc := range first {
		seen[tc.ID]++
		if tc.ID != second[i].ID {
			t.Fatal("order repair is not deterministic")
		}
		if tc.TwinGroup == "g" {
			if last >= 0 && i-last < V13TwinGap(len(cases)) {
				t.Fatalf("group members at %d and %d", last, i)
			}
			last = i
		}
	}
	if len(seen) != len(cases) {
		t.Fatalf("repair dropped or duplicated cases: %d of %d", len(seen), len(cases))
	}
	for i, tc := range first {
		if tc.RunAfterCaseID != "" {
			at := -1
			for j, other := range first {
				if other.ID == tc.RunAfterCaseID {
					at = j
				}
			}
			if at < 0 || i-at < V13FollowUpGap(len(cases)) {
				t.Fatalf("follow-up at %d, mutation at %d", i, at)
			}
		}
	}
}
