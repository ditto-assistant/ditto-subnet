package universe

import (
	"fmt"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// caseFileStoryOrder returns, for each presented row, the story position its
// filing date must follow. A case file shuffles only the rows after offset,
// and v13Perm draws from nothing but the seed and a salt, so the story order
// is recoverable without reproducing the generator's name draws.
func caseFileStoryOrder(seed int64, salt string, pairs, offset int) []int {
	perm := v13Perm(seed, salt, pairs-offset)
	order := make([]int, pairs)
	for i := range order {
		if i < offset {
			order[i] = i
			continue
		}
		order[i] = offset + perm[i-offset]
	}
	return order
}

// caseFileDatesOutOfOrder counts the pairs of rows whose filing dates disagree
// with the story: an earlier story position filed no earlier than a later one.
func caseFileDatesOutOfOrder(t *testing.T, cases []V10GeneratedCase, seed int64, salt func(group, pairs int) string, offset int) int {
	t.Helper()
	out := 0
	for index, c := range cases {
		group := index / 4
		order := caseFileStoryOrder(seed, salt(group, len(c.Pairs)), len(c.Pairs), offset)
		at := make([]time.Time, len(c.Pairs))
		for i, pair := range c.Pairs {
			parsed, err := time.Parse(time.RFC3339, pair.Timestamp)
			if err != nil {
				t.Fatalf("seed %d case %d row %d: %v", seed, index, i, err)
			}
			at[i] = parsed
		}
		for i := range order {
			for j := range order {
				if order[i] < order[j] && !at[i].Before(at[j]) {
					out++
				}
			}
		}
	}
	return out
}

func personalCaseFileSalt(group, pairs int) string {
	return fmt.Sprintf("p-rows-%d-%d", group, pairs)
}

func businessCaseFileSalt(group, _ int) string {
	return fmt.Sprintf("rows-%d", group)
}

// TestV14CaseFileDatesFollowTheStory is the generator invariant
// ditto-subnet#3088 asks for. Case-file groups are graded by `latest` over the
// narrative, so the record that supersedes another must not be filed before
// the record it replaces. v13 stamped each row from its shuffled position, so
// a plain statement could be dated after the change that replaced it, and a
// memory agent that trusted the latest filing date was marked wrong for it.
func TestV14CaseFileDatesFollowTheStory(t *testing.T) {
	for seed := int64(1); seed <= 20; seed++ {
		personal, err := GenerateV13PersonalProgramsForVersion(seed, 24, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		if n := caseFileDatesOutOfOrder(t, personal, seed, personalCaseFileSalt, 0); n != 0 {
			t.Fatalf("seed %d: %d personal case-file row pairs are filed against the story", seed, n)
		}
		business, err := GenerateV13ProgramsForVersion(seed, 28, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		if n := caseFileDatesOutOfOrder(t, business, seed, businessCaseFileSalt, 2); n != 0 {
			t.Fatalf("seed %d: %d business case-file row pairs are filed against the story", seed, n)
		}
	}
}

// TestV13CaseFileDatesKeepTheFrozenShuffleOrder records that v13 still files
// rows from their shuffled position. The live contract is frozen, so the fix
// is v14 only and v13 seeds must not move. One ordered shuffle proves nothing,
// so this counts across seeds; if it ever reaches zero, v13 generation changed
// and the frozen known vector should have caught it first.
func TestV13CaseFileDatesKeepTheFrozenShuffleOrder(t *testing.T) {
	total := 0
	for seed := int64(1); seed <= 20; seed++ {
		personal, err := GenerateV13PersonalPrograms(seed, 24)
		if err != nil {
			t.Fatal(err)
		}
		total += caseFileDatesOutOfOrder(t, personal, seed, personalCaseFileSalt, 0)
		business, err := GenerateV13Programs(seed, 28)
		if err != nil {
			t.Fatal(err)
		}
		total += caseFileDatesOutOfOrder(t, business, seed, businessCaseFileSalt, 2)
	}
	if total == 0 {
		t.Fatal("v13 case files are already filed in story order; the frozen v13 contract moved")
	}
}

// TestV13CaseFileSlotIsVersionGated pins the rule itself: v13 keeps the
// presented position, v14 takes the story position, and rows before the offset
// are never shuffled in either contract.
func TestV13CaseFileSlotIsVersionGated(t *testing.T) {
	perm := []int{2, 0, 3, 1}
	for presented := range perm {
		if got := v13CaseFileSlot(protocol.BenchVersionV13, perm, presented, 0); got != presented {
			t.Fatalf("v13 row %d: got slot %d, want the presented position", presented, got)
		}
		if got := v13CaseFileSlot(protocol.BenchVersionV14, perm, presented, 0); got != perm[presented] {
			t.Fatalf("v14 row %d: got slot %d, want story position %d", presented, got, perm[presented])
		}
	}
	for presented := 0; presented < 2; presented++ {
		for _, version := range []int{protocol.BenchVersionV13, protocol.BenchVersionV14} {
			if got := v13CaseFileSlot(version, perm, presented, 2); got != presented {
				t.Fatalf("v%d fixed row %d: got slot %d, want it unshuffled", version, presented, got)
			}
		}
	}
	if got := v13CaseFileSlot(protocol.BenchVersionV14, perm, 3, 2); got != 2+perm[1] {
		t.Fatalf("v14 row 3 after two fixed rows: got slot %d, want %d", got, 2+perm[1])
	}
}
