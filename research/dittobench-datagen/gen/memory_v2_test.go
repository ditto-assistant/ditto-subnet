package gen

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestGenerateMemoryV2Deterministic(t *testing.T) {
	seedA, caseA, _, err := GenerateMemoryV2(NewRNG(5), 12345, 20)
	if err != nil {
		t.Fatal(err)
	}
	seedB, caseB, _, err := GenerateMemoryV2(NewRNG(5), 12345, 20)
	if err != nil {
		t.Fatal(err)
	}
	ja, _ := json.Marshal(struct {
		S any
		C any
	}{seedA, caseA})
	jb, _ := json.Marshal(struct {
		S any
		C any
	}{seedB, caseB})
	if string(ja) != string(jb) {
		t.Fatal("GenerateMemoryV2 not reproducible for fixed (seed, rng stream)")
	}
}

func TestGenerateMemoryV2Shape(t *testing.T) {
	seedReq, cases, _, err := GenerateMemoryV2(NewRNG(1), 999, 30)
	if err != nil {
		t.Fatal(err)
	}
	if len(seedReq.Pairs) == 0 {
		t.Fatal("empty haystack")
	}
	if len(seedReq.Subjects) == 0 || len(seedReq.Links) == 0 {
		t.Fatal("Tier-A seeding should synthesize subjects + links")
	}
	if len(cases) == 0 {
		t.Fatal("no memory cases")
	}
	// Abstention share present, every case has a non-empty question + answer.
	var absN int
	types := map[string]int{}
	for _, c := range cases {
		types[c.QuestionType]++
		if strings.TrimSpace(c.Question) == "" || strings.TrimSpace(c.ExpectedAnswer) == "" {
			t.Fatalf("case %s has empty question or answer", c.ID)
		}
		if strings.Contains(c.QuestionType, "abstention") {
			absN++
		}
	}
	if absN == 0 {
		t.Error("no abstention cases at n=30")
	}
	if len(types) < 4 {
		t.Errorf("expected several question types, got %v", types)
	}
}

// TestGenerateMemoryV2AnswersAreSeeded checks the recall/knowledge-update/
// preference answers are actually present in the seeded haystack (the harness
// can recover them), while abstention answers are the decline sentinel and are
// NOT seeded.
func TestGenerateMemoryV2AnswersAreSeeded(t *testing.T) {
	seedReq, cases, _, err := GenerateMemoryV2(NewRNG(2), 42, 25)
	if err != nil {
		t.Fatal(err)
	}
	var sb strings.Builder
	for _, p := range seedReq.Pairs {
		sb.WriteString(p.Prompt)
		sb.WriteByte(' ')
		sb.WriteString(p.Response)
		sb.WriteByte(' ')
	}
	hay := strings.ToLower(sb.String())
	for _, c := range cases {
		if strings.Contains(c.QuestionType, "abstention") {
			if c.ExpectedAnswer != abstentionExpectedAnswer {
				t.Fatalf("abstention case %s answer is not the decline sentinel", c.ID)
			}
			continue
		}
		switch c.QuestionType {
		case persona.QTSingleSession, persona.QTKnowledgeUpdate, persona.QTPreference:
			if !strings.Contains(hay, strings.ToLower(c.ExpectedAnswer)) {
				t.Fatalf("case %s (%s): answer %q not present in seeded haystack",
					c.ID, c.QuestionType, c.ExpectedAnswer)
			}
		}
	}
}

// TestInjectionTwinFamilyEmitted asserts a full run carries an injection-framing
// invariance family: a TwinGroup of at least two QTInjection cases sharing the
// run payload as ForbiddenAnswer, exactly one of which is an action-bait case
// (BaitTool set). This is the v3 anti-laundering family — framing consistency
// plus one unscrubable observed-trajectory signal.
func TestInjectionTwinFamilyEmitted(t *testing.T) {
	prof, _ := ProfileFor("full")
	seen := 0
	for seed := int64(1); seed <= 10; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		groups := map[string][]protocol.MemoryCase{}
		for _, c := range cases {
			if strings.HasPrefix(c.TwinGroup, persona.InjectionTwinPrefix) {
				groups[c.TwinGroup] = append(groups[c.TwinGroup], c)
			}
		}
		for grp, members := range groups {
			if len(members) < 2 {
				t.Fatalf("seed %d: injection twin family %q has %d members (<2)", seed, grp, len(members))
			}
			baits := 0
			for _, m := range members {
				if m.QuestionType != persona.QTInjection {
					t.Fatalf("seed %d: injection twin %s is type %q, want %q", seed, m.ID, m.QuestionType, persona.QTInjection)
				}
				if m.ForbiddenAnswer != persona.InjectionPayload(seed) {
					t.Fatalf("seed %d: injection twin %s ForbiddenAnswer=%q, want run payload", seed, m.ID, m.ForbiddenAnswer)
				}
				if m.BaitTool != "" {
					baits++
				}
			}
			if baits != 1 {
				t.Fatalf("seed %d: injection twin family %q has %d bait siblings, want exactly 1", seed, grp, baits)
			}
			seen++
		}
	}
	if seen == 0 {
		t.Fatal("no injection twin family emitted in any full run")
	}
	t.Logf("injection-twin gate: %d families across seeds, each with one bait sibling", seen)
}

// TestNoDuplicateInjectionText guards against the injection-framing family
// emitting a case whose attack text is byte-identical to a broad single
// injection case (same template pool + ask). Such a duplicate is a benchmark
// fingerprint and a free consistency point. The broad loop now skips the twin
// attribute; assert no two injection cases in a run share exact text.
func TestNoDuplicateInjectionText(t *testing.T) {
	prof, _ := ProfileFor("full")
	dupSeeds := 0
	for seed := int64(1); seed <= 500; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		seen := map[string]string{}
		for _, c := range cases {
			if c.QuestionType != persona.QTInjection {
				continue
			}
			if prev, ok := seen[c.Question]; ok {
				dupSeeds++
				t.Errorf("seed %d: injection cases %s and %s share byte-identical text", seed, prev, c.ID)
				break
			}
			seen[c.Question] = c.ID
		}
	}
	if dupSeeds == 0 {
		t.Logf("no-duplicate-injection gate: 500 seeds, no byte-identical injection text")
	}
}
