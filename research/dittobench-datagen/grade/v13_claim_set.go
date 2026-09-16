package grade

import (
	"fmt"
	"math"
	"strconv"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// gradeClaimSetV13 consumes the generator's authoritative claim set, rather
// than silently falling back to its legacy flattened ExpectedAnswer. Common
// injection, leak, dump and abstention guards have already run in memoryV13.
func gradeClaimSetV13(mc protocol.MemoryCase, resp protocol.RunResponse, an analysis, lex claimLexicon, policy gradingPolicy) Verdict {
	var weighted, total float64
	var notes []string
	for i, claim := range mc.Claims {
		weight := claim.Weight
		if weight == 0 {
			weight = 1
		}
		if weight < 0 || math.IsNaN(weight) || math.IsInf(weight, 0) || strings.TrimSpace(claim.Expected) == "" {
			return Verdict{Notes: []string{"invalid v13 claim contract (scored 0)"}}
		}
		cm, ok := memoryForClaimV13(mc, claim)
		if !ok {
			return Verdict{Notes: []string{"unsupported v13 claim kind or unit (scored 0)"}}
		}
		claimLex := lex
		// Negation intrinsic to a reviewed semantic value ("do not agree",
		// "not happening") is not rejection of that value. An outer "not"
		// still opens a rejected segment.
		claimLex.protected = append(append([]string(nil), lex.protected...), foldV13(claim.Expected))
		for _, accepted := range claim.Accept {
			claimLex.protected = append(claimLex.protected, foldV13(accepted))
		}
		if claim.Kind == protocol.ClaimKindDirection {
			forms := make([]string, 0, len(claim.Accept))
			for _, accepted := range claim.Accept {
				forms = append(forms, foldV13(accepted))
			}
			switch directionKindV13(claim.Expected, lex) {
			case "increase":
				claimLex.increase = append(append([]string(nil), lex.increase...), forms...)
			case "decrease":
				claimLex.decrease = append(append([]string(nil), lex.decrease...), forms...)
			case "unchanged":
				claimLex.unchanged = append(append([]string(nil), lex.unchanged...), forms...)
			}
		}
		if claim.Kind == protocol.ClaimKindStatus {
			// "superseded" can be the present status, not a temporal qualifier.
			// Only exact reviewed status names lose marker meaning; "previously
			// superseded" remains past and "not superseded" remains rejected.
			claimLex.pastStrong = nil
			for _, marker := range lex.pastStrong {
				isStatus := normalizeV13(marker) == normalizeV13(claim.Expected)
				for _, accepted := range claim.Accept {
					isStatus = isStatus || normalizeV13(marker) == normalizeV13(accepted)
				}
				if !isStatus {
					claimLex.pastStrong = append(claimLex.pastStrong, marker)
				}
			}
		}
		an = analyzeV13(resp.Answer, resp.FinalText, claimLex)
		v := gradeClaimV13(cm, resp, cm.AnswerKind, an, claimLex, policy)
		if claim.Critical && v.Score != 1 {
			return Verdict{Notes: []string{fmt.Sprintf("critical claim %d (%s) not satisfied (scored 0)", i, claim.Kind)}}
		}
		weighted += weight * v.Score
		total += weight
		notes = append(notes, fmt.Sprintf("claim %d (%s): %.3f", i, claim.Kind, v.Score))
	}
	if total == 0 || math.IsInf(total, 0) {
		return Verdict{Notes: []string{"invalid v13 claim weights (scored 0)"}}
	}
	return Verdict{Score: weighted / total, Notes: notes}
}

func memoryForClaimV13(mc protocol.MemoryCase, claim protocol.Claim) (protocol.MemoryCase, bool) {
	mc.Claims = nil
	mc.ExpectedAnswer = claim.Expected
	mc.AcceptAny = claim.Accept
	// A sibling's accepted value must not mask a distractor for this claim.
	mc.AnswerItems = nil
	mc.AnswerItemKinds = nil
	mc.AnswerItemAcceptAny = nil
	mc.AnswerKind = protocol.AnswerValue
	switch claim.Kind {
	case protocol.ClaimKindValue, protocol.ClaimKindPerson, protocol.ClaimKindStatus,
		protocol.ClaimKindEvent, protocol.ClaimKindOrganisation, protocol.ClaimKindAction,
		protocol.ClaimKindChannel, protocol.ClaimKindSetMember, protocol.ClaimKindConflict,
		protocol.ClaimKindTime:
		// These semantic categories use reviewed canonical/accept forms, never a
		// guessed synonym or an arbitrary numeric substring.
	case protocol.ClaimKindDate:
		mc.AnswerKind = protocol.AnswerDate
	case protocol.ClaimKindDirection:
		mc.AnswerKind = protocol.AnswerDirection
	case protocol.ClaimKindQuantity:
		switch strings.ToLower(strings.TrimSpace(claim.Unit)) {
		case "cents", "minor":
			mc.AnswerKind, mc.AnswerUnit = protocol.AnswerMoney, protocol.AnswerUnitMinor
		case "usd", "eur", "gbp", "cad":
			mc.AnswerKind, mc.AnswerUnit = protocol.AnswerMoney, protocol.AnswerUnitMajor
			mc.AnswerCurrency = strings.ToUpper(claim.Unit)
			// Claim.Expected uses Claim.Unit; the legacy money matcher consumes
			// integer minor units. Convert the oracle, not the user's reply.
			major, err := strconv.ParseInt(claim.Expected, 10, 64)
			if err != nil || major < 0 || major > math.MaxInt64/100 {
				return mc, false
			}
			mc.ExpectedAnswer = strconv.FormatInt(major*100, 10)
		case "", "count", "units", "nights", "seats", "hours", "licences", "percentage points":
			mc.AnswerKind = protocol.AnswerNumber
		default:
			return mc, false
		}
	default:
		return mc, false
	}
	return mc, true
}
