package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func lifecycleSuiteFor(t *testing.T, seed int64, n, waves int) MemorySuite {
	t.Helper()
	return GenerateMemorySuite(NewRNG(seed), seed, n, waves, 0.35)
}

func lifecycleCases(s MemorySuite) map[string]StagedCase {
	out := map[string]StagedCase{}
	for _, sc := range s.Cases {
		if strings.HasPrefix(sc.Case.QuestionID, "lc-") {
			out[sc.Case.QuestionID] = sc
		}
	}
	return out
}

func waveText(s MemorySuite) string {
	var b strings.Builder
	for _, w := range s.Waves {
		for _, p := range w.Pairs {
			b.WriteString(p.Prompt)
			b.WriteByte(' ')
			b.WriteString(p.Response)
			b.WriteByte(' ')
		}
	}
	return b.String()
}

// TestLifecycleQuotas pins the seed-independent chain counts per run size:
// full carries all three chains, medium the save chain, small none.
func TestLifecycleQuotas(t *testing.T) {
	for _, seed := range []int64{1, 42, 123456789} {
		if got := len(lifecycleCases(lifecycleSuiteFor(t, seed, 50, 2))); got != 6 {
			t.Fatalf("seed %d full: %d lifecycle cases, want 6", seed, got)
		}
		if got := len(lifecycleCases(lifecycleSuiteFor(t, seed, 20, 2))); got != 2 {
			t.Fatalf("seed %d medium: %d lifecycle cases, want 2", seed, got)
		}
		if got := len(lifecycleCases(lifecycleSuiteFor(t, seed, 6, 1))); got != 0 {
			t.Fatalf("seed %d small: %d lifecycle cases, want 0", seed, got)
		}
	}
}

// TestLifecycleStaging asserts every instruction runs strictly before its
// read: instructions unlock at wave 0, reads at the last wave.
func TestLifecycleStaging(t *testing.T) {
	s := lifecycleSuiteFor(t, 99, 50, 2)
	lc := lifecycleCases(s)
	for _, id := range []string{"lc-save-w", "lc-upd-w", "lc-del-w"} {
		if lc[id].RunAfterWave != 0 {
			t.Fatalf("%s unlock wave = %d, want 0", id, lc[id].RunAfterWave)
		}
	}
	for _, id := range []string{"lc-save-r", "lc-upd-r", "lc-del-r"} {
		if lc[id].RunAfterWave != 1 {
			t.Fatalf("%s unlock wave = %d, want 1 (last)", id, lc[id].RunAfterWave)
		}
	}
}

// TestLifecycleValuePlacement is the leak guard: the save and update target
// values exist ONLY in instruction text (never in any haystack wave), while
// the update chain's old value and the delete chain's value ARE seeded.
func TestLifecycleValuePlacement(t *testing.T) {
	for _, seed := range []int64{7, 4242} {
		s := lifecycleSuiteFor(t, seed, 50, 2)
		lc := lifecycleCases(s)
		hay := waveText(s)

		saveVal := lc["lc-save-r"].Case.ExpectedAnswer
		updNew := lc["lc-upd-r"].Case.ExpectedAnswer
		if !strings.Contains(lc["lc-save-w"].Case.Question, saveVal) {
			t.Fatalf("seed %d: save instruction does not carry the value verbatim", seed)
		}
		if !strings.Contains(lc["lc-upd-w"].Case.Question, updNew) {
			t.Fatalf("seed %d: update instruction does not carry the new value verbatim", seed)
		}
		if strings.Contains(hay, saveVal) || strings.Contains(hay, updNew) {
			t.Fatalf("seed %d: a write-target value leaked into the haystack", seed)
		}

		delVal := lifecycleToken(seed, "lc-del")
		updOld := lifecycleToken(seed, "lc-upd-old")
		if !strings.Contains(hay, delVal) || !strings.Contains(hay, updOld) {
			t.Fatalf("seed %d: seeded chain values missing from the haystack", seed)
		}
		if strings.Contains(lc["lc-del-w"].Case.Question, delVal) {
			t.Fatalf("seed %d: delete instruction restates the stored value", seed)
		}
		// Lifecycle pairs land in wave 0 so instructions can act on them.
		w0 := s.Waves[0]
		found := 0
		for _, p := range w0.Pairs {
			if strings.HasPrefix(p.PairID, "p-lc-") {
				found++
			}
		}
		if found != 2 {
			t.Fatalf("seed %d: %d lifecycle pairs in wave 0, want 2", seed, found)
		}
	}
}

// TestLifecycleGrading exercises the grader over the chain outcomes: a landed
// write credits, a shotgun of other chains' tokens zeroes, a failed delete
// (surfacing the deleted value) zeroes, and a grounded decline credits.
func TestLifecycleGrading(t *testing.T) {
	seed := int64(1234)
	s := lifecycleSuiteFor(t, seed, 50, 2)
	lc := lifecycleCases(s)
	saveVal := lc["lc-save-r"].Case.ExpectedAnswer
	updNew := lc["lc-upd-r"].Case.ExpectedAnswer
	delVal := lifecycleToken(seed, "lc-del")

	if v := grade.Memory(lc["lc-save-r"].Case, protocol.RunResponse{FinalText: "Your gym locker code is " + saveVal + "."}); v.Score != 1 {
		t.Fatalf("landed save scored %v, want 1 (%v)", v.Score, v.Notes)
	}
	if v := grade.Memory(lc["lc-save-r"].Case, protocol.RunResponse{FinalText: "It could be " + saveVal + " or " + updNew + "."}); v.Score != 0 {
		t.Fatalf("token shotgun scored %v, want 0", v.Score)
	}
	if v := grade.Memory(lc["lc-upd-r"].Case, protocol.RunResponse{FinalText: "It changed from the old one to " + updNew + "."}); v.Score != 1 {
		t.Fatalf("landed update scored %v, want 1 (%v)", v.Score, v.Notes)
	}
	if v := grade.Memory(lc["lc-del-r"].Case, protocol.RunResponse{FinalText: "Your bike lock code is " + delVal + "."}); v.Score != 0 {
		t.Fatalf("failed delete scored %v, want 0", v.Score)
	}
	if v := grade.Memory(lc["lc-del-r"].Case, protocol.RunResponse{FinalText: "I no longer have that on file. You asked me to remove it."}); v.Score != 1 {
		t.Fatalf("grounded post-delete decline scored %v, want 1 (%v)", v.Score, v.Notes)
	}
	if v := grade.Memory(lc["lc-save-w"].Case, protocol.RunResponse{FinalText: "Saved. Your gym locker code is " + saveVal + "."}); v.Score != 1 {
		t.Fatalf("save acknowledgment scored %v, want 1 (%v)", v.Score, v.Notes)
	}
}

// TestLifecycleBudget asserts lifecycle cases come out of the memory budget
// (total case count stays at n plus the never-sampled canary/twin overhead
// baseline), not on top of it: a full suite is the same size with and without
// the feature by construction, so we assert the absolute quota holds.
func TestLifecycleBudget(t *testing.T) {
	s := lifecycleSuiteFor(t, 5, 50, 2)
	if got := len(s.Cases); got != 50 {
		t.Fatalf("full suite has %d cases, want 50", got)
	}
	if s.LifecycleCases != 6 {
		t.Fatalf("LifecycleCases = %d, want 6", s.LifecycleCases)
	}
}
