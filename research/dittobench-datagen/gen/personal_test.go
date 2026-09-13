package gen

import (
	"fmt"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// TestPersonalProgramsCoverSixDomainsWithZeroMoney: 24 cases per full seed,
// six groups over six distinct personal domains, no monetary group, typed
// claims on every member.
func TestPersonalProgramsCoverSixDomainsWithZeroMoney(t *testing.T) {
	domainsSeen := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		cases, pairs, err := GenerateV13PersonalPrograms(seed, v13PersonalProgramCaseCount(225))
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if len(cases) != 24 {
			t.Fatalf("seed %d: %d cases, want 24", seed, len(cases))
		}
		assertNoMoney(t, "personal", cases, pairs)
		perSeed := map[string]bool{}
		for _, sc := range cases {
			domain := sc.V10Provenance.Ontology[0].Wire
			perSeed[domain] = true
			domainsSeen[domain] = true
			if len(sc.Case.Claims) == 0 {
				t.Fatalf("seed %d case %s has no claims", seed, sc.Case.ID)
			}
			if sc.Case.QuestionType != universe.V13PersonalQuestionType || sc.Case.BenchVersion != protocol.BenchVersionV13 {
				t.Fatalf("seed %d case %s type/version %q/%d", seed, sc.Case.ID, sc.Case.QuestionType, sc.Case.BenchVersion)
			}
		}
		if len(perSeed) != 6 {
			t.Fatalf("seed %d: %d distinct domains, want 6: %v", seed, len(perSeed), perSeed)
		}
	}
	if len(domainsSeen) != len(universe.V13PersonalDomains) {
		t.Fatalf("across 40 seeds only %d of %d domains appeared", len(domainsSeen), len(universe.V13PersonalDomains))
	}
}

// TestPersonalDateGranularityVectors: an appointment case accepts every
// unambiguous rendering of the rescheduled day (ISO, 'March 4', 'Mar 4th',
// '4 March 2026'), rejects the original day and the decoy day, and never
// accepts a locale-ambiguous slashed form.
func TestPersonalDateGranularityVectors(t *testing.T) {
	seenDate, seenTime := false, false
	for seed := int64(1); seed <= 40 && !(seenDate && seenTime); seed++ {
		cases, _, err := GenerateV13PersonalPrograms(seed, 24)
		if err != nil {
			t.Fatal(err)
		}
		for _, sc := range cases {
			mc := sc.Case
			switch mc.Claims[0].Kind {
			case protocol.ClaimKindDate:
				seenDate = true
				var y, m, d int
				if _, err := fmt.Sscanf(mc.ExpectedAnswer, "%d-%d-%d", &y, &m, &d); err != nil {
					t.Fatalf("date answer %q is not ISO", mc.ExpectedAnswer)
				}
				for _, form := range universe.V13DateAccept(y, m, d) {
					if v := grade.Memory(mc, protocol.RunResponse{Answer: form}); v.Score != 1 {
						t.Fatalf("seed %d case %s: date rendering %q scored %.2f (%v)", seed, mc.ID, form, v.Score, v.Notes)
					}
				}
				slashed := fmt.Sprintf("%02d/%02d/%d", m, d, y)
				if v := grade.Memory(mc, protocol.RunResponse{Answer: slashed}); v.Score != 0 {
					t.Fatalf("seed %d case %s: ambiguous slashed date %q scored %.2f", seed, mc.ID, slashed, v.Score)
				}
			case protocol.ClaimKindTime:
				seenTime = true
				var h, min int
				if _, err := fmt.Sscanf(mc.ExpectedAnswer, "%d:%d", &h, &min); err != nil {
					t.Fatalf("time answer %q is not HH:MM", mc.ExpectedAnswer)
				}
				for _, form := range universe.V13TimeAccept(h, min) {
					if v := grade.Memory(mc, protocol.RunResponse{Answer: form}); v.Score != 1 {
						t.Fatalf("seed %d case %s: time rendering %q scored %.2f (%v)", seed, mc.ID, form, v.Score, v.Notes)
					}
				}
			}
		}
	}
	if !seenDate || !seenTime {
		t.Fatalf("date/time cases not both generated across seeds: date=%v time=%v", seenDate, seenTime)
	}
}

// TestPersonalSetMembershipGradesCurrentSet: the school list case credits the
// full current set, gives partial credit for a subset, and zeroes an answer
// slot that lists the dropped item.
func TestPersonalSetMembershipGradesCurrentSet(t *testing.T) {
	seen := false
	for seed := int64(1); seed <= 40 && !seen; seed++ {
		cases, _, err := GenerateV13PersonalPrograms(seed, 24)
		if err != nil {
			t.Fatal(err)
		}
		for _, sc := range cases {
			mc := sc.Case
			if mc.Claims[0].Kind != protocol.ClaimKindSetMember {
				continue
			}
			seen = true
			if len(mc.AnswerItems) != 4 {
				t.Fatalf("seed %d case %s: %d items, want 4", seed, mc.ID, len(mc.AnswerItems))
			}
			full := protocol.RunResponse{Answer: strings.Join(mc.AnswerItems, ", ")}
			if v := grade.Memory(mc, full); v.Score != 1 {
				t.Fatalf("seed %d case %s: full set scored %.2f (%v)", seed, mc.ID, v.Score, v.Notes)
			}
			partial := protocol.RunResponse{Answer: strings.Join(mc.AnswerItems[:2], ", ")}
			if v := grade.Memory(mc, partial); v.Score != 0.5 {
				t.Fatalf("seed %d case %s: half set scored %.2f", seed, mc.ID, v.Score)
			}
			stale := protocol.RunResponse{Answer: strings.Join(append(append([]string(nil), mc.AnswerItems...), mc.DistractorAnswers[0]), ", ")}
			if v := grade.Memory(mc, stale); v.Score != 0 {
				t.Fatalf("seed %d case %s: listing the dropped item scored %.2f", seed, mc.ID, v.Score)
			}
			// Mentioning the dropped item in prose, with the correct slot, is fine
			// under the v12+ slot-scoped scan.
			prose := protocol.RunResponse{Answer: strings.Join(mc.AnswerItems, ", "), FinalText: "They dropped the " + mc.DistractorAnswers[0] + "."}
			if v := grade.Memory(mc, prose); v.Score != 1 {
				t.Fatalf("seed %d case %s: prose mention of the dropped item scored %.2f (%v)", seed, mc.ID, v.Score, v.Notes)
			}
		}
	}
	if !seen {
		t.Fatal("no set-membership case generated")
	}
}
