package challenge

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func hasRule(r Report, caseID, rule string) bool {
	for _, is := range r.Issues {
		if is.Rule == rule && (caseID == "" || is.CaseID == caseID) {
			return true
		}
	}
	return false
}

// Each mutation breaks exactly one property a valid case must have; the
// validator must name the rule that caught it.
func TestValidateRejectsBrokenCases(t *testing.T) {
	type mutation struct {
		name   string
		rule   string
		mutate func(p *Pack)
	}
	first := func(p *Pack) *Case { return &p.Cases[0] } // code-update-0 (number)
	person := func(p *Pack) *Case { return &p.Cases[1] }
	mutations := []mutation{
		{"wrong contract", "contract", func(p *Pack) { p.Contract = "v0" }},
		{"older grading policy", "grading_bench_version", func(p *Pack) { p.GradingBenchVersion = 12 }},
		{"bad hotkey", "miner_hotkey", func(p *Pack) { p.MinerHotkey = "nope" }},
		{"bad generator name", "generator.name", func(p *Pack) { p.Generator.Name = "Bad Name" }},
		{"too few cases", "case_count", func(p *Pack) { p.Cases = p.Cases[:MinCases-1] }},
		{"duplicate case id", "case_id", func(p *Pack) { p.Cases[1].ID = p.Cases[0].ID }},
		{"accept_any on a number", "accept_any", func(p *Pack) { first(p).AcceptAny = []string{"90"} }},
		{"duplicate content", "duplicate_case", func(p *Pack) {
			c := p.Cases[0]
			c.ID = "copy"
			p.Cases = append(p.Cases, c)
		}},
		{"same question twice", "duplicate_question", func(p *Pack) {
			p.Cases[1].Question = strings.ToUpper(p.Cases[0].Question)
		}},
		{"answer not in evidence", "grounding", func(p *Pack) {
			c := first(p)
			c.ExpectedAnswer = "7777"
		}},
		{"evidence pair missing", "evidence", func(p *Pack) { first(p).EvidencePairIDs = []string{"p9999"} }},
		{"no evidence", "evidence", func(p *Pack) { first(p).EvidencePairIDs = nil }},
		{"evidence seeded too late", "evidence", func(p *Pack) { first(p).RunAfterWave = 1 }},
		{"answer leaked in question", "answer_leak", func(p *Pack) {
			c := first(p)
			c.Question = "Is my code " + c.ExpectedAnswer + " right now?"
		}},
		{"disallowed kind", "answer_kind", func(p *Pack) { first(p).AnswerKind = protocol.AnswerDecline }},
		{"bait tool", "unsupported_field", func(p *Pack) { first(p).BaitTool = "send_email" }},
		{"no interference: dump passes", "memory_dump", func(p *Pack) {
			// Without distractors the grader has nothing that disqualifies a
			// response reciting all of memory, so the case rewards dumping.
			person(p).DistractorAnswers = nil
		}},
		{"distractor equals gold", "distractor", func(p *Pack) {
			c := person(p)
			c.DistractorAnswers = []string{"Dr. " + c.ExpectedAnswer}
		}},
		{"unknown user", "user_id", func(p *Pack) { first(p).UserID = "ghost" }},
		{"run after missing wave", "run_after_wave", func(p *Pack) { first(p).RunAfterWave = 9 }},
		{"wave index mismatch", "memory_waves", func(p *Pack) { p.MemoryWaves[1].Wave = 5 }},
		{"duplicate pair id", "pair_id", func(p *Pack) {
			p.MemoryWaves[1].Pairs[0].PairID = p.MemoryWaves[0].Pairs[0].PairID
		}},
		{"bad timestamp", "pair", func(p *Pack) { p.MemoryWaves[0].Pairs[0].Timestamp = "yesterday" }},
		{"non-English", "language", func(p *Pack) { first(p).Language = "fr" }},
		{"list with one item", "answer_items", func(p *Pack) { p.Cases[2].AnswerItems = p.Cases[2].AnswerItems[:1] }},
	}
	for _, m := range mutations {
		t.Run(m.name, func(t *testing.T) {
			p := ReferencePack(11, testHotkey)
			m.mutate(&p)
			r := Validate(p)
			if r.Valid {
				t.Fatalf("mutation accepted")
			}
			if !hasRule(r, "", m.rule) {
				t.Fatalf("want rule %q, got %+v", m.rule, r.Issues)
			}
		})
	}
}

// The reference cases must credit an ordinary prose answer from an agent that
// retrieved the right fact, not just the canonical slot string.
func TestReferenceCasesCreditProseAnswers(t *testing.T) {
	p := ReferencePack(5, testHotkey)
	for _, c := range p.Cases {
		mc := c.GradingCase()
		prose := "Based on what you told me, it's " + goldResponse(mc, mc.AnswerKind) + "."
		if s := grade.Memory(mc, protocol.RunResponse{FinalText: prose}).Score; s < correctLine {
			t.Errorf("%s: prose answer %q scored %.2f", c.ID, prose, s)
		}
		wrong := "It's " + c.DistractorAnswers[0] + "."
		if s := grade.Memory(mc, protocol.RunResponse{FinalText: wrong}).Score; s >= correctLine {
			t.Errorf("%s: distractor prose %q scored %.2f", c.ID, wrong, s)
		}
	}
}

func TestMoneyCaseGrounding(t *testing.T) {
	p := ReferencePack(5, testHotkey)
	p.MemoryWaves[2].Pairs = append(p.MemoryWaves[2].Pairs, protocol.MemoryPair{
		PairID: "rent-new", SessionID: "s99", Timestamp: "2026-02-01T10:00:00Z",
		Prompt:   "Landlord raised my rent from $1,450.00 to $1,620.00 a month.",
		Response: "Noted, rent is now $1,620.00 per month.",
	})
	p.Cases = append(p.Cases, Case{
		MemoryCase: protocol.MemoryCase{
			ID: "rent-now", QuestionType: "knowledge-update", Question: "How much is my monthly rent now?",
			ExpectedAnswer: "162000", AnswerKind: protocol.AnswerMoney, DistractorAnswers: []string{"1450.00"},
		},
		RunAfterWave: 2, EvidencePairIDs: []string{"rent-new"},
	})
	r := Validate(p)
	if !r.Valid {
		t.Fatalf("money case rejected: %+v", r.Issues)
	}
	mc := p.Cases[len(p.Cases)-1].GradingCase()
	for _, answer := range []string{"Your rent is now $1,620.00 a month.", "1620.00"} {
		if s := grade.Memory(mc, protocol.RunResponse{FinalText: answer}).Score; s < correctLine {
			t.Errorf("money answer %q scored %.2f", answer, s)
		}
	}
}

func TestGroupThousands(t *testing.T) {
	for in, want := range map[string]string{"7": "7", "999": "999", "1620": "1,620", "1234567": "1,234,567"} {
		if got := groupThousands(in); got != want {
			t.Errorf("groupThousands(%q) = %q, want %q", in, got, want)
		}
	}
}
