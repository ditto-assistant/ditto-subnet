package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV6ComplexityCasesGated confirms the v6 complexity classes (multi-query,
// non-verbatim computed, passive-consolidation) are v6-gated: none draw on v5.
func TestV6ComplexityCasesGated(t *testing.T) {
	for _, bv := range []int{protocol.BenchVersionV5, protocol.BenchVersionV6} {
		prof, _ := ProfileForVersion("full", bv)
		r, _ := NewRNGForVersion(303, bv)
		s, err := GenerateMemorySuiteForVersion(r, 303, prof.Mem, prof.Waves, prof.RawPairsFrac, bv)
		if err != nil {
			t.Fatal(err)
		}
		cnt := map[string]int{}
		for _, sc := range s.Cases {
			cnt[sc.Case.QuestionType]++
		}
		for _, qt := range []string{QTMultiQuery, QTNonVerbatim, QTConsolidation} {
			if bv == protocol.BenchVersionV5 && cnt[qt] != 0 {
				t.Errorf("v5 must not carry %s, got %d", qt, cnt[qt])
			}
			if bv == protocol.BenchVersionV6 && cnt[qt] == 0 {
				t.Errorf("v6 must carry %s, got 0", qt)
			}
		}
	}
}

// TestV6ComplexityGrading pins each new class's discriminating contract:
//   - multi-query: the item matching BOTH filters passes; a single-filter decoy
//     (what a one-shot query returns) is a distractor and scores 0.
//   - non-verbatim: a converted accept-set form passes; the un-converted stored form
//     is deliberately NOT in the accept-set (a grep of the haystack cannot pass).
//   - consolidation: the earliest cross-session fact passes; a later (recency) value
//     is a distractor and scores 0.
func TestV6ComplexityGrading(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV6)
	r, _ := NewRNGForVersion(101, protocol.BenchVersionV6)
	s, err := GenerateMemorySuiteForVersion(r, 101, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV6)
	if err != nil {
		t.Fatal(err)
	}
	var mq, nv, cons protocol.MemoryCase
	for _, sc := range s.Cases {
		switch sc.Case.QuestionType {
		case QTMultiQuery:
			if mq.ExpectedAnswer == "" {
				mq = sc.Case
			}
		case QTNonVerbatim:
			if nv.ExpectedAnswer == "" {
				nv = sc.Case
			}
		case QTConsolidation:
			if cons.ExpectedAnswer == "" {
				cons = sc.Case
			}
		}
	}
	g := func(mc protocol.MemoryCase, txt string) float64 {
		return grade.Memory(mc, protocol.RunResponse{FinalText: txt}).Score
	}
	if mq.ExpectedAnswer == "" || nv.ExpectedAnswer == "" || cons.ExpectedAnswer == "" {
		t.Fatal("expected a case of each v6 complexity class")
	}
	if s := g(mq, "That would be "+mq.ExpectedAnswer+"."); s != 1 {
		t.Errorf("multi-query correct item must score 1, got %.2f", s)
	}
	if s := g(mq, "That would be "+mq.DistractorAnswers[0]+"."); s != 0 {
		t.Errorf("multi-query single-filter decoy must score 0, got %.2f", s)
	}
	if s := g(nv, "About "+nv.AcceptAny[0]+"."); s != 1 {
		t.Errorf("non-verbatim accept-set form must score 1, got %.2f", s)
	}
	if s := g(cons, "You first mentioned "+cons.ExpectedAnswer+"."); s != 1 {
		t.Errorf("consolidation earliest fact must score 1, got %.2f", s)
	}
	if s := g(cons, "You said "+cons.DistractorAnswers[0]+"."); s != 0 {
		t.Errorf("consolidation recency value must score 0, got %.2f", s)
	}
}

// TestV6DeclarativeAckSoftened pins the v6 softening of the conversational-declarative
// gate slice: a clear acknowledgment passes (not only a verbatim echo of the coined
// value), while ignoring the statement or dumping stored memory still fails, and v5
// keeps the strict echo (its bytes are frozen).
func TestV6DeclarativeAckSoftened(t *testing.T) {
	pick := func(bv int) protocol.MemoryCase {
		prof, _ := ProfileForVersion("full", bv)
		r, _ := NewRNGForVersion(101, bv)
		s, err := GenerateMemorySuiteForVersion(r, 101, prof.Mem, prof.Waves, prof.RawPairsFrac, bv)
		if err != nil {
			t.Fatal(err)
		}
		for _, sc := range s.Cases {
			if sc.Case.QuestionType == QTDeclarativeAck {
				return sc.Case
			}
		}
		t.Fatalf("no declarative-ack case for bv=%d", bv)
		return protocol.MemoryCase{}
	}
	score := func(mc protocol.MemoryCase, txt string) float64 {
		return grade.Memory(mc, protocol.RunResponse{FinalText: txt}).Score
	}
	v5, v6 := pick(protocol.BenchVersionV5), pick(protocol.BenchVersionV6)
	// v5 stays strict: a bare acknowledgment does not pass.
	if s := score(v5, "Got it, noted."); s != 0 {
		t.Errorf("v5 declarative-ack must still require the value echo, ack got %.2f", s)
	}
	// v6 accepts a clear acknowledgment...
	if s := score(v6, "Got it, noted."); s != 1 {
		t.Errorf("v6 declarative-ack must accept an acknowledgment, got %.2f", s)
	}
	// ...still requires engaging (ignoring the statement fails)...
	if s := score(v6, "Nice weather today."); s != 0 {
		t.Errorf("v6 declarative-ack must fail an unrelated reply, got %.2f", s)
	}
	// ...and the non-leak guard still zeroes a memory dump.
	if s := score(v6, "Got it. "+v6.ForbiddenAnswer); s != 0 {
		t.Errorf("v6 declarative-ack must zero a leak even with an ack, got %.2f", s)
	}
}
