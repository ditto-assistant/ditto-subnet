package challenge

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func oracleText(oc OracleCase) string {
	var b strings.Builder
	for _, m := range oc.Memories {
		b.WriteString(m.Prompt)
		b.WriteString("\n")
		b.WriteString(m.Response)
		b.WriteString("\n")
	}
	return b.String()
}

func TestOracleContextHoldsEvidenceAndDeclaredCompetitors(t *testing.T) {
	p := ReferencePack(21, testHotkey)
	if r := Validate(p); !r.Valid {
		t.Fatalf("reference pack invalid: %+v", r.Issues)
	}
	pairs := map[string]protocol.MemoryPair{}
	for _, w := range p.MemoryWaves {
		for _, pr := range w.Pairs {
			pairs[pr.PairID] = pr
		}
	}
	for _, c := range p.Cases {
		oc, err := OracleContext(p, c.ID)
		if err != nil {
			t.Fatalf("%s: %v", c.ID, err)
		}
		if oc.Contract != OracleContractV1 || oc.Question != c.Question || len(oc.Memories) > OracleMaxPairs {
			t.Fatalf("%s: malformed context %+v", c.ID, oc)
		}
		text := oracleText(oc)
		for _, pid := range c.EvidencePairIDs {
			if !strings.Contains(text, pairs[pid].Response) {
				t.Errorf("%s: evidence pair %s missing", c.ID, pid)
			}
		}
		// The declared distractors are in memory (the dump guard requires
		// it), so the oracle must see them and choose against them.
		for _, d := range c.DistractorAnswers {
			if !strings.Contains(text, d) {
				t.Errorf("%s: declared competitor %q missing from the oracle context", c.ID, d)
			}
		}
		// Chronological, and nothing names a pair or marks the evidence.
		for i := 1; i < len(oc.Memories); i++ {
			a, _ := time.Parse(time.RFC3339, oc.Memories[i-1].Timestamp)
			b, _ := time.Parse(time.RFC3339, oc.Memories[i].Timestamp)
			if b.Before(a) {
				t.Fatalf("%s: memories out of order", c.ID)
			}
		}
		raw, _ := json.Marshal(oc.Memories)
		for _, leak := range []string{"pair_id", "session_id", "evidence"} {
			if strings.Contains(string(raw), leak) {
				t.Fatalf("%s: oracle memories leak %q", c.ID, leak)
			}
		}
		if err := VerifyOracleContext(p, oc); err != nil {
			t.Fatalf("%s: %v", c.ID, err)
		}
		again, _ := OracleContext(p, c.ID)
		if again.ContextSHA256 != oc.ContextSHA256 {
			t.Fatalf("%s: context is not deterministic", c.ID)
		}
	}
}

func TestOracleContextIsBoundedOnAHugeHaystack(t *testing.T) {
	p := ReferencePack(7, testHotkey)
	// Bury the haystack in near-duplicates that share the question's terms.
	c := p.Cases[0]
	ts := time.Date(2026, time.March, 1, 0, 0, 0, 0, time.UTC)
	for i := 0; i < 1500; i++ {
		p.MemoryWaves[0].Pairs = append(p.MemoryWaves[0].Pairs, protocol.MemoryPair{
			PairID: fmt.Sprintf("bulk%04d", i), SessionID: "bulk",
			Timestamp: ts.Add(time.Duration(i) * time.Minute).Format(time.RFC3339),
			Prompt:    c.Question + fmt.Sprintf(" (asked again, variant %d)", i),
			Response:  "I am not sure, let me check.",
		})
	}
	oc, err := OracleContext(p, c.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(oc.Memories) > OracleMaxPairs {
		t.Fatalf("context holds %d pairs, cap %d", len(oc.Memories), OracleMaxPairs)
	}
	text := oracleText(oc)
	if !strings.Contains(text, c.ExpectedAnswer) {
		t.Fatal("the evidence must survive any amount of competition")
	}
	for _, d := range c.DistractorAnswers {
		if !strings.Contains(text, d) {
			t.Fatalf("declared competitor %q must outrank lexical near-duplicates", d)
		}
	}
}

func TestOracleContextSeesOnlyTheCasesVisibleMemory(t *testing.T) {
	p := ReferencePack(9, testHotkey)
	c := p.Cases[0]
	// A later wave and another user's memory are invisible to the case, even
	// when they mention the gold answer.
	p.MemoryWaves = append(p.MemoryWaves,
		protocol.SeedRequest{Wave: len(p.MemoryWaves), Pairs: []protocol.MemoryPair{{
			PairID: "future", SessionID: "f", Timestamp: "2027-01-01T00:00:00Z",
			Prompt: "Future note about " + c.Question, Response: "FUTURE-ONLY-MARKER",
		}}},
		protocol.SeedRequest{UserID: "someone-else", Wave: len(p.MemoryWaves) + 1, Pairs: []protocol.MemoryPair{{
			PairID: "other", SessionID: "o", Timestamp: "2026-01-05T09:00:00Z",
			Prompt: c.Question, Response: "OTHER-USER-MARKER",
		}}},
	)
	oc, err := OracleContext(p, c.ID)
	if err != nil {
		t.Fatal(err)
	}
	text := oracleText(oc)
	if strings.Contains(text, "FUTURE-ONLY-MARKER") || strings.Contains(text, "OTHER-USER-MARKER") {
		t.Fatal("the oracle context must hold only memory visible to the case")
	}
	if _, err := OracleContext(p, "no-such-case"); err == nil {
		t.Fatal("an unknown case must be refused")
	}
}

func TestFrontierAuditSample(t *testing.T) {
	ids := []string{"c0/a", "c0/b", "c0/c", "c0/d", "c0/e", "c0/b"}
	got := FrontierAuditSample("seed", "c0", ids, 3)
	if len(got) != 3 {
		t.Fatalf("got %v", got)
	}
	again := FrontierAuditSample("seed", "c0", []string{"c0/e", "c0/d", "c0/c", "c0/b", "c0/a"}, 3)
	if strings.Join(got, ",") != strings.Join(again, ",") {
		t.Fatalf("sample must not depend on input order: %v vs %v", got, again)
	}
	if all := FrontierAuditSample("seed", "c0", ids, 99); len(all) != 5 {
		t.Fatalf("an oversized sample is every distinct case: %v", all)
	}
	if FrontierAuditSample("seed", "c0", ids, 0) != nil {
		t.Fatal("n=0 samples nothing")
	}
	other := FrontierAuditSample("another-seed", "c0", ids, 3)
	if strings.Join(got, ",") == strings.Join(other, ",") && strings.Join(FrontierAuditSample("third", "c0", ids, 3), ",") == strings.Join(got, ",") {
		t.Fatal("the sample must depend on the secret round seed")
	}
}

func TestOraclePackIsolatesEachCase(t *testing.T) {
	p := ReferencePack(21, testHotkey)
	ids := []string{p.Cases[3].ID, p.Cases[0].ID, p.Cases[9].ID}
	op, contexts, err := OraclePack(p, ids)
	if err != nil {
		t.Fatal(err)
	}
	if len(op.Cases) != 3 || len(op.MemoryWaves) != 3 || len(contexts) != 3 || op.GradingBenchVersion != p.GradingBenchVersion {
		t.Fatalf("shape: %d cases, %d waves, %d contexts", len(op.Cases), len(op.MemoryWaves), len(contexts))
	}
	for i, c := range op.Cases {
		if c.ID != ids[i] || c.RunAfterWave != 2 || c.UserID != op.MemoryWaves[i].UserID {
			t.Fatalf("case %d: %+v", i, c)
		}
		if len(op.MemoryWaves[i].Pairs) != len(contexts[i].Memories) {
			t.Fatalf("case %d seeds %d pairs for a %d-pair context", i, len(op.MemoryWaves[i].Pairs), len(contexts[i].Memories))
		}
		// The oracle grades the source case unchanged.
		src := p.GradingCase(map[string]int{p.Cases[3].ID: 3, p.Cases[0].ID: 0, p.Cases[9].ID: 9}[c.ID])
		if got := op.GradingCase(i); got.ExpectedAnswer != src.ExpectedAnswer || got.AnswerKind != src.AnswerKind {
			t.Fatalf("case %d grading drifted", i)
		}
	}
	if _, _, err := OraclePack(p, []string{ids[0], ids[0]}); err == nil {
		t.Fatal("a repeated case must be refused")
	}
	if _, _, err := OraclePack(p, nil); err == nil {
		t.Fatal("an empty oracle pack must be refused")
	}
}
