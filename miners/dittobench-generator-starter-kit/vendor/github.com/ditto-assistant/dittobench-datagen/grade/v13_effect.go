package grade

import (
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"strings"
)

// MemoryEffectV13 verifies a planted tool-memory end state using the same
// assertion analysis as v13 memory grading. Unlike memory-axis answers, the
// tool contract permits a structured answer without redundant prose.
// Callers must separately enforce the tool trajectory and version boundary.
func MemoryEffectV13(expected string, stale []string, resp protocol.RunResponse) Verdict {
	if strings.TrimSpace(expected) == "" || resp.Abstain {
		return Verdict{Notes: []string{"missing effect contract or abstained (scored 0)"}}
	}
	mc := protocol.MemoryCase{BenchVersion: protocol.BenchVersionV13,
		ExpectedAnswer: expected, AnswerKind: protocol.AnswerValue, DistractorAnswers: stale}
	if isPureNumber(Normalize(expected)) {
		mc.AnswerKind = protocol.AnswerNumber
	}
	lex, _ := lexiconFor("")
	an := analyzeV13(resp.Answer, resp.FinalText, lex)
	spec := claimSpecV13(mc, mc.AnswerKind, expected, lex)
	// A possible new value does not establish the mutation's resulting state.
	for _, seg := range an.segments {
		if !seg.eligible() || !seg.hedge {
			continue
		}
		for _, m := range spec.extract(seg.text) {
			if spec.isExpected(m.key) {
				return Verdict{Notes: []string{"hedged the end state (scored 0)"}}
			}
		}
	}
	set, matched, _, outcome := scalarClaimV13(mc, mc.AnswerKind, expected, an, lex)
	if outcome.zeroNote != "" {
		return Verdict{Notes: []string{outcome.zeroNote}}
	}
	if matched && (!set.slotPopulated || set.slotInProse || strings.TrimSpace(resp.FinalText) == "") {
		return Verdict{Score: 1, Notes: []string{"asserted the planted end state without a stale assertion"}}
	}
	return Verdict{Notes: []string{"did not assert a consistent planted end state (scored 0)"}}
}
