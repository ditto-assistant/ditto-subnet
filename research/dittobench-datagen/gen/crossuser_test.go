package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// xuKey identifies the noun a cross-user lifecycle case is about.
func xuNoun(q string) string {
	switch {
	case strings.Contains(q, xuWriteNoun):
		return xuWriteNoun
	case strings.Contains(q, xuDelNoun):
		return xuDelNoun
	}
	return ""
}

// runCrossUser replays the cross-user lifecycle cases through a simulated
// harness. When perUser is false the harness keeps ONE global saved/deleted map
// (the B3 bug); when true it scopes state by user id, which is correct.
// Returns the mean score over the READ cases.
func runCrossUser(t *testing.T, seed int64, perUser bool) (float64, int) {
	t.Helper()
	iso, err := GenerateIsolationForVersion(seed, 50, 3, 8, protocol.BenchVersionV3)
	if err != nil {
		t.Fatalf("isolation: %v", err)
	}
	store := map[string]string{}
	key := func(user, noun string) string {
		if perUser {
			return user + "|" + noun
		}
		return noun // the leak: state is not scoped to a user
	}
	// B's own values arrive by SEEDED HAYSTACK, not by a case, so the simulated
	// harness has to ingest them the way a real one would: read B's pairs into
	// B's memory before any case runs.
	for _, pr := range iso.SecondaryWave.Pairs {
		noun := xuNoun(pr.Prompt)
		if noun == "" {
			continue
		}
		if v := strings.TrimSuffix(pr.Prompt[strings.LastIndex(pr.Prompt, " ")+1:], "."); v != "" {
			store[key(SecondaryUser, noun)] = v
		}
	}
	var total float64
	reads := 0
	for _, sc := range iso.Cases {
		noun := xuNoun(sc.Case.Question)
		if noun == "" {
			continue
		}
		switch sc.Case.QuestionType {
		case QTLifecycleWrite:
			if strings.Contains(sc.Case.Question, "forget") {
				delete(store, key(sc.UserID, noun))
			} else {
				store[key(sc.UserID, noun)] = sc.Case.ExpectedAnswer
			}
		case QTLifecycleRead:
			ans := store[key(sc.UserID, noun)]
			reads++
			total += grade.Memory(sc.Case, protocol.RunResponse{FinalText: ans, Answer: ans}).Score
		}
	}
	return total, reads
}

// TestRedTeamGlobalMemoryMapLeaksCrossUser is the B3 gate. Read-path cross-user
// leakage was already probed, but lifecycle chains carry no user id, so a
// WRITE or DELETE under user A followed by a read of the same attribute under
// user B was never exercised. A harness holding a single global saved/deleted
// map passes every pre-existing case and must fail here: on the write chain it
// returns A's value for B's question (a leak), and on the delete chain A's
// deletion wrongly erases B's value (a leak in the other direction).
func TestRedTeamGlobalMemoryMapLeaksCrossUser(t *testing.T) {
	var globalTotal, perUserTotal float64
	var globalReads, perUserReads int
	for seed := int64(1); seed <= 20; seed++ {
		g, gr := runCrossUser(t, seed, false)
		p, pr := runCrossUser(t, seed, true)
		globalTotal, globalReads = globalTotal+g, globalReads+gr
		perUserTotal, perUserReads = perUserTotal+p, perUserReads+pr
	}
	if globalReads == 0 || perUserReads == 0 {
		t.Fatal("no cross-user lifecycle read cases generated — B3 is not in effect")
	}
	globalMean := globalTotal / float64(globalReads)
	perUserMean := perUserTotal / float64(perUserReads)
	if perUserMean < 0.99 {
		t.Fatalf("correctly user-scoped harness scored %.3f, want ~1.0 — the probe false-fails a harness that does the right thing", perUserMean)
	}
	if globalMean > 0.01 {
		t.Fatalf("global-map harness scored %.3f over %d reads, want ~0 — the cross-user lifecycle leak is not detected", globalMean, globalReads)
	}
	t.Logf("cross-user lifecycle: global-map %.3f vs user-scoped %.3f over %d reads", globalMean, perUserMean, globalReads)
}

// TestCrossUserLifecycleIsUserScoped pins the wiring: the mutation half must run
// under the primary user and the read half under the secondary one. If both ran
// under the same user the case would silently degrade into an ordinary
// single-user lifecycle chain and detect nothing.
func TestCrossUserLifecycleIsUserScoped(t *testing.T) {
	iso, err := GenerateIsolationForVersion(7, 50, 3, 8, protocol.BenchVersionV3)
	if err != nil {
		t.Fatalf("isolation: %v", err)
	}
	writes, reads := 0, 0
	for _, sc := range iso.Cases {
		if xuNoun(sc.Case.Question) == "" {
			continue
		}
		switch sc.Case.QuestionType {
		case QTLifecycleWrite:
			writes++
			if sc.UserID != PrimaryUser {
				t.Errorf("case %s mutates under %q, want %q", sc.Case.QuestionID, sc.UserID, PrimaryUser)
			}
		case QTLifecycleRead:
			reads++
			if sc.UserID != SecondaryUser {
				t.Errorf("case %s reads under %q, want %q", sc.Case.QuestionID, sc.UserID, SecondaryUser)
			}
			if sc.RunAfterWave == 0 {
				t.Errorf("case %s reads in wave 0, before the mutation it is meant to follow", sc.Case.QuestionID)
			}
		}
	}
	if writes != 3 || reads != 2 {
		t.Fatalf("got %d mutation and %d read cases, want 3 and 2", writes, reads)
	}
}
