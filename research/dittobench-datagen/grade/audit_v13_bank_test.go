package grade

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The grader half of the v13 claim-provenance bank: every vector grades exactly
// as published, every credited vector reports the span it credited, and the
// same responses grade identically under v12 (the v13 policy changes no score).
func TestV13ProvenanceBankGraderVerdicts(t *testing.T) {
	seen := map[string]bool{}
	for _, vec := range V13ProvenanceBank {
		if vec.Name == "" || seen[vec.Name] {
			t.Fatalf("bank has an empty or duplicate vector name %q", vec.Name)
		}
		seen[vec.Name] = true
		if vec.Case.BenchVersion != protocol.BenchVersionV13 {
			t.Fatalf("%s: bank cases are stamped bench_version %d, got %d", vec.Name, protocol.BenchVersionV13, vec.Case.BenchVersion)
		}
		if len(vec.Calls) == 0 {
			t.Fatalf("%s: a vector needs at least one call", vec.Name)
		}
		v := Memory(vec.Case, vec.Response)
		if credited := v.Score > 0; credited != vec.WantCredited {
			t.Fatalf("%s: grader credited=%v (score %.2f, notes %v), want %v", vec.Name, credited, v.Score, v.Notes, vec.WantCredited)
		}
		if vec.WantCredited {
			if v.Provenance == nil {
				t.Fatalf("%s: credited v13 verdict carries no Provenance", vec.Name)
			}
			if v.Provenance.Span == "" || len(v.Provenance.Alternatives) == 0 {
				t.Fatalf("%s: provenance span/alternatives empty: %+v", vec.Name, v.Provenance)
			}
			wantSource := SpanSourceFinalText
			if vec.Response.Answer != "" {
				wantSource = SpanSourceAnswer
			}
			if v.Provenance.Source != wantSource {
				t.Fatalf("%s: provenance source %q, want %q", vec.Name, v.Provenance.Source, wantSource)
			}
		}
		// The v13 grading policy is v12 plus the provenance report: same score.
		v12 := vec.Case
		v12.BenchVersion = protocol.BenchVersionV12
		if got := Memory(v12, vec.Response); got.Score != v.Score || got.Provenance != nil {
			t.Fatalf("%s: v12 re-grade score %.2f provenance %v, want %.2f and nil", vec.Name, got.Score, got.Provenance, v.Score)
		}
	}
}

// The grader alone cannot see the GIH transcript: the served answer is right,
// so it is credited. The bank publishes that as a negative the scorer gate must
// catch (dittobench-api scoregates replays it).
func TestV13ProvenanceBankGIHNegativeIsGraderBlind(t *testing.T) {
	for _, vec := range V13ProvenanceBank {
		if vec.Name != "gih-transcript-answer-without-derivation" {
			continue
		}
		if v := Memory(vec.Case, vec.Response); v.Score != 1 {
			t.Fatalf("GIH transcript negative must be grader-credited (score 1) to be meaningful, got %.2f %v", v.Score, v.Notes)
		}
		if vec.WantModelEmitted {
			t.Fatal("GIH transcript negative must publish model_emitted=false")
		}
		return
	}
	t.Fatal("bank lacks the GIH transcript negative")
}

func TestClaimAlternatives(t *testing.T) {
	money := protocol.MemoryCase{AnswerKind: protocol.AnswerMoney, ExpectedAnswer: "411067"}
	if got := ClaimAlternatives(money); len(got) != 1 || got[0] != "4110.67" {
		t.Fatalf("money alternatives = %v, want [4110.67]", got)
	}
	whole := protocol.MemoryCase{AnswerKind: protocol.AnswerMoney, ExpectedAnswer: "24500"}
	if got := ClaimAlternatives(whole); len(got) != 1 || got[0] != "245.00" {
		t.Fatalf("whole-dollar money alternatives = %v, want [245.00]", got)
	}
	value := protocol.MemoryCase{ExpectedAnswer: "Lisbon", AcceptAny: []string{"Lisboa"}}
	if got := ClaimAlternatives(value); len(got) != 2 || got[0] != "Lisbon" || got[1] != "Lisboa" {
		t.Fatalf("value alternatives = %v", got)
	}
	list := protocol.MemoryCase{AnswerKind: protocol.AnswerList, AnswerItems: []string{"Osaka", "Lima"}, AnswerItemAcceptAny: [][]string{{"Ōsaka"}, nil}}
	if got := ClaimAlternatives(list); len(got) != 3 {
		t.Fatalf("list alternatives = %v, want item + alt + item", got)
	}
	direction := protocol.MemoryCase{AnswerKind: protocol.AnswerDirection, ExpectedAnswer: "went up"}
	if got := ClaimAlternatives(direction); len(got) != len(increasePhrases) {
		t.Fatalf("direction alternatives = %v", got)
	}
	for _, kind := range []string{protocol.AnswerDecline, protocol.AnswerAcknowledge, protocol.AnswerChitchat, protocol.AnswerPersistence, protocol.AnswerReversal, protocol.AnswerDuration} {
		if got := ClaimAlternatives(protocol.MemoryCase{AnswerKind: kind, ExpectedAnswer: "14 days", AnswerItems: []string{"tennis"}}); got != nil {
			t.Fatalf("%s: no value claim expected, got %v", kind, got)
		}
	}
	if _, ok := MoneyMajorForm("abc"); ok {
		t.Fatal("non-numeric money expected must not render")
	}
}

// Verdict.Provenance never appears below v13: the pointer stays nil on every
// earlier policy even for a credited answer.
func TestProvenanceNilBelowV13(t *testing.T) {
	for _, version := range []int{0, protocol.BenchVersionV8, protocol.BenchVersionV9, protocol.BenchVersionV12} {
		mc := protocol.MemoryCase{BenchVersion: version, ExpectedAnswer: "Lisbon"}
		if v := Memory(mc, protocol.RunResponse{Answer: "Lisbon"}); v.Score != 1 || v.Provenance != nil {
			t.Fatalf("v%d: score %.2f provenance %v", version, v.Score, v.Provenance)
		}
	}
	mc := protocol.MemoryCase{BenchVersion: protocol.BenchVersionV13, ExpectedAnswer: "Lisbon"}
	v := Memory(mc, protocol.RunResponse{FinalText: "You live in Lisbon."})
	if v.Provenance == nil || v.Provenance.Source != SpanSourceFinalText || v.Provenance.Span != "You live in Lisbon." || v.Provenance.Kind != protocol.AnswerValue {
		t.Fatalf("v13 final_text provenance = %+v", v.Provenance)
	}
	if v := Memory(mc, protocol.RunResponse{Answer: "Porto"}); v.Provenance != nil {
		t.Fatalf("uncredited verdict must carry no provenance: %+v", v.Provenance)
	}
}
