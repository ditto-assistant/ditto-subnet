package persona

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestFutureAnswerPoolsRejectIncidentalTokens(t *testing.T) {
	pools := map[string][]string{
		"city": cities, "occupation": occupations, "employer": companies,
		"car": carModels, "partner": firstNames, "instrument": instruments,
		"alma_mater": universities, "project": projectNames, "pet": petNames,
		"favorite_cuisine": cuisines, "dietary": dietaryStyles,
		"favorite_color": colors, "primary_language": softwareLanguages,
		"code_editor": softwareEditors, "service": softwareServices,
		"diagnosis": medicalDiagnoses, "medication": medicalMedications,
		"allergy": medicalAllergies, "practice_area": legalPracticeAreas,
		"bar_admission": legalJurisdictions, "legal_matter": legalMatters,
		"risk_tolerance": financeRiskProfiles, "brokerage": financeBrokerages,
		"holding": financeHoldings, "shoe_size": shoeSizes, "height": heightsCm,
		"favorite_song": favoriteSongs, "star_sign": starSigns,
		"middle_name": firstNames, "eye_color": eyeColors,
		"blood_type": bloodTypes, "birthday_month": birthdayMonths,
		"sports_team": sportsTeams, "favorite_film": favoriteFilms,
	}
	for attr, legacy := range pools {
		pool := answerPoolForVersion(attr, legacy, answerPoolFixBenchVersion)
		if len(pool) == 0 {
			t.Fatalf("future %s answer pool is empty", attr)
		}
		for _, value := range pool {
			for _, sentence := range incidentalProse {
				if grade.Hit(value, sentence) {
					t.Errorf("future %s value %q hits incidental %q", attr, value, sentence)
				}
			}
		}
	}
}

func TestPublishedAnswerPoolsRemainFrozen(t *testing.T) {
	for version := protocol.BenchVersionV8; version <= protocol.BenchVersionV13; version++ {
		for _, tc := range []struct {
			attr string
			base []string
			want []string
		}{
			{"eye_color", eyeColors, eyeColors},
			{"star_sign", starSigns, starSigns},
			{"middle_name", firstNames, v8HumanGivenNames},
		} {
			got := answerPoolForVersion(tc.attr, tc.base, version)
			if len(got) != len(tc.want) || &got[0] != &tc.want[0] {
				t.Fatalf("v%d %s pool changed", version, tc.attr)
			}
		}
	}
	if protocol.SupportedBenchVersion(answerPoolFixBenchVersion) {
		t.Fatal("answer-pool fix became runnable without a complete new-version rollout")
	}
}

func TestPublishedIncidentalExposureAcrossOneHundredSeeds(t *testing.T) {
	totals := map[int]int{}
	for seed := int64(1); seed <= 100; seed++ {
		for _, version := range []int{protocol.BenchVersionV7, protocol.BenchVersionV8, protocol.BenchVersionV9} {
			plan, err := BuildPlanForVersion(seed, fullOpts(), version)
			if err != nil {
				t.Fatal(err)
			}
			totals[version] += incidentalHits(plan)
		}
	}
	for version, want := range map[int]int{
		protocol.BenchVersionV7: 167,
		protocol.BenchVersionV8: 83,
		protocol.BenchVersionV9: 90,
	} {
		if got := totals[version]; got != want {
			t.Errorf("v%d incidental collisions over 100 full-profile seeds: got %d, want %d", version, got, want)
		}
	}
}

func incidentalHits(plan *Plan) int {
	hits := 0
	for _, fact := range plan.Facts {
		for _, sentence := range incidentalProse {
			if grade.Hit(fact.Value, sentence) {
				hits++
				break
			}
		}
	}
	return hits
}

func TestV8AmbiguousAnswerPoolsStayQualified(t *testing.T) {
	for attr, legacy := range map[string][]string{
		"favorite_color":   colors,
		"primary_language": softwareLanguages,
	} {
		pool := answerPoolForVersion(attr, legacy, protocol.BenchVersionV8)
		for _, value := range pool {
			parts := strings.Fields(value)
			if len(parts) < 2 {
				t.Fatalf("v8 %s value %q lacks a disambiguating qualifier", attr, value)
			}
			for _, part := range parts {
				if grade.Hit(value, "Let me "+strings.ToLower(part)+" check that.") {
					t.Fatalf("v8 %s value %q is creditable from incidental token %q", attr, value, part)
				}
			}
		}
	}
	if !grade.Hit("Go", "Let me go check that.") {
		t.Fatal("test fixture no longer reproduces the frozen v7 Go collision")
	}
	if grade.Hit(answerPoolForVersion("primary_language", softwareLanguages, protocol.BenchVersionV8)[1], "Let me go check that.") {
		t.Fatal("v8 qualified Go value matched incidental prose")
	}
	if got := answerPoolForVersion("primary_language", softwareLanguages, protocol.BenchVersionV7); &got[0] != &softwareLanguages[0] {
		t.Fatal("v7 software-language pool changed")
	}
}
